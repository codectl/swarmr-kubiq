"""The tools each role is given, and the exam under construction they share.

One responsibility: the LangChain tool surface. Every tool takes question
numbers, drill ids, YAML text or search terms — never a path, never a URL —
and finds the run's checkout and server through `target.current()`. The
knowledge lives next door: `drill` parses, `catalogue` reads what exists,
`similarity` scores, `docs` searches, `harness` and `kubiq_api` prove,
`proofs` remembers.

The exam under construction is module state, keyed by question number: the
plan (domain, competency, weight, namespace per slot), what the author
staged, the task text as first staged, the last verdict per question, and
the digest of what the server last loaded. `build` resets it at the start of
every run and `resume_staging` rebuilds it from the checkout and the proof
cache, so a rerun re-proves only what changed.

Design rules:
  * Everything arithmetic or structural is decided here, not by a model:
    `plan_exam` allocates weights that sum to 100, `stage_drill` refuses
    duplicates, foreign ids, a changed task text, near-copies and anything
    kubiq's own loader refuses — before a session is ever opened.
  * The server is never restarted: kubiq reads the checkout through a bind
    mount and `POST /api/reload` swaps the catalogue when staging changed.
  * A `FatalError` from the cluster is sticky: once seen, every cluster tool
    returns it without touching the server again.
  * A session is opened only by the proof tools, and only sessions this
    process opened are ever deleted. kubiq allows one session at a time.
"""

from __future__ import annotations

import hashlib
import re
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from langchain_core.tools import tool

from swarmr_kubiq import docs, harness, kubiq_api, pr, proofs, similarity
from swarmr_kubiq.catalogue import CURRICULUM, Catalogue, profile_catalogue
from swarmr_kubiq.drill import (
    Drill,
    DrillError,
    Exam,
    Question,
    dump_drill,
    load_drill,
    load_exam,
    parse_drill,
    validate_drill,
)
from swarmr_kubiq.kubiq_api import FatalError, KubiqError
from swarmr_kubiq.output import emit, guard
from swarmr_kubiq.report_tool import FILE_REPORT_TOOL
from swarmr_kubiq.target import Target, current

__all__ = [
    "AUTHOR_TOOLS",
    "COMMANDER_TOOLS",
    "CRITIC_TOOLS",
    "Slot",
    "allocate_weights",
    "catalogue_search",
    "finalize_exam",
    "grader_details",
    "plan_exam",
    "prove_all",
    "prove_questions",
    "read_doc",
    "read_drill",
    "record_rulings",
    "reset_staging",
    "resume_staging",
    "search_docs",
    "stage_drill",
    "write_exam_file",
]

_PASS_PERCENT = {"CKAD": 75, "CKA": 66, "CKS": 67}
_DURATION_MINUTES = 120
_WEIGHTS = (8, 4, 2, 1)
_MOCK_NUMBER = re.compile(r"-(\d+)$")
# A namespace topic word: `ckad07-bluegreen` is prefix + this.
_TOPIC = re.compile(r"^[a-z][a-z0-9]{0,20}$")


@dataclass(slots=True)
class Slot:
    """One planned question: where it sits in the curriculum and what it is worth."""

    question: int
    domain: str
    competency: str
    weight: int
    namespace: str | None
    file_graded: bool = False
    feature_version: str = ""


# The plan. Question number → slot; weights and namespaces live here only.
_plan: dict[int, Slot] = {}
# The exam under construction. Question number → the staged drill.
_staged: dict[int, Drill] = {}
# Question number → (drill id, task text) when that id was first staged there.
_task_at_staging: dict[int, tuple[str, str]] = {}
# Question number → the last verdict, as emitted by the harness.
_verdicts: dict[int, dict[str, Any]] = {}
# Digest of the staged exam the server last loaded; None until first reload.
_loaded_digest: str | None = None
# The text of the FatalError that ended cluster work, if one did.
_fatal: str | None = None
# kubiq has ONE session slot: every tool that reloads the server or opens a
# session takes this lock, so parallel calls from the model queue instead of
# racing each other.
_cluster_lock = threading.Lock()
# One docs session for the run; lodestar may be a subprocess.
_docs_client: docs.DocsClient | None = None


def reset_staging() -> None:
    """Forget the previous run's exam; `build` calls this before the first tool."""
    global _loaded_digest, _fatal, _docs_client, _served
    _served = None
    _plan.clear()
    _staged.clear()
    _task_at_staging.clear()
    _searched.clear()
    _read_pages.clear()
    _verdicts.clear()
    _loaded_digest = None
    _proofs_taken.clear()
    _fatal = None
    if _docs_client is not None:
        _docs_client.close()
    _docs_client = None


