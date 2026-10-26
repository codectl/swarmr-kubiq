"""What this team promises the shared machinery, and its own guardrails.

`core` renders and records without knowing any role name, so the team must
declare its own vocabulary. Filesystem permissions are the other half of the
non-mutation guarantee: prompts ask, permissions enforce. The tool-set
assertions pin which roles may touch the cluster at all.
"""

from __future__ import annotations

import subprocess
import sys

from langchain_core.tools import BaseTool
from swarmr.core.team import Lazy
from swarmr.teams import get
from swarmr.teams import names as registered_names

from swarmr_kubiq import TEAM
from swarmr_kubiq.agent import _BRIEFED, _EVIDENCE_ONLY, _NO_WRITES
from swarmr_kubiq.digest import digest_result, is_tool_error
from swarmr_kubiq.tools import AUTHOR_TOOLS, COMMANDER_TOOLS, CRITIC_TOOLS

_PROVING_TOOLS = {"prove_all", "prove_questions"}
_WRITING_TOOLS = {"stage_drill", "plan_exam", "finalize_exam"}
_DOCS_TOOLS = {"search_docs", "read_doc"}


def _names(tools: list[BaseTool]) -> set[str]:
    return {t.name for t in tools}


class TestVocabulary:
    def test_team_declares_every_name_core_uses(self) -> None:
        assert TEAM.name == "exam_author"
        assert TEAM.orchestrator == "commander"
        assert TEAM.audit_agents == ("critic",)
        assert TEAM.report_tool == "file_exam_report"
        assert TEAM.digest is digest_result
        assert TEAM.is_error is is_tool_error

    def test_roster_names_the_specialists(self) -> None:
        names = {member.name for member in TEAM.members}
        assert names == {"commander", "author", "critic"}
        assert TEAM.roster().count("\n") == len(TEAM.members) - 1

    def test_params_name_the_checkout_and_the_exam(self) -> None:
        declared = [p.name for p in TEAM.params]
        assert declared == [
            "kubiq",
            "exam",
            "certification",
            "server",
            "version",
            "questions",
        ]
        assert [p.name for p in TEAM.params if p.required] == ["exam"]

    def test_registry_exposes_the_team(self) -> None:
        assert get(TEAM.name) is TEAM
        assert TEAM.name in registered_names()

    def test_the_heavyweight_fields_are_declared_lazily(self) -> None:
        """Importing this team must not import its agent stack."""
        for name in ("build", "profile", "render_report"):
            declared = getattr(TEAM, name)
            assert isinstance(declared, Lazy), name
            assert declared.target.startswith("swarmr_kubiq.")
            declared.resolve()


class TestToolSets:
    def test_the_author_designs_stages_and_proves_its_own_fix(self) -> None:
        """A proof is a harness run with a deterministic result, so the author
        re-proves a fix itself instead of a round trip through the commander."""
        assert _names(AUTHOR_TOOLS) == {
            "catalogue_search",
            "read_drill",
            "search_docs",
            "read_doc",
            "stage_drill",
            "prove_questions",
            "grader_details",
        }

    def test_the_critic_reads_verdicts_drills_and_docs_and_holds_no_cluster_tool(
        self,
    ) -> None:
        """The harness records the untouched grade (`results_before`) during the
        proof; the critic reads it rather than opening a session of its own."""
        assert _names(CRITIC_TOOLS) == {
            "grader_details",
            "read_drill",
            "catalogue_search",
            "search_docs",
            "read_doc",
        }
        assert not _names(CRITIC_TOOLS) & (_PROVING_TOOLS | _WRITING_TOOLS)

    def test_the_commander_plans_proves_finalises_and_reports_but_cannot_author(
        self,
    ) -> None:
        assert _names(COMMANDER_TOOLS) == {
            TEAM.report_tool,
            "plan_exam",
            "prove_all",
            "prove_questions",
            "grader_details",
            "finalize_exam",
        }
        assert "stage_drill" not in _names(COMMANDER_TOOLS)

    def test_author_and_critic_both_read_the_upstream_docs(self) -> None:
        """The author grounds a feature question in the docs; the critic must be
        able to check that grounding with the same source, not take it on trust."""
        for tools in (AUTHOR_TOOLS, CRITIC_TOOLS):
            assert _names(tools) >= _DOCS_TOOLS

    def test_every_specialist_is_briefed_by_the_commander(self) -> None:
        """Nobody starts from the caller's request: the author needs a slot, the
        critic question numbers."""
        roster = {member.name for member in TEAM.members} - {TEAM.orchestrator}
        assert set(_BRIEFED) == roster
        assert set(TEAM.audit_agents) <= set(_BRIEFED)

    def test_no_tool_takes_a_path_a_command_or_a_url_to_the_server(self) -> None:
        """Tools take ids, numbers, slots and text; the checkout and the server
        come from the activated target, so a model cannot point one elsewhere,
        and nothing runs a command of the model's choosing."""
        for tools in (AUTHOR_TOOLS, CRITIC_TOOLS, COMMANDER_TOOLS):
            for t in tools:
                if t.name == TEAM.report_tool:
                    continue
                fields = set(t.args)
                assert not fields & {
                    "path",
                    "kubiq",
                    "server",
                    "checkout",
                    "base_url",
                    "command",
                }, (t.name, fields)
                assert fields <= {
                    "query",
                    "drill_id",
                    "url",
                    "drill_yaml",
                    "question_index",
                    "question_indexes",
                    "slots",
                    "confirmed_questions",
                    "summary",
                }, (t.name, fields)


class TestPermissions:
    def test_orchestrator_and_critic_grant_no_write_anywhere(self) -> None:
        assert [(r.operations, r.paths, r.mode) for r in _NO_WRITES] == [
            (["write"], ["/**"], "deny")
        ]

    def test_specialists_allow_evidence_before_the_catch_all_deny(self) -> None:
        """Rules are first-match-wins, so the order is the policy."""
        assert [r.mode for r in _EVIDENCE_ONLY] == ["allow", "deny"]
        assert _EVIDENCE_ONLY[0].paths == ["/evidence/**", "/**/evidence/**"]
        assert _EVIDENCE_ONLY[1].paths == ["/**"]
        assert all(rule.operations == ["write"] for rule in _EVIDENCE_ONLY)


def test_publishing_the_mcp_surface_imports_no_team_implementation() -> None:
    """A subprocess, because this process has already imported the agent module."""
    probe = (
        "import sys\n"
        "from swarmr.server import build_server\n"
        "build_server()\n"
        "heavy = [m for m in sys.modules if m.endswith('swarmr_kubiq.agent')]\n"
        "print(heavy, len(sys.modules))\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )
    loaded, _, count = done.stdout.strip().rpartition(" ")
    assert loaded == "[]", done.stdout
    assert int(count) < 1500, done.stdout
