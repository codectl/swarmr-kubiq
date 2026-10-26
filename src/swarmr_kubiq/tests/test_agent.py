"""The turn budget: a specialist that has not decided in N turns must report.

A looping subagent never returns to the commander and the run never ends; the
budget is the ceiling that makes that impossible, whatever the loop.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from swarmr_kubiq.agent import TurnBudget


@dataclass
class _Request:
    messages: list[Any]
    system_message: SystemMessage | None
    tools: list[Any] = field(default_factory=lambda: ["a-tool"])

    def override(self, **changes: Any) -> _Request:
        return replace(self, **changes)


def _request(turns: int) -> _Request:
    messages: list[Any] = [HumanMessage(content="brief")]
    messages += [AIMessage(content=f"turn {i}") for i in range(turns)]
    return _Request(messages, SystemMessage(content="You are the critic."))


def test_under_budget_the_request_passes_untouched() -> None:
    budget = TurnBudget("critic", limit=3)
    seen: list[_Request] = []
    budget.wrap_model_call(_request(2), lambda r: seen.append(r) or "ok")
    assert seen[0].tools == ["a-tool"]
    assert seen[0].system_message is not None
    assert "BUDGET" not in seen[0].system_message.content


def test_at_budget_the_tools_are_withdrawn_and_the_report_demanded() -> None:
    budget = TurnBudget("author", limit=3)
    seen: list[_Request] = []
    budget.wrap_model_call(_request(3), lambda r: seen.append(r) or "ok")
    assert seen[0].tools == []
    text = str(seen[0].system_message.content) if seen[0].system_message else ""
    assert text.startswith("You are the critic.")
    assert "TURN BUDGET EXHAUSTED (3 turns)" in text and "RESULT: FAILED" in text


def test_parallel_specialists_do_not_share_a_counter() -> None:
    """The count is the AI turns in the request, so two concurrent authors on
    one middleware instance each get the full budget."""
    budget = TurnBudget("author", limit=3)
    tools_seen = [
        budget.wrap_model_call(_request(n), lambda r: r.tools) for n in (3, 1, 3, 0)
    ]
    assert tools_seen == [[], ["a-tool"], [], ["a-tool"]]