def resume_staging(target: Target) -> list[dict[str, Any]]:
    """Load an exam an earlier run left in the checkout, so a rerun continues it.

    Questions are re-staged at their position with their drill, namespace and
    weight (domain and competency are blank until the commander plans again);
    the task text is taken as the baseline, so a later fix round is held to
    it. A proof recorded by an earlier run is restored when the drill text
    and namespace are exactly what it proved, so only changed questions are
    proved again. Returns one summary per resumed question for the commander's
    facts. Empty when there is nothing to resume.
    """
    if not target.exam_file.is_file():
        return []
    exam = load_exam(target.exam_file, partial=True)
    by_id: dict[str, Drill] = {}
    for path in target.drill_dir.glob("*.yaml"):
        try:
            d = load_drill(path)
        except DrillError:
            continue  # reported below through kubiq's own loader message
        by_id[d.id] = d
    # kubiq's loader is the authority; its message names the file it refuses.
    # A server that is down or has no /api/check is a fact for the commander,
    # not a reason to abort the build.
    loader_problem, loader_everywhere = "", False
    known = proofs.load_proofs(target)
    resumed: list[dict[str, Any]] = []
    for pos, q in enumerate(exam.questions, start=1):
        drill = by_id.get(q.drill)
        if drill is None:
            continue
        # The exam file keeps order, not slot numbers: a run that staged 2 and
        # 5 lists them first and second. The drill's `order` (<mock><NN>)
        # carries the slot; fall back to position when it does not parse.
        idx = drill.order % 100 if 1 <= drill.order % 100 <= 40 else pos
        if idx in _staged:
            idx = pos
        _staged[idx] = drill
        _plan[idx] = Slot(
            question=idx,
            domain="",
            competency="",
            weight=q.weight,
            namespace=q.namespace,
        )
        _task_at_staging[idx] = (drill.id, drill.task)
        proven, ruling = False, ""
        if (verdict := known.get(proofs.proof_key(drill, q.namespace))) is not None:
            _verdicts[idx] = {**verdict, "question": idx, "resumed": True}
            proven = bool(verdict.get("proven"))
            ruling = str(verdict.get("ruling") or "")
        # Re-check with today's rules: a drill staged by an older version may
        # carry what kubiq or the API server would refuse. Listed, not dropped:
        # the commander sends the exact problem to the author in one batch.
        problem = ""
        try:
            validate_drill(drill, drill.id, strict=True)
        except DrillError as exc:
            problem = str(exc)
        else:
            problem = harness.dry_run_setup(drill)
        resumed.append(
            {
                "question": idx,
                "drill": drill.id,
                "title": drill.title,
                "topic": drill.topic,
                "weight": q.weight,
                "namespace": q.namespace,
                "proven": proven,
                "ruling": ruling,
                "problem": problem,
            }
        )
    # An exam the operator trimmed (a question deleted by hand) is no longer a
    # full exam: file it back as a draft so kubiq loads the catalogue while the
    # missing slot is authored. Same content otherwise; the dump is canonical.
    if _staged and len(_staged) < target.questions and not exam.draft:
        write_exam_file()
    try:
        loader_problem = kubiq_api.KubiqClient(target.server).check()
    except Exception as exc:
        loader_problem, loader_everywhere = f"loader check unavailable: {exc}", True
    for row in resumed:
        if not row["problem"] and (loader_everywhere or row["drill"] in loader_problem):
            row["problem"] = loader_problem
    return resumed


def _feasible(points: int, n: int) -> tuple[int, int, int, int] | None:
    """Counts (c8, c4, c2, c1) of each weight for `n` slots worth `points`:
    fewest 1s, then fewest 2s, then most 8s. None when impossible."""
    best: tuple[int, int, int, int] | None = None
    best_key: tuple[int, int, int] | None = None
    for c8 in range(n + 1):
        for c4 in range(n - c8 + 1):
            for c2 in range(n - c8 - c4 + 1):
                c1 = n - c8 - c4 - c2
                if 8 * c8 + 4 * c4 + 2 * c2 + c1 != points:
                    continue
                key = (c1, c2, -c8)
                if best_key is None or key < best_key:
                    best, best_key = (c8, c4, c2, c1), key
    return best


def allocate_weights(points: int, n: int) -> list[int]:
    """`n` weights from {1, 2, 4, 8} summing exactly to `points`, descending.

    Chosen by brute force over the counts of each weight: the fewest 1-point
    questions, then the fewest 2-point ones, then the most 8-point ones, so a
    domain's share lands on substantial questions. Raises `ValueError` naming
    the slot counts that could carry `points` when `n` cannot.
    """
    if n < 1 or points < 1:
        raise ValueError(f"{points} points over {n} slots: both must be positive")
    counts = _feasible(points, n)
    if counts is None:
        feasible = [k for k in range(1, points + 1) if _feasible(points, k) is not None]
        raise ValueError(
            f"{points} points cannot be split into {n} weights from {_WEIGHTS}; "
            f"feasible slot counts: {_span(feasible)}"
        )
    return [w for w, c in zip(_WEIGHTS, counts, strict=True) for _ in range(c)]


def _span(values: list[int]) -> str:
    """`[4, 5, 6, 7]` → `4-7`; gaps are listed, e.g. `2, 4-7`."""
    if not values:
        return "none"
    runs: list[list[int]] = [[values[0], values[0]]]
    for v in values[1:]:
        if v == runs[-1][1] + 1:
            runs[-1][1] = v
        else:
            runs.append([v, v])
    return ", ".join(f"{a}-{b}" if a != b else str(a) for a, b in runs)


