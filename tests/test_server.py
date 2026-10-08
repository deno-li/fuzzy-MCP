# SPDX-FileCopyrightText: 2026 Deniz Özer
#
# SPDX-License-Identifier: MIT

import pytest
from mcp import Client

from fuzzy_mcp.config import Settings
from fuzzy_mcp.server import build_server

from .conftest import call

pytestmark = pytest.mark.anyio


async def test_meta_tools_and_sources_resource():
    server = build_server(Settings(enabled_sources=frozenset()), rate_limits={})
    async with Client(server) as client:
        names = {t.name for t in (await client.list_tools()).tools}
        assert {"fuzzy_list_sources", "fuzzy_api_notices"} <= names
        sources = await call(client, "fuzzy_list_sources")
        assert any(s["base_url"] == "https://statistikdatabasen.scb.se/api/v2" for s in sources)
        assert all(s["enabled"] is False for s in sources)
        notices = await call(client, "fuzzy_api_notices")
        assert notices == {"notices": [], "count": 0}
        res = await client.read_resource("fuzzy://sources")
        assert "Skolenhetsregistret" in res.contents[0].text


async def test_sub_sources_can_be_enabled_individually():
    from fuzzy_mcp.config import SUB_SOURCES

    settings = Settings(enabled_sources=frozenset({"skolverket.skolenhetsregistret", "reference"}))
    assert settings.source_enabled("skolverket.skolenhetsregistret") and settings.source_enabled("skolverket")
    assert not settings.source_enabled("skolverket.susa") and not settings.source_enabled("scb")
    async with Client(build_server(settings, rate_limits={})) as client:
        names = {t.name for t in (await client.list_tools()).tools}
    assert "skolverket_get_school_unit" in names
    assert not any(n.startswith(("skolverket_susa_", "skolverket_pe_", "scb_", "fohm_")) for n in names)
    assert {"scb.statistik", "scb.geodata", "skolverket.statistik"} <= set(SUB_SOURCES)


async def test_tool_descriptions_have_no_docstring_indentation():
    # Python < 3.13 keeps docstring indentation; the server strips it so all versions send the same text.
    async with Client(build_server(Settings(), rate_limits={})) as client:
        tools = (await client.list_tools()).tools
    indented = [t.name for t in tools if any(line.startswith(" ") for line in (t.description or "").splitlines())]
    assert not indented


def test_unknown_source_rejected(monkeypatch):
    monkeypatch.setenv("FUZZY_MCP_SOURCES", "scb,skolverket.okand")
    with pytest.raises(ValueError, match="Okända källor"):
        Settings.from_env()


async def test_closing_one_session_keeps_the_shared_http_client_for_others(router):
    import httpx2

    router.add("GET", r"/config$", {"apiVersion": "2.3.2", "maxDataCells": 150000})
    server = build_server(
        Settings(enabled_sources=frozenset({"scb"}), max_retries=0, cache_ttl_seconds=0),
        transport=httpx2.MockTransport(router),
        rate_limits={},
    )
    async with Client(server) as first:
        async with Client(server) as second:
            await call(second, "scb_get_config")
        # The second session has ended; the first must still reach the API.
        config = await call(first, "scb_get_config")
    assert config["apiVersion"] == "2.3.2"


async def test_services_close_http_client_only_after_last_session():
    from fuzzy_mcp.core import Services

    services = Services(settings=Settings(enabled_sources=frozenset()))
    services.acquire()
    services.acquire()
    http = services.http
    await services.release()
    assert services.http is http  # still shared: cache and rate limiter survive
    await services.release()
    assert services._http is None


def test_transport_security_allowlists():
    from fuzzy_mcp.__main__ import transport_security

    assert transport_security("") is None
    security = transport_security("mcp.exempel.se", "https://app.exempel.se")
    assert security is not None and security.enable_dns_rebinding_protection
    assert {"mcp.exempel.se", "mcp.exempel.se:*", "[::1]:*", "localhost:*"} <= set(security.allowed_hosts)
    assert "https://app.exempel.se" in security.allowed_origins and "http://localhost:*" in security.allowed_origins


async def test_healthz_route_on_http_transport():
    import httpx2

    server = build_server(Settings(enabled_sources=frozenset({"reference", "scb.statistik"})), rate_limits={})
    app = server.streamable_http_app(transport_security=None)
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://127.0.0.1:8000") as http:
        response = await http.get("/healthz")
        assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
        # Open without a token, so it reveals nothing but the status (no version or sources).
        assert response.json() == {"status": "ok"}
        assert (await http.post("/healthz")).status_code == 405
