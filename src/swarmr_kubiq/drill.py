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
    "GRADER_KINDS",
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


# Kinds kubiq's grader resolves by bare name (src/grader/kinds.rs). Anything
# else must be written as `group/version/Kind/plural[/cluster]`.
_GRADER_KINDS_TEXT = """
ClusterRole ClusterRoleBinding ConfigMap CronJob CustomResourceDefinition DaemonSet
Deployment Endpoints Event HorizontalPodAutoscaler Ingress IngressClass Job LimitRange
Namespace NetworkPolicy PersistentVolume PersistentVolumeClaim Pod PodDisruptionBudget
ReplicaSet ResourceQuota Role RoleBinding Secret Service ServiceAccount StatefulSet
StorageClass
"""
GRADER_KINDS = frozenset(_GRADER_KINDS_TEXT.split())

# A solution must do something. Read-only kubectl, `vim` and prose are not it.
_MUTATING = re.compile(
    r"\b(kubectl|k)\s+(apply|create|patch|set|scale|delete|replace|label|annotate|"
    r"expose|run|rollout|debug|edit|taint|cordon|uncordon|drain|autoscale|cp)\b"
    r"|\bhelm\s+(install|upgrade|uninstall|rollback|repo|dependency)\b"
    r"|\bkustomize\s+build\b|(^|[^<])>\s*\S|\btee\b|\bsed\s+-i\b|\bcp\b|\bmv\b"
)

# PEM private keys of any flavour (RSA, EC, PKCS#8, OpenSSH), plus a base64
# blob under `tls.key`, which is the same thing wearing a Secret.
_PRIVATE_KEY = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|^\s*tls\.key:\s*[A-Za-z0-9+/]{40,}", re.M
)


