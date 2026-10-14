"""The tools each role is given, and the exam under construction they share.

One responsibility: the LangChain tool surface. Every tool takes question
numbers, drill ids, YAML text or search terms — never a path, never a URL —
and finds the run's checkout and server through `target.current()`. The
knowledge lives next door: `drill` parses, `catalogue` reads what exists,
`similarity` scores, `docs` searches, `harness` and `kubiq_api` prove.

The exam under construction is module state, keyed by question number: what
the author staged, the task text as first staged (the critic's baseline), the
tester's last verdict, and the digest of what the server last loaded. `build`
resets it at the start of every run.

Design rules:
  * The only shell is `shell`, and it runs one `kubectl` invocation inside a
    session this process opened; chaining operators are refused.
  * A session is opened only when `/api/sessions` is empty, and only sessions
    this process opened are ever deleted. kubiq allows one session at a time.
  * The server is restarted only when the staged exam changed since the last
    restart, because the restart is the slow step.
"""

from __future__ import annotations

import hashlib
import re
import shlex
from pathlib import Path
from typing import Any

from langchain_core.tools import tool

from swarmr_kubiq import docs, harness, kubiq_api, similarity
from swarmr_kubiq.catalogue import Catalogue, profile_catalogue
from swarmr_kubiq.drill import (
    Drill,
    DrillError,
    Exam,
    Question,
    dump_drill,
    parse_drill,
    validate_drill,
)
from swarmr_kubiq.output import emit, guard
from swarmr_kubiq.report_tool import FILE_REPORT_TOOL
from swarmr_kubiq.target import Target, current

__all__ = [
    "AUTHOR_TOOLS",
    "COMMANDER_TOOLS",
    "CRITIC_TOOLS",
    "TESTER_TOOLS",
    "catalogue_search",
    "diff_task",
    "grader_details",
    "negative_control",
    "prove_question",
    "read_doc",
    "read_drill",
    "reset_staging",
    "search_docs",
    "shell",
    "similar_drills",
    "stage_drill",
    "validate_drill_yaml",
    "write_exam_file",
]

_PASS_PERCENT = {"CKAD": 75, "CKA": 66, "CKS": 67}
_DURATION_MINUTES = 120
_WEIGHTS = (1, 2, 4, 8)
_MOCK_NUMBER = re.compile(r"-(\d+)$")
_SHELL_OPERATORS = re.compile(r"[;&|<>`\n]|\$\(")

# The exam under construction. Question number → (drill, namespace, weight).
_staged: dict[int, tuple[Drill, str | None, int]] = {}
# Question number → task text when that drill id was first staged there.
_task_at_staging: dict[int, tuple[str, str]] = {}
# Question number → the tester's last verdict, as emitted.
_verdicts: dict[int, dict[str, Any]] = {}
# Digest of the staged exam the server last loaded; None until first restart.
_loaded_digest: str | None = None
# Session id this process opened for `shell`, if any.
_shell_session: str | None = None
# One docs session for the run; lodestar may be a subprocess.
_docs_client: docs.DocsClient | None = None


def reset_staging() -> None:
    """Forget the previous run's exam; `build` calls this before the first tool."""
    global _loaded_digest, _shell_session, _docs_client
    _staged.clear()
    _task_at_staging.clear()
    _verdicts.clear()
    _loaded_digest = None
    _shell_session = None
    if _docs_client is not None:
        _docs_client.close()
    _docs_client = None


# -- helpers -------------------------------------------------------------------


def _question(question_index: int) -> int:
    idx = int(question_index)
    if idx not in _staged:
        staged = ", ".join(str(i) for i in sorted(_staged)) or "none"
        raise ValueError(f"question {idx} is not staged; staged: {staged}")
    return idx


def _api_index(idx: int) -> int:
    """kubiq's `?question=N`: the 0-based position in the written exam file."""
    return sorted(_staged).index(idx)


def _catalogue() -> Catalogue:
    return profile_catalogue(current())


def _find_drill(cat: Catalogue, drill_id: str) -> Drill:
    drill = cat.by_id(drill_id.strip())
    if drill is None:
        raise ValueError(f"no drill with id {drill_id!r} in the catalogue")
    return drill


