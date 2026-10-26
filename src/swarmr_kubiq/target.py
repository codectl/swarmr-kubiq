"""What one run authors: the kubiq checkout, the exam to write, the server to test on.

One responsibility: turning the caller's per-call parameters into a validated
`Target` and making it reachable from every tool during that run.

The parameters arrive in `RunContext.params` — named in the MCP call or as CLI
flags — never from the environment. A process serving several callers cannot
have one kubiq checkout, and an exam written into the wrong repository is worse
than one that refuses to start.

The target rides a `ContextVar`. Tools are module-level functions that take
drill ids and question numbers, not paths or URLs, so they need an ambient way
to find the run's target. `build` activates it in the thread that then streams
the graph; LangGraph copies the context into the worker threads it fans out to.
"""

from __future__ import annotations

import re
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

from swarmr.core.team import Param, TeamError

__all__ = ["PARAMS", "Target", "TargetError", "activate", "current", "minor"]

PARAMS = (
    Param(
        "kubiq",
        "Path of the codectl/kubiq checkout to write the exam into; its root (the "
        "directory holding `drills/` and `exams/`). Defaults to the current "
        "directory, so running from inside the checkout needs no flag.",
        required=False,
    ),
    Param(
        "exam",
        "Identifier of the exam to author, e.g. `ckad-mock-7`. Becomes the exam "
        "file `exams/<exam>.yaml` and the drill directory `drills/<exam>/`. Must "
        "not exist yet.",
    ),
    Param(
        "certification",
        "Which certification the exam mocks: CKAD (default), CKA or CKS. Sets the "
        "curriculum the author follows and the group the exam is listed under.",
        required=False,
    ),
    Param(
        "server",
        "Base URL of a running kubiq that is serving the checkout, used by the "
        "tester to prove each question on a live cluster. Default "
        "http://localhost:8080.",
        required=False,
    ),
    Param(
        "version",
        "Kubernetes minor version to author against, e.g. 1.37: the docs the "
        "author reads and the features it may use. Must not exceed the version "
        "of the cluster kubiq tests on; the run refuses to start otherwise.",
        required=False,
    ),
    Param(
        "questions",
        "How many questions the exam should have. Default 21 (CKAD/CKA) or 19 (CKS), "
        "the smallest counts whose weights from 1, 2, 4 and 8 can carry every "
        "curriculum share exactly.",
        required=False,
    ),
)

_EXAM_ID = re.compile(r"^[a-z][a-z0-9-]{2,40}$")
_VERSION = re.compile(r"^v?(\d+)\.(\d+)")
_CERTS = {"CKAD": 21, "CKA": 21, "CKS": 19}


class TargetError(TeamError):
    """The run has no usable target; the message names what to supply."""


@dataclass(frozen=True, slots=True)
class Target:
    kubiq: Path
    exam: str
    certification: str
    server: str
    questions: int
    version: str | None = None

    @property
    def exam_file(self) -> Path:
        return self.kubiq / "exams" / f"{self.exam}.yaml"

    @property
    def drill_dir(self) -> Path:
        return self.kubiq / "drills" / self.exam


_current: ContextVar[Target | None] = ContextVar("swarmr_kubiq_target", default=None)


def _checkout(raw: str) -> Path:
    path = Path(raw.strip() or ".").expanduser().resolve()
    if not path.is_dir():
        raise TargetError(f"kubiq={raw!r} is not a directory.")
    if not raw.strip() and not (path / "drills").is_dir():
        raise TargetError(
            f"{path} is not a kubiq checkout (no `drills/`); run from the checkout "
            "root or pass it as `kubiq`."
        )
    for sub in ("drills", "exams"):
        if not (path / sub).is_dir():
            raise TargetError(
                f"{path} has no `{sub}/` directory; pass the root of the kubiq "
                "checkout as `kubiq`."
            )
    return path


def activate(params: dict[str, str] | None) -> Target:
    """Validate the caller's params and make them this run's target.

    `Team.context` has already guaranteed the required names are present; this
    checks what they point at, which only this team can judge.
    """
    given = params or {}
    kubiq = _checkout(given.get("kubiq", ""))
    exam = given.get("exam", "").strip()
    if not _EXAM_ID.match(exam):
        raise TargetError(
            f"exam={exam!r} is not a valid exam id; use lowercase letters, digits "
            "and dashes, e.g. ckad-mock-7."
        )
    cert = (given.get("certification") or "CKAD").strip().upper()
    if cert not in _CERTS:
        raise TargetError(
            f"certification={cert!r} is not one of {', '.join(sorted(_CERTS))}."
        )
    raw_n = (given.get("questions") or "").strip()
    try:
        questions = int(raw_n) if raw_n else _CERTS[cert]
    except ValueError as exc:
        raise TargetError(f"questions={raw_n!r} is not a number.") from exc
    if not 1 <= questions <= 40:
        raise TargetError("questions must be between 1 and 40.")
    server = (given.get("server") or "http://localhost:8080").strip().rstrip("/")
    if not server.startswith(("http://", "https://")):
        raise TargetError(f"server={server!r} must be an http(s) URL.")
    raw_v = (given.get("version") or "").strip()
    version: str | None = None
    if raw_v:
        m = _VERSION.match(raw_v)
        if not m:
            raise TargetError(f"version={raw_v!r} is not a Kubernetes version like 1.37.")
        version = f"{m.group(1)}.{m.group(2)}"
    target = Target(
        kubiq=kubiq,
        exam=exam,
        certification=cert,
        server=server,
        questions=questions,
        version=version,
    )
    _current.set(target)
    return target


def current() -> Target:
    """The active run's target, or an error when no run activated one."""
    target = _current.get()
    if target is None:
        raise TargetError(
            "No target is active: a tool was called outside a run. The team's "
            "`build` activates the target from the call's kubiq/exam params."
        )
    return target


def minor(version: str) -> tuple[int, int]:
    """(major, minor) of a version string such as `v1.35.5+k3s1` or `1.37`."""
    m = _VERSION.match(version.strip())
    if not m:
        raise TargetError(f"{version!r} is not a Kubernetes version")
    return int(m.group(1)), int(m.group(2))
