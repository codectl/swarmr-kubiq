"""The filed report renders every question and never raises on a thin filing."""

from __future__ import annotations

from swarmr_kubiq.report_tool import FILE_REPORT_TOOL, render_report_args

_FULL = {
    "exam_id": "ckad-mock-7",
    "questions": [
        {
            "index": 1,
            "drill": "ckad-mock-7-q01",
            "title": "Expose a deployment",
            "topic": "services",
            "weight": 4,
            "namespace": "shop",
            "verdict": "confirmed",
            "rounds": 1,
            "notes": "negative control failed, solution passed 3/3 checks",
        },
        {
            "index": 2,
            "drill": "ckad-mock-7-q02",
            "title": "Sidecar container",
            "topic": "pods",
            "weight": 6,
            "namespace": None,
            "verdict": "refuted",
            "rounds": 3,
            "notes": "check 'logs' passes before the solution runs",
        },
        {
            "index": 3,
            "drill": "ckad-mock-7-q03",
            "title": "Image volume",
            "topic": "storage",
            "weight": 5,
            "namespace": "media",
            "verdict": "unproven",
            "rounds": 2,
            "notes": "kubiq restart timed out",
        },
    ],
    "new_feature_questions": ["ckad-mock-7-q03 (1.37)"],
    "files": ["exams/ckad-mock-7.yaml", "drills/ckad-mock-7/q01-expose.yaml"],
    "caveats": "q03 needs a cluster with ImageVolume enabled.",
}


def test_full_filing_renders_every_question_and_the_push_command() -> None:
    text = render_report_args(_FULL)
    assert "EXAM ckad-mock-7 — 3 questions: 1 confirmed, 1 refuted, 1 unproven" in text
    assert "4 pts confirmed" in text
    assert "01. Expose a deployment — 4 pts — confirmed (1) [shop]" in text
    assert "02. Sidecar container — 6 pts — refuted (3)" in text
    assert "03. Image volume — 5 pts — unproven (2) [media]" in text
    assert "- ckad-mock-7-q03 (1.37)" in text
    assert "FILES WRITTEN (not committed)" in text
    assert "  drills/ckad-mock-7/q01-expose.yaml" in text
    assert "git add -- exams/ckad-mock-7.yaml drills/ckad-mock-7/q01-expose.yaml" in text
    assert "git push -u origin exam/ckad-mock-7" in text
    assert 'gh pr create --base main --head exam/ckad-mock-7 --title "feat: add' in text
    assert "ImageVolume enabled" in text
    assert "01: negative control failed" in text


def test_empty_filing_does_not_raise() -> None:
    text = render_report_args({})
    assert "EXAM (unnamed) — 0 questions" in text
    assert "none filed" in text
    assert "FILES WRITTEN (not committed)\n  none" in text


def test_odd_shapes_are_tolerated() -> None:
    text = render_report_args(
        {
            "questions": [{"title": "t", "weight": "x", "verdict": "maybe"}, "junk"],
            "new_feature_questions": "single",
            "files": "exams/x.yaml",
        }
    )
    assert "01. t — 0 pts — unproven (0)" in text
    assert "- single" in text
    assert "git push -u origin exam/<id>" in text


def test_tool_name_and_receipt() -> None:
    assert FILE_REPORT_TOOL.name == "file_exam_report"
    receipt = FILE_REPORT_TOOL.invoke(
        {
            "exam_id": "e",
            "questions": [],
            "new_feature_questions": [],
            "files": [],
        }
    )
    assert "filed" in receipt