def _foreign_ids(cat: Catalogue, target: Target) -> set[str]:
    """Drill ids owned by other exams; this exam's own staging may reuse its ids."""
    own = {q.drill for e in cat.exams if e.id == target.exam for q in e.questions}
    return cat.ids - own


def _parse_strict(drill_yaml: str) -> Drill:
    drill = parse_drill(drill_yaml, "<drill>")
    validate_drill(drill, "<drill>", strict=True)
    return drill


def _exam_title(target: Target) -> str:
    m = _MOCK_NUMBER.search(target.exam)
    number = f" {m.group(1)}" if m else f" ({target.exam})"
    return f"{target.certification} Mock Exam{number}"


def write_exam_file() -> list[Path]:
    """Assemble the exam from what is staged and write it with its drills.

    A draft while fewer than the target's question count are staged, so kubiq
    lists it; a full exam must have weights summing to 100.
    """
    target = current()
    questions = [
        Question(drill=drill.id, weight=weight, namespace=namespace)
        for _, (drill, namespace, weight) in sorted(_staged.items())
    ]
    exam = Exam(
        id=target.exam,
        title=_exam_title(target),
        certification=target.certification,
        description=(
            f"{target.questions} questions mirroring the {target.certification} "
            f"domains, {_DURATION_MINUTES // 60} hours, pass at "
            f"{_PASS_PERCENT[target.certification]} %."
        ),
        duration_minutes=_DURATION_MINUTES,
        pass_percent=_PASS_PERCENT[target.certification],
        questions=questions,
        draft=len(questions) < target.questions,
        planned_questions=target.questions,
    )
    return harness.stage_exam(target, exam, [d for d, _, _ in _staged.values()])


def _staged_digest() -> str:
    h = hashlib.sha256()
    for idx, (drill, namespace, weight) in sorted(_staged.items()):
        h.update(f"{idx}\0{namespace}\0{weight}\0".encode())
        h.update(dump_drill(drill).encode())
    return h.hexdigest()


def _client() -> kubiq_api.KubiqClient:
    return kubiq_api.KubiqClient(current().server)


def _close_shell_session(client: kubiq_api.KubiqClient) -> None:
    """Delete the session `shell` opened, if any; proofs need an empty server."""
    global _shell_session
    if _shell_session is not None:
        sid, _shell_session = _shell_session, None
        client.delete(sid)


def _ensure_loaded(client: kubiq_api.KubiqClient) -> bool:
    """Restart kubiq when the staged exam differs from what it last loaded."""
    global _loaded_digest
    digest = _staged_digest()
    if digest == _loaded_digest:
        return False
    target = current()
    _close_shell_session(client)
    kubiq_api.restart_server(target.kubiq, client, target.exam, len(_staged))
    _loaded_digest = digest
    return True


def _docs() -> docs.DocsClient:
    global _docs_client
    if _docs_client is None:
        _docs_client = docs.DocsClient()
    return _docs_client


def _match_terms(query: str) -> list[str]:
    terms = [t for t in re.split(r"[^a-z0-9.-]+", query.lower()) if len(t) > 1]
    if not terms:
        raise ValueError("query is empty")
    return terms


# -- author ----------------------------------------------------------------------


@tool(parse_docstring=True)
@guard
def catalogue_search(query: str) -> str:
    """Find existing drills whose id, title, topic or task mention the query words.

    Every word must match somewhere in the drill. Use it to see what the
    catalogue already covers in a domain before designing a question, and to
    find an example to read with read_drill.

    Args:
        query: One or more words, e.g. "network policy egress" or "cronjob".
    """
    terms = _match_terms(query)
    cat = _catalogue()
    hits = []
    for d in sorted(cat.drills, key=lambda d: (d.exam, d.order)):
        hay = f"{d.id}\n{d.title}\n{d.topic}\n{d.task}".lower()
        if all(t in hay for t in terms):
            hits.append({"id": d.id, "exam": d.exam, "topic": d.topic, "title": d.title})
    return emit(
        {
            "kind": "catalogue",
            "query": query,
            "count": len(hits),
            "total_drills": len(cat.drills),
            "hits": hits[:40],
        }
    )


