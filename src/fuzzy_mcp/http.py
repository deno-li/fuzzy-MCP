# SPDX-FileCopyrightText: 2026 Deniz Özer
#
# SPDX-License-Identifier: MIT

"""Shared async HTTP layer: caching, per-host rate limiting, retries and
capture of API lifecycle headers (Deprecation/Sunset) as recommended by
the DIGG REST API profile.
"""

import hashlib
import html
import json
import re
import time
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from typing import Any

import anyio
import httpx2

from .config import Settings
from .errors import UpstreamError

RETRY_STATUSES = frozenset({429, 502, 503, 504})
LIFECYCLE_HEADERS = ("deprecation", "sunset", "warning")
MAX_RETRY_AFTER_SECONDS = 30.0

QueryParams = Mapping[str, str | int | float | bool | list[str] | None]


@dataclass(frozen=True)
class RateLimit:
    """At most ``calls`` requests per ``per_seconds`` sliding window."""

    calls: int
    per_seconds: float


class SlidingWindowLimiter:
    def __init__(self, limit: RateLimit, clock: Callable[[], float] = time.monotonic) -> None:
        self._limit = limit
        self._clock = clock
        self._stamps: deque[float] = deque()
        self._lock = anyio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = self._clock()
                while self._stamps and now - self._stamps[0] >= self._limit.per_seconds:
                    self._stamps.popleft()
                if len(self._stamps) < self._limit.calls:
                    self._stamps.append(now)
                    return
                await anyio.sleep(self._limit.per_seconds - (now - self._stamps[0]) + 0.01)


class TTLCache:
    """Small in-memory LRU cache with per-entry expiry and a byte budget."""

    def __init__(
        self,
        max_entries: int,
        ttl_seconds: float,
        clock: Callable[[], float] = time.monotonic,
        max_bytes: int = 64_000_000,
    ):
        self._max = max_entries
        self._ttl = ttl_seconds
        self._clock = clock
        self._max_bytes = max_bytes
        self._bytes = 0
        self._data: OrderedDict[str, tuple[float, Any, int]] = OrderedDict()

    def get(self, key: str) -> Any | None:
        item = self._data.get(key)
        if item is None:
            return None
        expires, value, _ = item
        if expires < self._clock():
            self._drop(key)
            return None
        self._data.move_to_end(key)
        return value

    def set(self, key: str, value: Any, size: int = 0) -> None:
        """Store ``value``; ``size`` is its approximate byte size (large answers are not cached)."""
        if self._max <= 0 or self._ttl <= 0 or size > self._max_bytes // 4:
            return
        if key in self._data:
            self._drop(key)
        self._data[key] = (self._clock() + self._ttl, value, size)
        self._bytes += size
        while self._data and (len(self._data) > self._max or self._bytes > self._max_bytes):
            self._drop(next(iter(self._data)))

    def _drop(self, key: str) -> None:
        _, _, size = self._data.pop(key)
        self._bytes -= size

    def clear(self) -> None:
        self._data.clear()
        self._bytes = 0

    @property
    def bytes(self) -> int:
        return self._bytes

    def __len__(self) -> int:
        return len(self._data)


@dataclass
class ApiResponse:
    url: str
    status: int
    data: Any
    content_type: str
    notices: list[str] = field(default_factory=list)
    from_cache: bool = False


def _cache_key(method: str, url: str, params: QueryParams | None, body: Any, accept: str | None) -> str:
    payload = json.dumps(
        [method.upper(), url, sorted((params or {}).items()), body, accept],
        sort_keys=True,
        default=str,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _clean_params(params: QueryParams | None) -> dict[str, Any]:
    cleaned: dict[str, Any] = {}
    for key, value in (params or {}).items():
        if value is None:
            continue
        if isinstance(value, bool):
            cleaned[key] = "true" if value else "false"
        else:
            cleaned[key] = value
    return cleaned


def _retry_after_seconds(response: httpx2.Response) -> float | None:
    raw = response.headers.get("retry-after")
    if not raw:
        return None
    try:
        return min(float(raw), MAX_RETRY_AFTER_SECONDS)
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(when.timestamp() - time.time(), MAX_RETRY_AFTER_SECONDS))


