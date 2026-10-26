"""The proving sequence, against a fake kubiq and a fake kubectl.

No cluster, no server: `KubiqClient` is duck-typed by `FakeKubiq`, which
records every call so the test can assert the order and the arguments the
harness used, and `harness._run` is replaced by a scripted `kubectl get`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from swarmr_kubiq import harness
from swarmr_kubiq.digest import digest_result
from swarmr_kubiq.drill import (
    Check,
    Drill,
    DrillError,
    Exam,
    Question,
    load_drill,
    load_exam,
)
from swarmr_kubiq.kubiq_api import FatalError
from swarmr_kubiq.target import Target

SOLUTION_WITH_HEREDOC = """\
# create the pod from a manifest
kubectl apply -f - <<EOF
apiVersion: v1
kind: Pod
metadata:
  name: web
  # this hash is YAML content, not a shell comment
  labels: { app: web }
spec:
  containers:
    - name: nginx
      image: nginx:alpine
EOF
  # indented comment goes too
kubectl get pod web   # trailing comment stays
"""


def _drill(
    id: str = "pod-basics", solution: str = "kubectl run web --image=nginx"
) -> Drill:
    return Drill(
        id=id,
        order=701,
        exam="CKAD",
        title="Run a Pod",
        topic="Application Design and Build",
        task="Create a Pod named `web` running `nginx:alpine`.",
        solution=solution,
        checks=[
            Check(
                name="web runs nginx",
                resource={"kind": "Pod", "name": "web"},
                assert_=[{"path": "spec.containers[0].image", "contains": "nginx"}],
            )
        ],
    )


def _exam(questions: list[Question], exam_id: str = "ckad-mock-7") -> Exam:
    return Exam(
        id=exam_id,
        title="CKAD Mock Exam 7",
        certification="CKAD",
        description="test exam",
        duration_minutes=120,
        pass_percent=75,
        questions=questions,
    )


def _target(tmp_path: Path, questions: int = 2) -> Target:
    return Target(
        kubiq=tmp_path,
        exam="ckad-mock-7",
        certification="CKAD",
        server="http://localhost:8080",
        questions=questions,
    )


def _prove_one(
    kubiq: FakeKubiq, tmp_path: Path, drill: Drill | None = None, namespace: str = "ns0"
) -> harness.Verdict:
    """Prove a one-question exam; the single verdict."""
    drill = drill or _drill()
    exam = _exam([Question(drill.id, 100, namespace)])
    return harness.prove_exam(kubiq, _target(tmp_path, 1), exam, [drill])[0]


class FakeKubiq:
    """A kubiq that grades by whether the solution has been run."""

    def __init__(
        self,
        *,
        leftover: list[dict[str, Any]] | None = None,
        passes_unsolved: bool = False,
        fails_solved: bool = False,
    ) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.leftover = leftover or []
        self.passes_unsolved = passes_unsolved
        self.fails_solved = fails_solved
        self.solved: set[int] = set()
        self.last_checked = -1
        self.scripts: list[tuple[str, str | None]] = []
        self.alive: set[str] = set()

    def sessions(self) -> list[dict[str, Any]]:
        self.calls.append(("sessions", None))
        return list(self.leftover)

    def create_session(
        self, exam_id: str, questions: list[int] | None = None
    ) -> dict[str, Any]:
        self.calls.append(("create", (exam_id, questions)))
        self.alive.add("abc12345")
        return {"id": "abc12345", "exam_id": exam_id, "state": "provisioning"}

    def wait_ready(self, sid: str, timeout: float = 300) -> dict[str, Any]:
        self.calls.append(("wait_ready", sid))
        return {"id": sid, "state": "ready"}

    def _grade(self, question: int) -> dict[str, Any]:
        solved = question in self.solved
        ok = (solved and not self.fails_solved) or (not solved and self.passes_unsolved)
        detail = "image nginx:alpine" if ok else 'pods "web" not found'
        return {
            "passed": ok,
            "results": [{"name": "web runs nginx", "ok": ok, "detail": detail}],
        }

    def grade(self, sid: str, question: int) -> dict[str, Any]:
        self.calls.append(("grade", question))
        self.last_checked = question
        return self._grade(question)

    def shell_exec(
        self, sid: str, cmd: str, namespace: str | None = None, timeout: float = 400
    ) -> tuple[int, str, str]:
        self.calls.append(("shell_exec", namespace))
        self.scripts.append((cmd, namespace))
        # The harness grades right before it solves; that is the question solved.
        self.solved.add(self.last_checked)
        return 0, "pod/web created\n", ""

    def finish(self, sid: str, timeout: float = 600) -> dict[str, Any]:
        self.calls.append(("finish", sid))
        questions = [
            {
                "index": i,
                "drill_id": f"d{i}",
                "namespace": f"ns{i}",
                "weight": 50,
                **self._grade(i),
            }
            for i in range(len(self.scripts))
        ]
        return {"score_percent": 100, "passed": True, "questions": questions}

    def delete(self, sid: str) -> None:
        self.calls.append(("delete", sid))
        self.alive.discard(sid)


class FakeKubectl:
    """`kubectl get <kind> -o name` answering from a scripted cluster, and
    `kubectl get pods -n <ns> -o json` answering from `pods` (empty: settled)."""

    def __init__(
        self,
        after: dict[str, list[str]] | None = None,
        pods: list[dict[str, Any]] | None = None,
    ) -> None:
        self.before = {
            "namespaces": ["namespace/default", "namespace/kube-system"],
            "crds": [],
            "pv": [],
            "clusterrole": ["clusterrole.rbac.authorization.k8s.io/admin"],
            "clusterrolebinding": [],
        }
        self.after = after
        self.pods = pods or []
        self.argv: list[list[str]] = []
        self.session_created = False

    def __call__(
        self,
        argv: list[str],
        *,
        timeout: float,
        cwd: Path | None = None,
        stdin: str | None = None,
    ) -> tuple[int, str, str]:
        self.argv.append(argv)
        if argv[:3] == ["kubectl", "get", "pods"]:
            assert argv[-2:] == ["-o", "json"]
            return 0, json.dumps({"items": self.pods}), ""
        assert argv[:2] == ["kubectl", "get"] and argv[-2:] == ["-o", "name"]
        table = self.after if (self.after and self.session_created) else self.before
        return 0, "\n".join(table[argv[2]]) + "\n", ""

    def pod_queries(self) -> list[str]:
        """Namespaces whose Pods were polled, in order."""
        return [a[4] for a in self.argv if a[:3] == ["kubectl", "get", "pods"]]


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    slept: list[float] = []
    monkeypatch.setattr(harness, "_sleep", slept.append)
    return slept


@pytest.fixture
def kubectl(monkeypatch: pytest.MonkeyPatch) -> FakeKubectl:
    fake = FakeKubectl()
    monkeypatch.setattr(harness, "_run", fake)
    return fake


def test_refuses_when_a_session_it_did_not_create_exists(
    tmp_path: Path, no_sleep: list[float], kubectl: FakeKubectl
) -> None:
    kubiq = FakeKubiq(
        leftover=[{"id": "someone", "exam_id": "ckad-mock-6", "state": "ready"}]
    )
    with pytest.raises(FatalError, match=r"someone.*Do not retry"):
        _prove_one(kubiq, tmp_path)
    assert [c[0] for c in kubiq.calls] == ["sessions"]
    assert kubectl.argv == []


def test_reclaims_a_session_an_earlier_run_of_this_exam_left(
    tmp_path: Path, no_sleep: list[float], kubectl: FakeKubectl
) -> None:
    """Nobody can start a draft exam, so a session on it is ours: delete and go on."""
    kubiq = FakeKubiq(
        leftover=[{"id": "stale", "exam_id": "ckad-mock-7", "state": "ready"}]
    )
    verdict = _prove_one(kubiq, tmp_path)
    assert verdict.passed
    assert kubiq.calls[:2] == [("sessions", None), ("delete", "stale")]


def test_negative_control_then_settle_then_solution_then_grade_then_teardown(
    tmp_path: Path, no_sleep: list[float], kubectl: FakeKubectl
) -> None:
    kubiq = FakeKubiq()
    exam = _exam([Question(f"pod-{i}", 25, f"ckad0{i + 1}-pod") for i in range(4)])
    drills = [_drill(f"pod-{i}") for i in range(4)]
    [verdict] = harness.prove_exam(kubiq, _target(tmp_path, 4), exam, drills, [3])
    assert [c[0] for c in kubiq.calls] == [
        "sessions",
        "create",
        "wait_ready",
        "grade",
        "shell_exec",
        "grade",
        "finish",
        "delete",
    ]
    assert kubiq.calls[3] == ("grade", 3), "question index passes through 0-based"
    assert kubiq.scripts[0][1] == "ckad04-pod"
    assert kubectl.pod_queries() == ["ckad04-pod"], "setup settles before the solution"
    pods_at = kubectl.argv.index(
        ["kubectl", "get", "pods", "-n", "ckad04-pod", "-o", "json"]
    )
    assert pods_at > 0, "the baseline snapshot comes first"
    assert verdict.question == 3
    assert verdict.negative_control_ok is True
    assert verdict.passed is True
    assert verdict.proven is True
    assert verdict.results_before[0]["ok"] is False
    assert verdict.results[0]["ok"] is True
    assert verdict.stdout == "pod/web created"
    assert verdict.session == "abc12345"
    assert verdict.setup_settle_seconds >= 0
    assert no_sleep == [], "a grade that passes first time needs no polling"
    assert kubiq.alive == set(), "the session it created is deleted"


def test_a_check_that_passes_unsolved_fails_the_negative_control(
    tmp_path: Path, no_sleep: list[float], kubectl: FakeKubectl
) -> None:
    verdict = _prove_one(FakeKubiq(passes_unsolved=True), tmp_path)
    assert verdict.negative_control_ok is False
    assert verdict.passed is True
    assert verdict.proven is False
    assert "negative control FAILED" in _digest(verdict)


def test_heredoc_in_the_solution_reaches_the_shell_intact(
    tmp_path: Path, no_sleep: list[float], kubectl: FakeKubectl
) -> None:
    kubiq = FakeKubiq()
    _prove_one(kubiq, tmp_path, _drill(solution=SOLUTION_WITH_HEREDOC))
    script, _ = kubiq.scripts[0]
    assert "# create the pod" not in script
    assert "# indented comment" not in script
    assert "  # this hash is YAML content, not a shell comment\n" in script
    assert "kubectl get pod web   # trailing comment stays" in script
    assert script.index("<<EOF") < script.index("\nEOF\n")


def test_solution_script_drops_only_comment_lines_outside_heredocs() -> None:
    script = harness.solution_script("# a\ncat <<'X'\n# b\nX\n#c\necho done\n")
    assert script == "cat <<'X'\n# b\nX\necho done\n"
    assert harness.solution_script('grep "#" <<<"a#b"\n# gone\n') == 'grep "#" <<<"a#b"\n'


class _SlowKubiq(FakeKubiq):
    """Grades pass only on the Nth check after the solution ran."""

    def __init__(self, passes_on: int) -> None:
        super().__init__()
        self.passes_on = passes_on
        self.graded_after_solve = 0

    def grade(self, sid: str, question: int) -> dict[str, Any]:
        report = super().grade(sid, question)
        if question in self.solved:
            self.graded_after_solve += 1
            if self.graded_after_solve < self.passes_on:
                report = {**report, "passed": False}
        return report


def test_grading_is_polled_until_the_cluster_converges(
    tmp_path: Path, no_sleep: list[float], kubectl: FakeKubectl
) -> None:
    verdict = _prove_one(_SlowKubiq(passes_on=3), tmp_path)
    assert verdict.passed is True
    assert no_sleep == [harness.SETTLE_POLL, harness.SETTLE_POLL]


def test_grading_stops_at_the_settle_cap(
    tmp_path: Path,
    no_sleep: list[float],
    kubectl: FakeKubectl,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KUBIQ_SETTLE_SECONDS", "0")
    kubiq = _SlowKubiq(passes_on=99)
    verdict = _prove_one(kubiq, tmp_path)
    assert verdict.passed is False
    assert no_sleep == [], "a zero cap grades exactly once"
    assert "finish" in [c[0] for c in kubiq.calls], "the exam is still ended"


class _BrokenSolutionKubiq(_SlowKubiq):
    """The reference solution exits non-zero; the grade would never converge."""

    def shell_exec(
        self, sid: str, cmd: str, namespace: str | None = None, timeout: float = 400
    ) -> tuple[int, str, str]:
        super().shell_exec(sid, cmd, namespace, timeout)
        return 1, "", "error: the server doesn't have a resource type"


def test_a_failed_solution_is_graded_once_not_polled(
    tmp_path: Path, no_sleep: list[float], kubectl: FakeKubectl
) -> None:
    kubiq = _BrokenSolutionKubiq(passes_on=99)
    verdict = _prove_one(kubiq, tmp_path)
    assert verdict.passed is False
    assert verdict.exit_code == 1
    assert no_sleep == [], "no settle polling after a non-zero solution exit"
    assert kubiq.graded_after_solve == 1


def test_verdict_json_carries_kind_and_the_fields_the_report_reads(
    tmp_path: Path, no_sleep: list[float], kubectl: FakeKubectl
) -> None:
    verdict = _prove_one(FakeKubiq(), tmp_path)
    data = verdict.to_json()
    assert data["kind"] == "verdict"
    assert data["drill"] == "pod-basics"
    assert data["passed"] is True
    assert data["proven"] is True
    assert data["negative_control_ok"] is True
    assert data["results"] == [
        {"name": "web runs nginx", "ok": True, "detail": "image nginx:alpine"}
    ]
    assert data["results_before"] == [
        {"name": "web runs nginx", "ok": False, "detail": 'pods "web" not found'}
    ]
    assert data["leftovers"] == []
    assert data["namespace"] == "ns0"
    assert data["question"] == 0
    assert data["exit_code"] == 0
    assert data["setup_settle_seconds"] == verdict.setup_settle_seconds
    assert isinstance(data["setup_settle_seconds"], float)


def test_leftovers_are_the_objects_teardown_did_not_remove(
    tmp_path: Path, no_sleep: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeKubectl(
        after={
            "namespaces": ["namespace/default", "namespace/kube-system", "namespace/ns0"],
            "crds": ["customresourcedefinition.apiextensions.k8s.io/widgets.example.com"],
            "pv": [],
            "clusterrole": ["clusterrole.rbac.authorization.k8s.io/admin"],
            "clusterrolebinding": [],
        }
    )
    monkeypatch.setattr(harness, "_run", fake)

    class Kubiq(FakeKubiq):
        def create_session(
            self, exam_id: str, questions: list[int] | None = None
        ) -> dict[str, Any]:
            fake.session_created = True
            return super().create_session(exam_id, questions)

    verdict = _prove_one(Kubiq(), tmp_path)
    assert verdict.passed is True
    assert verdict.proven is False
    assert verdict.leftovers == [
        "customresourcedefinition.apiextensions.k8s.io/widgets.example.com",
        "namespace/ns0",
    ]
    # Teardown is asynchronous: the harness polled, bounded, not giving up at once.
    assert no_sleep.count(harness._TEARDOWN_POLL) == int(
        harness._TEARDOWN_WAIT / harness._TEARDOWN_POLL
    )


def test_prove_exam_solves_everything_in_one_session(
    tmp_path: Path, no_sleep: list[float], kubectl: FakeKubectl
) -> None:
    kubiq = FakeKubiq()
    drills = [
        _drill("pod-a"),
        _drill("pod-b", solution="kubectl rollout restart deploy/x"),
    ]
    exam = _exam([Question("pod-a", 50, "ns0"), Question("pod-b", 50, "ns1")])
    verdicts = harness.prove_exam(kubiq, _target(tmp_path), exam, drills)
    assert [c[0] for c in kubiq.calls].count("create") == 1
    assert [c[0] for c in kubiq.calls].count("finish") == 1
    assert kubiq.calls[1] == ("create", ("ckad-mock-7", None)), "whole exam provisioned"
    assert [v.drill for v in verdicts] == ["pod-a", "pod-b"]
    assert [v.namespace for v in verdicts] == ["ns0", "ns1"]
    assert all(v.passed and v.negative_control_ok for v in verdicts)
    assert all(v.settle_seconds >= 0 for v in verdicts)
    assert no_sleep == []
    assert [s[1] for s in kubiq.scripts] == ["ns0", "ns1"]
    assert kubectl.pod_queries() == ["ns0", "ns1"]


def test_prove_exam_with_indexes_solves_only_those_in_index_order(
    tmp_path: Path, no_sleep: list[float], kubectl: FakeKubectl
) -> None:
    kubiq = FakeKubiq()
    drills = [_drill("pod-a"), _drill("pod-b"), _drill("pod-c")]
    exam = _exam(
        [
            Question("pod-a", 40, "ns0"),
            Question("pod-b", 30, "ns1"),
            Question("pod-c", 30, "ns2"),
        ]
    )
    verdicts = harness.prove_exam(kubiq, _target(tmp_path, 3), exam, drills, [1])
    assert [(v.drill, v.question, v.namespace) for v in verdicts] == [("pod-b", 1, "ns1")]
    assert [c for c in kubiq.calls if c[0] == "grade"] == [("grade", 1), ("grade", 1)]
    assert [s[1] for s in kubiq.scripts] == ["ns1"]
    assert ("create", ("ckad-mock-7", [1])) in kubiq.calls, "only q2 provisioned"
    assert kubectl.pod_queries() == ["ns1"]
    assert [c[0] for c in kubiq.calls].count("create") == 1
    assert "finish" in [c[0] for c in kubiq.calls]
    assert "setup_settle_seconds" in verdicts[0].to_json()

    kubiq = FakeKubiq()
    verdicts = harness.prove_exam(kubiq, _target(tmp_path, 3), exam, drills, [2, 0, 2])
    assert [v.question for v in verdicts] == [0, 2], "index order, no duplicates"
    assert [s[1] for s in kubiq.scripts] == ["ns0", "ns2"]


def test_prove_exam_refuses_an_index_the_exam_does_not_have(
    tmp_path: Path, no_sleep: list[float], kubectl: FakeKubectl
) -> None:
    kubiq = FakeKubiq()
    exam = _exam([Question("pod-a", 100, "ns0")])
    with pytest.raises(DrillError, match=r"\[1\] do not exist in a 1-question exam"):
        harness.prove_exam(kubiq, _target(tmp_path, 1), exam, [_drill("pod-a")], [1])
    assert kubiq.calls == [], "refused before any session"


def test_prove_exam_names_a_drill_the_exam_lists_but_nobody_supplied(
    tmp_path: Path, no_sleep: list[float], kubectl: FakeKubectl
) -> None:
    exam = _exam([Question("pod-a", 100, "ns0")])
    with pytest.raises(DrillError, match="not supplied: pod-a"):
        harness.prove_exam(FakeKubiq(), _target(tmp_path), exam, [])


def test_stage_exam_writes_files_kubiq_reloads(tmp_path: Path) -> None:
    target = _target(tmp_path, questions=2)
    drills = [_drill("pod-a"), _drill("pod-b", solution=SOLUTION_WITH_HEREDOC)]
    exam = _exam(
        [Question("pod-a", 40, "ns0"), Question("pod-b", 60, "ns1")], exam_id="scratch"
    )
    written = harness.stage_exam(target, exam, drills)
    assert written == [
        tmp_path / "exams" / "ckad-mock-7.yaml",
        tmp_path / "drills" / "ckad-mock-7" / "q01-pod-a.yaml",
        tmp_path / "drills" / "ckad-mock-7" / "q02-pod-b.yaml",
    ]
    reloaded = load_exam(written[0])
    assert reloaded.id == "ckad-mock-7", "the exam takes the target's id"
    assert reloaded.draft is False
    assert [(q.drill, q.weight, q.namespace) for q in reloaded.questions] == [
        ("pod-a", 40, "ns0"),
        ("pod-b", 60, "ns1"),
    ]
    assert load_drill(written[1]).id == "pod-a"
    assert load_drill(written[2]).solution == SOLUTION_WITH_HEREDOC


def test_stage_exam_marks_a_short_exam_as_draft(tmp_path: Path) -> None:
    target = _target(tmp_path, questions=21)
    exam = _exam([Question("pod-a", 4, "ns0")])
    written = harness.stage_exam(target, exam, [_drill("pod-a")])
    reloaded = load_exam(written[0])
    assert reloaded.draft is True
    assert reloaded.planned_questions == 21
    assert len(reloaded.questions) == 1


def test_stage_exam_at_full_size_must_weigh_100(tmp_path: Path) -> None:
    target = _target(tmp_path, questions=1)
    with pytest.raises(DrillError, match="weights sum to 40"):
        harness.stage_exam(
            target, _exam([Question("pod-a", 40, "ns0")]), [_drill("pod-a")]
        )


def test_stage_exam_rejects_a_drill_set_disagreeing_with_the_exam(tmp_path: Path) -> None:
    target = _target(tmp_path, questions=2)
    with pytest.raises(DrillError, match="not listed in the exam: pod-b"):
        harness.stage_exam(
            target,
            _exam([Question("pod-a", 100, "ns0")]),
            [_drill("pod-a"), _drill("pod-b")],
        )
    with pytest.raises(DrillError, match="not supplied: pod-b"):
        harness.stage_exam(
            target,
            _exam([Question("pod-a", 50, "ns0"), Question("pod-b", 50, "ns1")]),
            [_drill("pod-a")],
        )


def test_stage_exam_removes_a_drill_file_no_longer_listed(tmp_path: Path) -> None:
    target = _target(tmp_path, questions=1)
    harness.stage_exam(
        target, _exam([Question("pod-old", 100, "ns0")]), [_drill("pod-old")]
    )
    assert (target.drill_dir / "q01-pod-old.yaml").exists()
    harness.stage_exam(
        target, _exam([Question("pod-new", 100, "ns0")]), [_drill("pod-new")]
    )
    assert sorted(p.name for p in target.drill_dir.iterdir()) == ["q01-pod-new.yaml"]


def _digest(verdict: harness.Verdict) -> str:
    return digest_result(json.dumps(verdict.to_json()))


def test_prove_exam_grades_each_question_in_its_own_namespace_not_at_finish(
    tmp_path: Path, no_sleep: list[float], kubectl: FakeKubectl
) -> None:
    """`finish` grades with the shell left in the last question's namespace, so
    exec checks that rely on the current context fail there for reasons that
    are not the drill's. The verdict must be the grade taken right after the
    solution, in-namespace."""

    class FinishLies(FakeKubiq):
        def finish(self, sid: str, timeout: float = 600) -> dict[str, Any]:
            report = super().finish(sid, timeout)
            for q in report["questions"]:
                q["passed"] = False
                q["results"] = [{"name": "x", "ok": False, "detail": "not found"}]
            return report

    kubiq = FinishLies()
    exam = _exam([Question("pod-a", 50, "ns0"), Question("pod-b", 50, "ns1")])
    verdicts = harness.prove_exam(
        kubiq, _target(tmp_path), exam, [_drill("pod-a"), _drill("pod-b")]
    )
    assert all(v.passed and v.proven for v in verdicts)
    assert "finish" in [c[0] for c in kubiq.calls], "the exam is still ended"


def _ticking_clock(monkeypatch: pytest.MonkeyPatch, step: float) -> None:
    """`time.monotonic` advancing `step` seconds on every call."""
    now = [0.0]

    def monotonic() -> float:
        now[0] += step
        return now[0]

    monkeypatch.setattr(harness.time, "monotonic", monotonic)


def test_a_slow_solution_is_not_proven(
    tmp_path: Path,
    no_sleep: list[float],
    kubectl: FakeKubectl,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Correct but slow is refused: the exam gives six minutes a question."""
    _ticking_clock(monkeypatch, 100.0)
    verdict = _prove_one(FakeKubiq(), tmp_path)
    assert verdict.passed and verdict.solve_seconds >= 200
    assert verdict.too_slow and not verdict.proven
    monkeypatch.setenv("KUBIQ_SOLVE_BUDGET", "1000")
    assert not verdict.too_slow and verdict.proven


