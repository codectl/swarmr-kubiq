"""Exam-authoring team: a Kubernetes mock exam for kubiq, every question proven.

A commander plans the slots, an author writes one question at a time, a
tester proves each one on a live kubiq session — negative control, the
drill's own solution, grading, teardown — and a critic independently tries to
refute every proven question before it is filed. Input is an exam id and a
certification; output is the exam written into the kubiq checkout, with a
per-question verdict and the branch to open a pull request from.

The team declares itself here. Deleting this package and its tests removes it
completely: discovery is by entry point, so nothing else in the tree refers to
it, and uninstalling the distribution unregisters it.

Importing this module must stay cheap. It is what `teams --list` and the MCP
server read to publish the team, so anything that pulls in the agent framework
or the model SDK is declared with `Lazy` and loaded on first use. Only the
fields `core` may call before a run — the digest and the error test, both of
which are plain string handling — are imported directly.
"""

from __future__ import annotations

from swarmr.core.team import Lazy, Member, Team

from swarmr_kubiq.digest import digest_result, is_tool_error
from swarmr_kubiq.prompts import SWEEP_REQUEST
from swarmr_kubiq.target import PARAMS

__all__ = ["TEAM"]

_MODULE = "swarmr_kubiq"

TEAM = Team(
    name="exam_author",
    summary="Author a Kubernetes mock exam for kubiq and prove every question live.",
    description=(
        "Write a new CKAD, CKA or CKS mock exam into a kubiq checkout and prove "
        "every question on a running kubiq cluster before it is filed. A commander "
        "plans the slots against the official curriculum shares with the real "
        "exam's weights, then for each question an author designs a drill in the "
        "official exam voice and checks it for novelty against the whole "
        "catalogue, a tester proves it in a fresh session — the grader must fail "
        "on the untouched namespace, pass after the drill's own solution, and "
        "leave nothing behind — and a critic independently tries to refute it: "
        "too-permissive checks, a task that changed after a fix, a repeat of an "
        "existing question. Use for 'write ckad-mock-7', 'add a CKS mock with 16 "
        "questions', 'author an exam that covers the 1.34 features', 'generate "
        "more questions in the domains the catalogue lacks', and 'prove this "
        "draft exam before I open the PR'. Writes the exam and its drills into "
        "the checkout and restarts the kubiq server to load them; needs kubectl "
        "access to the cluster kubiq runs on. Files only questions the critic "
        "confirmed; a slot that could not be proven is reported as dropped, never "
        "filled with an unproven question."
    ),
    build=Lazy(f"{_MODULE}.agent:build"),
    profile=Lazy(f"{_MODULE}.agent:profile_target"),
    params=PARAMS,
    default_request=SWEEP_REQUEST,
    report_tool="file_exam_report",
    orchestrator="commander",
    audit_agents=("critic",),
    digest=digest_result,
    is_error=is_tool_error,
    render_report=Lazy(f"{_MODULE}.report_tool:render_report_args"),
    members=(
        Member("commander", "plans the slots; holds no authoring or cluster tools"),
        Member("author", "designs one question, checks novelty, stages it"),
        Member("tester", "proves one question in a live kubiq session"),
        Member("critic", "independently tries to refute a proven question"),
    ),
    prompt_hint="CKAD, 21 questions, include two questions on Kubernetes 1.33 features",
)
