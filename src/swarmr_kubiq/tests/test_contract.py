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
from swarmr_kubiq.tools import (
    AUTHOR_TOOLS,
    COMMANDER_TOOLS,
    CRITIC_TOOLS,
    TESTER_TOOLS,
)

_PROVING_TOOLS = {"prove_question", "shell"}
_CLUSTER_TOOLS = _PROVING_TOOLS | {"negative_control"}
_WRITING_TOOLS = {"stage_drill"}


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
        assert names == {"commander", "author", "tester", "critic"}
        assert TEAM.roster().count("\n") == len(TEAM.members) - 1

    def test_params_name_the_checkout_and_the_exam(self) -> None:
        declared = [p.name for p in TEAM.params]
        assert declared == ["kubiq", "exam", "certification", "server", "questions"]
        assert [p.name for p in TEAM.params if p.required] == ["kubiq", "exam"]

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
    def test_only_the_tester_proves_and_has_a_shell(self) -> None:
        assert _names(TESTER_TOOLS) >= _PROVING_TOOLS
        for tools in (AUTHOR_TOOLS, CRITIC_TOOLS, COMMANDER_TOOLS):
            assert not _names(tools) & _PROVING_TOOLS

    def test_the_critic_grades_the_untouched_namespace_itself(self) -> None:
        assert {"negative_control", "diff_task"} <= _names(CRITIC_TOOLS)
        assert not _names(CRITIC_TOOLS) & _WRITING_TOOLS

    def test_author_and_critic_both_read_the_upstream_docs(self) -> None:
        """The author grounds a feature question in the docs; the critic must be
        able to check that grounding with the same source, not take it on trust."""
        for tools in (AUTHOR_TOOLS, CRITIC_TOOLS):
            assert {"search_docs", "read_doc"} <= _names(tools)

    def test_the_author_stages_but_never_touches_the_cluster(self) -> None:
        names = _names(AUTHOR_TOOLS)
        assert names >= _WRITING_TOOLS
        assert names >= {"similar_drills", "validate_drill_yaml", "search_docs"}
        assert not names & _CLUSTER_TOOLS

    def test_the_commander_holds_only_the_report_tool(self) -> None:
        assert _names(COMMANDER_TOOLS) == {TEAM.report_tool}

    def test_every_specialist_is_briefed_by_the_commander(self) -> None:
        """Nobody starts from the caller's request: the author needs a slot, the
        tester and the critic a question number."""
        roster = {member.name for member in TEAM.members} - {TEAM.orchestrator}
        assert set(_BRIEFED) == roster
        assert set(TEAM.audit_agents) <= set(_BRIEFED)

    def test_no_tool_takes_a_path_or_a_url_to_the_server(self) -> None:
        """Tools take ids, numbers and text; the checkout and the server come
        from the activated target, so a model cannot point one elsewhere."""
        for tools in (AUTHOR_TOOLS, TESTER_TOOLS, CRITIC_TOOLS):
            for t in tools:
                fields = set(t.args)
                assert not fields & {"path", "kubiq", "server", "checkout", "base_url"}, (
                    t.name,
                    fields,
                )
                assert fields <= {
                    "query",
                    "drill_id",
                    "url",
                    "drill_yaml",
                    "question_index",
                    "namespace",
                    "weight",
                    "command",
                }, (t.name, fields)

    def test_the_shell_is_the_only_tool_taking_a_command(self) -> None:
        with_command = {
            t.name
            for tools in (AUTHOR_TOOLS, TESTER_TOOLS, CRITIC_TOOLS)
            for t in tools
            if "command" in t.args
        }
        assert with_command == {"shell"}


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
