"""The lodestar client over a mocked streamable-HTTP transport; failures degrade."""

from __future__ import annotations

import json
from typing import Any

import httpx

from swarmr_kubiq.docs import DocsClient, _parse_hits, whats_new

PDB_URL = "https://kubernetes.io/docs/concepts/workloads/pods/disruptions"
CONFIGURE_URL = "https://kubernetes.io/docs/tasks/run-application/configure-pdb"
SEARCH_TEXT = f"""Documentation passages (kubernetes)

Query "pod disruption budget" — 2 hit(s)

[kubernetes] Disruptions > Pod disruption budgets
{PDB_URL}#pod-disruption-budgets  (document_id: 5159, upstream 2025-09-01)
Kubernetes offers features to help you run highly available applications.

A PDB limits the number of Pods of a replicated application that are down.

[kubernetes] Specifying a Disruption Budget for your Application
{CONFIGURE_URL}  (document_id: 5778, matches 2 of 3 terms)
Use `minAvailable` or `maxUnavailable`.
[truncated: 1 more result(s) omitted to stay within the token budget; narrow the query]
---
GROUNDING: the passages above are the only source for this answer.
"""


def _rpc_result(req: dict[str, Any], result: dict[str, Any]) -> httpx.Response:
    return httpx.Response(
        200,
        json={"jsonrpc": "2.0", "id": req["id"], "result": result},
        headers={"Mcp-Session-Id": "s-1"},
    )


class FakeLodestar:
    """A streamable-HTTP MCP server answering initialize and tools/call."""

    def __init__(
        self, text: str = SEARCH_TEXT, *, is_error: bool = False, sse: bool = False
    ):
        self.text, self.is_error, self.sse = text, is_error, sse
        self.calls: list[dict[str, Any]] = []
        self.sessions: list[str | None] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        req = json.loads(request.content)
        self.sessions.append(request.headers.get("Mcp-Session-Id"))
        if req["method"] == "initialize":
            return _rpc_result(req, {"protocolVersion": "2025-03-26", "capabilities": {}})
        if "id" not in req:  # notifications/initialized
            return httpx.Response(202)
        assert req["method"] == "tools/call"
        self.calls.append(req["params"])
        result = {
            "content": [{"type": "text", "text": self.text}],
            "isError": self.is_error,
        }
        if not self.sse:
            return _rpc_result(req, result)
        body = (
            "event: message\ndata: "
            + json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": result})
            + "\n\n"
        )
        headers = {"content-type": "text/event-stream"}
        return httpx.Response(200, text=body, headers=headers)


def _client(server: FakeLodestar) -> DocsClient:
    return DocsClient("http://lodestar.test/mcp", client=_http(server))


def _http(handler: Any) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_search_parses_hits_and_reuses_session() -> None:
    server = FakeLodestar()
    c = _client(server)
    hits = c.search("pod disruption budget", limit=2)
    assert c.note == ""
    assert [h["document_id"] for h in hits] == [5159, 5778]
    assert hits[0]["title"] == "Disruptions > Pod disruption budgets"
    assert hits[0]["url"].endswith("/disruptions#pod-disruption-budgets")
    assert hits[0]["snippet"].startswith("Kubernetes offers")
    assert hits[0]["snippet"].endswith("are down.")  # blank line inside kept
    assert hits[1]["snippet"] == "Use `minAvailable` or `maxUnavailable`."  # trailers cut
    assert server.calls == [
        {
            "name": "search_docs",
            "arguments": {"query": "pod disruption budget", "limit": 2},
        }
    ]
    # initialize, initialized notification, tools/call: session header from call two on.
    assert server.sessions[0] is None and server.sessions[1:] == ["s-1", "s-1"]
    c.search("again")
    assert server.sessions[-1] == "s-1" and len(server.calls) == 2


def test_search_over_sse_response() -> None:
    c = _client(FakeLodestar(sse=True))
    assert len(c.search("x")) == 2


def test_page_targets_id_or_url() -> None:
    server = FakeLodestar(text="# Page\nbody")
    c = _client(server)
    assert c.page(5159) == "# Page\nbody"
    assert c.page("https://kubernetes.io/docs/x", fields=("body",), section="intro")
    assert server.calls[0]["arguments"] == {
        "fields": ["summary", "headings"],
        "document_id": 5159,
    }
    assert server.calls[1]["arguments"] == {
        "fields": ["body"],
        "url": "https://kubernetes.io/docs/x",
        "section": "intro",
    }


def test_tool_error_degrades_without_dropping_session() -> None:
    server = FakeLodestar(text="no indexed document for that url", is_error=True)
    c = _client(server)
    assert c.search("x") == []
    assert c.note.startswith("tool error: lodestar search_docs")
    assert "no indexed document" in c.note
    assert c.page("nope") == ""
    assert server.sessions.count(None) == 1  # no second handshake


def test_transport_failure_returns_empty_with_note() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    c = DocsClient("http://down.test/mcp", client=_http(boom))
    assert c.search("x") == []
    assert c.note.startswith("tool error: lodestar search_docs failed: ConnectError")
    assert c.page(1) == ""
    assert "get_page" in c.note

    c = DocsClient("http://bad.test/mcp", client=_http(lambda r: httpx.Response(500)))
    assert c.search("x") == [] and "tool error" in c.note


def test_whats_new_dedupes_by_url_and_tags_query() -> None:
    server = FakeLodestar()
    c = _client(server)
    out = whats_new("1.34", c)
    assert [p["arguments"]["query"] for p in server.calls] == [
        "Kubernetes 1.34 what's new",
        "feature gate graduated 1.34",
        "deprecated 1.34",
    ]
    assert len(out) == 2  # three searches, same two URLs each time
    assert out[0]["query"] == "Kubernetes 1.34 what's new"
    assert {"title", "url", "snippet", "document_id", "query"} <= out[0].keys()


def test_parse_hits_on_empty_or_no_match_text() -> None:
    assert _parse_hits("") == []
    assert _parse_hits("Documentation passages (all)\nno matches\n") == []
