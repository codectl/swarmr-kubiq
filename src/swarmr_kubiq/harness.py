"""Proving a drill on a live cluster: stage it, solve it, grade it, leave nothing.

One responsibility: the sequence that turns "the author thinks this works"
into evidence. A question is proven only when all of these hold in one fresh
session: the checks FAIL before anything is done (a check that passes on an
empty namespace grades nothing), the drill's own `solution` run in the
candidate shell makes them PASS, and the cluster is as it was once the session
is finished and deleted (no namespace, CRD, PV, ClusterRole or binding left).

kubiq reads exams and drills from the bind-mounted checkout on `POST
/api/reload`, so `stage_exam` writes the candidate files into the checkout and
the caller reloads the server (`KubiqClient.reload`) before proving. Only one
session may exist at a time and this harness never deletes one it did not
create. Before a solution runs, `settle_setup` waits for the question's setup
Pods to stop being created, so a grade never races kubiq's own provisioning.

`_run` and `_sleep` are the only paths to a subprocess or a clock; tests
replace them.
"""

from __future__ import annotations

import json
import os
import re
import sys
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
from swarmr_kubiq.kubiq_api import FatalError, KubiqError
from swarmr_kubiq.target import Target

__all__ = [
    "SETTLE_MAX",
    "SETTLE_POLL",
    "SETUP_SETTLE_MAX",
    "TEARDOWN_KINDS",
    "Verdict",
    "prove_exam",
    "reclaim_sessions",
    "settle_cap",
    "settle_setup",
    "solution_script",
    "solve_budget",
    "stage_exam",
]

_run = kubiq_api.run
_sleep = time.sleep


def _progress(message: str) -> None:
    """One line on stderr per step: a proof runs minutes inside one tool call
    and the operator must see it move. stderr so it never enters a tool result."""
    print(f"  harness: {message}", file=sys.stderr, flush=True)


# A solution's effects land asynchronously (a PV rebinding, a rollout, a Job
# completing); grading is polled until every check passes or this cap elapses.
SETTLE_MAX = 90.0
SETTLE_POLL = 3.0
# A question's solution plus grader convergence must fit this budget. The real
# exam gives ~6 minutes per question INCLUDING reading and typing; a reference
# solution that needs more than this to take effect (a controller to install,
# a crash loop to mature, a cert to mint) is a slow question, and a slow
# question is refused as unproven rather than waited for. KUBIQ_SOLVE_BUDGET.
SOLVE_BUDGET = 120.0
# Where `dry_run_setup` validates setup manifests. Persistent on purpose.
DRY_RUN_NAMESPACE = "swarmr-kubiq-dryrun"
# kubiq applies a question's `setup` while the session provisions; a solution
# run while those Pods are still being created grades against a half-built
# namespace. Wait, bounded, until every Pod has stopped creating.
SETUP_SETTLE_MAX = 60.0
_CREATING_REASONS = frozenset({"ContainerCreating", "PodInitializing"})
_STUCK_REASONS = frozenset(
    {
        "ImagePullBackOff",
        "ErrImagePull",
        "CrashLoopBackOff",
        "CreateContainerConfigError",
        "CreateContainerError",
        "InvalidImageName",
        "RunContainerError",
    }
)

# Cluster-scoped kinds an exam may create and kubiq's teardown must remove.
TEARDOWN_KINDS = ("namespaces", "crds", "pv", "clusterrole", "clusterrolebinding")
_TEARDOWN_WAIT = 120.0
_TEARDOWN_POLL = 3.0

_HEREDOC = re.compile(r"(?<!<)<<(?!<)-?\s*['\"]?(?P<tag>[A-Za-z_][A-Za-z0-9_]*)['\"]?")
_COMMENT = re.compile(r"^\s*#")


class SessionClient(Protocol):
    """What `prove_exam` needs from a `KubiqClient`; tests pass a fake."""

    def sessions(self) -> list[dict[str, Any]]: ...
    def create_session(
        self, exam_id: str, questions: list[int] | None = None
    ) -> dict[str, Any]: ...
    def wait_ready(self, sid: str, timeout: float = ...) -> dict[str, Any]: ...
    def grade(self, sid: str, question: int) -> dict[str, Any]: ...
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
    solve_seconds: float = 0.0
    setup_settle_seconds: float = 0.0

    @property
    def too_slow(self) -> bool:
        return self.solve_seconds > solve_budget()

    @property
    def proven(self) -> bool:
        return (
            self.passed
            and self.negative_control_ok
            and not self.leftovers
            and not self.too_slow
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "kind": "verdict",
            "proven": self.proven,
            "too_slow": self.too_slow,
            **asdict(self),
        }


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


