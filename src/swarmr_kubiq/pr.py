"""Names for the pull request the operator opens after a run.

One responsibility: the branch and commit message convention for an exam. The
team never runs git — it writes files into the checkout and the report prints
these names with the commands to use them. Keeping the convention here means
the report and any future automation agree on it.
"""

from __future__ import annotations

__all__ = ["branch_name", "commit_message"]


def branch_name(exam: str) -> str:
    """`exam/<exam>`, the branch an exam is reviewed on."""
    return f"exam/{exam}"


def commit_message(exam: str, n_questions: int) -> str:
    """The commit subject and a one-line body naming the question count."""
    plural = "question" if n_questions == 1 else "questions"
    body = f"{n_questions} {plural}, each proven on a live kubiq session."
    return f"feat: add {exam}\n\n{body}"