_HTML_TITLE = re.compile(r"<(title|h1|h2)[^>]*>(.*?)</\1>", re.IGNORECASE | re.DOTALL)
_HTML_TAG = re.compile(r"<[^>]+>")


def _html_summary(text: str) -> str:
    """Title/headings of an HTML error page (IIS and PxWeb answer errors as HTML) instead of raw markup."""
    parts: list[str] = []
    for _, inner in _HTML_TITLE.findall(text[:20_000]):
        clean = " ".join(html.unescape(_HTML_TAG.sub(" ", inner)).split())
        if clean and clean not in parts:
            parts.append(clean)
    return " – ".join(parts)[:300] or "HTML-sida utan rubrik"


def extract_error_detail(response: httpx2.Response) -> str:
    """Best-effort human readable error from RFC 7807 problem documents,
    PxWeb/Skolverket style ``{"message": ...}`` bodies or plain text."""
    text = response.text or ""
    try:
        body = response.json()
    except (ValueError, json.JSONDecodeError):
        if "<html" in text[:500].lower() or "<!doctype" in text[:200].lower():
            return _html_summary(text)
        return text.strip()[:500]
    if isinstance(body, dict):
        for key in ("detail", "title", "message", "error", "Message", "errorMessage"):
            value = body.get(key)
            if isinstance(value, str) and value.strip():
                extra = body.get("errors")
                if extra:
                    return f"{value.strip()} {json.dumps(extra, ensure_ascii=False)[:300]}"
                return value.strip()
        return json.dumps(body, ensure_ascii=False)[:500]
    return json.dumps(body, ensure_ascii=False)[:500]


def lifecycle_notices(response: httpx2.Response) -> list[str]:
    notices = [f"{name.title()}: {response.headers[name]}" for name in LIFECYCLE_HEADERS if name in response.headers]
    link = response.headers.get("link", "")
    if 'rel="deprecation"' in link or "rel=deprecation" in link:
        notices.append(f"Link: {link}")
    return notices


