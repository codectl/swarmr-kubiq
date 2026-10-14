"""The kubiq drill and exam file formats.

One responsibility: the shape of what this team writes. A drill is one question
— task, setup, files, solution, checks — stored as `drills/<exam>/qNN-<slug>.yaml`
and referenced by `id` from `exams/<exam>.yaml`. The schema mirrors kubiq's
`src/drill.rs` and `src/exam.rs`; anything kubiq's loader would reject must be
rejected here first, because the tester only learns about a bad file when the
server refuses to start.

Nothing here touches a cluster or a model: parse, validate, dump.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

__all__ = [
    "ALLOWED_IMAGES",
    "Check",
    "Drill",
    "DrillError",
    "Exam",
    "Question",
    "dump_drill",
    "dump_exam",
    "load_drill",
    "load_exam",
    "parse_drill",
    "parse_exam",
    "validate_drill",
    "validate_exam",
]

_ID = re.compile(r"^[a-z][a-z0-9-]{1,60}$")
_NS = re.compile(r"^[a-z][a-z0-9-]{1,62}$")
_ASSERT_KEYS = {"path", "equals", "contains", "exists", "len_min", "exit", "any_of"}
ALLOWED_IMAGES = (
    "nginx",
    "nginxinc/nginx-unprivileged",
    "busybox",
    "alpine",
    "redis",
    "httpd",
    "hashicorp/http-echo",
    "polinux/stress",
    "python",
    "perl",
    "registry.k8s.io/ingress-nginx/controller",
    "ghcr.io/headlamp-k8s/headlamp",
    "mysql",
    "ubuntu",
)


class DrillError(ValueError):
    """A drill or exam file kubiq would refuse, with the field named."""


@dataclass(slots=True)
class Check:
    name: str
    assert_: list[dict[str, Any]]
    resource: dict[str, Any] | None = None
    exec: dict[str, Any] | None = None

    def to_yaml(self) -> dict[str, Any]:
        out: dict[str, Any] = {"name": self.name}
        if self.resource is not None:
            out["resource"] = self.resource
        if self.exec is not None:
            out["exec"] = self.exec
        out["assert"] = self.assert_
        return out


@dataclass(slots=True)
class Drill:
    id: str
    order: int
    exam: str
    title: str
    topic: str
    task: str
    solution: str
    checks: list[Check]
    docs: list[dict[str, str]] = field(default_factory=list)
    hint: str | None = None
    setup: str | None = None
    cleanup: list[str] = field(default_factory=list)
    files: dict[str, str] = field(default_factory=dict)

    def to_yaml(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "order": self.order,
            "exam": self.exam,
            "title": self.title,
            "topic": self.topic,
            "task": self.task,
        }
        if self.docs:
            out["docs"] = self.docs
        if self.hint:
            out["hint"] = self.hint
        if self.setup:
            out["setup"] = self.setup
        if self.files:
            out["files"] = self.files
        if self.cleanup:
            out["cleanup"] = self.cleanup
        out["solution"] = self.solution
        out["checks"] = [c.to_yaml() for c in self.checks]
        return out

    def images(self) -> list[str]:
        """Container images referenced in setup, files and solution.

        Both manifest lines (`image: x`) and imperative flags (`--image=x`,
        `--image x`) count; a drill can introduce an image either way.
        """
        text = "\n".join([self.setup or "", self.solution, *self.files.values()])
        found = re.findall(
            r"(?:image:\s*|--image[= ])[\"']?([A-Za-z0-9][\w./:-]*)[\"']?", text
        )
        # Helm templates reference values (`{{ .Values.image }}`), not images.
        return sorted(
            {f for f in found if not f.startswith("{{") and not f.endswith(":")}
        )


@dataclass(slots=True)
class Question:
    drill: str
    weight: int
    namespace: str | None = None

    def to_yaml(self) -> dict[str, Any]:
        out: dict[str, Any] = {"drill": self.drill}
        if self.namespace:
            out["namespace"] = self.namespace
        out["weight"] = self.weight
        return out


@dataclass(slots=True)
class Exam:
    id: str
    title: str
    certification: str
    description: str
    duration_minutes: int
    pass_percent: int
    questions: list[Question]
    draft: bool = False
    planned_questions: int = 0

    def to_yaml(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "title": self.title,
            "certification": self.certification,
            "description": self.description,
            "duration_minutes": self.duration_minutes,
            "pass_percent": self.pass_percent,
        }
        if self.draft:
            out["draft"] = True
        if self.planned_questions:
            out["planned_questions"] = self.planned_questions
        out["questions"] = [q.to_yaml() for q in self.questions]
        return out


def _str(d: dict[str, Any], key: str, where: str, required: bool = True) -> str:
    v = d.get(key)
    if v is None:
        if required:
            raise DrillError(f"{where}: missing `{key}`")
        return ""
    if not isinstance(v, str):
        raise DrillError(f"{where}: `{key}` must be a string")
    return v


def parse_drill(text: str, where: str = "<drill>") -> Drill:
    """A `Drill` from YAML text, or `DrillError` naming the field."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise DrillError(f"{where}: not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise DrillError(f"{where}: top level must be a mapping")
    checks_raw = data.get("checks")
    if not isinstance(checks_raw, list) or not checks_raw:
        raise DrillError(f"{where}: `checks` must be a non-empty list")
    checks: list[Check] = []
    for i, c in enumerate(checks_raw):
        if not isinstance(c, dict):
            raise DrillError(f"{where}: checks[{i}] must be a mapping")
        asserts = c.get("assert")
        if not isinstance(asserts, list) or not asserts:
            raise DrillError(f"{where}: checks[{i}] `assert` must be a non-empty list")
        checks.append(
            Check(
                name=_str(c, "name", f"{where}: checks[{i}]"),
                assert_=asserts,
                resource=c.get("resource"),
                exec=c.get("exec"),
            )
        )
    files = data.get("files") or {}
    if not isinstance(files, dict):
        raise DrillError(f"{where}: `files` must be a mapping of path -> content")
    cleanup = data.get("cleanup") or []
    docs = data.get("docs") or []
    order = data.get("order")
    if not isinstance(order, int):
        raise DrillError(f"{where}: `order` must be an integer")
    drill = Drill(
        id=_str(data, "id", where),
        order=order,
        exam=_str(data, "exam", where),
        title=_str(data, "title", where),
        topic=_str(data, "topic", where),
        task=_str(data, "task", where),
        solution=_str(data, "solution", where),
        checks=checks,
        docs=[dict(d) for d in docs],
        hint=data.get("hint"),
        setup=data.get("setup"),
        cleanup=[str(x) for x in cleanup],
        files={str(k): str(v) for k, v in files.items()},
    )
    validate_drill(drill, where)
    return drill