@tool(parse_docstring=True)
@guard
def plan_exam(slots: list[dict[str, Any]]) -> str:
    """Fix the exam's plan: one slot per question, weights allocated here.

    Give every question its curriculum domain and competency (copied verbatim
    from the curriculum facts), its namespace and, when it must cover a recent
    feature, the Kubernetes version. Weights are NOT chosen by you: each
    domain's share of 100 points is split over its slots mechanically, so the
    exam always sums to 100 and every domain with a share has a question.
    Call it once before dispatching authors; calling it again with a changed
    plan rewrites the exam file around what is already staged.

    Args:
        slots: One mapping per question with `question` (1-based number),
            `domain` (a curriculum domain, verbatim), `competency` (one of that
            domain's competencies, verbatim), `topic` (ONE lowercase word for
            the namespace, e.g. "bluegreen": the namespace is composed as
            `<cert>NN-<topic>`, "ckad07-bluegreen", and must not be used by
            another exam), optional `file_graded` (true when the cluster cannot
            host the competency and the question is graded from files in the
            candidate's shell) and `feature_version` (e.g. "1.35" for a
            recent-feature slot).
    """
    target = current()
    curriculum = CURRICULUM[target.certification]
    if len(slots) != target.questions:
        raise ValueError(
            f"the exam has {target.questions} questions; {len(slots)} slots given"
        )
    cat = profile_catalogue(target)
    taken = cat.namespaces() - {
        q.namespace for e in cat.exams if e.id == target.exam for q in e.questions
    }
    planned: dict[int, Slot] = {}
    seen_ns: dict[str, int] = {}
    for raw in slots:
        if not isinstance(raw, dict):
            raise ValueError(f"each slot must be a mapping, not {raw!r}")
        try:
            number = int(raw["question"])
        except (KeyError, TypeError, ValueError):
            raise ValueError(f"slot {raw!r} needs an integer `question`") from None
        if not 1 <= number <= target.questions:
            raise ValueError(f"question {number} is outside 1..{target.questions}")
        if number in planned:
            raise ValueError(f"question {number} is planned twice")
        domain = str(raw.get("domain", "")).strip()
        if domain not in curriculum:
            raise ValueError(
                f"question {number}: domain {domain!r} is not one of: "
                + "; ".join(curriculum)
            )
        competency = str(raw.get("competency", "")).strip()
        if competency not in curriculum[domain][1]:
            raise ValueError(
                f"question {number}: competency {competency!r} is not verbatim one of "
                f"{domain}'s: " + " | ".join(curriculum[domain][1])
            )
        # The namespace is composed here, never trusted: the commander gives
        # the topic word, the tool prefixes the certification and the question
        # number. A full `namespace` is accepted only when it equals that.
        prefix = f"{target.certification.lower()}{number:02d}-"
        topic = str(raw.get("topic", "") or "").strip().lower()
        given = str(raw.get("namespace", "") or "").strip().lower()
        if not topic and given.startswith(prefix):
            topic = given[len(prefix) :]
        if not _TOPIC.match(topic):
            raise ValueError(
                f"question {number}: `topic` must be one lowercase word of letters "
                f"and digits (the namespace becomes {prefix}<topic>), got "
                f"{raw.get('topic', raw.get('namespace'))!r}"
            )
        namespace = prefix + topic
        if given and given != namespace:
            raise ValueError(
                f"question {number}: namespace {given!r} does not match the composed "
                f"{namespace!r}; give `topic` only"
            )
        if namespace in seen_ns:
            raise ValueError(
                f"question {number}: namespace {namespace!r} is also question "
                f"{seen_ns[namespace]}'s"
            )
        if namespace in taken:
            raise ValueError(
                f"question {number}: namespace {namespace!r} belongs to another exam"
            )
        seen_ns[namespace] = number
        planned[number] = Slot(
            question=number,
            domain=domain,
            competency=competency,
            weight=0,
            namespace=namespace,
            file_graded=bool(raw.get("file_graded", False)),
            feature_version=str(raw.get("feature_version", "") or "").strip(),
        )
    missing = sorted(set(range(1, target.questions + 1)) - set(planned))
    if missing:
        raise ValueError(f"questions not planned: {', '.join(map(str, missing))}")
    domains: dict[str, dict[str, int]] = {}
    for domain, (share, _) in curriculum.items():
        numbers = sorted(n for n, s in planned.items() if s.domain == domain)
        if share > 0 and not numbers:
            raise ValueError(
                f"domain {domain!r} has a {share} % share and no slot; every domain "
                "with a share needs at least one question"
            )
        if not numbers:
            continue
        try:
            weights = allocate_weights(share, len(numbers))
        except ValueError as exc:
            raise ValueError(f"domain {domain!r}: {exc}") from None
        for number, weight in zip(numbers, weights, strict=True):
            planned[number].weight = weight
        domains[domain] = {"share": share, "slots": len(numbers), "points": share}
    # A staged question whose namespace moves was proved somewhere else.
    for idx in _staged:
        old = _plan.get(idx)
        if old is not None and old.namespace != planned[idx].namespace:
            _verdicts.pop(idx, None)
    _plan.clear()
    _plan.update(planned)
    if _staged:
        write_exam_file()
    return emit(
        {
            "kind": "plan",
            "slots": [asdict(planned[n]) for n in sorted(planned)],
            "weight_total": sum(s.weight for s in planned.values()),
            "domains": domains,
            "staged": sorted(_staged),
        }
    )


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


