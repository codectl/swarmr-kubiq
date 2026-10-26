"""Talking to a running kubiq: its HTTP API, its candidate shell, its reload.

One responsibility: the transport. Nothing here knows what a drill is or what
"proven" means; `harness` does. The client wraps the session endpoints kubiq
exposes to its browser UI, `shell_exec` reaches the candidate's shell the way
the grader does (`kubectl exec` into `cand-<session>`), `reload` makes kubiq
re-read the bind-mounted checkout into its live catalogue and `check` asks the
loader whether it would, without swapping anything.

A server that is not there, lacks `/api/reload`, or has a session open that a
reload would pull the catalogue from under is a `FatalError`: retrying cannot
change any of those, and the tools stop cluster work on it.

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
    "FatalError",
    "KubiqClient",
    "KubiqError",
    "run",
]

# Where kubiq runs the candidate shell pod; fixed by kubiq's cluster module.
SESSIONS_NAMESPACE = "kubiq-sessions"

_POLL_SECONDS = 2.0
_TERMINAL_STATES = ("ready", "failed")


class FatalError(Exception):
    """The run cannot continue and retrying will not change that.

    Not a `KubiqError`: `guard` turns those into a `tool error:` the model may
    retry. This one is re-raised as a plain tool error prefixed FATAL, and the
    prompts say a FATAL result ends the proof.
    """


class KubiqError(Exception):
    """kubiq refused or failed; the message carries the server's own text."""


def run(
    argv: list[str], *, timeout: float, cwd: Path | None = None, stdin: str | None = None
) -> tuple[int, str, str]:
    """Run a command to completion; a timeout is reported as exit 124, not raised."""
    try:
        done = subprocess.run(
            argv,
            cwd=cwd,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
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

    def _call(
        self, method: str, path: str, *, allow: tuple[int, ...] = (), **kwargs: Any
    ) -> httpx.Response:
        """One request; an error status not in `allow` is a `KubiqError`."""
        try:
            response = self._http.request(method, path, **kwargs)
        except httpx.TransportError as exc:
            raise FatalError(
                f"kubiq is not running at {self.base_url}; start it with docker "
                "compose up -d in the kubiq checkout. Do not retry."
            ) from exc
        if response.is_error and response.status_code not in allow:
            body = response.text.strip() or "no body"
            raise KubiqError(f"kubiq {response.status_code} on {method} {path}: {body}")
        return response

    def reload(self) -> dict[str, Any]:
        """Re-read `drills/` and `exams/` from disk into the live catalogue.

        Returns kubiq's `{"drills": n, "exams": n}`. A loader refusal (422) is a
        `KubiqError` carrying the loader's own text and leaves the old catalogue
        in place. A session open on the server (409) or a server without the
        endpoint (404) is a `FatalError`.
        """
        response = self._call("POST", "/api/reload", allow=(404, 409, 422))
        if response.status_code == 404:
            raise FatalError(self._no_endpoint("/api/reload"))
        if response.status_code == 409:
            raise FatalError(
                f"another session is open on kubiq at {self.base_url}; a reload would "
                "change the catalogue under it. Proofs cannot run until it is "
                "finished. Do not retry."
            )
        if response.status_code == 422:
            raise KubiqError(_error_text(response))
        return response.json()

    def check(self) -> str:
        """Ask the loader whether the checkout loads; "" when it does, else its text.

        Never swaps the catalogue, so it is allowed while a session runs.
        """
        response = self._call("POST", "/api/check", allow=(404, 422))
        if response.status_code == 404:
            raise FatalError(self._no_endpoint("/api/check"))
        if response.status_code == 422:
            return _error_text(response)
        return ""

    def _no_endpoint(self, path: str) -> str:
        return (
            f"kubiq at {self.base_url} has no {path}: rebuild it from a checkout with "
            "hot reload (docker compose up -d --build in the kubiq checkout). "
            "Do not retry."
        )

    def exams(self) -> list[dict[str, Any]]:
        return self._call("GET", "/api/exams").json()

    def sessions(self) -> list[dict[str, Any]]:
        return self._call("GET", "/api/sessions").json()

    def session(self, sid: str) -> dict[str, Any]:
        return self._call("GET", f"/api/sessions/{sid}").json()

    def create_session(
        self, exam_id: str, questions: list[int] | None = None
    ) -> dict[str, Any]:
        """Open a session; with `questions` (0-based indexes) kubiq provisions
        only those questions' namespaces and files."""
        body: dict[str, Any] = {"exam_id": exam_id}
        if questions is not None:
            body["questions"] = questions
        return self._call("POST", "/api/sessions", json=body).json()

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

    def grade(self, sid: str, question: int) -> dict[str, Any]:
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


def _error_text(response: httpx.Response) -> str:
    """kubiq's `{"error": ...}` body as text; the raw body when it is not JSON."""
    try:
        error = response.json().get("error")
    except (ValueError, AttributeError):
        error = None
    return str(error) if error else response.text.strip() or "no body"
