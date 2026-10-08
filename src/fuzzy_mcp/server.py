# SPDX-FileCopyrightText: 2026 Deniz Özer
#
# SPDX-License-Identifier: MIT

"""Builds the MCP server and registers every enabled source module."""

import functools
import importlib
import inspect
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import httpx2
from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, TextContent
from pydantic_core import to_jsonable_python
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import __version__
from .config import Settings
from .core import (
    Services,
    client_size,
    compact_json,
    fit_to_limit,
    inline_schema_refs,
    structured,
    too_large_result,
)
from .http import RateLimit

INSTRUCTIONS = """\
Den här servern ger läsåtkomst till svensk öppen data:

* SCB Statistikdatabasen (PxWebApi 2): verktyg `scb_*`.
  Arbetsgång: `scb_search_tables` → `scb_get_table_metadata` (variabler, värdekoder, enheter, länkar)
  → `scb_get_table_data` med ett urval {variabelkod: [värdekoder eller uttryck]}; `scb_build_query`
  ger delbar URL och Power Query (M).
* SCB:s öppna geodata (WFS): `scb_geodata_*` – DeSO, RegSO, tätorter m.m.; koderna kopplar till SCB-statistik.
* Folkhälsomyndigheten Folkhälsodata (PxWeb): verktyg `fohm_*`, samma arbetsgång
  (`fohm_browse`/`fohm_search_tables` → `fohm_get_table_metadata` → `fohm_get_table_data`).
* Skolverket: Skolenhetsregistret (`skolverket_*school_unit*`, huvudmän), Läroplan/Syllabus-API
  (ämnen, kurser, program, läroplaner), Planerad utbildning (`skolverket_pe_*`), Susa-navet
  (`skolverket_susa_*`) och Skolverkets statistikdatabas (`skolverket_stat_*`: kommunala jämförelsetal
  och underlag för analys per kommun, huvudman och skolenhet).
* Sveriges dataportal: `dataportal_*` för att hitta dataset (DCAT-AP-SE) från alla myndigheter.
* Referensdata: `ref_*` och resurser under `fuzzy://` – kodlistor (kommun/län, betygsskala, skolformer,
  SS 12000), entitetskatalog och nycklar för att koppla ihop källorna.

Urvalsuttryck i PxWeb: "*" (alla), "202*" (jokertecken), "TOP(5)" (senaste 5 perioderna för tid),
"BOTTOM(n)", "RANGE(a,b)", "FROM(a)", "TO(b)". Utelämnade variabler elimineras om tabellen tillåter det.
Koppla ihop källor via kommunkod (4 siffror, t.ex. 0180 = Stockholm), länskod (2 siffror),
skolenhetskod (8 siffror), organisationsnummer (huvudman) och DeSO/RegSO-kod. Ange alltid källa
(myndighet + tabell/endpoint) när du redovisar siffror. All data är öppen; se `fuzzy://sources` för licenser.
Namn på enskilda personer (t.ex. rektor, c/o-adress, kontaktperson) returneras bara när verktyget anropas
med en uttrycklig parameter för personuppgifter – använd den bara när användaren behöver uppgiften.
"""

# Default request budgets per host. SCB publishes 30 calls per 10 s in /config; FoHM's
# PxWeb allows far more (1 000 per 10 s) but community practice is a few calls per second.
DEFAULT_RATE_LIMITS: dict[str, RateLimit] = {
    "statistikdatabasen.scb.se": RateLimit(calls=30, per_seconds=10),
    "api.scb.se": RateLimit(calls=10, per_seconds=10),
    "geodata.scb.se": RateLimit(calls=10, per_seconds=10),
    "fohm-app.folkhalsomyndigheten.se": RateLimit(calls=20, per_seconds=10),
    "api.skolverket.se": RateLimit(calls=20, per_seconds=10),
    "statistikdatabasen.skolverket.se": RateLimit(calls=10, per_seconds=10),
    "admin.dataportal.se": RateLimit(calls=10, per_seconds=10),
}

# source key -> modules exposing ``register(server, services)``
SOURCE_MODULES: dict[str, tuple[str, ...]] = {
    "reference": ("fuzzy_mcp.reference.tools",),
    "scb.statistik": ("fuzzy_mcp.sources.scb",),
    "scb.geodata": ("fuzzy_mcp.sources.scb_geodata",),
    "fohm": ("fuzzy_mcp.sources.fohm",),
    "skolverket.skolenhetsregistret": ("fuzzy_mcp.sources.skolverket.skolenhetsregistret",),
    "skolverket.syllabus": ("fuzzy_mcp.sources.skolverket.syllabus",),
    "skolverket.planerad": ("fuzzy_mcp.sources.skolverket.planned_educations",),
    "skolverket.susa": ("fuzzy_mcp.sources.skolverket.susa_navet",),
    "skolverket.statistik": ("fuzzy_mcp.sources.skolverket.statistikdatabas",),
    "dataportal": ("fuzzy_mcp.sources.dataportal",),
}


