# SPDX-FileCopyrightText: 2026 Deniz Özer
#
# SPDX-License-Identifier: MIT

import json

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


async def test_lifecycle_history_does_not_reveal_another_clients_query():
    import httpx2

    secret = "private-user-question-123"

    def handler(request):
        assert request.url.params["query"] == secret
        return httpx2.Response(
            200,
            json={"tables": []},
            headers={
                "Deprecation": secret,
                "Sunset": "Wed, 30 Jun 2027 00:00:00 GMT",
                "Warning": f'299 example.se "{secret}"',
                "Link": f'<https://example.se/{secret}?q={secret}>; rel="deprecation"',
            },
        )

    server = build_server(
        Settings(enabled_sources=frozenset({"scb.statistik"})),
        transport=httpx2.MockTransport(handler),
        rate_limits={},
    )
    services = server.fuzzy_services
    services.acquire()  # HTTP serving keeps the shared services alive between connections.
    try:
        async with Client(server) as first:
            await call(first, "scb_search_tables", {"query": secret})
            async with Client(server) as second:
                simultaneous = await call(second, "fuzzy_api_notices")
        async with Client(server) as later:
            sequential = await call(later, "fuzzy_api_notices")
    finally:
        await services.release()
    assert (
        simultaneous
        == sequential
        == {
            "notices": [
                "scb: Deprecation: mottagen, datum kunde inte valideras",
                "scb: Sunset: 2027-06-30T00:00:00Z",
            ],
            "count": 2,
        }
    )
    assert secret not in str(sequential)


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
    assert {"https://app.exempel.se", "http://localhost:*"} <= set(security.allowed_origins)


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


async def test_tool_timeout_answers_before_the_client_gives_up(caplog):
    """FUZZY_MCP_TOOL_TIMEOUT: a slow agency gives a clear error in time (Eneo waits 60 s), and the pending upstream
    call is cancelled."""
    import anyio
    import httpx2

    finished: list[str] = []

    async def slow(request: httpx2.Request) -> httpx2.Response:
        await anyio.sleep(5)
        finished.append(str(request.url))
        return httpx2.Response(200, json={})

    settings = Settings(enabled_sources=frozenset({"scb"}), tool_timeout_seconds=0.2, max_retries=0)
    server = build_server(settings, transport=httpx2.MockTransport(slow), rate_limits={})
    async with Client(server, raise_exceptions=False) as client:
        start = anyio.current_time()
        result = await client.call_tool("scb_search_tables", {"query": "folkmängd"})
        elapsed = anyio.current_time() - start
    text = "".join(getattr(c, "text", "") for c in result.content)
    assert result.is_error and "scb_search_tables hann inte svara inom 0.2 s" in text
    assert elapsed < 2 and not finished
    assert any("avbröts efter 0.2 s" in r.getMessage() for r in caplog.records)


async def test_tool_timeout_is_off_by_default_and_leaves_fast_tools_alone():
    async with Client(build_server(Settings(tool_timeout_seconds=30), rate_limits={})) as client:
        result = await client.call_tool("ref_lookup_region", {"query": "Gävle"})
    assert not result.is_error and "2180" in "".join(getattr(c, "text", "") for c in result.content)
    assert Settings().tool_timeout_seconds == 0


def _slow_shortening(router, monkeypatch):
    """A large scb_search_tables answer, and a shortening that waits for ``release`` after setting ``started``."""
    import threading

    from fuzzy_mcp import server as server_module

    from .conftest import load_fixture

    tables = load_fixture("scb_tables.json")
    tables["tables"] = tables["tables"] * 60
    router.add("GET", r"/tables\?", tables)
    fit_to_limit = server_module.fit_to_limit
    started, release = threading.Event(), threading.Event()

    def slow(data, limit, cuts=()):
        started.set()
        assert release.wait(5), "händelseloopen stod still medan svaret kortades"
        return fit_to_limit(data, limit, cuts)

    monkeypatch.setattr(server_module, "fit_to_limit", slow)
    return started, release


