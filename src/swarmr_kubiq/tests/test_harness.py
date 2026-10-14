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
from swarmr_kubiq.kubiq_api import KubiqError
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

    def create_session(self, exam_id: str) -> dict[str, Any]:
        self.calls.append(("create", exam_id))
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

    def check(self, sid: str, question: int) -> dict[str, Any]:
        self.calls.append(("check", question))
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
    """`kubectl get <kind> -o name` answering from a scripted cluster."""

    def __init__(self, after: dict[str, list[str]] | None = None) -> None:
        self.before = {
            "namespaces": ["namespace/default", "namespace/kube-system"],
            "crds": [],
            "pv": [],
            "clusterrole": ["clusterrole.rbac.authorization.k8s.io/admin"],
            "clusterrolebinding": [],
        }
        self.after = after
        self.argv: list[list[str]] = []
        self.session_created = False

    def __call__(
        self, argv: list[str], *, timeout: float, cwd: Path | None = None
    ) -> tuple[int, str, str]:
        self.argv.append(argv)
        assert argv[:2] == ["kubectl", "get"] and argv[-2:] == ["-o", "name"]
        table = self.after if (self.after and self.session_created) else self.before
        return 0, "\n".join(table[argv[2]]) + "\n", ""


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
    kubiq = FakeKubiq(leftover=[{"id": "someone", "state": "ready"}])
    with pytest.raises(KubiqError, match="someone"):
        harness.prove_question(
            kubiq, _target(tmp_path), "ckad-mock-7", 0, _drill(), "ns0"
        )
    assert [c[0] for c in kubiq.calls] == ["sessions"]
    assert kubectl.argv == []


def test_negative_control_then_solution_then_grade_then_teardown(
    tmp_path: Path, no_sleep: list[float], kubectl: FakeKubectl
) -> None:
    kubiq = FakeKubiq()
    verdict = harness.prove_question(
        kubiq, _target(tmp_path), "ckad-mock-7", 3, _drill(), "ckad04-pod"
    )
    assert [c[0] for c in kubiq.calls] == [
        "sessions",
        "create",
        "wait_ready",
        "check",
        "shell_exec",
        "check",
        "finish",
        "delete",
    ]
    assert kubiq.calls[3] == ("check", 3), "question index passes through 0-based"
    assert kubiq.scripts[0][1] == "ckad04-pod"
    assert verdict.negative_control_ok is True
    assert verdict.passed is True
    assert verdict.proven is True
    assert verdict.results_before[0]["ok"] is False
    assert verdict.results[0]["ok"] is True
    assert verdict.stdout == "pod/web created"
    assert verdict.session == "abc12345"
    assert no_sleep == [harness.SETTLE_SHORT]
    assert kubiq.alive == set(), "the session it created is deleted"


def test_a_check_that_passes_unsolved_fails_the_negative_control(
    tmp_path: Path, no_sleep: list[float], kubectl: FakeKubectl
) -> None:
    kubiq = FakeKubiq(passes_unsolved=True)
    verdict = harness.prove_question(
        kubiq, _target(tmp_path), "ckad-mock-7", 0, _drill(), "ns"
    )
    assert verdict.negative_control_ok is False
    assert verdict.passed is True
    assert verdict.proven is False
    assert "negative control FAILED" in _digest(verdict)


def test_heredoc_in_the_solution_reaches_the_shell_intact(
    tmp_path: Path, no_sleep: list[float], kubectl: FakeKubectl
) -> None:
    kubiq = FakeKubiq()
    drill = _drill(solution=SOLUTION_WITH_HEREDOC)
    harness.prove_question(kubiq, _target(tmp_path), "ckad-mock-7", 0, drill, "ns")
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


def test_settle_is_longer_for_slow_resources() -> None:
    assert harness.settle_seconds("kubectl run x --image=nginx") == harness.SETTLE_SHORT
    assert (
        harness.settle_seconds("kubectl rollout status deploy/x") == harness.SETTLE_LONG
    )
    assert harness.settle_seconds("helm install x ./chart") == harness.SETTLE_LONG
    assert (
        harness.settle_seconds("kubectl create job j --image=busybox")
        == harness.SETTLE_LONG
    )


def test_verdict_json_carries_kind_and_the_fields_the_report_reads(
    tmp_path: Path, no_sleep: list[float], kubectl: FakeKubectl
) -> None:
    verdict = harness.prove_question(
        FakeKubiq(), _target(tmp_path), "ckad-mock-7", 0, _drill(), "ns0"
    )
    data = verdict.to_json()
    assert data["kind"] == "verdict"
    assert data["drill"] == "pod-basics"
    assert data["passed"] is True
    assert data["proven"] is True
    assert data["negative_control_ok"] is True
    assert data["results"] == [
        {"name": "web runs nginx", "ok": True, "detail": "image nginx:alpine"}
    ]
    assert data["leftovers"] == []
    assert data["namespace"] == "ns0"
    assert data["question"] == 0
    assert data["exit_code"] == 0


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
        def create_session(self, exam_id: str) -> dict[str, Any]:
            fake.session_created = True
            return super().create_session(exam_id)

    verdict = harness.prove_question(
        Kubiq(), _target(tmp_path), "ckad-mock-7", 0, _drill(), "ns0"
    )
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
    assert kubiq.calls[1] == ("create", "ckad-mock-7")
    assert [v.drill for v in verdicts] == ["pod-a", "pod-b"]
    assert [v.namespace for v in verdicts] == ["ns0", "ns1"]
    assert all(v.passed and v.negative_control_ok for v in verdicts)
    assert [v.settle_seconds for v in verdicts] == [
        harness.SETTLE_SHORT,
        harness.SETTLE_LONG,
    ]
    assert [s[1] for s in kubiq.scripts] == ["ns0", "ns1"]


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
