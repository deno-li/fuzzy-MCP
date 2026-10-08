# SPDX-FileCopyrightText: 2026 Deniz Özer
#
# SPDX-License-Identifier: MIT

import json

import anyio
import httpx2
import pytest

from fuzzy_mcp.config import Settings
from fuzzy_mcp.errors import UpstreamError
from fuzzy_mcp.http import HttpClient, RateLimit, SlidingWindowLimiter, TTLCache

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def _no_sleep(_: float) -> None:
    return None


def _client(handler, **kwargs) -> HttpClient:
    settings = Settings(max_retries=kwargs.pop("max_retries", 2))
    return HttpClient(settings, transport=httpx2.MockTransport(handler), sleep=_no_sleep, **kwargs)


async def test_get_json_caches_and_sends_user_agent():
    calls = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(request)
        return httpx2.Response(200, json={"ok": True, "q": request.url.params.get("q")})

    async with _client(handler) as client:
        first = await client.get_json("test", "https://example.se/api", params={"q": "skola", "x": None})
        second = await client.get_json("test", "https://example.se/api", params={"q": "skola"})
    assert first.data == {"ok": True, "q": "skola"} and not first.from_cache
    assert second.from_cache and len(calls) == 1
    assert calls[0].headers["user-agent"].startswith("fuzzy-mcp/")
    assert "x" not in calls[0].url.params


async def test_retries_on_429_then_succeeds():
    attempts = {"n": 0}

    def handler(request):
        attempts["n"] += 1
        if attempts["n"] < 3:
            return httpx2.Response(429, headers={"Retry-After": "1"}, json={"message": "slow down"})
        return httpx2.Response(200, json=[1, 2])

    async with _client(handler) as client:
        res = await client.get_json("scb", "https://example.se/t")
    assert res.data == [1, 2] and attempts["n"] == 3


async def test_error_detail_from_problem_document():
    def handler(request):
        return httpx2.Response(
            400,
            headers={"content-type": "application/problem+json"},
            content=json.dumps({"type": "about:blank", "title": "Bad", "detail": "Okänd variabel: Regio"}),
        )

    async with _client(handler) as client:
        with pytest.raises(UpstreamError) as info:
            await client.post_json("scb", "https://example.se/data", {"selection": []})
    err = info.value
    assert err.status == 400 and err.detail == "Okänd variabel: Regio"
    assert "[scb]" in str(err)


async def test_transport_error_becomes_upstream_error():
    def handler(request):
        raise httpx2.ConnectError("nope", request=request)

    async with _client(handler) as client:
        with pytest.raises(UpstreamError) as info:
            await client.get_json("fohm", "https://example.se/x")
    assert "Kunde inte nå" in info.value.message


async def test_lifecycle_headers_are_reported():
    def handler(request):
        return httpx2.Response(
            200, json={}, headers={"Deprecation": "@1767225600", "Sunset": "Wed, 30 Jun 2027 00:00:00 GMT"}
        )

    async with _client(handler) as client:
        res = await client.get_json("skolverket", "https://example.se/v1/x")
    assert any(n.startswith("Deprecation") for n in res.notices)
    assert any(n.startswith("Sunset") for n in res.notices)
    assert len(client.recent_notices) == 2


async def test_non_json_content_is_returned_as_text():
    def handler(request):
        return httpx2.Response(200, text="a;b\n1;2", headers={"content-type": "text/csv"})

    async with _client(handler) as client:
        res = await client.get_json("scb", "https://example.se/csv")
    assert res.data == "a;b\n1;2"


def test_ttl_cache_expiry_and_lru():
    now = {"t": 0.0}
    cache = TTLCache(2, 10, clock=lambda: now["t"])
    cache.set("a", 1)
    cache.set("b", 2)
    cache.get("a")
    cache.set("c", 3)  # evicts b (least recently used)
    assert cache.get("b") is None and cache.get("a") == 1
    now["t"] = 11
    assert cache.get("a") is None


async def test_sliding_window_limiter_blocks_when_full():
    limiter = SlidingWindowLimiter(RateLimit(calls=2, per_seconds=0.2))
    start = anyio.current_time()
    for _ in range(3):
        await limiter.acquire()
    assert anyio.current_time() - start >= 0.18


async def test_html_page_with_status_200_is_an_error_for_json_calls():
    def handler(request):
        return httpx2.Response(200, text="<html><title>Underhåll</title></html>", headers={"content-type": "text/html"})

    async with _client(handler) as client:
        with pytest.raises(UpstreamError, match="inte JSON"):
            await client.get_json("scb", "https://example.se/tables")


async def test_plain_text_json_is_parsed():
    def handler(request):
        return httpx2.Response(200, text="42", headers={"content-type": "text/plain"})

    async with _client(handler) as client:
        res = await client.get_json("skolverket", "https://example.se/count")
    assert res.data == 42


@pytest.mark.parametrize(
    "error",
    [
        httpx2.TooManyRedirects("Exceeded maximum allowed redirects."),
        httpx2.DecodingError("Error -3 while decompressing data"),
        RuntimeError("Cannot send a request, as the client has been closed."),
    ],
)
async def test_request_errors_become_upstream_errors(error):
    def handler(request):
        raise error

    async with _client(handler, max_retries=2) as client:
        with pytest.raises(UpstreamError) as info:
            await client.get_json("scb", "https://example.se/x")
    assert type(error).__name__ in str(info.value) or "inte tillgänglig" in str(info.value)


def test_ttl_cache_byte_budget():
    cache = TTLCache(100, 10, max_bytes=1000)
    cache.set("big", "x", size=400)  # more than a quarter of the budget: not cached
    assert cache.get("big") is None
    for i in range(5):
        cache.set(str(i), i, size=240)
    assert cache.bytes <= 1000 and cache.get("0") is None and cache.get("4") == 4
    cache.set("4", 4, size=10)
    assert cache.bytes == 3 * 240 + 10


async def test_html_error_page_detail_is_summarised():
    page = (
        "<!DOCTYPE html><html><head><title>404 - File or directory not found.</title></head><body>"
        "<h2>404 - File or directory not found.</h2><h3>The resource you are looking for might have been removed"
        "</h3><script>var x = 1;</script></body></html>"
    )

    def handler(request):
        return httpx2.Response(404, text=page, headers={"content-type": "text/html"})

    async with _client(handler) as client:
        with pytest.raises(UpstreamError) as info:
            await client.get_json("fohm", "https://example.se/x.px")
    message = str(info.value)
    assert "404 - File or directory not found." in message and "<" not in message and "var x" not in message
