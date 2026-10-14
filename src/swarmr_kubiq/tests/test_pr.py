"""`open_pr` commits only what the run wrote, and refuses to sweep the user's work."""

from __future__ import annotations

from pathlib import Path

import pytest

from swarmr_kubiq import pr
from swarmr_kubiq.pr import PrError, Run, branch_name, commit_message, open_pr
from swarmr_kubiq.target import Target


class FakeGit:
    """Records every argv; answers `status`, `rev-parse` and `gh` from fields."""

    def __init__(
        self, *, status: str = "", branch_exists: bool = False, gh_ok: bool = False
    ) -> None:
        self.status = status
        self.branch_exists = branch_exists
        self.gh_ok = gh_ok
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], cwd: Path) -> Run:
        self.calls.append(argv)
        if argv[:2] == ["git", "status"]:
            return Run(0, self.status)
        if argv[:3] == ["git", "rev-parse", "--verify"]:
            return Run(0 if self.branch_exists else 1, "")
        if argv[:2] == ["git", "rev-parse"]:
            return Run(0, "abc1234")
        if argv[:3] == ["gh", "auth", "status"]:
            return Run(0 if self.gh_ok else 1, "")
        if argv[:3] == ["gh", "pr", "create"]:
            return Run(0, "https://github.com/codectl/kubiq/pull/42")
        return Run(0, "")

    def of(self, *prefix: str) -> list[list[str]]:
        return [c for c in self.calls if c[: len(prefix)] == list(prefix)]


def _target(tmp_path: Path) -> Target:
    return Target(
        kubiq=tmp_path,
        exam="ckad-mock-7",
        certification="CKAD",
        server="http://localhost:8080",
        questions=2,
    )


def _written(tmp_path: Path) -> list[Path]:
    return [
        tmp_path / "exams" / "ckad-mock-7.yaml",
        tmp_path / "drills" / "ckad-mock-7" / "q01.yaml",
        tmp_path / "drills" / "ckad-mock-7" / "q02.yaml",
    ]


def test_names() -> None:
    assert branch_name("ckad-mock-7") == "exam/ckad-mock-7"
    assert commit_message("ckad-mock-7", 2).startswith("feat: add ckad-mock-7\n")
    assert "2 questions" in commit_message("ckad-mock-7", 2)
    assert "1 question," in commit_message("ckad-mock-7", 1)


def test_creates_branch_adds_only_written_paths_and_commits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pr.shutil, "which", lambda _name: None)
    git = FakeGit(
        status="?? exams/ckad-mock-7.yaml\n?? drills/ckad-mock-7/q01.yaml\n"
        "?? drills/ckad-mock-7/q02.yaml\n"
    )
    result = open_pr(_target(tmp_path), _written(tmp_path), "t", "b", run=git)

    assert git.of("git", "checkout") == [["git", "checkout", "-b", "exam/ckad-mock-7"]]
    (add,) = git.of("git", "add")
    assert add == [
        "git",
        "add",
        "--",
        "drills/ckad-mock-7/q01.yaml",
        "drills/ckad-mock-7/q02.yaml",
        "exams/ckad-mock-7.yaml",
    ]
    (commit,) = git.of("git", "commit")
    assert commit[:3] == ["git", "commit", "-m"]
    assert commit[3].startswith("feat: add ckad-mock-7")
    assert "2 questions" in commit[3]
    assert result == {
        "branch": "exam/ckad-mock-7",
        "commit": "abc1234",
        "pr_url": None,
        "push": "git push -u origin exam/ckad-mock-7",
    }
    assert not git.of("gh")


def test_switches_to_an_existing_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pr.shutil, "which", lambda _name: None)
    git = FakeGit(branch_exists=True)
    open_pr(_target(tmp_path), _written(tmp_path), "t", "b", run=git)
    assert git.of("git", "checkout") == [["git", "checkout", "exam/ckad-mock-7"]]


def test_refuses_when_foreign_files_are_dirty(tmp_path: Path) -> None:
    git = FakeGit(status=" M src/main.rs\n?? exams/ckad-mock-7.yaml\n")
    with pytest.raises(PrError, match=r"src/main\.rs"):
        open_pr(_target(tmp_path), _written(tmp_path), "t", "b", run=git)
    assert not git.of("git", "checkout")
    assert not git.of("git", "add")


def test_refuses_paths_outside_the_checkout(tmp_path: Path) -> None:
    outside = tmp_path.parent / "elsewhere.yaml"
    with pytest.raises(PrError, match="outside"):
        open_pr(_target(tmp_path), [outside], "t", "b", run=FakeGit())


def test_refuses_an_empty_write_set(tmp_path: Path) -> None:
    with pytest.raises(PrError, match="nothing to commit"):
        open_pr(_target(tmp_path), [], "t", "b", run=FakeGit())


def test_opens_a_pr_when_gh_is_logged_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pr.shutil, "which", lambda _name: "/usr/bin/gh")
    git = FakeGit(gh_ok=True)
    result = open_pr(_target(tmp_path), _written(tmp_path), "Title", "Body", run=git)
    (create,) = git.of("gh", "pr", "create")
    assert create[3:7] == ["--base", "main", "--head", "exam/ckad-mock-7"]
    assert create[7:] == ["--title", "Title", "--body", "Body"]
    assert result["pr_url"] == "https://github.com/codectl/kubiq/pull/42"
    assert "push" not in result


def test_gh_present_but_logged_out_falls_back_to_push_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pr.shutil, "which", lambda _name: "/usr/bin/gh")
    git = FakeGit(gh_ok=False)
    result = open_pr(_target(tmp_path), _written(tmp_path), "t", "b", run=git)
    assert result["pr_url"] is None
    assert result["push"] == "git push -u origin exam/ckad-mock-7"
    assert not git.of("gh", "pr")