def _own_ids(cat: Catalogue, target: Target) -> set[str]:
    """Drill ids this exam already files or stages."""
    own = {q.drill for e in cat.exams if e.id == target.exam for q in e.questions}
    return own | {d.id for d in _staged.values()}


def _foreign_ids(cat: Catalogue, target: Target) -> set[str]:
    """Drill ids owned by other exams; this exam's own staging may reuse its ids."""
    return cat.ids - _own_ids(cat, target)


def _others(cat: Catalogue, target: Target) -> Catalogue:
    """The catalogue without this exam's own drills, for similarity scoring."""
    own = _own_ids(cat, target)
    return Catalogue(drills=[d for d in cat.drills if d.id not in own])


def _parse_strict(drill_yaml: str) -> Drill:
    drill = parse_drill(drill_yaml, "<drill>")
    validate_drill(drill, "<drill>", strict=True)
    return drill


def _exam_title(target: Target) -> str:
    m = _MOCK_NUMBER.search(target.exam)
    number = f" {m.group(1)}" if m else f" ({target.exam})"
    return f"{target.certification} Mock Exam{number}"


def _questions() -> list[Question]:
    return [
        Question(drill=drill.id, weight=_plan[idx].weight, namespace=_plan[idx].namespace)
        for idx, drill in sorted(_staged.items())
    ]


def write_exam_file() -> list[Path]:
    """Assemble the exam from what is staged and write it with its drills.

    A draft while fewer than the target's question count are staged, so kubiq
    lists it; a full exam must have weights summing to 100.
    """
    target = current()
    questions = _questions()
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
        generated_by="swarmr-kubiq",
    )
    return harness.stage_exam(target, exam, list(_staged.values()))


def _staged_digest() -> str:
    h = hashlib.sha256()
    for idx, drill in sorted(_staged.items()):
        slot = _plan[idx]
        h.update(f"{idx}\0{slot.namespace}\0{slot.weight}\0".encode())
        h.update(dump_drill(drill).encode())
    return h.hexdigest()


def _client() -> kubiq_api.KubiqClient:
    return kubiq_api.KubiqClient(current().server)


@contextmanager
def _cluster() -> Iterator[kubiq_api.KubiqClient]:
    """Serialised access to the server; a FatalError seen once is final."""
    global _fatal
    with _cluster_lock:
        if _fatal is not None:
            raise FatalError(_fatal)
        try:
            yield _client()
        except FatalError as exc:
            _fatal = str(exc)
            raise


def _ensure_loaded(client: kubiq_api.KubiqClient) -> bool:
    """Reload kubiq's catalogue when the staged exam differs from what it last loaded."""
    global _loaded_digest
    digest = _staged_digest()
    if digest == _loaded_digest:
        return False
    target = current()
    harness.reclaim_sessions(client, target.exam)
    client.reload()
    entry = next((e for e in client.exams() if e.get("id") == target.exam), None)
    if entry is None:
        raise KubiqError(f"kubiq reloaded but does not list exam {target.exam!r}")
    if entry.get("questions") != len(_staged):
        raise KubiqError(
            f"kubiq reloaded {target.exam!r} with {entry.get('questions')} questions, "
            f"{len(_staged)} are staged"
        )
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


# Every search this run made, by word set, with its payload. A model that
# re-issues the same query wants a different answer; the corpus will not give
# one, so the same hits come back with a note instead of feeding the loop.
# (Shared by every subagent of the run, hence the hits are returned, not
# dropped: a colleague's first search must not come back empty.)
_searched: dict[frozenset[str], dict[str, Any]] = {}


@tool(parse_docstring=True)
@guard
def search_docs(query: str) -> str:
    """Search the indexed kubernetes.io documentation.

    Returns ranked passages with their page URLs; read a whole page with
    read_doc. Use it to ground a recent-feature question ("Kubernetes 1.34
    what's new", "feature gate graduated 1.33") and to copy exact field names
    a check must assert on. Only kubernetes.io is indexed: for Helm, cite the
    helm.sh page the catalogue's Helm drills cite (read_drill one of them).

    Args:
        query: Free text, e.g. "sidecar containers restartPolicy Always".
    """
    words = frozenset(_match_terms(query))
    if words in _searched:
        seen = dict(_searched[words])
        seen["note"] = (
            "already searched this run: these are the same hits. The corpus has "
            "nothing more for these words. Stop searching: write from the pages "
            "you read, or read_drill a catalogue drill on this topic for its docs "
            "link."
        )
        return emit(seen)
    client = _docs()
    hits = client.search(query)
    payload: dict[str, Any] = {"kind": "docs", "query": query, "hits": hits}
    if not hits and client.note:
        payload["note"] = client.note
    if "helm" in words:
        payload["note"] = (
            "helm.sh is not indexed; these hits are kubernetes.io pages that mention "
            "Helm. For the drill's `docs`, use the helm.sh URL a catalogue Helm "
            "drill cites (catalogue_search 'helm', then read_drill)."
        )
    _searched[words] = payload
    return emit(payload)


# Pages read this run, by normalised URL. Re-reading a page — above all one
# that was NOT retrievable — is the same loop as re-searching; answer from
# memory and say so.
_read_pages: dict[str, dict[str, Any]] = {}