# --- the setup settle gate ---------------------------------------------------


def _pod(
    phase: str,
    *,
    waiting: str | None = None,
    init_waiting: str | None = None,
    unschedulable: bool = False,
) -> dict[str, Any]:
    def container(reason: str | None) -> dict[str, Any]:
        state = {"waiting": {"reason": reason}} if reason else {"running": {}}
        return {"name": "c", "state": state}

    status: dict[str, Any] = {"phase": phase, "containerStatuses": [container(waiting)]}
    if init_waiting:
        status["initContainerStatuses"] = [container(init_waiting)]
    if unschedulable:
        status["conditions"] = [
            {"type": "PodScheduled", "status": "False", "reason": "Unschedulable"}
        ]
    return {"metadata": {"name": "p"}, "status": status}


def _pods_run(
    monkeypatch: pytest.MonkeyPatch, answers: list[list[dict[str, Any]] | None]
) -> list[list[str]]:
    """`_run` answering `kubectl get pods` from `answers` in turn (None: kubectl
    fails); the last answer repeats. Returns the argv log."""
    calls: list[list[str]] = []

    def run(argv: list[str], *, timeout: float, **kw: Any) -> tuple[int, str, str]:
        calls.append(argv)
        answer = answers[min(len(calls), len(answers)) - 1]
        if answer is None:
            return 1, "", "error: namespace not found"
        return 0, json.dumps({"items": answer}), ""

    monkeypatch.setattr(harness, "_run", run)
    return calls