def validate_drill(drill: Drill, where: str = "<drill>", *, strict: bool = False) -> None:
    """Reject what kubiq's loader would reject; with `strict`, also this team's
    authoring rules (allowed images, no vendor references) for drills it writes."""
    if not _ID.match(drill.id):
        raise DrillError(f"{where}: id {drill.id!r} is not lowercase-dash")
    if not drill.task.strip():
        raise DrillError(f"{where}: task is empty")
    if not drill.solution.strip():
        raise DrillError(f"{where}: solution is empty")
    if ":" in drill.title and not drill.title.startswith(('"', "'")):
        # kubiq parses the file with serde_yaml; an unquoted `a: b` title breaks it.
        # dump_drill quotes, but a hand-written file may not.
        pass
    for i, c in enumerate(drill.checks):
        if c.resource is None and c.exec is None:
            raise DrillError(f"{where}: checks[{i}] needs `resource` or `exec`")
        if c.resource is not None and "kind" not in c.resource:
            raise DrillError(f"{where}: checks[{i}].resource needs `kind`")
        if c.exec is not None and not isinstance(c.exec.get("command"), list):
            raise DrillError(f"{where}: checks[{i}].exec needs a `command` list")
        for j, a in enumerate(c.assert_):
            if not isinstance(a, dict):
                raise DrillError(f"{where}: checks[{i}].assert[{j}] must be a mapping")
            unknown = set(a) - _ASSERT_KEYS
            if unknown:
                raise DrillError(
                    f"{where}: checks[{i}].assert[{j}] unknown keys {sorted(unknown)}"
                )
    for item in drill.cleanup:
        if "/" not in item:
            raise DrillError(f"{where}: cleanup entry {item!r} must be Kind/name")
    if not strict:
        return
    for img in drill.images():
        if not img.startswith(ALLOWED_IMAGES):
            raise DrillError(
                f"{where}: image {img!r} is not in the allowed set; use one of "
                f"{', '.join(ALLOWED_IMAGES)}"
            )
    low = (drill.task + drill.solution + (drill.setup or "")).lower()
    if "kodekloud" in low:
        raise DrillError(f"{where}: vendor reference 'kodekloud' is not allowed")


