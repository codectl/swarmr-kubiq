"""Talking to a running kubiq: its HTTP API, its candidate shell, its restart.

One responsibility: the transport. Nothing here knows what a drill is or what
"proven" means; `harness` does. The client wraps the session endpoints kubiq
exposes to its browser UI, `shell_exec` reaches the candidate's shell the way
the grader does (`kubectl exec` into `cand-<session>`), and `restart_server`
reloads the checkout because kubiq reads drills and exams from disk only at
startup.

Every subprocess goes through the module-level `_run`, every pause through
`_sleep`, so tests can fake both without a cluster or a clock.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Any

import httpx

__all__ = [
    "SESSIONS_NAMESPACE",
    "KubiqClient",
    "KubiqError",
    "restart_server",
    "run",
]

# Where kubiq runs the candidate shell pod; fixed by kubiq's cluster module.
SESSIONS_NAMESPACE = "kubiq-sessions"

_POLL_SECONDS = 2.0
_TERMINAL_STATES = ("ready", "failed")


class KubiqError(Exception):
    """kubiq refused or failed; the message carries the server's own text."""


def run(
    argv: list[str], *, timeout: float, cwd: Path | None = None
) -> tuple[int, str, str]:
    """Run a command to completion; a timeout is reported as exit 124, not raised."""
    try:
        done = subprocess.run(
            argv, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False
        )
    except FileNotFoundError as exc:
        raise KubiqError(f"`{argv[0]}` is not on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout.decode() if isinstance(exc.stdout, bytes) else exc.stdout or ""
        err = exc.stderr.decode() if isinstance(exc.stderr, bytes) else exc.stderr or ""
        return 124, out, f"{err}\ntimed out after {timeout:g}s".strip()
    return done.returncode, done.stdout, done.stderr


_run = run
_sleep = time.sleep


class KubiqClient:
    """The session endpoints of one kubiq server, plus the candidate shell."""

    def __init__(self, base_url: str, timeout: float = 30) -> None:
        self.base_url = base_url.rstrip("/")
        self._http = httpx.Client(base_url=self.base_url, timeout=timeout)

    def _call(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        response = self._http.request(method, path, **kwargs)
        if response.is_error:
            body = response.text.strip() or "no body"
            raise KubiqError(f"kubiq {response.status_code} on {method} {path}: {body}")
        return response

    def exams(self) -> list[dict[str, Any]]:
        return self._call("GET", "/api/exams").json()

    def sessions(self) -> list[dict[str, Any]]:
        return self._call("GET", "/api/sessions").json()

    def session(self, sid: str) -> dict[str, Any]:
        return self._call("GET", f"/api/sessions/{sid}").json()

    def create_session(self, exam_id: str) -> dict[str, Any]:
        return self._call("POST", "/api/sessions", json={"exam_id": exam_id}).json()

    def wait_ready(self, sid: str, timeout: float = 300) -> dict[str, Any]:
        """Poll until the shell and the question namespaces exist.

        `failed` carries kubiq's own message (an image that would not pull, a
        namespace that already existed); it is raised verbatim.
        """
        deadline = time.monotonic() + timeout
        view = self.session(sid)
        while view.get("state") not in _TERMINAL_STATES:
            if time.monotonic() >= deadline:
                raise KubiqError(
                    f"session {sid} still {view.get('state', '?')} after {timeout:g}s"
                )
            _sleep(_POLL_SECONDS)
            view = self.session(sid)
        if view.get("state") == "failed":
            message = view.get("message") or "no message"
            raise KubiqError(f"session {sid} failed to provision: {message}")
        return view

    def check(self, sid: str, question: int) -> dict[str, Any]:
        """Grade one question now: {passed, results: [{name, ok, detail}]}."""
        return self._call(
            "POST", f"/api/sessions/{sid}/check", params={"question": question}
        ).json()

    def finish(self, sid: str, timeout: float = 600) -> dict[str, Any]:
        """End the exam and wait for the grade; returns the `result` object."""
        view = self._call("POST", f"/api/sessions/{sid}/finish").json()
        deadline = time.monotonic() + timeout
        while not view.get("finished"):
            if view.get("state") == "failed":
                raise KubiqError(
                    f"session {sid} failed while grading: {view.get('message') or '?'}"
                )
            if time.monotonic() >= deadline:
                raise KubiqError(f"session {sid} not graded after {timeout:g}s")
            _sleep(_POLL_SECONDS)
            view = self.session(sid)
        result = view.get("result")
        if not isinstance(result, dict):
            raise KubiqError(f"session {sid} finished without a result")
        return result

    def delete(self, sid: str) -> None:
        self._call("DELETE", f"/api/sessions/{sid}")

    def shell_exec(
        self,
        sid: str,
        cmd: str,
        namespace: str | None = None,
        timeout: float = 400,
    ) -> tuple[int, str, str]:
        """Run a script in the candidate's shell, as the candidate would.

        The shell pod carries the candidate's kubeconfig, so `kubectl` inside it
        sees exactly what the candidate sees. With `namespace`, the current
        context is switched first, the way the exam text tells the candidate to.
        `KUBECONFIG` in this process's environment selects the cluster.
        """
        script = cmd
        if namespace:
            script = (
                f"kubectl config set-context --current --namespace={namespace} "
                f">/dev/null; {cmd}"
            )
        argv = [
            "kubectl",
            "exec",
            "-n",
            SESSIONS_NAMESPACE,
            f"cand-{sid}",
            "--",
            "bash",
            "-lc",
            script,
        ]
        return _run(argv, timeout=timeout)


def restart_server(
    checkout: Path,
    client: KubiqClient,
    exam_id: str,
    questions: int,
    timeout: float = 600,
) -> dict[str, Any]:
    """Rebuild and recreate the kubiq container so it reloads the checkout.

    Returns the `/api/exams` entry once the server answers and lists `exam_id`
    with `questions` questions. Anything else — compose failing, the server
    never coming up, the exam missing or short — raises with the tail of the
    compose logs, which is where kubiq prints the drill it refused.

    Refuses while any session exists: a recreate kills it, and the server has
    one slot that a person may be sitting an exam in. The caller closes its own
    sessions first; whatever remains is not ours to end.
    """
    try:
        open_sessions = client.sessions()
    except (httpx.HTTPError, KubiqError):
        open_sessions = []  # server down: nothing to protect, restart is the fix
    if open_sessions:
        ids = ", ".join(str(s.get("id", "?")) for s in open_sessions)
        raise KubiqError(
            f"not restarting kubiq: session(s) {ids} are open on {client.base_url} "
            "and a restart would end them. Wait for the exam to finish."
        )
    rc, _, err = _run(
        ["docker", "compose", "up", "-d", "--build", "--force-recreate"],
        timeout=timeout,
        cwd=checkout,
    )
    if rc != 0:
        tail = err.strip()[-600:]
        logs = _compose_logs(checkout)
        raise KubiqError(f"docker compose up exited {rc}: {tail}\n{logs}")
    deadline = time.monotonic() + timeout
    seen = "no answer yet"
    while time.monotonic() < deadline:
        try:
            exams = client.exams()
        except (httpx.HTTPError, KubiqError) as exc:
            seen = f"{type(exc).__name__}: {exc}"
        else:
            entry = next((e for e in exams if e.get("id") == exam_id), None)
            if entry is None:
                seen = f"exam {exam_id!r} not listed"
            elif entry.get("questions") != questions:
                seen = f"exam {exam_id!r} lists {entry.get('questions')} questions"
            else:
                return entry
        _sleep(_POLL_SECONDS)
    raise KubiqError(
        f"kubiq did not come back with {exam_id!r} ({questions} questions) within "
        f"{timeout:g}s; last seen: {seen}\n{_compose_logs(checkout)}"
    )


def _compose_logs(checkout: Path) -> str:
    rc, out, err = _run(
        ["docker", "compose", "logs", "--no-color", "--tail", "20"],
        timeout=60,
        cwd=checkout,
    )
    text = out if rc == 0 else err
    lines = text.strip().splitlines()[-20:]
    return "docker compose logs (last 20 lines):\n" + "\n".join(lines)
