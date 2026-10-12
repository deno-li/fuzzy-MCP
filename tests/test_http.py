# SPDX-FileCopyrightText: 2026 Deniz Özer
#
# SPDX-License-Identifier: MIT

import json

import anyio
import httpx2
import pytest

from fuzzy_mcp.config import DEFAULT_BASE_URLS, Settings
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
    assert list(client.recent_notices) == [
        "skolverket: Deprecation: 2026-01-01T00:00:00Z",
        "skolverket: Sunset: 2027-06-30T00:00:00Z",
    ]


@pytest.mark.parametrize(
    "headers",
    [
        {"Deprecation": "private-question", "Sunset": "private-question"},
        {"Deprecation": "@1767225600 private-question", "Sunset": "Wed, 30 Jun 2027 00:00:00 GMT private-question"},
        {"Deprecation": "@999999999999999", "Sunset": "Wed, 99 Jun 2027 00:00:00 GMT"},
        {
            "Warning": '299 example.se "private-question"',
            "Link": '<https://example.se/private-question>; rel="deprecation"',
        },
    ],
)
async def test_shared_notices_do_not_copy_unvalidated_headers_or_request_urls(headers):
    def handler(request):
        return httpx2.Response(200, json={}, headers=headers)

    async with _client(handler) as client:
        res = await client.get_json("test", "https://example.se/private-question", params={"q": "private-question"})
        cached = await client.get_json("test", "https://example.se/private-question", params={"q": "private-question"})
    assert res.notices and cached.notices == res.notices  # originating callers keep their detailed diagnostics
    assert cached.from_cache
    shared = list(client.recent_notices)
    assert len(shared) == sum(name in headers for name in ("Deprecation", "Sunset"))
    assert all(notice.endswith("mottagen, datum kunde inte valideras") for notice in shared)
    assert "private-question" not in str(shared) and "https://" not in str(shared)


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


def _redirecting(routes: dict[str, httpx2.Response], seen: list[str]):
    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(str(request.url))
        for prefix, response in routes.items():
            if str(request.url).startswith(prefix):
                return response
        return httpx2.Response(404, text="SECRET-BODY")

    return handler


@pytest.mark.parametrize(
    "location",
    [
        "http://169.254.169.254/latest/meta-data/",  # another host (cloud metadata)
        "https://eneo_backend:8000/api/",  # an internal service next to the server
        "http://api.skolverket.se/skolenhetsregistret/v2/x",  # same host, https → http
    ],
)
async def test_redirect_outside_the_agencies_is_refused(location):
    seen: list[str] = []
    handler = _redirecting({"https://api.skolverket.se/": httpx2.Response(302, headers={"location": location})}, seen)
    async with _client(handler) as client:
        with pytest.raises(UpstreamError) as info:
            await client.get_json("skolverket", "https://api.skolverket.se/skolenhetsregistret/v2/school-units/1")
    message = str(info.value)
    assert "stoppades" in message and "SECRET" not in message
    target = httpx2.URL(location)
    assert f"{target.scheme}://{target.host}" in message and target.path not in message
    assert len(seen) == 1  # the target was never requested, and the refusal is not retried


async def test_redirect_within_an_agency_host_and_to_another_agency_is_followed():
    seen: list[str] = []
    handler = _redirecting(
        {
            "https://api.skolverket.se/old": httpx2.Response(301, headers={"location": "/new"}),
            "https://api.skolverket.se/new": httpx2.Response(
                307, headers={"location": "https://statistikdatabasen.scb.se/api/v2/x"}
            ),
            "https://statistikdatabasen.scb.se/": httpx2.Response(200, json={"ok": True}),
        },
        seen,
    )
    async with _client(handler) as client:
        res = await client.get_json("skolverket", "https://api.skolverket.se/old")
    assert res.data == {"ok": True} and res.url == "https://statistikdatabasen.scb.se/api/v2/x"
    assert len(seen) == 3