def test_settle_waits_while_a_container_is_still_creating(
    no_sleep: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _pods_run(
        monkeypatch,
        [
            [_pod("Pending", waiting="ContainerCreating")],
            [_pod("Running", waiting="PodInitializing")],
            [_pod("Pending", init_waiting="PodInitializing")],
            [_pod("Running")],
        ],
    )
    waited = harness.settle_setup("ckad03-job")
    assert calls[0] == ["kubectl", "get", "pods", "-n", "ckad03-job", "-o", "json"]
    assert len(calls) == 4
    assert no_sleep == [harness.SETTLE_POLL] * 3
    assert 0 <= waited <= harness.SETUP_SETTLE_MAX


@pytest.mark.parametrize(
    "pods",
    [
        [],
        [_pod("Running")],
        [_pod("Succeeded"), _pod("Failed")],
        [_pod("Pending", unschedulable=True)],
        [_pod("Pending", waiting="ImagePullBackOff")],
        [_pod("Pending", waiting="ErrImagePull")],
        [_pod("Pending", waiting="CreateContainerConfigError")],
        [_pod("Pending", init_waiting="InvalidImageName")],
        [_pod("Running", waiting="CrashLoopBackOff")],
        [_pod("Running"), _pod("Pending", unschedulable=True)],
    ],
    ids=[
        "no pods",
        "running",
        "terminal",
        "unschedulable trap",
        "image pull backoff",
        "image pull error",
        "config error",
        "init image invalid",
        "crash loop",
        "mixed settled",
    ],
)
def test_settle_is_immediate_when_nothing_is_still_being_created(
    no_sleep: list[float], monkeypatch: pytest.MonkeyPatch, pods: list[dict[str, Any]]
) -> None:
    calls = _pods_run(monkeypatch, [pods])
    waited = harness.settle_setup("ns")
    assert len(calls) == 1
    assert no_sleep == []
    assert 0 <= waited < 1


def test_settle_treats_a_failing_kubectl_as_settled(
    no_sleep: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _pods_run(monkeypatch, [None])
    assert harness.settle_setup("gone") >= 0
    assert len(calls) == 1 and no_sleep == []


def test_settle_gives_up_at_the_cap_without_raising(
    no_sleep: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    _ticking_clock(monkeypatch, 10.0)
    calls = _pods_run(monkeypatch, [[_pod("Pending")]])  # scheduled, kubelet silent
    waited = harness.settle_setup("ckad05-slow")
    assert waited == harness.SETUP_SETTLE_MAX
    assert len(calls) >= 2 and len(no_sleep) == len(calls) - 1
    calls = _pods_run(monkeypatch, [[_pod("Running", waiting="ContainerCreating")]])
    assert harness.settle_setup("ckad05-slow", timeout=20.0) == 20.0
