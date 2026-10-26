"""The kubiq client's catalogue calls: reload and check, and a server that is gone.

No server: `httpx.MockTransport` answers each request from a scripted handler,
so every status kubiq can return is exercised with the body it would send.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from swarmr_kubiq.kubiq_api import FatalError, KubiqClient, KubiqError

URL = "http://kubiq.test"


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> KubiqClient:
    client = KubiqClient(URL)
    client._http = httpx.Client(base_url=URL, transport=httpx.MockTransport(handler))
    return client


def _answer(
    status: int, body: object = None
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        if body is None:
            return httpx.Response(status)
        return httpx.Response(status, json=body)

    return handler


def test_reload_posts_and_returns_the_counts() -> None:
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        return httpx.Response(200, json={"drills": 42, "exams": 3})

    assert _client(handler).reload() == {"drills": 42, "exams": 3}
    assert seen == [("POST", "/api/reload")]


def test_reload_without_the_endpoint_is_fatal() -> None:
    with pytest.raises(FatalError, match=r"has no /api/reload.*Do not retry") as exc:
        _client(_answer(404)).reload()
    assert URL in str(exc.value)
    assert "docker compose up -d --build" in str(exc.value)


def test_reload_over_an_open_session_is_fatal() -> None:
    with pytest.raises(FatalError, match=r"another session is open.*Do not retry"):
        _client(_answer(409, {"error": "1 session open"})).reload()


def test_reload_refused_by_the_loader_carries_its_text() -> None:
    body = {"error": "drills/ckad-mock-7/q03-x.yaml: check 1: missing `name`"}
    with pytest.raises(
        KubiqError, match=r"^drills/ckad-mock-7/q03-x.yaml: check 1"
    ) as exc:
        _client(_answer(422, body)).reload()
    assert type(exc.value) is KubiqError, "a loader refusal is retryable, not fatal"


def test_reload_other_errors_are_plain_kubiq_errors() -> None:
    with pytest.raises(KubiqError, match=r"kubiq 500 on POST /api/reload"):
        _client(_answer(500)).reload()


def test_check_is_empty_when_the_checkout_loads() -> None:
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        return httpx.Response(200, json={"drills": 1, "exams": 1})

    assert _client(handler).check() == ""
    assert seen == [("POST", "/api/check")]


def test_check_returns_the_loader_text_on_refusal() -> None:
    body = {"error": "exams/ckad-mock-7.yaml: weights sum to 96"}
    assert (
        _client(_answer(422, body)).check() == "exams/ckad-mock-7.yaml: weights sum to 96"
    )


def test_check_without_the_endpoint_is_fatal() -> None:
    with pytest.raises(FatalError, match=r"has no /api/check"):
        _client(_answer(404)).check()


def test_check_falls_back_to_the_raw_body_when_not_json() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, text="bad yaml")

    assert _client(handler).check() == "bad yaml"


@pytest.mark.parametrize("method", ["reload", "check", "exams", "sessions"])
def test_a_server_that_is_not_running_is_fatal(method: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(
        FatalError, match=r"not running at http://kubiq.test.*Do not retry"
    ):
        getattr(_client(handler), method)()


def test_session_error_statuses_stay_plain_kubiq_errors() -> None:
    with pytest.raises(KubiqError, match=r"kubiq 404 on GET /api/sessions/nope"):
        _client(_answer(404, {"error": "no such session"})).session("nope")