def build_server(
    settings: Settings | None = None,
    *,
    transport: httpx2.AsyncBaseTransport | None = None,
    rate_limits: dict[str, RateLimit] | None = None,
) -> MCPServer[Services]:
    """Create the server.

    ``transport`` lets tests inject :class:`httpx2.MockTransport`.
    """
    settings = settings or Settings.from_env()
    services = Services(
        settings=settings,
        transport=transport,
        rate_limits=DEFAULT_RATE_LIMITS if rate_limits is None else rate_limits,
    )

    @asynccontextmanager
    async def lifespan(_: MCPServer[Services]) -> AsyncIterator[Services]:
        # Entered once per session (SSE) or in-process client: share one HTTP client,
        # cache and rate limiter, and close it only when the last session ends.
        services.acquire()
        try:
            yield services
        finally:
            await services.release()

    server: MCPServer[Services] = MCPServer(
        name="fuzzy-mcp",
        title="Svensk öppen data – SCB, Skolverket, Folkhälsomyndigheten",
        description="Statistik, skolregister, läroplaner och folkhälsodata från myndigheters öppna API:er.",
        instructions=INSTRUCTIONS,
        version=__version__,
        website_url="https://github.com/deno-li/fuzzy-MCP",
        lifespan=lifespan,
    )

    @server.custom_route("/healthz", methods=["GET"], include_in_schema=False)
    async def healthz(_: Request) -> JSONResponse:
        """Liveness for HTTP transports (load balancers, container HEALTHCHECK). Open without a token, so it
        reveals nothing but the status; it never calls the agencies' APIs (scripts/smoke_test.py --live does)."""
        return JSONResponse({"status": "ok"}, headers={"Cache-Control": "no-store"})

    # The HTTP entry point holds a reference for the process lifetime (the SSE transport enters the lifespan per
    # connection, which would otherwise drop the shared HTTP client, cache and rate limiter between connections).
    server.fuzzy_services = services  # type: ignore[attr-defined]

    from .meta import register as register_meta
    from .prompts import register as register_prompts

    register_meta(server, services)
    register_prompts(server, services)
    for source, modules in SOURCE_MODULES.items():
        if not settings.source_enabled(source):
            continue
        for module_name in modules:
            module = importlib.import_module(module_name)
            register: Callable[[MCPServer[Any], Services], None] = module.register
            register(server, services)
    for tool in server._tool_manager.list_tools():
        # Plain input schemas (no $ref/$defs) for LLM providers that reject references.
        tool.parameters = inline_schema_refs(tool.parameters)
        # Docstring indentation is kept by Python < 3.13: strip it so every version sends the same, shorter text.
        if tool.description:
            tool.description = inspect.cleandoc(tool.description)
        _compact_plain_results(tool)
        if settings.max_output_chars > 0:
            _limit_output(tool, settings.max_output_chars)
    return server


def _limit_output(tool: Any, limit: int) -> None:
    """Per-server size cap (FUZZY_MCP_MAX_OUTPUT_CHARS): shorten structured answers so a client that cuts long
    results (Eneo: 32 768 characters) still receives valid JSON; report an error if even that cannot fit."""
    fn = tool.fn

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        result = await fn(*args, **kwargs)
        if not isinstance(result, CallToolResult) or result.is_error:
            return result
        data = result.structured_content
        if not isinstance(data, dict):
            text = "".join(getattr(block, "text", "") for block in result.content)
            return result if client_size(text) <= limit else too_large_result(client_size(text), limit)
        fitted, changed = fit_to_limit(data, limit)
        if not changed:
            return result
        if fitted is None:
            return too_large_result(client_size(compact_json(data)), limit)
        return CallToolResult(content=[TextContent(type="text", text=compact_json(fitted))], structured_content=fitted)

    tool.fn = wrapper


def _compact_plain_results(tool: Any) -> None:
    """Tools that return a plain dict/list get the same treatment as ``core.structured``: compact JSON in ONE
    text block, the size cap, and structured content in the shape the SDK validates against
    (``{"result": ...}`` for wrapped outputs)."""
    if "CallToolResult" in str(inspect.signature(tool.fn).return_annotation):
        return
    fn = tool.fn
    wrap = bool(tool.fn_metadata.wrap_output and tool.fn_metadata.output_schema is not None)

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        result = await fn(*args, **kwargs)
        if isinstance(result, CallToolResult):
            return result
        value = to_jsonable_python(result)
        data = {"result": value} if wrap else value
        return structured(data, drop_empty=False) if isinstance(data, dict) else result

    tool.fn = wrapper
