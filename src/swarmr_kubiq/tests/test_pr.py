"""The PR naming convention the report prints."""

from __future__ import annotations

from swarmr_kubiq.pr import branch_name, commit_message


def test_names() -> None:
    assert branch_name("ckad-mock-7") == "exam/ckad-mock-7"
    assert commit_message("ckad-mock-7", 1).startswith(
        "feat: add ckad-mock-7\n\n1 question,"
    )
    assert "21 questions," in commit_message("ckad-mock-7", 21)
