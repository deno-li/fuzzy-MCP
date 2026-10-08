# SPDX-FileCopyrightText: 2026 Deniz Özer
#
# SPDX-License-Identifier: MIT

"""Shared test helpers.

All network access is mocked with :class:`httpx2.MockTransport`; tests never
reach the real APIs. Use :class:`Router` to map requests to canned JSON.
"""

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx2
import pytest
from mcp import Client

from fuzzy_mcp.config import Settings
from fuzzy_mcp.server import build_server

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


Responder = Callable[[httpx2.Request], httpx2.Response] | dict | list | str | httpx2.Response


@dataclass
class Route:
    method: str
    pattern: re.Pattern[str]
    responder: Responder


@dataclass
class Router:
    """Maps ``METHOD url-regex`` to a responder and records every request.

    The regex is matched with ``re.search`` against the full URL including the
    query string, e.g. ``router.add("GET", r"/tables/TAB638/metadata", {...})``.
    """

    routes: list[Route] = field(default_factory=list)
    requests: list[httpx2.Request] = field(default_factory=list)

    def add(self, method: str, pattern: str, responder: Responder) -> "Router":
        self.routes.append(Route(method.upper(), re.compile(pattern), responder))
        return self

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        url = str(request.url)
        for route in self.routes:
            if route.method == request.method and route.pattern.search(url):
                responder = route.responder
                if callable(responder) and not isinstance(responder, httpx2.Response):
                    return responder(request)
                if isinstance(responder, httpx2.Response):
                    return responder
                if isinstance(responder, str):
                    return httpx2.Response(200, text=responder)
                return httpx2.Response(200, json=responder)
        return httpx2.Response(404, json={"message": f"no mock route for {request.method} {url}"})

    def last(self) -> httpx2.Request:
        return self.requests[-1]

    def json_body(self, index: int = -1) -> Any:
        return json.loads(self.requests[index].content.decode("utf-8"))


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def router() -> Router:
    return Router()


@pytest.fixture
def make_client(router: Router):
    """``async with make_client("scb") as client`` → in-process MCP client whose
    HTTP traffic goes to ``router``. Pass several sources as a comma list."""

    def factory(sources: str = "scb,fohm,skolverket,dataportal,reference", **settings_overrides: Any) -> Client:
        enabled = frozenset(s.strip() for s in sources.split(",") if s.strip())
        settings = Settings(enabled_sources=enabled, max_retries=0, cache_ttl_seconds=0, **settings_overrides)
        server = build_server(settings, transport=httpx2.MockTransport(router), rate_limits={})
        return Client(server, raise_exceptions=False)

    return factory


async def call(client: Client, tool: str, args: dict[str, Any] | None = None) -> Any:
    """Call a tool and return structured content (or parsed text). Raises
    AssertionError with the error text when the tool reports an error."""
    result = await client.call_tool(tool, args or {})
    text = "".join(getattr(c, "text", "") for c in result.content)
    if result.is_error:
        raise AssertionError(f"tool {tool} failed: {text}")
    if result.structured_content is not None:
        content = result.structured_content
        if set(content) == {"result"}:
            return content["result"]
        return content
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


async def call_error(client: Client, tool: str, args: dict[str, Any] | None = None) -> str:
    """Call a tool that is expected to fail and return the error text."""
    result = await client.call_tool(tool, args or {})
    text = "".join(getattr(c, "text", "") for c in result.content)
    assert result.is_error, f"expected {tool} to fail, got: {text[:300]}"
    return text
