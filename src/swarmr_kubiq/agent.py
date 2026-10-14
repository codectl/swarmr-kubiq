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

from deepagents import FilesystemPermission, SubAgent, create_deep_agent
from langchain_openai import ChatOpenAI
from swarmr.core.attribution import Attribution
from swarmr.core.middleware import AnnounceName, FirstRoundBriefing
from swarmr.core.model import build_model
from swarmr.core.team import RunContext, TeamBuild

from swarmr_kubiq import prompts
from swarmr_kubiq.catalogue import banner, profile_catalogue, render_facts
from swarmr_kubiq.report_tool import FILE_REPORT_TOOL
from swarmr_kubiq.target import activate
from swarmr_kubiq.tools import (
    AUTHOR_TOOLS,
    CRITIC_TOOLS,
    TESTER_TOOLS,
    reset_staging,
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
# output; the tester's and the critic's carry a question number that the
# caller's request does not contain. Nobody starts from the symptom alone.
_BRIEFED = ("author", "tester", "critic")


def _subagents(model: ChatOpenAI, facts: str, attribution: Attribution) -> list[SubAgent]:
    """The three specialists.

    Each `description` is what the commander reads when routing, so it names
    the question the specialist answers and what it needs to be told.
    """
    return [
        {
            "name": "author",
            "description": (
                "Designs one question from a slot specification — question "
                "number, curriculum domain, weight, namespace, and a Kubernetes "
                "version when the slot must cover a recent feature — checks it "
                "for novelty against the catalogue, and stages it into the exam "
                "under construction. On a fix round, send the same question "
                "number with the tester's grader lines quoted verbatim; it may "
                "change solution, checks or setup, never the task."
            ),
            "system_prompt": prompts.author(facts),
            "middleware": [AnnounceName("author", attribution)],
            "tools": AUTHOR_TOOLS,
            "model": model,
            "permissions": _EVIDENCE_ONLY,
        },
        {
            "name": "tester",
            "description": (
                "Proves one staged question on the live kubiq server: negative "
                "control, the drill's own solution, grading, teardown proof. "
                "Send the question number ONLY. Reports the grader's checks "
                "verbatim; one tester at a time, because the server allows one "
                "session."
            ),
            "system_prompt": prompts.tester(facts),
            "middleware": [AnnounceName("tester", attribution)],
            "tools": TESTER_TOOLS,
            "model": model,
            "permissions": _EVIDENCE_ONLY,
        },
        {
            "name": "critic",
            "description": (
                "Adjudicates a proven question by trying to refute it: grades "
                "the untouched namespace itself, compares the task text with its "
                "first staging, reads the checks against the task, and scores "
                "the catalogue for a repeat. Send ONLY the question number and "
                "the author's one-line mechanic claim, never the tester's output. "
                "Returns RULING: confirmed | refuted | unproven. Must run for "
                "every question before it is filed."
            ),
            "system_prompt": prompts.critic(facts),
            "middleware": [AnnounceName("critic", attribution)],
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
    model = build_model()
    catalogue = profile_catalogue(target)
    facts = render_facts(catalogue, target)

    graph = create_deep_agent(
        model=model,
        # The commander holds no authoring or cluster tools on purpose: its
        # context stays clean, and it cannot fabricate an observation it never
        # received. The one tool it does hold files the final report.
        tools=[FILE_REPORT_TOOL],
        system_prompt=prompts.commander(facts),
        subagents=_subagents(model, facts, run.attribution),
        # Planning belongs in write_todos, which the harness already provides.
        permissions=_NO_WRITES,
        # No specialist is briefed with the caller's request: the author needs
        # the slot the commander planned, the tester and the critic need a
        # question number. Rewriting any of them to the request would send the
        # whole exam brief to a role that works one question at a time.
        middleware=[FirstRoundBriefing(exempt=_BRIEFED)],
    )
    return TeamBuild(graph=graph, banner=banner(catalogue, target))