def _strip_comments(text: str) -> str:
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
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

    def manifest_kinds(self) -> set[tuple[str, str]]:
        """Every (apiVersion, Kind) applied by setup, solution or files, plus
        the kinds the checks read in `group/version/Kind/plural` form."""
        found: set[tuple[str, str]] = set()
        text = "\n".join([self.setup or "", self.solution, *self.files.values()])
        version: str | None = None
        for line in text.splitlines():
            if m := re.match(r"\s*apiVersion:\s*[\"']?([\w./-]+)", line):
                version = m.group(1)
            elif m := re.match(r"\s*kind:\s*[\"']?([A-Z][A-Za-z0-9]+)", line):
                if version and not version.startswith("{{"):
                    found.add((version, m.group(1)))
                version = None
        for c in self.checks:
            kind = str((c.resource or {}).get("kind", ""))
            parts = kind.split("/")
            if len(parts) >= 4:
                found.add(("/".join(parts[:2]), parts[2]))
        return found

    def setup_objects(self) -> dict[str, str]:
        """`name -> Kind` of every object the setup creates."""
        out: dict[str, str] = {}
        try:
            docs = list(yaml.safe_load_all(self.setup or ""))
        except yaml.YAMLError:
            return out
        for doc in docs:
            if isinstance(doc, dict):
                name = (doc.get("metadata") or {}).get("name")
                if name and doc.get("kind"):
                    out[str(name)] = str(doc["kind"])
        return out

    def declared_kinds(self) -> set[tuple[str, str]]:
        """(apiVersion, Kind) pairs served by CRDs the drill itself installs,
        from every `CustomResourceDefinition` document in setup, files or
        solution: the cluster serves them only once setup has run, so the
        served-kinds gate must not count them as missing."""
        text = "\n".join([self.setup or "", self.solution, *self.files.values()])
        out: set[tuple[str, str]] = set()
        for block in re.split(r"^---\s*$|<<-?\s*'?EOF'?|^EOF\s*$", text, flags=re.M):
            if "CustomResourceDefinition" not in block:
                continue
            group = re.search(r"(?:^|[{,\s])group:\s*[\"']?([\w.-]+)", block)
            # `spec.names.kind` is the only `kind:` in a CRD document besides
            # the CRD's own; block or flow style.
            kinds = [
                k
                for k in re.findall(
                    r"(?:^|[{,\s])kind:\s*[\"']?([A-Z][A-Za-z0-9]+)", block
                )
                if k != "CustomResourceDefinition"
            ]
            versions = re.findall(r"^\s*-\s*name:\s*[\"']?(v\d\w*)", block, re.M)
            if group and kinds:
                out.update((f"{group.group(1)}/{v}", kinds[0]) for v in versions)
        return out


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
    generated_by: str = ""

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
        if self.generated_by:
            out["generated_by"] = self.generated_by
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
    for i, d in enumerate(docs):
        if not isinstance(d, dict):
            raise DrillError(
                f"{where}: docs[{i}] must be a mapping with `label` and `url`, not "
                f"{type(d).__name__}"
            )
    setup = data.get("setup")
    if setup is not None and not isinstance(setup, str):
        raise DrillError(
            f"{where}: `setup` must be one block string of YAML documents (`setup: |`, "
            "documents separated by `---`), not a YAML list"
        )
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
        setup=setup,
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
    if len(drill.docs) > 1:
        raise DrillError(f"{where}: at most one `docs` link (the main topic) is allowed")
    for i, c in enumerate(drill.checks):
        if (c.resource is None) == (c.exec is None):
            raise DrillError(
                f"{where}: checks[{i}] needs exactly one of `resource`/`exec`"
            )
        if c.resource is not None and "kind" not in c.resource:
            raise DrillError(f"{where}: checks[{i}].resource needs `kind`")
        if c.resource is not None:
            kind = str(c.resource["kind"])
            if kind not in GRADER_KINDS and kind.count("/") not in (3, 4):
                raise DrillError(
                    f"{where}: checks[{i}].resource kind {kind!r} is not one kubiq's "
                    "grader knows; use one of "
                    f"{', '.join(sorted(GRADER_KINDS))}, or write it as "
                    "`group/version/Kind/plural` (add `/cluster` if cluster-scoped), "
                    "e.g. admissionregistration.k8s.io/v1/ValidatingAdmissionPolicy/"
                    "validatingadmissionpolicies/cluster"
                )
        if c.resource is not None and not (
            c.resource.get("name") or c.resource.get("selector")
        ):
            raise DrillError(f"{where}: checks[{i}].resource needs `name` or `selector`")
        if c.exec is not None:
            cmd = c.exec.get("command")
            if not isinstance(cmd, list) or not cmd:
                raise DrillError(f"{where}: checks[{i}].exec needs a `command` list")
            for j, part in enumerate(cmd):
                if not isinstance(part, str):
                    raise DrillError(
                        f"{where}: checks[{i}].exec.command[{j}] must be a string, got "
                        f"{type(part).__name__} — a shell line containing `: ` must be "
                        "quoted or written as a block scalar (`- |`), else YAML reads it "
                        "as a mapping"
                    )
            if not c.exec.get("shell") and not c.exec.get("pod"):
                raise DrillError(
                    f"{where}: checks[{i}].exec needs `pod` or `shell: true`"
                )
        for j, a in enumerate(c.assert_):
            if not isinstance(a, dict):
                raise DrillError(f"{where}: checks[{i}].assert[{j}] must be a mapping")
            unknown = set(a) - _ASSERT_KEYS
            if unknown:
                raise DrillError(
                    f"{where}: checks[{i}].assert[{j}] unknown keys {sorted(unknown)}"
                )
            if "path" in a and not isinstance(a["path"], str):
                raise DrillError(
                    f"{where}: checks[{i}].assert[{j}].path must be a string"
                )
            if "contains" in a and not isinstance(a["contains"], str):
                raise DrillError(
                    f"{where}: checks[{i}].assert[{j}].contains must be a string"
                )
            if not (
                set(a) & {"equals", "contains", "exists", "len_min", "exit", "any_of"}
            ):
                raise DrillError(
                    f"{where}: checks[{i}].assert[{j}] has no predicate "
                    "(equals/contains/exists/len_min/exit/any_of)"
                )
    if drill.setup:
        try:
            docs = [d for d in yaml.safe_load_all(drill.setup) if isinstance(d, dict)]
        except yaml.YAMLError as exc:
            raise DrillError(f"{where}: setup is not valid YAML: {exc}") from exc
        for d in docs:
            kind = str(d.get("kind") or "")
            if kind not in GRADER_KINDS:
                raise DrillError(
                    f"{where}: setup kind {kind!r} is not supported by kubiq; setup may "
                    f"only contain {', '.join(sorted(GRADER_KINDS))}. Create anything "
                    "else (custom resources, CRD instances) from a start hook: put the "
                    "manifest under `files` and apply it from "
                    "`/opt/student-node/start.d/<drill id>.sh`."
                )
    for item in drill.cleanup:
        if "/" not in item:
            raise DrillError(f"{where}: cleanup entry {item!r} must be Kind/name")
    if not strict:
        return
    for i, c in enumerate(drill.checks):
        if c.resource is None or not c.resource.get("selector"):
            continue
        # A selector resolves to a LIST; the grader reads `status.phase` on a
        # list as <missing> and the check can never pass. House style is
        # `[*].status.phase` / `[0].metadata.name` (and bare `len_min`).
        for j, a in enumerate(c.assert_):
            path = str(a.get("path", ""))
            if path and not path.startswith("["):
                raise DrillError(
                    f"{where}: checks[{i}].assert[{j}].path {path!r} reads a field "
                    "on a `selector` result, which is a list; start the path "
                    f"with `[*].` or `[0].` (e.g. `[*].{path}`)"
                )
    created = drill.setup_objects()
    for i, c in enumerate(drill.checks):
        for j, a in enumerate(c.assert_):
            path = str(a.get("path", ""))
            if "\\." in path:
                # The grader's path syntax quotes a dotted key; a backslash is
                # a literal character it never finds.
                key = path.split("\\.")[0].rsplit(".", 1)[-1]
                raise DrillError(
                    f"{where}: checks[{i}].assert[{j}].path {path!r} escapes a dot; "
                    f'the grader reads dotted keys as `["{key}.…"]` '
                    f'(e.g. `data["mode.conf"]`), never `\\.`'
                )
            if "?(" in path or "@" in path:
                # kubectl JSONPath filters; kubiq's path engine knows only
                # `[N]`, `[*]`, `[key=value]` and `["quoted.key"]`.
                raise DrillError(
                    f"{where}: checks[{i}].assert[{j}].path {path!r} uses a JSONPath "
                    "filter; the grader supports only `[N]`, `[*]`, `[name=web]` and "
                    '`["dotted.key"]`. For a condition, use an exec check with '
                    "kubectl's own jsonpath"
                )
        pod = (c.exec or {}).get("pod")
        if pod and created.get(str(pod), "Pod") != "Pod":
            # Pods of a controller carry generated names; exec'ing into the
            # controller's name finds nothing.
            raise DrillError(
                f"{where}: checks[{i}].exec.pod {pod!r} is the setup's "
                f"{created[str(pod)]}, not a Pod; its Pods have generated names. "
                'Use `exec: {shell: true, command: [bash, -lc, "kubectl exec -n <ns> '
                '$(kubectl get pod -n <ns> -l <selector> -o name | head -1) -- …"]}`'
            )
    for src in (drill.setup or "", drill.solution, *drill.files.values()):
        for m in re.finditer(r"hostPath:\s*\n((?:[ \t]+\S.*\n?)+)", src):
            block = m.group(1)
            if "type:" not in block:
                raise DrillError(
                    f"{where}: a hostPath volume must set `type: DirectoryOrCreate` "
                    "(the node has no such path yet; without it the Pod never mounts)"
                )
    for field_name, text in (
        ("setup", drill.setup or ""),
        ("solution", drill.solution),
        *((f"files[{p}]", body) for p, body in drill.files.items()),
    ):
        if _PRIVATE_KEY.search(text):
            raise DrillError(
                f"{where}: {field_name} embeds a private key; key material never "
                "sits in a drill file. Mint it at provision time instead — an "
                "`openssl req -x509 -newkey rsa:2048 -nodes …` in a "
                "`/opt/student-node/start.d/<id>.sh` hook that then creates the "
                "TLS Secret — or make the candidate create it"
            )
    if not _MUTATING.search(_strip_comments(drill.solution)):
        raise DrillError(
            f"{where}: the solution runs no command that changes anything (no kubectl "
            "apply/create/patch/set/scale/delete/replace/label/annotate/expose/run/"
            "rollout/debug, no helm install/upgrade/uninstall, no file write); a "
            "solution that only explains cannot be proven"
        )
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


def parse_exam(text: str, where: str = "<exam>", *, partial: bool = False) -> Exam:
    """An `Exam` from YAML text. `partial` accepts an exam under construction —
    an operator may have removed a question — where the weights need not
    sum to 100 yet; the writer files it back as a draft until it is full."""
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
        generated_by=str(data.get("generated_by") or ""),
    )
    validate_exam(exam, where, partial=partial)
    return exam


def validate_exam(exam: Exam, where: str = "<exam>", *, partial: bool = False) -> None:
    if exam.draft and not exam.questions:
        return
    total = sum(q.weight for q in exam.questions)
    if not exam.draft and not partial and total != 100:
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


def load_exam(path: Path, *, partial: bool = False) -> Exam:
    return parse_exam(path.read_text(), str(path), partial=partial)