async def test_redirect_on_the_host_first_asked_is_followed():
    """Hosts outside the default base URLs (a mirror set with FUZZY_MCP_<KÄLLA>_BASE_URL) may redirect within
    themselves."""
    seen: list[str] = []
    handler = _redirecting(
        {
            "https://example.se/a": httpx2.Response(302, headers={"location": "https://example.se/b"}),
            "https://example.se/b": httpx2.Response(200, json=[1]),
        },
        seen,
    )
    async with _client(handler) as client:
        res = await client.get_json("scb", "https://example.se/a")
    assert res.data == [1] and len(seen) == 2


async def test_redirect_loop_is_stopped():
    seen: list[str] = []
    handler = _redirecting({"https://example.se/": httpx2.Response(302, headers={"location": "/loop"})}, seen)
    async with _client(handler) as client:
        with pytest.raises(UpstreamError, match="För många omdirigeringar"):
            await client.get_json("scb", "https://example.se/start")
    assert len(seen) == 6  # the request and MAX_REDIRECTS (5) redirects


async def test_redirect_keeps_the_rate_limit_of_the_target_host():
    acquired: list[str] = []

    class Counting(SlidingWindowLimiter):
        def __init__(self, host: str) -> None:
            super().__init__(RateLimit(calls=100, per_seconds=1))
            self.host = host

        async def acquire(self) -> None:
            acquired.append(self.host)
            await super().acquire()

    seen: list[str] = []
    handler = _redirecting(
        {
            "https://api.skolverket.se/": httpx2.Response(
                302, headers={"location": "https://statistikdatabasen.scb.se/x"}
            ),
            "https://statistikdatabasen.scb.se/": httpx2.Response(200, json={}),
        },
        seen,
    )
    async with _client(handler) as client:
        client._limiters = {host: Counting(host) for host in ("api.skolverket.se", "statistikdatabasen.scb.se")}
        await client.get_json("skolverket", "https://api.skolverket.se/a")
    assert acquired == ["api.skolverket.se", "statistikdatabasen.scb.se"]


async def test_downgrade_to_http_is_refused_with_its_own_message():
    seen: list[str] = []
    location = "http://api.skolverket.se/skolenhetsregistret/v2/x"
    handler = _redirecting({"https://api.skolverket.se/": httpx2.Response(302, headers={"location": location})}, seen)
    async with _client(handler) as client:
        with pytest.raises(UpstreamError, match="aldrig en omdirigering från https till en okrypterad adress"):
            await client.get_json("skolverket", "https://api.skolverket.se/skolenhetsregistret/v2/school-units/1")
    assert len(seen) == 1


async def test_http_mirror_may_redirect_over_http_but_never_back_down_from_https():
    """With an http base URL (a mirror or test double) http is followed while every hop so far was http; once a hop
    is https, a redirect back to http is refused."""
    settings = Settings(max_retries=0, base_urls={**DEFAULT_BASE_URLS, "scb": "http://mirror.example.se/api/v2"})
    seen: list[str] = []
    handler = _redirecting(
        {
            "http://mirror.example.se/start": httpx2.Response(302, headers={"location": "/done"}),
            "http://mirror.example.se/done": httpx2.Response(200, json={"ok": True}),
            "http://mirror.example.se/up": httpx2.Response(301, headers={"location": "https://api.skolverket.se/b"}),
            "https://api.skolverket.se/b": httpx2.Response(
                302, headers={"location": "http://api.skolverket.se/c?q=user-text"}
            ),
        },
        seen,
    )
    async with HttpClient(settings, transport=httpx2.MockTransport(handler), sleep=_no_sleep) as client:
        res = await client.get_json("scb", "http://mirror.example.se/start")
        assert res.data == {"ok": True}
        with pytest.raises(UpstreamError, match="okrypterad"):
            await client.get_json("scb", "http://mirror.example.se/up")
    assert seen[-1] == "https://api.skolverket.se/b" and not any("user-text" in url for url in seen)
