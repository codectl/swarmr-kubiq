"""target: the run's checkout/exam/server, validated once and ambient to every tool."""

from __future__ import annotations

import contextvars
import threading
from pathlib import Path

import pytest
from swarmr.core.team import TeamError

from swarmr_kubiq.target import TargetError, activate, current


def _checkout(tmp_path: Path) -> Path:
    root = tmp_path / "kubiq"
    (root / "drills").mkdir(parents=True)
    (root / "exams").mkdir()
    return root


def test_activate_validates_the_checkout_and_fills_the_defaults(tmp_path: Path) -> None:
    root = _checkout(tmp_path)
    t = activate({"kubiq": str(root), "exam": "ckad-mock-7"})
    assert t.kubiq == root.resolve()
    assert t.exam == "ckad-mock-7"
    assert t.certification == "CKAD"
    assert t.questions == 21
    assert t.server == "http://localhost:8080"
    assert t.exam_file == root.resolve() / "exams" / "ckad-mock-7.yaml"
    assert t.drill_dir == root.resolve() / "drills" / "ckad-mock-7"
    assert current() is t


def test_kubiq_defaults_to_the_current_directory(tmp_path: Path) -> None:
    (tmp_path / "drills").mkdir()
    (tmp_path / "exams").mkdir()
    with pytest.MonkeyPatch.context() as mp:
        mp.chdir(tmp_path)
        assert activate({"exam": "ckad-mock-7"}).kubiq == tmp_path.resolve()


def test_activate_rejects_a_directory_that_is_not_a_kubiq_checkout(
    tmp_path: Path,
) -> None:
    monkeypatch_cwd = tmp_path / "elsewhere"
    monkeypatch_cwd.mkdir()
    with pytest.MonkeyPatch.context() as mp:
        mp.chdir(monkeypatch_cwd)
        with pytest.raises(TeamError, match="not a kubiq checkout"):
            activate({"kubiq": "  ", "exam": "ckad-mock-7"})
    with pytest.raises(TeamError, match="not a directory"):
        activate({"kubiq": str(tmp_path / "nope"), "exam": "ckad-mock-7"})
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(TeamError, match="no `drills/` directory"):
        activate({"kubiq": str(plain), "exam": "ckad-mock-7"})
    (plain / "drills").mkdir()
    with pytest.raises(TeamError, match="no `exams/` directory"):
        activate({"kubiq": str(plain), "exam": "ckad-mock-7"})


@pytest.mark.parametrize(
    "exam", ["", "CKAD-Mock-7", "7mock", "ab", "ckad mock 7", "a" * 42]
)
def test_activate_rejects_an_exam_id_outside_the_pattern(
    tmp_path: Path, exam: str
) -> None:
    with pytest.raises(TeamError, match="not a valid exam id"):
        activate({"kubiq": str(_checkout(tmp_path)), "exam": exam})


def test_certification_is_upper_cased_and_sets_the_question_count(
    tmp_path: Path,
) -> None:
    root = _checkout(tmp_path)
    cks = activate({"kubiq": str(root), "exam": "cks-mock-4", "certification": "cks"})
    assert (cks.certification, cks.questions) == ("CKS", 19)
    t = activate({"kubiq": str(root), "exam": "cka-mock-6", "certification": " cka "})
    assert (t.certification, t.questions) == ("CKA", 21)
    with pytest.raises(TeamError, match="certification='CKX' is not one of"):
        activate({"kubiq": str(root), "exam": "ckx-mock-1", "certification": "ckx"})


def test_questions_and_server_are_validated(tmp_path: Path) -> None:
    root = _checkout(tmp_path)
    t = activate(
        {
            "kubiq": str(root),
            "exam": "ckad-mock-7",
            "questions": "5",
            "server": "http://kubiq.local:9000/",
        }
    )
    assert t.questions == 5
    assert t.server == "http://kubiq.local:9000"
    with pytest.raises(TeamError, match="not a number"):
        activate({"kubiq": str(root), "exam": "ckad-mock-7", "questions": "many"})
    with pytest.raises(TeamError, match="between 1 and 40"):
        activate({"kubiq": str(root), "exam": "ckad-mock-7", "questions": "0"})
    with pytest.raises(TeamError, match="must be an http"):
        activate({"kubiq": str(root), "exam": "ckad-mock-7", "server": "kubiq:8080"})


def test_errors_are_team_errors_the_operator_can_act_on() -> None:
    assert issubclass(TargetError, TeamError)
    with pytest.raises(TargetError):
        activate(None)


def test_tools_outside_a_run_fail_with_a_sentence() -> None:
    def in_fresh_context() -> None:
        with pytest.raises(TeamError, match="No target is active"):
            current()

    contextvars.Context().run(in_fresh_context)


def test_the_target_is_per_context_not_per_process(tmp_path: Path) -> None:
    """Two runs in one MCP server must not see each other's checkout."""
    a, b = _checkout(tmp_path / "a"), _checkout(tmp_path / "b")
    seen: dict[str, Path] = {}

    def run(name: str, root: Path) -> None:
        activate({"kubiq": str(root), "exam": f"ckad-mock-{name}"})
        seen[name] = current().kubiq

    contextvars.Context().run(run, "1", a)
    contextvars.Context().run(run, "2", b)
    assert seen == {"1": a.resolve(), "2": b.resolve()}


def test_the_target_survives_a_thread_that_copies_context(tmp_path: Path) -> None:
    """LangGraph fans tool calls out to worker threads with copy_context."""
    root = _checkout(tmp_path)
    activate({"kubiq": str(root), "exam": "ckad-mock-7"})
    seen: list[Path] = []

    def observe() -> None:
        seen.append(current().kubiq)

    worker = threading.Thread(target=contextvars.copy_context().run, args=(observe,))
    worker.start()
    worker.join()
    assert seen == [root.resolve()]


def test_a_version_above_the_test_cluster_refuses_to_start(tmp_path: Path) -> None:
    from swarmr_kubiq.catalogue import check_version

    (tmp_path / "drills").mkdir()
    (tmp_path / "exams").mkdir()
    t = activate({"kubiq": str(tmp_path), "exam": "ckad-mock-7", "version": "1.37"})
    assert t.version == "1.37"
    with pytest.raises(TeamError, match=r"cluster kubiq tests on runs v1\.35\.5"):
        check_version(t, "v1.35.5+k3s1")
    check_version(t, "v1.37.0")  # equal is fine
    check_version(t, "")  # unknown cluster: no guess, no refusal
    check_version(activate({"kubiq": str(tmp_path), "exam": "ckad-mock-7"}), "v1.30.0")
    with pytest.raises(TeamError, match="not a Kubernetes version"):
        activate({"kubiq": str(tmp_path), "exam": "ckad-mock-7", "version": "latest"})
