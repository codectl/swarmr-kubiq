"""Upstream documentation through the lodestar MCP server.

One responsibility: `search_docs` and `get_page` as plain Python calls, so the
author can ground a "what's new in 1.3x" question in the indexed Kubernetes
docs rather than in recollection. Lodestar is a JSON-RPC MCP server; on this
machine it runs over stdio (the `lodestar` entry of `~/.omp/agent/mcp.json`),
elsewhere it may sit behind streamable HTTP (`LODESTAR_URL`). Both are spoken
here. Every failure degrades to an empty result with the cause in `.note`,
prefixed `tool error:` — docs are an aid, never a reason to abort a run.
"""

from __future__ import annotations

import json
import os
import re
import select
import shlex
import subprocess
import threading
from pathlib import Path
from typing import Any

import httpx

__all__ = ["DEFAULT_URL", "DocsClient", "whats_new"]

DEFAULT_URL = "http://localhost:8765/mcp"
_MCP_JSON = Path.home() / ".omp" / "agent" / "mcp.json"
_HIT = re.compile(
    r"^\[[\w-]+\] (?P<title>.+)\n(?P<url>https?://\S+)\s+\(document_id: (?P<id>\d+)",
    re.MULTILINE,
)
# What follows the last hit of a query: the next query label, the grounding
# footer, or the truncation notice.
_TRAILERS = ('\nQuery "', "\n---\n", "\n[truncated:")


