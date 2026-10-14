"""Proving a drill on a live cluster: stage it, solve it, grade it, leave nothing.

One responsibility: the sequence that turns "the author thinks this works"
into evidence. A question is proven only when all of these hold in one fresh
session: the checks FAIL before anything is done (a check that passes on an
empty namespace grades nothing), the drill's own `solution` run in the
candidate shell makes them PASS, and the cluster is as it was once the session
is finished and deleted (no namespace, CRD, PV, ClusterRole or binding left).

kubiq reads exams and drills from disk at startup, so `stage_exam` writes the
candidate files into the checkout; the caller restarts the server
(`kubiq_api.restart_server`) before proving. Only one session may exist at a
time and this harness never deletes one it did not create.

`_run` and `_sleep` are the only paths to a subprocess or a clock; tests
replace them.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol

from swarmr_kubiq import kubiq_api
from swarmr_kubiq.drill import (
    Drill,
    DrillError,
    Exam,
    dump_drill,
    dump_exam,
    validate_drill,
    validate_exam,
)
from swarmr_kubiq.kubiq_api import KubiqError
from swarmr_kubiq.target import Target

__all__ = [
    "SETTLE_MAX",
    "SETTLE_POLL",
    "TEARDOWN_KINDS",
    "Verdict",
    "prove_exam",
    "prove_question",
    "settle_cap",
    "solution_script",
    "stage_exam",
]

_run = kubiq_api.run
_sleep = time.sleep

# A solution's effects land asynchronously (a PV rebinding, a rollout, a Job
# completing); grading is polled until every check passes or this cap elapses.
SETTLE_MAX = 90.0
SETTLE_POLL = 3.0

# Cluster-scoped kinds an exam may create and kubiq's teardown must remove.
TEARDOWN_KINDS = ("namespaces", "crds", "pv", "clusterrole", "clusterrolebinding")
_TEARDOWN_WAIT = 120.0
_TEARDOWN_POLL = 3.0

_HEREDOC = re.compile(r"(?<!<)<<(?!<)-?\s*['\"]?(?P<tag>[A-Za-z_][A-Za-z0-9_]*)['\"]?")
_COMMENT = re.compile(r"^\s*#")


class SessionClient(Protocol):
    """What `prove_*` needs from a `KubiqClient`; tests pass a fake."""

    def sessions(self) -> list[dict[str, Any]]: ...
    def create_session(self, exam_id: str) -> dict[str, Any]: ...
    def wait_ready(self, sid: str, timeout: float = ...) -> dict[str, Any]: ...
    def check(self, sid: str, question: int) -> dict[str, Any]: ...
    def finish(self, sid: str, timeout: float = ...) -> dict[str, Any]: ...
    def delete(self, sid: str) -> None: ...
    def shell_exec(
        self, sid: str, cmd: str, namespace: str | None = None, timeout: float = ...
    ) -> tuple[int, str, str]: ...


@dataclass(slots=True)
class Verdict:
    """What one proving run observed about one question."""

    drill: str
    question: int
    namespace: str
    passed: bool
    negative_control_ok: bool
    results: list[dict[str, Any]]
    exit_code: int
    stdout: str
    stderr: str
    leftovers: list[str]
    session: str
    settle_seconds: float
    results_before: list[dict[str, Any]] = field(default_factory=list)

    @property
    def proven(self) -> bool:
        return self.passed and self.negative_control_ok and not self.leftovers

    def to_json(self) -> dict[str, Any]:
        return {"kind": "verdict", "proven": self.proven, **asdict(self)}


def stage_exam(target: Target, exam: Exam, drills: list[Drill]) -> list[Path]:
    """Write the exam and its drills into the checkout, kubiq-loadable.

    The exam goes to `exams/<exam>.yaml`, each drill to
    `drills/<exam>/qNN-<drill id>.yaml` numbered by its position in the exam.
    With fewer questions than the target asks for, the exam is written as a
    draft (kubiq lists it, weights need not sum to 100); at full size it must
    validate. `drills/<exam>/` belongs to this exam, so a `q*.yaml` from an
    earlier staging that is no longer listed is removed rather than left to
    collide on id.
    """
    by_id = {d.id: d for d in drills}
    if len(by_id) != len(drills):
        dupes = sorted({d.id for d in drills if sum(x.id == d.id for x in drills) > 1})
        raise DrillError(f"drills listed twice: {', '.join(dupes)}")
    listed = [q.drill for q in exam.questions]
    missing = [d for d in listed if d not in by_id]
    if missing:
        raise DrillError(f"exam lists drills not supplied: {', '.join(missing)}")
    unlisted = sorted(set(by_id) - set(listed))
    if unlisted:
        raise DrillError(f"drills not listed in the exam: {', '.join(unlisted)}")

    short = len(exam.questions) < target.questions
    staged = replace(
        exam,
        id=target.exam,
        draft=short,
        planned_questions=target.questions if short else 0,
        questions=list(exam.questions),
    )
    validate_exam(staged, str(target.exam_file))
    for n, drill_id in enumerate(listed, start=1):
        validate_drill(by_id[drill_id], f"q{n:02d}-{drill_id}")

    written: list[Path] = []
    target.exam_file.parent.mkdir(parents=True, exist_ok=True)
    target.exam_file.write_text(dump_exam(staged))
    written.append(target.exam_file)
    target.drill_dir.mkdir(parents=True, exist_ok=True)
    for n, drill_id in enumerate(listed, start=1):
        path = target.drill_dir / f"q{n:02d}-{drill_id}.yaml"
        path.write_text(dump_drill(by_id[drill_id]))
        written.append(path)
    for stale in target.drill_dir.glob("q*.yaml"):
        if stale not in written:
            stale.unlink()
    return written


def solution_script(solution: str) -> str:
    """The drill's solution as one bash script: comment-only lines dropped,
    heredoc bodies untouched (a `#` inside a manifest is content, not a
    comment)."""
    kept: list[str] = []
    terminator: str | None = None
    for line in solution.splitlines():
        if terminator is not None:
            kept.append(line)
            if line.strip() == terminator:
                terminator = None
            continue
        if _COMMENT.match(line):
            continue
        kept.append(line)
        opened = _HEREDOC.search(line)
        if opened:
            terminator = opened.group("tag")
    return "\n".join(kept).strip() + "\n"


def settle_cap() -> float:
    """Seconds to wait for checks to converge; KUBIQ_SETTLE_SECONDS, default 90."""
    return float(os.environ.get("KUBIQ_SETTLE_SECONDS", SETTLE_MAX))


def prove_question(
    client: SessionClient,
    target: Target,
    exam_id: str,
    question_index: int,
    drill: Drill,
    namespace: str,
) -> Verdict:
    """Prove one question in a fresh session of its own.

    `question_index` is the 0-based position in the exam file, what kubiq's
    `?question=N` expects. Raises `KubiqError` rather than touching a session
    this harness did not create.
    """
    _refuse_leftover(client)
    before = _snapshot()
    sid = str(client.create_session(exam_id)["id"])
    try:
        client.wait_ready(sid)
        negative = client.check(sid, question_index)
        rc, out, err, settle, after_solve = _solve(
            client, sid, question_index, drill, namespace
        )
        client.finish(sid)
    finally:
        client.delete(sid)
    leftovers = _leftovers(before)
    return Verdict(
        drill=drill.id,
        question=question_index,
        namespace=namespace,
        passed=bool(after_solve.get("passed")),
        negative_control_ok=not negative.get("passed"),
        results=_results(after_solve),
        results_before=_results(negative),
        exit_code=rc,
        stdout=_tail(out),
        stderr=_tail(err),
        leftovers=leftovers,
        session=sid,
        settle_seconds=settle,
    )


def prove_exam(
    client: SessionClient, target: Target, exam: Exam, drills: list[Drill]
) -> list[Verdict]:
    """Prove every question of the exam in ONE session: one provision, one
    teardown, one grade from `finish`. Faster than `prove_question` per
    question; the teardown proof covers the whole exam, so a leftover is
    reported on every verdict."""
    by_id = {d.id: d for d in drills}
    missing = [q.drill for q in exam.questions if q.drill not in by_id]
    if missing:
        raise DrillError(f"exam lists drills not supplied: {', '.join(missing)}")
    _refuse_leftover(client)
    before = _snapshot()
    sid = str(client.create_session(target.exam)["id"])
    negatives: list[dict[str, Any]] = []
    runs: list[tuple[int, str, str, float, dict[str, Any]]] = []
    try:
        client.wait_ready(sid)
        for index, question in enumerate(exam.questions):
            negatives.append(client.check(sid, index))
            runs.append(
                _solve(
                    client,
                    sid,
                    index,
                    by_id[question.drill],
                    question.namespace or "default",
                )
            )
        result = client.finish(sid)
    finally:
        client.delete(sid)
    leftovers = _leftovers(before)
    graded = {
        int(q.get("index", i)): q for i, q in enumerate(result.get("questions") or [])
    }
    verdicts: list[Verdict] = []
    for index, question in enumerate(exam.questions):
        grade = graded.get(index, {})
        rc, out, err, settle, _ = runs[index]
        verdicts.append(
            Verdict(
                drill=question.drill,
                question=index,
                namespace=question.namespace or "default",
                passed=bool(grade.get("passed")),
                negative_control_ok=not negatives[index].get("passed"),
                results=_results(grade),
                results_before=_results(negatives[index]),
                exit_code=rc,
                stdout=_tail(out),
                stderr=_tail(err),
                leftovers=leftovers,
                session=sid,
                settle_seconds=settle,
            )
        )
    return verdicts


def _refuse_leftover(client: SessionClient) -> None:
    existing = client.sessions()
    if existing:
        ids = ", ".join(str(s.get("id", "?")) for s in existing)
        raise KubiqError(
            f"a session already exists ({ids}) and kubiq allows one at a time. It is "
            "not this harness's, so it will not be deleted; finish it in the kubiq UI "
            "or ask the operator, then retry."
        )


def _solve(
    client: SessionClient, sid: str, question_index: int, drill: Drill, namespace: str
) -> tuple[int, str, str, float, dict[str, Any]]:
    """Run the solution, then grade repeatedly until it passes or the cap elapses.

    Returns the shell result, the seconds spent converging and the last grade.
    """
    rc, out, err = client.shell_exec(sid, solution_script(drill.solution), namespace)
    cap = settle_cap()
    started = time.monotonic()
    while True:
        report = client.check(sid, question_index)
        elapsed = time.monotonic() - started
        if report.get("passed") or elapsed >= cap:
            return rc, out, err, round(elapsed, 1), report
        _sleep(SETTLE_POLL)


def _results(report: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "name": str(r.get("name", "?")),
            "ok": bool(r.get("ok")),
            "detail": str(r.get("detail", "")),
        }
        for r in report.get("results") or []
    ]


def _tail(text: str, lines: int = 40, chars: int = 3000) -> str:
    kept = "\n".join(text.strip().splitlines()[-lines:])
    return kept[-chars:]


def _snapshot() -> set[str]:
    """Every cluster-scoped object of the kinds an exam could leave behind."""
    found: set[str] = set()
    for kind in TEARDOWN_KINDS:
        rc, out, err = _run(["kubectl", "get", kind, "-o", "name"], timeout=60)
        if rc != 0:
            raise KubiqError(f"kubectl get {kind} failed: {err.strip() or rc}")
        found.update(line.strip() for line in out.splitlines() if line.strip())
    return found


def _leftovers(before: set[str]) -> list[str]:
    """Objects present after teardown that were not before.

    kubiq's teardown runs in the background and namespace deletion is
    asynchronous, so a diff taken immediately would list namespaces still
    Terminating. Poll a bounded number of times until the diff is empty.
    """
    extra = sorted(_snapshot() - before)
    for _ in range(int(_TEARDOWN_WAIT / _TEARDOWN_POLL)):
        if not extra:
            break
        _sleep(_TEARDOWN_POLL)
        extra = sorted(_snapshot() - before)
    return extra