def dry_run_setup(drill: Drill) -> str:
    """Validate the drill's `setup` against the live API server without creating anything.

    `kubectl apply --dry-run=server` runs the same schema and enum validation an
    exam session would hit when kubiq applies the setup, so a wrong field name
    or enum value (`resizePolicy: RestartNotRequired`) is caught at staging —
    before any session or fix round. Namespaced objects are validated in the
    persistent `DRY_RUN_NAMESPACE`. Returns "" when valid, else kubectl's message.
    """
    if not drill.setup or not drill.setup.strip():
        return ""
    # One scratch namespace for every dry-run, created once and never deleted:
    # a dry-run creates nothing in it, and deleting the question's own
    # namespace here left it terminating, unlabelled, under the next session's
    # feet ("already exists and is not managed by kubiq").
    _run(["kubectl", "create", "namespace", DRY_RUN_NAMESPACE], timeout=30)
    rc, out, err = _run(
        ["kubectl", "apply", "-n", DRY_RUN_NAMESPACE, "--dry-run=server", "-f", "-"],
        timeout=60,
        stdin=drill.setup,
    )
    if rc == 0:
        return ""
    return (err or out).strip()[-1500:]


def served_kinds() -> set[tuple[str, str]]:
    """Every (apiVersion, Kind) the target cluster serves, from `kubectl api-resources`.

    A drill that applies or grades a kind the cluster does not serve (an alpha
    API, a beta behind a gate, a CRD nobody installed) can never be proven on
    it; staging refuses it against this list, whatever the cluster. Empty when
    kubectl cannot answer, and the gate is then skipped.
    """
    rc, out, _ = _run(["kubectl", "api-resources", "--no-headers"], timeout=60)
    if rc != 0:
        return set()
    served: set[tuple[str, str]] = set()
    for line in out.splitlines():
        cols = line.split()
        # NAME [SHORTNAMES] APIVERSION NAMESPACED KIND
        if len(cols) >= 4:
            served.add((cols[-3], cols[-1]))
    return served


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


def solve_budget() -> float:
    """Seconds a solution plus convergence may take; KUBIQ_SOLVE_BUDGET, default 120."""
    return float(os.environ.get("KUBIQ_SOLVE_BUDGET", SOLVE_BUDGET))


def settle_cap() -> float:
    """Seconds to wait for checks to converge; KUBIQ_SETTLE_SECONDS, default 90."""
    return float(os.environ.get("KUBIQ_SETTLE_SECONDS", SETTLE_MAX))


def prove_exam(
    client: SessionClient,
    target: Target,
    exam: Exam,
    drills: list[Drill],
    indexes: list[int] | None = None,
) -> list[Verdict]:
    """Prove questions of the exam in ONE session: one provision, one teardown.

    `indexes` are 0-based positions in the exam file, what kubiq's `?question=N`
    expects; `None` proves every question. Only those get a negative control,
    a solution and a grade, and only they get a verdict, in index order. The
    teardown proof covers the whole session, so a leftover is reported on every
    verdict returned.
    """
    by_id = {d.id: d for d in drills}
    missing = [q.drill for q in exam.questions if q.drill not in by_id]
    if missing:
        raise DrillError(f"exam lists drills not supplied: {', '.join(missing)}")
    total = len(exam.questions)
    wanted = list(range(total)) if indexes is None else sorted(set(indexes))
    out_of_range = [i for i in wanted if not 0 <= i < total]
    if out_of_range:
        raise DrillError(
            f"question indexes {out_of_range} do not exist in a {total}-question exam"
        )
    reclaim_sessions(client, target.exam)
    before = _snapshot()
    subset = None if indexes is None else wanted
    sid = str(client.create_session(target.exam, subset)["id"])
    _progress(
        f"session {sid}: provisioning {len(wanted)} of {total} questions, proving "
        f"{len(wanted)}"
    )
    negatives: dict[int, dict[str, Any]] = {}
    runs: dict[int, tuple[int, str, str, float, dict[str, Any], float]] = {}
    try:
        client.wait_ready(sid)
        _progress("session ready; solving")
        for index in wanted:
            question = exam.questions[index]
            negatives[index] = client.grade(sid, index)
            runs[index] = _solve(
                client, sid, index, by_id[question.drill], question.namespace or "default"
            )
            rc, _, _, settle, grade, _ = runs[index]
            _progress(
                f"q{index + 1}/{total} {question.drill}: negative control "
                f"{_verdict_word(negatives[index])}, solution exit {rc}, grade "
                f"{'PASS' if grade.get('passed') else 'FAIL'}, "
                f"{grade.get('solve_seconds', 0):g}s end to end"
            )
        _progress("ending the exam")
        client.finish(sid)
    finally:
        client.delete(sid)
    _progress("teardown: waiting for the cluster to return to baseline")
    leftovers = _leftovers(before)
    # The verdict is the grade taken right after each solution, with the shell
    # still in that question's namespace — what a candidate pressing Check
    # sees. `finish` grades every question at once with the shell left in the
    # LAST namespace, so an exec check that relies on the current context (as
    # a candidate's would) fails there for reasons that are not the drill's.
    verdicts: list[Verdict] = []
    for index in wanted:
        question = exam.questions[index]
        rc, out, err, settle, grade, setup_settle = runs[index]
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
                solve_seconds=float(grade.get("solve_seconds", 0.0)),
                setup_settle_seconds=setup_settle,
            )
        )
    return verdicts