def _configured_command() -> tuple[list[str], dict[str, str]] | None:
    """The stdio launch line: LODESTAR_COMMAND, else the omp MCP registration."""
    if raw := os.environ.get("LODESTAR_COMMAND"):
        return shlex.split(raw), {}
    try:
        entry = json.loads(_MCP_JSON.read_text())["mcpServers"]["lodestar"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if entry.get("type", "stdio") != "stdio" or not entry.get("command"):
        return None
    return [entry["command"], *entry.get("args", [])], dict(entry.get("env", {}))


class DocsClient:
    """One MCP session against lodestar; `search` and `page` never raise."""

    def __init__(
        self,
        url: str | None = None,
        *,
        client: httpx.Client | None = None,
        timeout: float = 30.0,
    ) -> None:
        # One subprocess pipe (or one MCP session) serves every specialist of
        # the run, and they call concurrently: the lock keeps JSON-RPC frames
        # whole, and `note` is per thread so a colleague's miss is not yours.
        self._lock = threading.Lock()
        self._local = threading.local()
        self.timeout = timeout
        self.url = url or os.environ.get("LODESTAR_URL")
        self._http = client
        self._command = None if self.url or client else _configured_command()
        if not self.url and not self._command:
            self.url = DEFAULT_URL
        self._proc: subprocess.Popen[bytes] | None = None
        self._buf = b""
        self._session: str | None = None
        self._next_id = 0

    @property
    def note(self) -> str:
        """Why the last call from THIS thread returned nothing, or ""."""
        return getattr(self._local, "note", "")

    @note.setter
    def note(self, value: str) -> None:
        self._local.note = value

    # -- public ----------------------------------------------------------------

    # lodestar also indexes Argo, Flux and Cilium; an exam question cites only
    # kubernetes.io, and the other corpora cost the author calls on noise.
    SOURCES = ("kubernetes",)

    def search(self, query: str, limit: int = 8) -> list[dict[str, Any]]:
        """Ranked kubernetes.io passages: [{title, url, snippet, document_id}], or []."""
        text = self._tool(
            "search_docs",
            {"query": query, "limit": limit, "sources": list(self.SOURCES)},
        )
        return _parse_hits(text) if text else []

    def page(
        self,
        url_or_id: str | int,
        *,
        fields: tuple[str, ...] = ("summary", "headings"),
        section: str | None = None,
    ) -> str:
        """One indexed page (summary + outline by default), or ""."""
        args: dict[str, Any] = {"fields": list(fields)}
        if isinstance(url_or_id, int) or str(url_or_id).isdigit():
            args["document_id"] = int(url_or_id)
        else:
            args["url"] = str(url_or_id)
        if section:
            args["section"] = section
        return self._tool("get_page", args)

    def close(self) -> None:
        if self._proc:
            self._proc.kill()
            self._proc = None

    # -- transport -------------------------------------------------------------

    def _tool(self, name: str, arguments: dict[str, Any]) -> str:
        with self._lock:
            return self._tool_locked(name, arguments)

    def _tool_locked(self, name: str, arguments: dict[str, Any]) -> str:
        try:
            if self._session is None:
                self._handshake()
            result = self._call("tools/call", {"name": name, "arguments": arguments})
        except Exception as exc:
            # Transport or protocol failure: drop the session so the next call
            # starts clean (a fresh handshake, a fresh subprocess).
            self.note = f"tool error: lodestar {name} failed: {type(exc).__name__}: {exc}"
            self.close()
            self._session = None
            return ""
        content = result.get("content", [])
        text = "\n".join(c.get("text", "") for c in content if c.get("type") == "text")
        if result.get("isError"):
            # The server answered; the miss is feedback (unknown page, empty index).
            self.note = f"tool error: lodestar {name}: {text.strip()[:300]}"
            return ""
        self.note = ""
        return text

    def _handshake(self) -> None:
        self._call(
            "initialize",
            {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "swarmr-kubiq", "version": "0.1"},
            },
        )
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self._session = self._session or "stdio"

    def _call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._next_id += 1
        reply = self._send(
            {"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params}
        )
        if reply is None:
            raise RuntimeError(f"no reply to {method}")
        if "error" in reply:
            raise RuntimeError(reply["error"].get("message", reply["error"]))
        return reply.get("result") or {}

    def _send(self, msg: dict[str, Any]) -> dict[str, Any] | None:
        return self._send_stdio(msg) if self._command else self._send_http(msg)

    def _send_http(self, msg: dict[str, Any]) -> dict[str, Any] | None:
        if self._http is None:
            self._http = httpx.Client(timeout=self.timeout)
        headers = {"Accept": "application/json, text/event-stream"}
        if self._session and self._session != "stdio":
            headers["Mcp-Session-Id"] = self._session
        resp = self._http.post(self.url or DEFAULT_URL, json=msg, headers=headers)
        resp.raise_for_status()
        if sid := resp.headers.get("Mcp-Session-Id"):
            self._session = sid
        if "id" not in msg:
            return None
        body = resp.text
        if resp.headers.get("content-type", "").startswith("text/event-stream"):
            for line in body.splitlines():
                if line.startswith("data:"):
                    data = json.loads(line[5:])
                    if data.get("id") == msg["id"]:
                        return data
            raise RuntimeError("no matching SSE event")
        return json.loads(body)

    def _send_stdio(self, msg: dict[str, Any]) -> dict[str, Any] | None:
        assert self._command is not None
        if self._proc is None or self._proc.poll() is not None:
            argv, env = self._command
            self._proc = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env={**os.environ, **env},
            )
            self._buf = b""
        assert self._proc.stdin and self._proc.stdout
        self._proc.stdin.write(json.dumps(msg).encode() + b"\n")
        self._proc.stdin.flush()
        if "id" not in msg:
            return None
        out = self._proc.stdout
        while True:
            while b"\n" in self._buf:
                line, _, self._buf = self._buf.partition(b"\n")
                if line.strip():
                    data = json.loads(line)
                    if data.get("id") == msg["id"]:
                        return data
            ready, _, _ = select.select([out], [], [], self.timeout)
            if not ready:
                raise TimeoutError(f"lodestar silent for {self.timeout}s")
            chunk = os.read(out.fileno(), 1 << 16)
            if not chunk:
                raise RuntimeError("lodestar exited")
            self._buf += chunk


def _parse_hits(text: str) -> list[dict[str, Any]]:
    """Hit blocks of a `search_docs` response: `[src] Title > Heading`, URL, snippet."""
    matches = list(_HIT.finditer(text))
    hits: list[dict[str, Any]] = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        block = text[m.end() : end]
        snippet = block.split("\n", 1)[1] if "\n" in block else ""
        for marker in _TRAILERS:
            snippet = snippet.split(marker, 1)[0]
        snippet = snippet.strip()
        hits.append(
            {
                "title": m["title"].strip(),
                "url": m["url"],
                "snippet": snippet,
                "document_id": int(m["id"]),
            }
        )
    return hits


def whats_new(version: str, client: DocsClient | None = None) -> list[dict[str, Any]]:
    """Release-note material for a Kubernetes minor, deduplicated by URL."""
    c = client or DocsClient()
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for query in (
        f"Kubernetes {version} what's new",
        f"feature gate graduated {version}",
        f"deprecated {version}",
    ):
        for hit in c.search(query, limit=5):
            if hit["url"] not in seen:
                seen.add(hit["url"])
                out.append({**hit, "query": query})
    if client is None:
        c.close()
    return out
