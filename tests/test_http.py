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


@pytest.mark.parametrize("value,expected", [("nan", 0.5), ("inf", 30.0), ("-5", 0.0)])
async def test_unusable_retry_after_falls_back_to_a_finite_delay(value, expected):
    delays: list[float] = []

    async def sleep(seconds: float) -> None:
        delays.append(seconds)
        await anyio.sleep(0 if seconds == seconds else seconds)  # anyio rejects NaN (NaN != NaN)

    attempts = {"n": 0}

    def handler(request):
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx2.Response(503, headers={"Retry-After": value}, json={})
        return httpx2.Response(200, json=[1])

    async with HttpClient(Settings(max_retries=1), transport=httpx2.MockTransport(handler), sleep=sleep) as client:
        res = await client.get_json("scb", "https://example.se/t")
    assert res.data == [1]
    assert delays == [expected]  # nan: the default backoff; inf: the maximum; negative: no wait


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


async def test_shared_notices_are_unique_so_one_api_cannot_push_out_another():
    def handler(request):
        if request.url.host == "fohm.example.se":
            return httpx2.Response(200, json={}, headers={"Sunset": "Wed, 30 Jun 2027 00:00:00 GMT"})
        return httpx2.Response(200, json={}, headers={"Deprecation": "@1767225600"})

    async with _client(handler) as client:
        await client.get_json("fohm", "https://fohm.example.se/x")
        for i in range(60):
            await client.get_json("scb", f"https://scb.example.se/t{i}")
    assert list(client.recent_notices) == [
        "fohm: Sunset: 2027-06-30T00:00:00Z",
        "scb: Deprecation: 2026-01-01T00:00:00Z",
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


@pytest.mark.parametrize("location", ["https:evil.example/x", "https://api.skolverket.se:abc/x", "http://[::1/x"])
async def test_malformed_redirect_location_becomes_upstream_error_without_retries(location):
    seen: list[str] = []
    handler = _redirecting({"https://api.skolverket.se/": httpx2.Response(302, headers={"location": location})}, seen)
    async with _client(handler) as client:
        with pytest.raises(UpstreamError) as info:
            await client.get_json("skolverket", "https://api.skolverket.se/skolenhetsregistret/v2/school-units/1")
    assert info.value.message.startswith("Anropet misslyckades")
    assert len(seen) == 1


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


OTHER_HOST = "stoppades – servern följer bara omdirigeringar till myndigheternas egna adresser"
OTHER_PORT = "stoppades – servern följer bara omdirigeringar till samma port och protokoll som i myndighetens adress"


@pytest.mark.parametrize(
    ("location", "where"),
    [
        ("https://api.skolverket.se:22/x", "https://api.skolverket.se:22"),  # ssh on an agency's host
        ("https://API.Skolverket.SE:8443/x?q=user-text", "https://api.skolverket.se:8443"),
        ("https://api.skolverket.se:022/x", "https://api.skolverket.se:22"),  # another spelling of port 22
        ("https://statistikdatabasen.scb.se:80/api/v2", "https://statistikdatabasen.scb.se:80"),  # another agency
        ("https://api.skolverket.se:0/x", "https://api.skolverket.se:0"),
    ],
)
async def test_redirect_to_another_port_on_an_agency_host_is_refused(location, where):
    seen: list[str] = []
    handler = _redirecting({"https://api.skolverket.se/": httpx2.Response(302, headers={"location": location})}, seen)
    async with _client(handler) as client:
        with pytest.raises(UpstreamError) as info:
            await client.get_json("skolverket", "https://api.skolverket.se/skolenhetsregistret/v2/school-units/1")
    assert info.value.message == f"Omdirigeringen till {where} {OTHER_PORT}"
    assert len(seen) == 1  # the target was never requested, and the refusal is not retried


@pytest.mark.parametrize(
    "location",
    [
        "https://api.skolverket.se:443/new",  # the default port written out is the same origin
        "https://API.SKOLVERKET.SE/new",  # host names are case-insensitive
        "/new",
        "https://statistikdatabasen.scb.se:443/api/v2/x",  # another agency, default port written out
    ],
)
async def test_redirect_to_an_agency_origin_is_followed_with_or_without_its_default_port(location):
    seen: list[str] = []
    handler = _redirecting(
        {
            "https://api.skolverket.se/old": httpx2.Response(301, headers={"location": location}),
            "https://api.skolverket.se/new": httpx2.Response(200, json={"ok": True}),
            "https://statistikdatabasen.scb.se/api/v2/x": httpx2.Response(200, json={"ok": True}),
        },
        seen,
    )
    async with _client(handler) as client:
        res = await client.get_json("skolverket", "https://api.skolverket.se/old")
    assert res.data == {"ok": True} and len(seen) == 2


async def test_base_url_with_a_port_allows_that_port_only():
    """A mirror at https://mirror.example.se:8443 (FUZZY_MCP_<KÄLLA>_BASE_URL) may be redirected to, from an agency
    or from itself, on port 8443 only: its default port 443 and every other port on the host stay closed."""
    settings = Settings(max_retries=0, base_urls={**DEFAULT_BASE_URLS, "scb": "https://mirror.example.se:8443/api/v2"})
    seen: list[str] = []
    handler = _redirecting(
        {
            "https://api.skolverket.se/to-mirror": httpx2.Response(
                302, headers={"location": "https://mirror.example.se:8443/api/v2/x"}
            ),
            "https://api.skolverket.se/to-443": httpx2.Response(
                302, headers={"location": "https://mirror.example.se/api/v2/x"}
            ),
            "https://mirror.example.se:8443/self": httpx2.Response(302, headers={"location": "/api/v2/x"}),
            "https://mirror.example.se:8443/to-9000": httpx2.Response(
                302, headers={"location": "https://mirror.example.se:9000/admin"}
            ),
            "https://mirror.example.se:8443/to-443": httpx2.Response(
                302, headers={"location": "https://mirror.example.se/admin"}
            ),
            "https://mirror.example.se:8443/api/v2/x": httpx2.Response(200, json={"ok": True}),
        },
        seen,
    )
    async with HttpClient(settings, transport=httpx2.MockTransport(handler), sleep=_no_sleep) as client:
        assert (await client.get_json("scb", "https://api.skolverket.se/to-mirror")).data == {"ok": True}
        assert (await client.get_json("scb", "https://mirror.example.se:8443/self")).data == {"ok": True}
        for start, where in [
            ("https://api.skolverket.se/to-443", "https://mirror.example.se:443"),
            ("https://mirror.example.se:8443/to-9000", "https://mirror.example.se:9000"),
            ("https://mirror.example.se:8443/to-443", "https://mirror.example.se:443"),
        ]:
            with pytest.raises(UpstreamError) as info:
                await client.get_json("scb", start)
            assert info.value.message == f"Omdirigeringen till {where} {OTHER_PORT}"
    assert all(url.startswith(("https://api.skolverket.se/", "https://mirror.example.se:8443/")) for url in seen)


async def test_http_mirror_is_followed_on_its_own_origin_only():
    """An http base URL allows plain http on its own origin only: not another port on the mirror, not https on the
    mirror (port 443) and not http on an agency's host (port 80, where only https on 443 is allowed)."""
    settings = Settings(max_retries=0, base_urls={**DEFAULT_BASE_URLS, "scb": "http://mirror.example.se/api/v2"})
    seen: list[str] = []
    handler = _redirecting(
        {
            "http://mirror.example.se/to-8080": httpx2.Response(
                302, headers={"location": "http://mirror.example.se:8080/admin"}
            ),
            "http://mirror.example.se/to-https": httpx2.Response(
                302, headers={"location": "https://mirror.example.se/api/v2/x"}
            ),
            "http://mirror.example.se/to-agency": httpx2.Response(
                302, headers={"location": "http://api.skolverket.se/x"}
            ),
        },
        seen,
    )
    async with HttpClient(settings, transport=httpx2.MockTransport(handler), sleep=_no_sleep) as client:
        for start, where in [
            ("http://mirror.example.se/to-8080", "http://mirror.example.se:8080"),
            ("http://mirror.example.se/to-https", "https://mirror.example.se:443"),
            ("http://mirror.example.se/to-agency", "http://api.skolverket.se:80"),
        ]:
            with pytest.raises(UpstreamError) as info:
                await client.get_json("scb", start)
            assert info.value.message == f"Omdirigeringen till {where} {OTHER_PORT}"
    assert len(seen) == 3 and all(url.startswith("http://mirror.example.se/") for url in seen)


@pytest.mark.parametrize(
    ("location", "where"),
    [
        ("https://api.skolverket.se@evil.example/x", "https://evil.example"),  # userinfo, not the host
        ("https://api.skolverket.se:443@evil.example/x", "https://evil.example"),
        ("https://api.skolverket.se\\@evil.example/x", "https://evil.example"),
        ("https://evil.example#@api.skolverket.se", "https://evil.example"),  # a fragment, not userinfo
        ("https://user:secret@evil.example:8443/x", "https://evil.example:8443"),  # userinfo is never shown
        ("https://api.skolverket.se.evil.example/x", "https://api.skolverket.se.evil.example"),
        ("https://api.skolverket.se./x", "https://api.skolverket.se."),  # a trailing dot is another origin
        ("https://[::1]/x", "https://[::1]"),
        ("https://[0:0::1]:443/x", "https://[0:0::1]"),
        ("https://[::ffff:169.254.169.254]:8080/x", "https://[::ffff:169.254.169.254]:8080"),
        ("https://[fe80::1%25eth0]:22/x", "https://[fe80::1%25eth0]:22"),
    ],
)
async def test_redirect_is_judged_by_the_host_actually_contacted(location, where):
    seen: list[str] = []
    handler = _redirecting({"https://api.skolverket.se/": httpx2.Response(302, headers={"location": location})}, seen)
    async with _client(handler) as client:
        with pytest.raises(UpstreamError) as info:
            await client.get_json("skolverket", "https://api.skolverket.se/skolenhetsregistret/v2/school-units/1")
    assert info.value.message == f"Omdirigeringen till {where} {OTHER_HOST}"
    assert len(seen) == 1


async def test_ipv6_mirror_is_compared_by_address_and_port():
    """An IPv6 base URL matches every spelling of its address, on its own port only."""
    settings = Settings(max_retries=0, base_urls={**DEFAULT_BASE_URLS, "scb": "http://[::1]:8080/api/v2"})
    seen: list[str] = []
    handler = _redirecting(
        {
            "http://[::1]:8080/a": httpx2.Response(302, headers={"location": "http://[0:0::1]:8080/b"}),
            "http://[0:0::1]:8080/b": httpx2.Response(200, json={"ok": True}),
            "http://[::1]:8080/c": httpx2.Response(302, headers={"location": "http://[::1]:8081/admin"}),
        },
        seen,
    )
    async with HttpClient(settings, transport=httpx2.MockTransport(handler), sleep=_no_sleep) as client:
        assert (await client.get_json("scb", "http://[::1]:8080/a")).data == {"ok": True}
        with pytest.raises(UpstreamError) as info:
            await client.get_json("scb", "http://[::1]:8080/c")
    assert info.value.message == f"Omdirigeringen till http://[::1]:8081 {OTHER_PORT}"
    assert seen == ["http://[::1]:8080/a", "http://[0:0::1]:8080/b", "http://[::1]:8080/c"]


async def test_base_url_without_scheme_does_not_stop_the_client():
    """A misconfigured base URL (no https://) has no origin: it allows no redirects, but the client still starts."""
    settings = Settings(max_retries=0, base_urls={**DEFAULT_BASE_URLS, "scb": "mirror.example.se/api/v2"})
    seen: list[str] = []
    handler = _redirecting(
        {
            "https://api.skolverket.se/a": httpx2.Response(302, headers={"location": "https://mirror.example.se/x"}),
            "https://api.skolverket.se/b": httpx2.Response(302, headers={"location": "/c"}),
            "https://api.skolverket.se/c": httpx2.Response(200, json={"ok": True}),
        },
        seen,
    )
    async with HttpClient(settings, transport=httpx2.MockTransport(handler), sleep=_no_sleep) as client:
        with pytest.raises(UpstreamError) as info:
            await client.get_json("scb", "https://api.skolverket.se/a")
        assert info.value.message == f"Omdirigeringen till https://mirror.example.se {OTHER_HOST}"
        assert (await client.get_json("scb", "https://api.skolverket.se/b")).data == {"ok": True}