def _verdict_word(report: dict[str, Any]) -> str:
    """How a negative control reads: it must FAIL, so 'failed as required'."""
    return "passed (TOO PERMISSIVE)" if report.get("passed") else "failed as required"


def reclaim_sessions(client: SessionClient, exam_id: str) -> list[str]:
    """Free the one session slot for a proof. Returns the ids it deleted.

    A session on the exam being authored is this harness's by construction: the
    exam is a draft while it is written and kubiq lets nobody start a draft, so
    only a run of this team could have opened it — typically one a killed run
    left behind. It is deleted. A session on any other exam is a person's, and
    the harness stops hard: `FatalError` tells the tester there is nothing to
    retry.
    """
    reclaimed: list[str] = []
    for s in client.sessions():
        sid = str(s.get("id", "?"))
        if s.get("exam_id") == exam_id:
            client.delete(sid)
            reclaimed.append(sid)
        else:
            raise FatalError(
                f"session {sid} on exam {s.get('exam_id')!r} is open and kubiq allows "
                "one at a time. It is not this run's, so it will not be ended; "
                "proofs cannot run until it is finished. Do not retry."
            )
    return reclaimed


def settle_setup(namespace: str, timeout: float = SETUP_SETTLE_MAX) -> float:
    """Wait until every Pod kubiq's setup created in `namespace` has settled.

    Settled means the Pod has stopped being created: it Succeeded or Failed; it
    is Running with no container still `ContainerCreating`/`PodInitializing`;
    or it is Pending for a reason that will not change by waiting — it is
    unschedulable (a deliberate trap, `PodScheduled: False`) or a container is
    in a pull/start error loop. Polls `kubectl get pods -o json` through `_run`
    every `SETTLE_POLL` seconds up to `timeout`; a namespace with no Pods, or a
    kubectl that fails, counts as settled. Returns the seconds waited, capped
    at `timeout`; never raises.
    """
    started = time.monotonic()
    while True:
        rc, out, _ = _run(
            ["kubectl", "get", "pods", "-n", namespace, "-o", "json"], timeout=30
        )
        if rc != 0:
            break
        try:
            pods = json.loads(out).get("items") or []
        except (ValueError, AttributeError):
            break
        if all(_pod_settled(pod) for pod in pods):
            break
        if time.monotonic() - started >= timeout:
            return timeout
        _sleep(SETTLE_POLL)
    return min(round(time.monotonic() - started, 1), timeout)


def _pod_settled(pod: dict[str, Any]) -> bool:
    status = pod.get("status") or {}
    phase = status.get("phase")
    if phase in ("Succeeded", "Failed"):
        return True
    waiting = {
        reason
        for key in ("initContainerStatuses", "containerStatuses")
        for container in status.get(key) or []
        if (reason := ((container.get("state") or {}).get("waiting") or {}).get("reason"))
    }
    if phase == "Running":
        return not waiting & _CREATING_REASONS
    if phase == "Pending":
        unschedulable = any(
            c.get("type") == "PodScheduled" and c.get("status") == "False"
            for c in status.get("conditions") or []
        )
        return unschedulable or bool(waiting & _STUCK_REASONS)
    return False


def _solve(
    client: SessionClient, sid: str, question_index: int, drill: Drill, namespace: str
) -> tuple[int, str, str, float, dict[str, Any], float]:
    """Wait for the setup to settle, run the solution, then grade repeatedly
    until it passes or the cap elapses.

    Returns the shell result, the seconds spent converging, the last grade and
    the seconds the setup took to settle (not counted against the solution).
    """
    setup_settle = settle_setup(namespace)
    begun = time.monotonic()
    # The solution may not run longer than the whole solve budget: a reference
    # solution that blocks in `kubectl wait` on a Pod that never comes up is a
    # slow question, and 400 s of silence per such question is what it cost.
    rc, out, err = client.shell_exec(
        sid, solution_script(drill.solution), namespace, timeout=solve_budget()
    )
    if rc == 124:
        err = f"{err}\nsolution killed after {solve_budget():g}s (KUBIQ_SOLVE_BUDGET)"
    # A solution that exited non-zero produced a state nobody designed; the
    # grade is taken once for the record, not polled — 90 s of waiting on a
    # broken reference solution was the single largest cost of a failed proof.
    cap = settle_cap() if rc == 0 else 0.0
    started = time.monotonic()
    while True:
        report = client.grade(sid, question_index)
        elapsed = time.monotonic() - started
        if report.get("passed") or elapsed >= cap:
            report["solve_seconds"] = round(time.monotonic() - begun, 1)
            return rc, out, err, round(elapsed, 1), report, setup_settle
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