# A page body beyond this is a page the model will skim anyway; the summary,
# the outline and the matching section carry what a drill needs.
_DOC_CHARS = 6_000


@tool(parse_docstring=True)
@guard
def read_doc(url: str) -> str:
    """Read one indexed documentation page by the URL a search hit returned.

    Returns the page summary and outline, plus the body of the section the
    URL's `#anchor` names (or the start of the page without one), capped at
    6 kB. Read the section you need, not the page.

    Args:
        url: The page URL exactly as search_docs returned it, `#section` kept.
    """
    clean = url.strip().rstrip("/")
    key, _, anchor = clean.partition("#")
    memo = f"{key}#{anchor}"
    if memo in _read_pages:
        seen = dict(_read_pages[memo])
        seen["note"] = "already read this run; this is the same answer. " + (
            "The page is not retrievable: rule or write from the search snippets "
            "and the drill, and say the page body was unavailable."
            if not seen.get("text")
            else "Work from this text; do not read it again."
        )
        return emit(seen)
    client = _docs()
    text = client.page(
        key, fields=("summary", "headings", "body"), section=anchor or None
    )
    if len(text) > _DOC_CHARS:
        text = text[:_DOC_CHARS] + "\n… (cut; name a #section of this page to read more)"
    payload: dict[str, Any] = {"kind": "doc", "url": url, "text": text}
    if not text and client.note:
        payload["note"] = client.note
    _read_pages[memo] = payload
    return emit(payload)


def _sibling_scores(drill: Drill, idx: int | None) -> list[tuple[int, str, float, str]]:
    """Similarity of `drill` to every staged question other than `idx`:
    (question, id, score, why), best first."""
    siblings = {
        other.id: other_idx
        for other_idx, other in _staged.items()
        if other_idx != idx and other.id != drill.id
    }
    ranked = similarity.nearest(
        drill, Catalogue(drills=[_staged[i] for i in siblings.values()]), n=len(siblings)
    )
    return [(siblings[other], other, value, reason) for other, value, reason in ranked]


def _nearest(drill: Drill, idx: int, cat: Catalogue, target: Target) -> list[dict]:
    """The three closest drills (catalogue and staged siblings) with scores,
    for the author's SIMILARITY line; the refusal itself is `_refuse_duplicates`."""
    matches = [
        {"id": other, "score": round(value, 3), "reason": reason}
        for other, value, reason in similarity.nearest(drill, _others(cat, target), n=3)
    ]
    matches += [
        {"id": f"staged q{other_idx} {other_id}", "score": round(value, 3), "reason": r}
        for other_idx, other_id, value, r in _sibling_scores(drill, idx)[:3]
    ]
    matches.sort(key=lambda m: (-m["score"], m["id"]))
    return matches[:3]


def _refuse_duplicates(drill: Drill, idx: int, cat: Catalogue, target: Target) -> None:
    """Refuse a near-copy of a staged sibling or of another exam's drill."""
    for other_idx, other_id, value, reason in _sibling_scores(drill, idx):
        if similarity.is_duplicate(value):
            raise ValueError(
                f"drill {drill.id!r} is the same question as staged question "
                f"{other_idx} ({other_id!r}): similarity {value:.2f} ≥ "
                f"{similarity.THRESHOLD} ({reason}); redesign around a different "
                "mechanic"
            )
    ranked = similarity.nearest(drill, _others(cat, target), n=1)
    if ranked and similarity.is_duplicate(ranked[0][1]):
        other_id, value, reason = ranked[0]
        raise ValueError(
            f"drill {drill.id!r} is the same question as catalogue drill "
            f"{other_id!r}: similarity {value:.2f} ≥ {similarity.THRESHOLD} "
            f"({reason}); redesign around a different mechanic"
        )


# kubectl verbs that need no namespace: the check is not about this question's
# objects.
_UNSCOPED_KUBECTL = re.compile(
    r"kubectl\s+(get|describe)\s+(no|node|nodes|ns|namespace|namespaces|"
    r"pv|persistentvolumes?|sc|storageclass(es)?|crd|customresourcedefinitions?|"
    r"clusterrole(s|bindings?)?|ingressclass(es)?)\b"
    r"|kubectl\s+(config|api-resources|api-versions|version|auth)\b"
)


# (apiVersion, Kind) the target cluster serves; read once per run.
_served: set[tuple[str, str]] | None = None


def _refuse_unserved_kinds(drill: Drill) -> None:
    """A kind the cluster does not serve can never be proven on it, however
    correct the drill: refuse before any session, naming what is missing."""
    global _served
    if _served is None:
        _served = harness.served_kinds()
    if not _served:
        return  # kubectl could not answer; the proof will say
    # `files` are candidate input and may be deliberately broken (a manifest
    # on a removed apiVersion the candidate must migrate); only what the
    # setup, the solution and the checks apply or read must be served.
    missing = sorted(
        f"{version} {kind}"
        for version, kind in (
            replace(drill, files={}).manifest_kinds() - drill.declared_kinds()
        )
        if (version, kind) not in _served
    )
    if missing:
        raise ValueError(
            f"the target cluster does not serve {', '.join(missing)} (kubectl "
            "api-resources); a proof there can never pass. Use an API this cluster "
            "serves, or a setup that installs the CRD first"
        )