async def test_output_cap_shortens_without_blocking_other_sessions(router, make_client, monkeypatch):
    """Issue #23: the output cap shortened on the event loop, so a large answer stalled every session. It runs in a
    worker thread: here the event loop releases the shortening while it is under way."""
    import anyio

    started, release = _slow_shortening(router, monkeypatch)

    async def other_session() -> None:
        assert await anyio.to_thread.run_sync(started.wait, 5)
        release.set()  # on the event loop

    async with make_client("scb", max_output_chars=3000) as client, anyio.create_task_group() as group:
        group.start_soon(other_session)
        result = await client.call_tool("scb_search_tables", {"query": "x", "page_size": 100})
    assert not result.is_error and "_kortat" in result.structured_content


async def test_tool_timeout_interrupts_a_slow_shortening(router, make_client, monkeypatch):
    """FUZZY_MCP_TOOL_TIMEOUT also covers the output cap: the answer does not wait for the shortening to finish."""
    import anyio

    started, release = _slow_shortening(router, monkeypatch)
    try:
        async with make_client("scb", max_output_chars=3000, tool_timeout_seconds=0.5) as client:
            start = anyio.current_time()
            result = await client.call_tool("scb_search_tables", {"query": "x", "page_size": 100})
            elapsed = anyio.current_time() - start
    finally:
        release.set()  # the abandoned thread ends
    assert started.is_set(), "tidsgränsen nåddes innan kortningen började"
    assert result.is_error and "hann inte svara inom 0.5 s" in result.content[0].text and elapsed < 3


@pytest.mark.parametrize("value", ["-1", "abc", "nan", "inf"])
def test_tool_timeout_must_be_seconds(monkeypatch, value):
    monkeypatch.setenv("FUZZY_MCP_TOOL_TIMEOUT", value)
    with pytest.raises(ValueError, match="FUZZY_MCP_TOOL_TIMEOUT"):
        Settings.from_env()


def test_tool_timeout_from_environment(monkeypatch):
    monkeypatch.setenv("FUZZY_MCP_TOOL_TIMEOUT", "50")
    assert Settings.from_env().tool_timeout_seconds == 50


PAGING_INPUTS = {"offset", "page", "start_page"}
PAGING_OUTPUTS = ("next_offset", "next_page", "next_skip_matches")


@pytest.mark.parametrize("v2", [False, True])
def test_tools_with_paging_declare_cut_contracts(v2):
    """A tool that pages (an offset/page/start_page input or a next_* pointer in its answer) must tell the output cap
    how its paging continues after a cut (issue #20); otherwise the cap can only leave a dead end."""
    from fuzzy_mcp.config import DEFAULT_API_VERSIONS
    from fuzzy_mcp.core import inline_schema_refs

    versions = {**DEFAULT_API_VERSIONS, **({"fohm": "v2", "skolverket_statistik": "v2"} if v2 else {})}
    server = build_server(Settings(api_versions=versions))
    missing, unresolved = [], []
    for tool in server._tool_manager.list_tools():
        schema = inline_schema_refs(tool.fn_metadata.output_schema or {})
        output = json.dumps(schema)
        pages = PAGING_INPUTS & set(tool.parameters.get("properties", {})) or any(k in output for k in PAGING_OUTPUTS)
        cuts = getattr(tool.fn, "_fuzzy_cuts", ())
        if pages and not cuts:
            missing.append(tool.name)
        for cut in cuts:
            node = schema
            for part in cut.items.split("."):
                node = (node.get("properties") or {}).get(part, {})
                node = next((s for s in node.get("anyOf", []) if s.get("type") != "null"), node)  # optional field
            if node.get("type") != "array":
                unresolved.append(f"{tool.name}: {cut.items}")
    assert not missing, f"verktyg som bläddrar utan @cut_contract: {missing}"
    assert not unresolved, f"kontrakt vars lista inte finns i utdataschemat: {unresolved}"
