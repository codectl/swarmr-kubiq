"""The report the commander files once the exam is written and proven.

One responsibility: the shape of a filed report — the tool the commander calls
and the plain-text rendering of its arguments. The verdicts come from the tester
and the critic; the branch and commit come from `pr.py`; nothing in here is
measured, only rendered.

The report is a normal tool rather than `response_format=ToolStrategy(...)`:
that sets `tool_choice="required"`, which the model rejects while thinking is
enabled. A tool call's arguments are visible in the update stream, which makes
the filing deterministic to capture and visible in the delegation trail, and it
stops the commander from ending on the critic's ruling with nothing filed.
"""

from __future__ import annotations

from typing import Any

from langchain_core.tools import tool

__all__ = ["FILE_REPORT_TOOL", "VERDICTS", "render_report_args"]

VERDICTS = ("confirmed", "refuted", "unproven")


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _text(value: Any) -> str:
    return str(value or "").strip()


def _verdict(value: Any) -> str:
    verdict = _text(value).lower()
    return verdict if verdict in VERDICTS else "unproven"


def _questions(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    return [q for q in raw if isinstance(q, dict)]


def _question_line(position: int, q: dict[str, Any]) -> str:
    index = _int(q.get("index"), position)
    title = _text(q.get("title")) or _text(q.get("drill")) or "untitled"
    weight = _int(q.get("weight"))
    verdict = _verdict(q.get("verdict"))
    rounds = _int(q.get("rounds"))
    line = f"{index:02d}. {title} — {weight} pts — {verdict} ({rounds})"
    if namespace := _text(q.get("namespace")):
        line += f" [{namespace}]"
    return line


def _push_command(branch: str) -> str:
    return f"git push -u origin {branch}"


def _pr_command(exam_id: str, branch: str, n_questions: int) -> str:
    title = f"feat: add {exam_id}" if exam_id else "feat: add exam"
    body = f"{n_questions} questions, every one proven on a live kubiq session."
    return f'gh pr create --base main --head {branch} --title "{title}" --body "{body}"'


def render_report_args(args: dict[str, Any]) -> str:
    """Render filed report arguments as plain text; tolerant of missing keys."""
    exam_id = _text(args.get("exam_id"))
    questions = _questions(args.get("questions"))
    counts: dict[str, int] = dict.fromkeys(VERDICTS, 0)
    confirmed_weight = 0
    for q in questions:
        verdict = _verdict(q.get("verdict"))
        counts[verdict] += 1
        if verdict == "confirmed":
            confirmed_weight += _int(q.get("weight"))

    lines = [
        f"EXAM {exam_id or '(unnamed)'} — {len(questions)} questions: "
        f"{counts['confirmed']} confirmed, {counts['refuted']} refuted, "
        f"{counts['unproven']} unproven — {confirmed_weight} pts confirmed"
    ]

    lines += ["", "QUESTIONS"]
    if questions:
        lines += [f"  {_question_line(i, q)}" for i, q in enumerate(questions, start=1)]
        for i, q in enumerate(questions, start=1):
            if notes := _text(q.get("notes")):
                lines.append(f"  {_int(q.get('index'), i):02d}: {notes}")
    else:
        lines.append("  none filed")

    new_features = args.get("new_feature_questions") or []
    if isinstance(new_features, str):
        new_features = [new_features]
    lines += ["", "NEW-FEATURE COVERAGE"]
    if new_features:
        lines += [f"  - {_text(item)}" for item in new_features if _text(item)]
    else:
        lines.append("  none")

    branch = _text(args.get("branch"))
    commit = _text(args.get("commit"))
    lines += ["", "PR"]
    if branch:
        lines.append(f"  branch {branch}" + (f" at {commit}" if commit else ""))
        lines.append(f"  {_push_command(branch)}")
        lines.append(f"  {_pr_command(exam_id, branch, len(questions))}")
    else:
        lines.append("  nothing committed")

    if caveats := _text(args.get("caveats")):
        lines += ["", "CAVEATS", f"  {caveats}"]
    return "\n".join(lines)


@tool(parse_docstring=True)
def file_exam_report(
    exam_id: str,
    questions: list[dict[str, Any]],
    new_feature_questions: list[str],
    branch: str,
    commit: str,
    caveats: str = "",
) -> str:
    """File the final exam report. Call this exactly once, as your last action.

    Args:
        exam_id: The exam identifier as written, for example "ckad-mock-7".
        questions: One entry per question, in exam order, each with the keys
            index (int, 1-based), drill (the drill id), title, topic, weight
            (int, points), namespace (str or null), verdict ("confirmed",
            "refuted" or "unproven" — the critic's word, never your own),
            rounds (int, how many author/tester rounds it took) and notes
            (one line: what the harness observed, or why it is not confirmed).
        new_feature_questions: Drill ids that cover recent-version material,
            each followed by the version, for example "ckad-mock-7-q04 (1.37)".
            Empty when the exam has none.
        branch: The branch `open_pr` created, for example "exam/ckad-mock-7".
            "" when nothing was committed.
        commit: The commit sha `open_pr` reported. "" when nothing was committed.
        caveats: What the reader must know before merging: questions left
            unproven and why, a kubiq restart that failed, a session the
            harness could not create.
    """
    _ = (exam_id, questions, new_feature_questions, branch, commit, caveats)
    # The arguments are the report. Returning a receipt keeps the transcript
    # small; the caller reads the filed arguments from the run stream.
    return "Report filed. Stop here; do not repeat it in prose."


FILE_REPORT_TOOL = file_exam_report