def _refuse_unscoped_exec(drill: Drill, namespace: str) -> None:
    """A `shell: true` exec check runs kubectl in the candidate's shell with
    whatever namespace the candidate's context holds; kubiq injects nothing.
    The catalogue pins `-n <namespace>` on every such kubectl, and so must a
    drill the team writes, or a candidate who presses Check from another
    namespace fails for a reason that is not theirs."""
    for i, c in enumerate(drill.checks):
        if c.exec is None or not c.exec.get("shell"):
            continue
        command = " ".join(str(p) for p in c.exec.get("command") or [])
        if "kubectl" not in command or namespace in command:
            continue
        scoped = _UNSCOPED_KUBECTL.sub("", command)
        if "kubectl" in scoped:
            raise ValueError(
                f"checks[{i}] ({c.name!r}) runs kubectl in the candidate's shell "
                f"without `-n {namespace}`; the shell's current namespace is "
                "whatever the candidate left it at, so every kubectl in a shell "
                "exec check must name this question's namespace"
            )


# Staging mutates `_staged`, `_plan` and the exam file; four authors stage at
# once, so the whole operation is one critical section.
_staging_lock = threading.Lock()


@tool(parse_docstring=True)
@guard
def stage_drill(drill_yaml: str, question_index: int) -> str:
    """Validate a drill strictly and write it into the exam under construction.

    The drill is written into the checkout and merged into the exam file at its
    question number; weight and namespace come from the plan. Refused, in
    order: a drill kubiq or this team's rules would not accept; an id staged
    at another question or owned by another exam; the same id with a changed
    task text (the task is frozen at first staging — change solution, checks
    or setup, or stage a new id); a near-copy of a staged sibling or of the
    catalogue; a setup the API server rejects; anything kubiq's own loader
    refuses. A refusal leaves the checkout as it was. A verdict recorded for
    the question is dropped when the drill changed (`proof_stale`).

    Args:
        drill_yaml: The complete drill as YAML text.
        question_index: The question number, 1-based, as the commander assigned.
    """
    with _staging_lock:
        return _stage_drill(drill_yaml, question_index)


def _stage_drill(drill_yaml: str, question_index: int) -> str:
    idx = int(question_index)
    if idx < 1:
        raise ValueError("question_index must be 1 or more")
    slot = _plan.get(idx)
    if slot is None:
        raise ValueError(
            f"no plan for question {idx}; the commander must call plan_exam first"
        )
    drill = _parse_strict(drill_yaml)
    target = current()
    for other, staged in _staged.items():
        if other != idx and staged.id == drill.id:
            raise ValueError(f"drill {drill.id!r} is already staged as question {other}")
    cat = profile_catalogue(target)
    if drill.id in _foreign_ids(cat, target):
        raise ValueError(f"drill id {drill.id!r} already exists in the catalogue")
    baseline = _task_at_staging.get(idx)
    replaced = baseline is not None and baseline[0] != drill.id
    if baseline is not None and not replaced and baseline[1] != drill.task:
        raise ValueError(
            f"the task text of {drill.id!r} is frozen since first staging; change "
            "solution, checks or setup, or stage a new drill id"
        )
    _refuse_duplicates(drill, idx, cat, target)
    namespace = slot.namespace or "default"
    _refuse_unscoped_exec(drill, namespace)
    _refuse_unserved_kinds(drill)
    if problem := harness.dry_run_setup(drill):
        raise ValueError(
            "the API server rejects this drill's setup (server dry-run); fix the "
            f"field or value it names, from the docs page you cited:\n{problem}"
        )

    previous = _staged.get(idx)
    _staged[idx] = drill
    try:
        written = write_exam_file()
        with _cluster() as client:
            problem = client.check()
        if problem:
            raise ValueError(
                "kubiq itself refuses to load the staged files (this is the server's "
                f"own loader, the final word); fix what it names:\n{problem}"
            )
    except Exception:
        if previous is None:
            del _staged[idx]
        else:
            _staged[idx] = previous
        # Put the checkout back to what kubiq last accepted.
        if _staged:
            write_exam_file()
        else:
            target.exam_file.unlink(missing_ok=True)
            for stale in target.drill_dir.glob("q*.yaml"):
                stale.unlink()
        raise
    if baseline is None or replaced:
        _task_at_staging[idx] = (drill.id, drill.task)
    key = proofs.proof_key(drill, slot.namespace)
    stale = previous is not None and proofs.proof_key(previous, slot.namespace) != key
    if stale:
        _verdicts.pop(idx, None)
    path = target.drill_dir / f"q{_api_index(idx) + 1:02d}-{drill.id}.yaml"
    if path not in written or not path.is_file():
        raise RuntimeError(f"staging wrote {written} but not the expected {path}")
    return emit(
        {
            "kind": "staged",
            "question": idx,
            "id": drill.id,
            "namespace": slot.namespace,
            "weight": slot.weight,
            "path": str(path),
            "exam_file": str(target.exam_file),
            "staged_questions": len(_staged),
            "planned_questions": target.questions,
            "weight_total": sum(_plan[i].weight for i in _staged),
            "replaced": replaced,
            "proof_stale": stale,
            "nearest": _nearest(drill, idx, cat, target),
        }
    )