class HttpClient:
    """Thin wrapper around :class:`httpx2.AsyncClient` used by every source."""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
        rate_limits: Mapping[str, RateLimit] | None = None,
        sleep: Callable[[float], Awaitable[None]] = anyio.sleep,
    ) -> None:
        self.settings = settings
        self._client = httpx2.AsyncClient(
            transport=transport,
            timeout=httpx2.Timeout(settings.timeout_seconds),
            headers={"User-Agent": settings.user_agent},
            follow_redirects=True,
        )
        self._cache = TTLCache(
            settings.cache_max_entries, settings.cache_ttl_seconds, max_bytes=settings.cache_max_bytes
        )
        self._limiters = {host: SlidingWindowLimiter(limit) for host, limit in (rate_limits or {}).items()}
        self._sleep = sleep
        self.recent_notices: deque[str] = deque(maxlen=50)

    @property
    def cache(self) -> TTLCache:
        return self._cache

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "HttpClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def get_json(
        self,
        source: str,
        url: str,
        *,
        params: QueryParams | None = None,
        headers: Mapping[str, str] | None = None,
        use_cache: bool = True,
    ) -> ApiResponse:
        return await self.request(source, "GET", url, params=params, headers=headers, use_cache=use_cache)

    async def post_json(
        self,
        source: str,
        url: str,
        body: Any,
        *,
        params: QueryParams | None = None,
        headers: Mapping[str, str] | None = None,
        use_cache: bool = True,
    ) -> ApiResponse:
        return await self.request(
            source, "POST", url, params=params, json_body=body, headers=headers, use_cache=use_cache
        )

    async def request(
        self,
        source: str,
        method: str,
        url: str,
        *,
        params: QueryParams | None = None,
        json_body: Any = None,
        headers: Mapping[str, str] | None = None,
        use_cache: bool = True,
        expect: str = "json",
    ) -> ApiResponse:
        """Perform a request and decode the body.

        ``expect`` is ``"json"`` (decode JSON, fall back to text when the
        server answers with another content type) or ``"text"``.
        """
        request_headers = {"Accept": "application/json"}
        if headers:
            request_headers.update(headers)
        cleaned = _clean_params(params)
        key = _cache_key(method, url, cleaned, json_body, request_headers.get("Accept"))
        if use_cache:
            cached = self._cache.get(key)
            if cached is not None:
                return ApiResponse(
                    url=cached.url,
                    status=cached.status,
                    data=cached.data,
                    content_type=cached.content_type,
                    notices=list(cached.notices),
                    from_cache=True,
                )

        response = await self._send_with_retries(source, method, url, cleaned, json_body, request_headers)
        notices = lifecycle_notices(response)
        for notice in notices:
            self.recent_notices.append(f"{source}: {notice} ({response.request.url})")

        content_type = response.headers.get("content-type", "")
        data: Any
        if expect == "json" and ("json" in content_type or not content_type):
            try:
                data = response.json()
            except (ValueError, json.JSONDecodeError) as exc:
                raise UpstreamError(
                    source,
                    "Svaret kunde inte tolkas som JSON",
                    status=response.status_code,
                    url=str(response.request.url),
                    detail=(response.text or "")[:300],
                ) from exc
        elif expect == "json" and ("html" in content_type or "xml" in content_type):
            # A 200 HTML/XML page (portal, proxy or maintenance page) is never a usable JSON answer.
            raise UpstreamError(
                source,
                f"Svaret var inte JSON (content-type {content_type.split(';')[0].strip()})",
                status=response.status_code,
                url=str(response.request.url),
                detail=extract_error_detail(response),
            )
        elif expect == "json":
            # e.g. text/plain: some endpoints send JSON or a bare number with a plain content type.
            try:
                data = response.json()
            except (ValueError, json.JSONDecodeError):
                data = response.text
        else:
            data = response.text

        result = ApiResponse(
            url=str(response.request.url),
            status=response.status_code,
            data=data,
            content_type=content_type,
            notices=notices,
        )
        if use_cache:
            self._cache.set(key, result, size=len(response.content))
        return result

    async def _send_with_retries(
        self,
        source: str,
        method: str,
        url: str,
        params: dict[str, Any],
        json_body: Any,
        headers: dict[str, str],
    ) -> httpx2.Response:
        host = httpx2.URL(url).host
        limiter = self._limiters.get(host)
        attempts = max(1, self.settings.max_retries + 1)
        last_error: Exception | None = None
        for attempt in range(attempts):
            if limiter is not None:
                await limiter.acquire()
            try:
                response = await self._client.request(
                    method, url, params=params or None, json=json_body, headers=headers
                )
            except httpx2.TransportError as exc:
                last_error = exc
                if attempt + 1 < attempts:
                    await self._sleep(min(0.5 * 2**attempt, 8.0))
                    continue
                raise UpstreamError(
                    source,
                    f"Kunde inte nå API:t ({type(exc).__name__})",
                    url=url,
                    detail=str(exc) or None,
                ) from exc
            except httpx2.RequestError as exc:
                # TooManyRedirects, DecodingError (corrupt gzip) and the like: not worth retrying.
                raise UpstreamError(
                    source,
                    f"Anropet misslyckades ({type(exc).__name__})",
                    url=url,
                    detail=str(exc) or None,
                ) from exc
            except RuntimeError as exc:
                # e.g. "Cannot send a request, as the client has been closed".
                raise UpstreamError(source, "HTTP-klienten är inte tillgänglig", url=url, detail=str(exc)) from exc

            if response.status_code in RETRY_STATUSES and attempt + 1 < attempts:
                delay = _retry_after_seconds(response)
                await self._sleep(delay if delay is not None else min(0.5 * 2**attempt, 8.0))
                continue

            if response.status_code >= 400:
                raise UpstreamError(
                    source,
                    _status_message(response.status_code),
                    status=response.status_code,
                    url=str(response.request.url),
                    detail=extract_error_detail(response) or None,
                )
            return response

        raise UpstreamError(source, "Alla försök misslyckades", url=url, detail=str(last_error))


def _status_message(status: int) -> str:
    if status == 400:
        return "Ogiltig förfrågan (kontrollera koder och urval)"
    if status == 403:
        return "Åtkomst nekad, eller urvalet är för stort för API:t"
    if status == 404:
        return "Hittades inte (kontrollera id/kod)"
    if status == 429:
        return "För många anrop – API:ts anropsgräns nådd, försök igen om en stund"
    if 500 <= status < 600:
        return "Fel hos API-leverantören"
    return "API-fel"
