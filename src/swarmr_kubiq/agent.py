"""Assembly of the exam-authoring team.

Stock Deep Agents: one `create_deep_agent` commander plus three `SubAgent`
specialists reached through the built-in `task` tool. Findings return as the
task result; the shared filesystem carries only bulk evidence. The exam itself
is written into the kubiq checkout by the tools, never by an agent's
filesystem.

Nothing about the target is hardcoded. `catalogue` profiles the checkout at
build time and the profile is injected into every prompt, so the same code
authors any exam against any kubiq checkout.
"""

from __future__ import annotations

from typing import Any

from deepagents import (
    FilesystemMiddleware,
    FilesystemPermission,
    SubAgent,
    create_deep_agent,
)
from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import AIMessage, SystemMessage
from langchain_openai import ChatOpenAI
from swarmr.core.attribution import Attribution
from swarmr.core.middleware import AnnounceName, FirstRoundBriefing
from swarmr.core.model import build_model
from swarmr.core.team import RunContext, TeamBuild

from swarmr_kubiq import prompts
from swarmr_kubiq.catalogue import banner, profile_catalogue, render_facts
from swarmr_kubiq.target import activate
from swarmr_kubiq.tools import (
    AUTHOR_TOOLS,
    COMMANDER_TOOLS,
    CRITIC_TOOLS,
    reset_staging,
    resume_staging,
)

__all__ = ["build", "profile_target"]

# Rules are first-match-wins, and a subagent that omits `permissions` inherits
# the parent's, so every role states its own. Paths are absolute in the agent's
# virtual filesystem.
_NO_WRITES = [FilesystemPermission(operations=["write"], paths=["/**"], mode="deny")]

# Specialists may write only their evidence file. Anything else is scratch
# nobody reads, and it clutters the delegation trail.
_EVIDENCE_ONLY = [
    FilesystemPermission(
        operations=["write"], paths=["/evidence/**", "/**/evidence/**"], mode="allow"
    ),
    FilesystemPermission(operations=["write"], paths=["/**"], mode="deny"),
]

# Every specialist's briefing must pass through the harness untouched: the
# author's carries the slot specification and, on a fix round, the grader
# output; the critic's carries question numbers that the caller's request does
# not contain. Nobody starts from the symptom alone.
_BRIEFED = ("author", "critic")


# The specialists' scratch filesystem holds nothing: the catalogue comes from
# catalogue_search/read_drill, drafts go straight to stage_drill. Every
# `ls`/`glob`/`grep`/`write_file` a model tried was a wasted, denied turn, so
# only `read_file` (which deepagents requires) stays visible. Same name as the
# default filesystem middleware, so deepagents swaps it in and keeps the
# per-role permissions.
def _read_only_fs() -> Any:
    return FilesystemMiddleware(tools=["read_file"])


# Model turns a specialist may take per briefing before its tools are taken
# away and it must report. A specialist that has not decided in this many
# turns is looping (re-reading a page, re-searching the corpus), and a loop in
# a subagent never returns to the commander: the run hangs. The author's
# budget covers a design, two proofs and the fixes between them; the critic's
# covers seven questions at ~3 calls each.
_TURN_BUDGET = {"author": 40, "critic": 30}


class TurnBudget(AgentMiddleware):
    """Withdraw the tools after `limit` model turns so the specialist answers.

    Stateless: the turns taken are the AI messages already in the request, so
    concurrent specialists of the same role never share a counter.
    """

    def __init__(self, agent_name: str, limit: int) -> None:
        super().__init__()
        self.agent_name = agent_name
        self.limit = limit

    @property
    def name(self) -> str:
        return f"TurnBudget[{self.agent_name}]"

    def wrap_model_call(self, request: Any, handler: Any) -> Any:
        turns = sum(isinstance(m, AIMessage) for m in request.messages)
        if turns < self.limit:
            return handler(request)
        notice = (
            f"\n\nTURN BUDGET EXHAUSTED ({self.limit} turns). Your tools are "
            "withdrawn. Report NOW in the required format from what you already "
            "observed; anything undecided is reported as such (an author reports "
            "RESULT: FAILED with the last grader lines, a critic rules `unproven` "
            "with the gap named). Do not apologise, do not plan further work."
        )
        base = request.system_message.content if request.system_message else ""
        return handler(
            request.override(
                tools=[], system_message=SystemMessage(content=f"{base}{notice}")
            )
        )