def _summary(payload: dict[str, Any]) -> dict[str, Any]:
    """What the commander needs to route a verdict; the full record stays in
    `_verdicts` for grader_details. Keeps 21 verdicts at ~21 lines."""
    failing = [r for r in payload.get("results") or [] if not r.get("ok")]
    permissive = [r["name"] for r in payload.get("results_before") or [] if r.get("ok")]
    out: dict[str, Any] = {
        "question": payload["question"],
        "drill": payload["drill"],
        "proven": payload["proven"],
    }
    if not payload["proven"]:
        out["why"] = (
            "negative control passed untouched"
            if not payload["negative_control_ok"]
            else "checks pass on the untouched namespace: "
            + ", ".join(permissive)
            + " — each grades nothing; fold what it guards into a check that "
            "also asserts something only the solution produces, or drop it"
            if permissive and not failing
            else f"too slow: {payload['solve_seconds']:g}s from solution start to a "
            f"passing grade, budget {harness.solve_budget():g}s — redesign the "
            "question so its effect lands fast; do not add waits"
            if payload.get("too_slow")
            else "checks failed"
            if failing
            else "leftovers after teardown"
            if payload["leftovers"]
            else "see grader_details"
        )
        if failing:
            out["failing"] = [
                {"check": r["name"], "detail": r["detail"]} for r in failing
            ]
        if payload.get("exit_code"):
            out["solution_exit"] = payload["exit_code"]
            out["stderr"] = payload["stderr"][-600:]
        if payload["leftovers"]:
            out["leftovers"] = payload["leftovers"]
    if permissive:
        out["passes_untouched"] = permissive
    return out


def _prove(idxs: list[int], skipped: list[int]) -> str:
    """Prove the given staged questions in one session and record every verdict.

    Caller has validated `idxs` against `_staged`."""
    target = current()
    rows: list[dict[str, Any]] = []
    if idxs:
        with _cluster() as client:
            reloaded = _ensure_loaded(client)
            exam = Exam(
                id=target.exam,
                title=_exam_title(target),
                certification=target.certification,
                description="",
                duration_minutes=_DURATION_MINUTES,
                pass_percent=_PASS_PERCENT[target.certification],
                questions=_questions(),
                draft=True,
            )
            verdicts = harness.prove_exam(
                client,
                target,
                exam,
                list(_staged.values()),
                indexes=[_api_index(i) for i in idxs],
            )
        for idx, verdict in zip(idxs, verdicts, strict=True):
            payload = verdict.to_json()
            payload["question"] = idx
            payload["server_reloaded"] = reloaded
            _verdicts[idx] = payload
            proofs.save_proof(
                target, proofs.proof_key(_staged[idx], _plan[idx].namespace), payload
            )
            rows.append(_summary(payload))
    return emit(
        {
            "kind": "proof_batch",
            "proven": [r["question"] for r in rows if r["proven"]],
            "failed": [r["question"] for r in rows if not r["proven"]],
            "skipped": skipped,
            "questions": rows,
        }
    )


@tool(parse_docstring=True)
@guard
def prove_all() -> str:
    """Prove every staged question that has no proof yet, in ONE kubiq session.

    One provision, one teardown: the negative control and the drill's own
    solution run for each question in order, then the exam is ended and
    graded once. Questions already proven (this run, or an earlier run of
    the same files) are skipped. Returns one line per question: proven, or
    why not (the failing checks with the grader's detail, the solution's exit
    code and stderr). Takes several minutes; call it once.
    """
    if not _staged:
        raise ValueError("nothing is staged")
    todo = [i for i in sorted(_staged) if not _verdicts.get(i, {}).get("proven")]
    skipped = [i for i in sorted(_staged) if i not in todo]
    return _prove(todo, skipped)


# Proofs taken per (question, drill id) this run. A drill that has not passed
# in this many proofs is redesigned (REPLACE: a new id, a fresh budget), not
# proved a seventh time; the prompt said so and the tool now holds the line.
PROOF_BUDGET = 3
_proofs_taken: dict[tuple[int, str], int] = {}


@tool(parse_docstring=True)
@guard
def prove_questions(question_indexes: list[int]) -> str:
    """Prove the given staged questions in ONE kubiq session, proven or not.

    Stage, then prove exactly the questions you changed; the verdict replaces
    the one on record. A drill id may be proved at most 3 times at a question
    in one run; after that the call is refused and the question is reported
    FAILED — a replacement with a new drill id gets its own budget.

    Args:
        question_indexes: The question numbers, 1-based.
    """
    idxs = sorted({_question(n) for n in question_indexes})
    if not idxs:
        raise ValueError("no questions given")
    spent = [
        (i, _staged[i].id)
        for i in idxs
        if _proofs_taken.get((i, _staged[i].id), 0) >= PROOF_BUDGET
    ]
    if spent:
        names = ", ".join(f"question {i} ({d!r})" for i, d in spent)
        raise ValueError(
            f"proof budget exhausted for {names}: {PROOF_BUDGET} proofs taken this "
            "run. Report RESULT: FAILED with the last grader lines; a new drill id "
            "(REPLACE) gets a fresh budget"
        )
    for i in idxs:
        key = (i, _staged[i].id)
        _proofs_taken[key] = _proofs_taken.get(key, 0) + 1
    return _prove(idxs, [])