class _Dumper(yaml.SafeDumper):
    pass


def _str_presenter(dumper: yaml.SafeDumper, data: str) -> yaml.Node:
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    if ":" in data or data.strip() != data or data.startswith(("*", "&", "!")):
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style='"')
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


_Dumper.add_representer(str, _str_presenter)


def dump_drill(drill: Drill) -> str:
    """YAML text kubiq accepts; block scalars for multi-line fields, quoted titles."""
    return yaml.dump(
        drill.to_yaml(), Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=200
    )


def load_drill(path: Path) -> Drill:
    return parse_drill(path.read_text(), str(path))


def parse_exam(text: str, where: str = "<exam>") -> Exam:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise DrillError(f"{where}: not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise DrillError(f"{where}: top level must be a mapping")
    qs: list[Question] = []
    for i, q in enumerate(data.get("questions") or []):
        if not isinstance(q, dict) or "drill" not in q or "weight" not in q:
            raise DrillError(f"{where}: questions[{i}] needs `drill` and `weight`")
        qs.append(
            Question(
                drill=str(q["drill"]),
                weight=int(q["weight"]),
                namespace=q.get("namespace"),
            )
        )
    exam = Exam(
        id=_str(data, "id", where),
        title=_str(data, "title", where),
        certification=str(data.get("certification") or "CKAD"),
        description=_str(data, "description", where, required=False),
        duration_minutes=int(data.get("duration_minutes") or 120),
        pass_percent=int(data.get("pass_percent") or 75),
        questions=qs,
        draft=bool(data.get("draft", False)),
        planned_questions=int(data.get("planned_questions") or 0),
    )
    validate_exam(exam, where)
    return exam


def validate_exam(exam: Exam, where: str = "<exam>") -> None:
    if exam.draft and not exam.questions:
        return
    total = sum(q.weight for q in exam.questions)
    if not exam.draft and total != 100:
        raise DrillError(f"{where}: weights sum to {total}, must be 100")
    seen: set[str] = set()
    for q in exam.questions:
        if q.drill in seen:
            raise DrillError(f"{where}: drill {q.drill!r} listed twice")
        seen.add(q.drill)
        if q.weight < 1:
            raise DrillError(f"{where}: drill {q.drill!r} has weight {q.weight}")
        if q.namespace is not None and not _NS.match(q.namespace):
            raise DrillError(f"{where}: namespace {q.namespace!r} is invalid")


def dump_exam(exam: Exam) -> str:
    return yaml.dump(
        exam.to_yaml(), Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=200
    )


def load_exam(path: Path) -> Exam:
    return parse_exam(path.read_text(), str(path))