def _subagents(
    model: ChatOpenAI, facts: dict[str, str], attribution: Attribution
) -> list[SubAgent]:
    """The three specialists.

    Each `description` is what the commander reads when routing, so it names
    the question the specialist answers and what it needs to be told.
    """
    return [
        {
            "name": "author",
            "description": (
                "Designs one question from a slot specification — question "
                "number, curriculum domain and competency, namespace, and a "
                "Kubernetes version when the slot must cover a recent feature — "
                "checks it for novelty against the catalogue, and stages it into "
                "the exam under construction (weight and namespace come from the "
                "plan). On a fix round, send the same question number with the "
                "grader's failing lines quoted verbatim; it may change solution, "
                "checks or setup, never the task, and it proves its own fix with "
                "prove_questions, returning PROVEN or FAILED with the grader lines."
            ),
            "system_prompt": prompts.author(facts["author"]),
            "middleware": [
                _read_only_fs(),
                AnnounceName("author", attribution),
                TurnBudget("author", _TURN_BUDGET["author"]),
            ],
            "tools": AUTHOR_TOOLS,
            "model": model,
            "permissions": _EVIDENCE_ONLY,
        },
        {
            "name": "critic",
            "description": (
                "Adjudicates a BATCH of proven questions (send 5 to 7 numbers) by "
                "trying to refute each from the recorded verdict: every check "
                "must fail untouched and pass after the solution within budget, "
                "the checks must grade the task as written, the cited docs must "
                "say what the drill claims, the slot must fit the curriculum. It "
                "holds no cluster tool. Send ONLY the question numbers, never "
                "grader output. Returns one RULING per question: confirmed | "
                "refuted | unproven. Every question needs a ruling before it is "
                "filed."
            ),
            "system_prompt": prompts.critic(facts["critic"]),
            "middleware": [
                _read_only_fs(),
                AnnounceName("critic", attribution),
                TurnBudget("critic", _TURN_BUDGET["critic"]),
            ],
            "tools": CRITIC_TOOLS,
            "model": model,
            # The critic reports a ruling; it has nothing to persist.
            "permissions": _NO_WRITES,
        },
    ]


def profile_target(run: RunContext) -> str:
    """Describe the target checkout without building an agent.

    Separate from `build` because building constructs a model client: an
    operator asking "which checkout am I pointed at" should not need a model
    API key to find out.
    """
    target = activate(dict(run.params))
    return banner(profile_catalogue(target), target)


def build(run: RunContext) -> TeamBuild:
    """Profile the checkout, then build the team around what is actually there.

    The target is activated first, in this thread: the graph streams from here
    and every tool call it fans out inherits this context, so `kubiq`, `exam`
    and `server` from the call are what the tools write to and prove against.
    """
    target = activate(dict(run.params))
    reset_staging()
    resumed = resume_staging(target)
    model = build_model()
    catalogue = profile_catalogue(target)
    facts = {
        "author": render_facts(catalogue, target, resumed=resumed),
        "critic": render_facts(catalogue, target, drills=False),
    }

    graph = create_deep_agent(
        model=model,
        # The commander holds no authoring tools: it cannot write a question
        # itself. It does hold the proof tools, because a proof is a harness
        # run with a deterministic result — no model needs to sit between
        # "prove" and "verdict", and one fewer dispatch per question.
        tools=COMMANDER_TOOLS,
        system_prompt=prompts.commander(facts["author"]),
        subagents=_subagents(model, facts, run.attribution),
        # Planning belongs in write_todos, which the harness already provides.
        permissions=_NO_WRITES,
        # No specialist is briefed with the caller's request: the author needs
        # the slot the commander planned, the critic a batch of question
        # numbers. Rewriting either to the request would send the whole exam
        # brief to a role that works one question at a time.
        middleware=[FirstRoundBriefing(exempt=_BRIEFED)],
    )
    return TeamBuild(graph=graph, banner=banner(catalogue, target))