@tool(parse_docstring=True)
@guard
def read_drill(drill_id: str) -> str:
    """Return the full YAML of one existing drill, as kubiq loads it.

    Read one or two drills from the same domain before writing: they are the
    house style for setup, hints, solutions and checks.

    Args:
        drill_id: The drill id, e.g. "label-then-expose-all-ns".
    """
    drill = _find_drill(_catalogue(), drill_id)
    return emit(
        {
            "kind": "drill",
            "id": drill.id,
            "exam": drill.exam,
            "topic": drill.topic,
            "title": drill.title,
            "yaml": dump_drill(drill),
        }
    )


@tool(parse_docstring=True)
@guard
def search_docs(query: str) -> str:
    """Search the indexed Kubernetes and Helm documentation.

    Returns ranked passages with their page URLs; read a whole page with
    read_doc. Use it to ground a recent-feature question ("Kubernetes 1.34
    what's new", "feature gate graduated 1.33") and to copy exact field names
    a check must assert on.

    Args:
        query: Free text, e.g. "sidecar containers restartPolicy Always".
    """
    client = _docs()
    hits = client.search(query)
    payload: dict[str, Any] = {"kind": "docs", "query": query, "hits": hits}
    if not hits and client.note:
        payload["note"] = client.note
    return emit(payload)


@tool(parse_docstring=True)
@guard
def read_doc(url: str) -> str:
    """Read one indexed documentation page by the URL a search hit returned.

    Args:
        url: The page URL exactly as search_docs returned it.
    """
    client = _docs()
    text = client.page(url.strip())
    payload: dict[str, Any] = {"kind": "doc", "url": url, "text": text}
    if not text and client.note:
        payload["note"] = client.note
    return emit(payload)


@tool(parse_docstring=True)
@guard
def similar_drills(drill_yaml: str) -> str:
    """Score a drill against every drill in the catalogue and list the nearest.

    Scores combine the JSON paths the checks assert on, the resource kinds and
    kubectl verbs involved, and the task wording, with names stripped. A
    neighbour at 0.62 or above is the same question; redesign around a
    different mechanic, because renaming objects does not move the score.

    Args:
        drill_yaml: The complete drill as YAML text.
    """
    drill = parse_drill(drill_yaml, "<drill>")
    ranked = similarity.nearest(drill, _catalogue())
    matches = [
        {
            "id": other,
            "score": round(value, 3),
            "duplicate": similarity.is_duplicate(value),
            "reason": reason,
        }
        for other, value, reason in ranked
    ]
    return emit(
        {
            "kind": "similarity",
            "id": drill.id,
            "threshold": similarity.THRESHOLD,
            "matches": matches,
        }
    )


@tool(parse_docstring=True)
@guard
def validate_drill_yaml(drill_yaml: str) -> str:
    """Parse and strictly validate a drill without writing anything.

    Strict means kubiq's own rules plus this team's: images from the allowed
    set only, no vendor names. Returns ok, or the first error with its field.

    Args:
        drill_yaml: The complete drill as YAML text.
    """
    try:
        drill = _parse_strict(drill_yaml)
    except DrillError as exc:
        return emit({"kind": "validation", "ok": False, "error": str(exc)})
    return emit(
        {
            "kind": "validation",
            "ok": True,
            "id": drill.id,
            "checks": len(drill.checks),
            "images": drill.images(),
            "cleanup": drill.cleanup,
        }
    )