@tool(parse_docstring=True)
@guard
def grader_details(question_index: int) -> str:
    """Return the last verdict recorded for a question, verbatim.

    Every check's name, ok flag and detail: `results_before` is the grade on
    the untouched namespace (every check must be ok=false), `results` the
    grade after the drill's own solution (every check must be ok=true), and
    `solve_seconds` how long the solution took to land.

    Args:
        question_index: The question number, 1-based.
    """
    idx = _question(question_index)
    verdict = _verdicts.get(idx)
    if verdict is None:
        raise ValueError(f"question {idx} has not been proved yet")
    return emit(verdict)


RULINGS = ("confirmed", "refuted", "unproven")


@tool(parse_docstring=True)
@guard
def record_rulings(rulings: dict[str, str]) -> str:
    """Record the critic's ruling per question on the proof record.

    The ruling is stored on the question's persisted verdict, under the same
    key as the proof (the drill text and namespace), so it survives a rerun
    exactly as long as the proof does and dies with any edit to the drill.
    finalize_exam files only questions whose record says `confirmed`; a rerun
    on unchanged questions therefore needs no critic at all.

    Args:
        rulings: Question number (1-based, as a string key) to ruling, one of
            `confirmed`, `refuted`, `unproven` — exactly as the critic ruled.
    """
    if not rulings:
        raise ValueError("no rulings given")
    target = current()
    recorded: dict[int, str] = {}
    for raw_idx, raw_ruling in rulings.items():
        idx = _question(int(raw_idx))
        ruling = str(raw_ruling).strip().lower()
        if ruling not in RULINGS:
            raise ValueError(
                f"question {idx}: ruling {raw_ruling!r} is not one of {RULINGS}"
            )
        verdict = _verdicts.get(idx)
        if verdict is None or not verdict.get("proven"):
            raise ValueError(
                f"question {idx} has no passing proof on record; a ruling needs a "
                "proof to rule on"
            )
        verdict["ruling"] = ruling
        key = proofs.proof_key(_staged[idx], _plan[idx].namespace)
        proofs.save_proof(target, key, verdict)
        recorded[idx] = ruling
    return emit(
        {
            "kind": "rulings",
            "recorded": {str(i): r for i, r in sorted(recorded.items())},
            "confirmed": sorted(i for i, r in recorded.items() if r == "confirmed"),
        }
    )


@tool(parse_docstring=True)
@guard
def finalize_exam(confirmed_questions: list[int], summary: str) -> str:
    """Write the final exam with only the confirmed questions. Nothing is committed.

    Questions not in `confirmed_questions` are removed from the exam first, so
    nothing the critic refuted or left unproven is filed; the exam is written
    as a draft when fewer than the planned number remain. Git is never
    touched: the operator reviews the files and opens the PR. Returns the
    paths written and the suggested branch, commit message and PR body.

    Args:
        confirmed_questions: Question numbers (1-based) the critic confirmed.
        summary: One paragraph for the PR body: what the exam covers and which
            questions exercise recent Kubernetes features.
    """
    keep = {_question(n) for n in confirmed_questions}
    if not keep:
        raise ValueError("no confirmed questions: nothing to file")
    unrecorded = sorted(
        i for i in keep if (_verdicts.get(i) or {}).get("ruling") != "confirmed"
    )
    if unrecorded:
        raise ValueError(
            f"questions {unrecorded} have no `confirmed` ruling on record; call "
            "record_rulings with the critic's rulings first (a filed question is "
            "one the record says was proven AND confirmed)"
        )
    dropped = sorted(set(_staged) - keep)
    for idx in dropped:
        del _staged[idx]
        _task_at_staging.pop(idx, None)
    target = current()
    written = write_exam_file()
    body = (
        f"{summary.strip()}\n\nEvery question was proven on a live kubiq session: "
        "the grader fails on the untouched namespace, passes after the drill's "
        "own solution, and the session tears down without leftovers."
    )
    return emit(
        {
            "kind": "finalized",
            "exam": target.exam,
            "filed": sorted(keep),
            "dropped": dropped,
            "files": [str(p.relative_to(target.kubiq)) for p in written],
            "suggested_branch": pr.branch_name(target.exam),
            "suggested_commit": pr.commit_message(target.exam, len(keep)),
            "pr_body": body,
        }
    )


# Tool sets per role. The author designs, stages and proves its own fix rounds;
# the commander plans, proves the whole exam once and files; the critic reads
# verdicts, drills and docs — it holds no cluster tool, because the harness
# already recorded the untouched grade it would otherwise take itself.
AUTHOR_TOOLS = [
    catalogue_search,
    read_drill,
    search_docs,
    read_doc,
    stage_drill,
    prove_questions,
    grader_details,
]
CRITIC_TOOLS = [
    grader_details,
    read_drill,
    catalogue_search,
    search_docs,
    read_doc,
]
# The commander holds no authoring tools: its context stays clean, and it
# cannot fabricate an observation it never received. It finalises the files
# and files the report; both are records of what the specialists returned.
COMMANDER_TOOLS = [
    plan_exam,
    prove_all,
    prove_questions,
    grader_details,
    record_rulings,
    finalize_exam,
    FILE_REPORT_TOOL,
]
