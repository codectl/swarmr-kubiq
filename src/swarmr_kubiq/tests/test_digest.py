"""One-line summaries of this team's tool payloads.

Each kind the tools emit reads as the one fact a progress line needs: which
drill passed or failed and on which check, the nearest catalogue match, the
session state, how many docs hits came back.
"""

from __future__ import annotations

import json

from swarmr_kubiq.digest import digest_result, is_tool_error


def test_verdict_reads_drill_outcome_and_first_failing_check() -> None:
    payload = json.dumps(
        {
            "kind": "verdict",
            "drill": "pod-basics",
            "passed": False,
            "negative_control_ok": True,
            "results": [
                {"name": "web exists", "ok": True, "detail": ""},
                {"name": "web runs nginx", "ok": False, "detail": "image busybox"},
                {"name": "web is ready", "ok": False, "detail": "Pending"},
            ],
            "leftovers": [],
        }
    )
    assert (
        digest_result(payload)
        == "pod-basics failed (3 checks; first failing: web runs nginx)"
    )


def test_passing_verdict_flags_a_broken_negative_control_and_leftovers() -> None:
    payload = json.dumps(
        {
            "kind": "verdict",
            "drill": "pod-basics",
            "passed": True,
            "negative_control_ok": False,
            "results": [{"name": "web exists", "ok": True, "detail": ""}],
            "leftovers": ["namespace/ns0"],
        }
    )
    assert (
        digest_result(payload)
        == "pod-basics passed (1 checks), negative control FAILED, 1 leftovers"
    )


def test_similarity_reads_the_top_match_and_score() -> None:
    payload = json.dumps(
        {
            "kind": "similarity",
            "query": "pod-basics",
            "matches": [
                {"id": "label-then-expose-all-ns", "score": 0.7312, "duplicate": True},
                {"id": "env-expansion", "score": 0.21, "duplicate": False},
            ],
        }
    )
    assert (
        digest_result(payload)
        == "similarity: top label-then-expose-all-ns at 0.73 DUPLICATE"
    )


def test_similarity_without_matches_says_so() -> None:
    assert digest_result(json.dumps({"kind": "similarity", "matches": []})) == (
        "similarity: no match"
    )


def test_session_reads_its_state() -> None:
    payload = json.dumps(
        {"kind": "session", "id": "abc12345", "exam_id": "ckad-mock-7", "state": "ready"}
    )
    assert digest_result(payload) == "session abc12345 ready (ckad-mock-7)"


def test_failed_session_carries_the_server_message() -> None:
    payload = json.dumps(
        {
            "kind": "session",
            "id": "abc12345",
            "state": "failed",
            "message": "namespace ckad01-pod already exists",
        }
    )
    assert (
        digest_result(payload)
        == "session abc12345 failed: namespace ckad01-pod already exists"
    )


def test_docs_counts_hits_and_names_the_first() -> None:
    payload = json.dumps(
        {
            "kind": "docs",
            "query": "pod security standards",
            "hits": [
                {"title": "Pod Security Standards", "url": "https://k8s.io/a"},
                {"title": "Pod Security Admission", "url": "https://k8s.io/b"},
            ],
        }
    )
    assert digest_result(payload) == "docs: 2 hits, first Pod Security Standards"


def test_docs_without_hits_says_so() -> None:
    assert digest_result(json.dumps({"kind": "docs", "hits": []})) == "docs: no hits"


def test_tool_error_is_recognised_and_shown_verbatim() -> None:
    assert is_tool_error("tool error: a session already exists (someone)")
    assert not is_tool_error('{"kind": "verdict"}')
    assert digest_result("tool error: a session already exists").startswith("tool error")


def test_unknown_kind_falls_back_to_its_shape() -> None:
    payload = json.dumps({"kind": "catalogue", "ids": [], "namespaces": []})
    assert digest_result(payload) == "catalogue with ids, namespaces"


def test_truncated_json_is_reported_as_such() -> None:
    assert "truncated json" in digest_result('{"kind": "verdict", "results": [{"na')


def test_empty_and_plain_text() -> None:
    assert digest_result("   ") == "empty"
    assert digest_result("first line\nsecond") == "first line"