@tool(parse_docstring=True)
@guard
def stage_drill(drill_yaml: str, question_index: int, namespace: str, weight: int) -> str:
    """Validate a drill strictly and write it into the exam under construction.

    The drill is written into the checkout and merged into the exam file at its
    question number with the namespace and weight. Staging a question number
    again replaces its drill; if the drill id is the same, the task text is
    compared with the text first staged and any change is reported, because
    the critic treats a changed task as a violation.

    Args:
        drill_yaml: The complete drill as YAML text.
        question_index: The question number, 1-based, as the commander assigned.
        namespace: The question's namespace, e.g. "ckad07-bluegreen"; empty for a
            cluster-scoped question.
        weight: The question's weight: 1, 2, 4 or 8.
    """
    idx = int(question_index)
    if idx < 1:
        raise ValueError("question_index must be 1 or more")
    if int(weight) not in _WEIGHTS:
        raise ValueError(f"weight must be one of {_WEIGHTS}, not {weight}")
    drill = _parse_strict(drill_yaml)
    ns = namespace.strip() or None
    target = current()
    for other, (staged, _, _) in _staged.items():
        if other != idx and staged.id == drill.id:
            raise ValueError(f"drill {drill.id!r} is already staged as question {other}")
    if drill.id in _foreign_ids(profile_catalogue(target), target):
        raise ValueError(f"drill id {drill.id!r} already exists in the catalogue")

    baseline = _task_at_staging.get(idx)
    replaced = baseline is not None and baseline[0] != drill.id
    task_changed = not replaced and baseline is not None and baseline[1] != drill.task

    previous = _staged.get(idx)
    _staged[idx] = (drill, ns, int(weight))
    try:
        written = write_exam_file()
    except Exception:
        if previous is None:
            del _staged[idx]
        else:
            _staged[idx] = previous
        raise
    if baseline is None or replaced:
        _task_at_staging[idx] = (drill.id, drill.task)
        _verdicts.pop(idx, None)
    filename = f"q{_api_index(idx) + 1:02d}-{drill.id}.yaml"
    return emit(
        {
            "kind": "staged",
            "question": idx,
            "id": drill.id,
            "namespace": ns,
            "weight": int(weight),
            "path": str(next(p for p in written if p.name == filename)),
            "exam_file": str(target.exam_file),
            "staged_questions": len(_staged),
            "planned_questions": target.questions,
            "weight_total": sum(w for _, _, w in _staged.values()),
            "replaced": replaced,
            "task_changed": task_changed,
        }
    )


# -- tester ----------------------------------------------------------------------


@tool(parse_docstring=True)
@guard
def prove_question(question_index: int) -> str:
    """Prove one staged question on the live server, end to end.

    Restarts kubiq if the staged exam changed since it last loaded, opens a
    fresh session, grades the untouched namespace (the negative control, which
    must fail), runs the drill's own solution in the candidate's shell, grades
    again, ends the exam and lists any cluster-scoped object left behind. The
    verdict carries every check's name, ok flag and detail verbatim.

    Args:
        question_index: The question number, 1-based.
    """
    idx = _question(question_index)
    drill, namespace, _ = _staged[idx]
    target = current()
    client = _client()
    restarted = _ensure_loaded(client)
    _close_shell_session(client)
    verdict = harness.prove_question(
        client, target, target.exam, _api_index(idx), drill, namespace or "default"
    )
    payload = verdict.to_json()
    payload["question"] = idx
    payload["server_restarted"] = restarted
    _verdicts[idx] = payload
    return emit(payload)


@tool(parse_docstring=True)
@guard
def shell(question_index: int, command: str) -> str:
    """Run one kubectl command in the candidate's shell, in the question's namespace.

    For diagnosis after a verdict: what state the solution produced, what the
    grader saw. Opens a session when none is open; the session is closed before
    the next proof. kubectl only — no pipes, chaining, redirection or files.

    Args:
        question_index: The question number, 1-based; sets the namespace.
        command: A single kubectl invocation, e.g. "kubectl get pods -o wide".
    """
    global _shell_session
    idx = _question(question_index)
    _, namespace, _ = _staged[idx]
    cmd = command.strip()
    if _SHELL_OPERATORS.search(cmd):
        raise ValueError("command must be one kubectl invocation without shell operators")
    argv = shlex.split(cmd)
    if not argv or argv[0] != "kubectl":
        raise ValueError("command must start with kubectl")
    client = _client()
    restarted = _ensure_loaded(client)
    sid = _shell_session
    if sid is None:
        existing = client.sessions()
        if existing:
            ids = ", ".join(str(s.get("id", "?")) for s in existing)
            raise ValueError(
                f"a session exists that this run did not open ({ids}); it is not "
                "deleted on your behalf"
            )
        sid = str(client.create_session(current().exam)["id"])
        _shell_session = sid
        client.wait_ready(sid)
    rc, out, err = client.shell_exec(sid, cmd, namespace)
    return emit(
        {
            "kind": "shell",
            "question": idx,
            "namespace": namespace,
            "session": sid,
            "server_restarted": restarted,
            "exit": rc,
            "stdout": out[-4000:],
            "stderr": err[-2000:],
        }
    )


