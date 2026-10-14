"""Reading this team's tool results: one-line summary, and what counts as failure.

Domain knowledge, so it lives with the team: only this team knows that a
`verdict` is read off its drill id, pass flag and first failing check, that a
`similarity` payload's first match is the one that matters, that a `session` is
summarised by its state, or that its own tools report trouble by opening with
"tool error". `core` gets a shape-only fallback, marks nothing as an error, and
stays domain-free.
"""

from __future__ import annotations

import json
from typing import Any

from swarmr.core.text import clip

__all__ = ["digest_result", "is_tool_error"]

# How this team's tools say "that did not work". `guard` writes it.
_ERROR_PREFIXES = ("tool error",)


def is_tool_error(text: str) -> bool:
    """Whether a tool result is a failure rather than an observation."""
    return text.strip().startswith(_ERROR_PREFIXES)


def digest_result(text: str) -> str:
    """One informative line for an exam-authoring tool result.

    A byte count plus the first line is worthless here: every JSON result opens
    with "{". What a reader wants is which kind came back and its verdict, the
    drill it points at, or the error.
    """
    stripped = text.strip()
    if not stripped:
        return "empty"
    if is_tool_error(stripped):
        return clip(stripped, 100)
    if stripped.startswith("{"):
        return _digest_json(stripped, len(text))
    return clip(stripped.splitlines()[0], 100)


def _digest_json(stripped: str, size: int) -> str:
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        return f"{size}B truncated json"
    kind = str(data.get("kind", ""))
    line = _BY_KIND.get(kind)
    if line is not None:
        return line(data)
    # Shape fallback for a payload this module does not know.
    keys = ", ".join(sorted(k for k in data if k != "kind"))
    return clip(f"{kind or 'object'} with {keys}" if keys else kind or "object", 100)


def _verdict(data: dict[str, Any]) -> str:
    drill = data.get("drill") or "?"
    if data.get("error"):
        return clip(f"{drill}: error {data['error']}", 100)
    outcome = "passed" if data.get("passed") else "failed"
    results = data.get("results") or []
    failing = next((r for r in results if not r.get("ok")), None)
    line = f"{drill} {outcome} ({len(results)} checks"
    if failing is not None:
        line += f"; first failing: {failing.get('name', '?')}"
    line += ")"
    if data.get("negative_control_ok") is False:
        line += ", negative control FAILED"
    if data.get("leftovers"):
        line += f", {len(data['leftovers'])} leftovers"
    return clip(line, 100)


def _similarity(data: dict[str, Any]) -> str:
    matches = data.get("matches") or []
    if not matches:
        return "similarity: no match"
    top = matches[0]
    score = top.get("score", 0)
    try:
        shown = f"{float(score):.2f}"
    except (TypeError, ValueError):
        shown = str(score)
    flag = " DUPLICATE" if top.get("duplicate") else ""
    return clip(f"similarity: top {top.get('id', '?')} at {shown}{flag}", 100)


def _session(data: dict[str, Any]) -> str:
    sid = data.get("id") or "?"
    state = data.get("state") or ("finished" if data.get("finished") else "?")
    line = f"session {sid} {state}"
    if data.get("message"):
        line += f": {data['message']}"
    elif data.get("exam_id"):
        line += f" ({data['exam_id']})"
    return clip(line, 100)


def _docs(data: dict[str, Any]) -> str:
    hits = data.get("hits") or []
    if not hits:
        return "docs: no hits"
    first = hits[0].get("title") or hits[0].get("url") or "?"
    return clip(f"docs: {len(hits)} hits, first {first}", 100)


_BY_KIND = {
    "verdict": _verdict,
    "similarity": _similarity,
    "session": _session,
    "docs": _docs,
}
