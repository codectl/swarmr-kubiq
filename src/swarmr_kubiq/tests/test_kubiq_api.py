"""The kubiq client's one destructive call: restarting the server."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from swarmr_kubiq import kubiq_api
from swarmr_kubiq.kubiq_api import KubiqError


class _Client:
    base_url = "http://kubiq.test"

    def __init__(self, sessions: list[dict[str, Any]], exams: list[dict[str, Any]]):
        self._sessions = sessions
        self._exams = exams

    def sessions(self) -> list[dict[str, Any]]:
        return self._sessions

    def exams(self) -> list[dict[str, Any]]:
        return self._exams


def test_restart_refuses_while_a_session_is_open(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        kubiq_api, "_run", lambda argv, **kw: calls.append(argv) or (0, "", "")
    )
    client = _Client(sessions=[{"id": "abc", "state": "ready"}], exams=[])
    with pytest.raises(KubiqError, match="session\\(s\\) abc are open"):
        kubiq_api.restart_server(Path("."), client, "ckad-mock-7", 21)  # type: ignore[arg-type]
    assert calls == [], "docker compose must not run over an open session"


def test_restart_runs_compose_and_waits_for_the_exam(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        kubiq_api, "_run", lambda argv, **kw: calls.append(argv) or (0, "", "")
    )
    monkeypatch.setattr(kubiq_api, "_sleep", lambda s: None)
    client = _Client(sessions=[], exams=[{"id": "ckad-mock-7", "questions": 21}])
    entry = kubiq_api.restart_server(Path("."), client, "ckad-mock-7", 21)  # type: ignore[arg-type]
    assert entry["id"] == "ckad-mock-7"
    assert calls[0][:3] == ["docker", "compose", "up"]