@tool(parse_docstring=True)
@guard
def grader_details(question_index: int) -> str:
    """Return the last verdict recorded for a question, verbatim.

    Args:
        question_index: The question number, 1-based.
    """
    idx = _question(question_index)
    verdict = _verdicts.get(idx)
    if verdict is None:
        raise ValueError(f"question {idx} has not been proved yet")
    return emit(verdict)


# -- critic ----------------------------------------------------------------------


@tool(parse_docstring=True)
@guard
def negative_control(question_index: int) -> str:
    """Grade a question on an untouched namespace; every check must fail.

    Opens a fresh session, grades without applying any solution, and lists the
    checks that passed anyway — each one grades nothing and lets a candidate
    earn the points by doing nothing. The session is deleted afterwards.

    Args:
        question_index: The question number, 1-based.
    """
    idx = _question(question_index)
    drill, namespace, _ = _staged[idx]
    client = _client()
    restarted = _ensure_loaded(client)
    _close_shell_session(client)
    existing = client.sessions()
    if existing:
        ids = ", ".join(str(s.get("id", "?")) for s in existing)
        raise ValueError(
            f"a session exists that this run did not open ({ids}); it is not "
            "deleted on your behalf"
        )
    sid = str(client.create_session(current().exam)["id"])
    try:
        client.wait_ready(sid)
        report = client.check(sid, _api_index(idx))
    finally:
        client.delete(sid)
    results = [
        {
            "name": str(r.get("name", "?")),
            "ok": bool(r.get("ok")),
            "detail": str(r.get("detail", "")),
        }
        for r in report.get("results") or []
    ]
    return emit(
        {
            "kind": "negative_control",
            "question": idx,
            "drill": drill.id,
            "namespace": namespace,
            "server_restarted": restarted,
            "passed": bool(report.get("passed")),
            "too_permissive": [r["name"] for r in results if r["ok"]],
            "results": results,
        }
    )


@tool(parse_docstring=True)
@guard
def diff_task(question_index: int) -> str:
    """Compare a question's task text now with the text when it was first staged.

    A fix round may change the solution, the checks or the setup; a task that
    changed after the first staging is a violation, because the tester proved
    one contract and the file carries another.

    Args:
        question_index: The question number, 1-based.
    """
    idx = _question(question_index)
    drill, _, _ = _staged[idx]
    _, baseline = _task_at_staging[idx]
    return emit(
        {
            "kind": "task_diff",
            "question": idx,
            "drill": drill.id,
            "changed": baseline != drill.task,
            "task_at_staging": baseline,
            "task_now": drill.task,
        }
    )


# Tool sets per role. Only the tester proves and only the tester has a shell;
# the author designs and stages but never touches the cluster, so a claim from
# it that a question works is, by construction, an inference. The critic grades
# the untouched namespace and compares texts; it cannot stage or solve.
AUTHOR_TOOLS = [
    catalogue_search,
    read_drill,
    search_docs,
    read_doc,
    similar_drills,
    validate_drill_yaml,
    stage_drill,
]
TESTER_TOOLS = [prove_question, shell, grader_details]
CRITIC_TOOLS = [
    negative_control,
    diff_task,
    read_drill,
    catalogue_search,
    similar_drills,
    validate_drill_yaml,
    search_docs,
    read_doc,
]
# The commander holds no authoring or cluster tools: its context stays clean,
# and it cannot fabricate an observation it never received.
COMMANDER_TOOLS = [FILE_REPORT_TOOL]
