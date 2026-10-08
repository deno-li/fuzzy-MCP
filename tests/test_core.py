# SPDX-FileCopyrightText: 2026 Deniz Özer
#
# SPDX-License-Identifier: MIT

from typing import Annotated

import pytest
from mcp import Client
from mcp.server.mcpserver import MCPServer
from pydantic import Field

from fuzzy_mcp.core import READ_ONLY_OPEN, compact, tool_errors
from fuzzy_mcp.errors import InvalidInputError, UpstreamError

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _server() -> MCPServer:
    server = MCPServer("t")

    @server.tool(annotations=READ_ONLY_OPEN)
    @tool_errors
    async def fails(kind: Annotated[str, Field(description="vilket fel")] = "upstream") -> dict:
        if kind == "upstream":
            raise UpstreamError("scb", "Hittades inte", status=404, url="https://x/tables/TAB0")
        raise InvalidInputError("Ogiltig variabel 'Regio'")

    return server


async def test_tool_errors_surface_messages_and_keep_schema():
    async with Client(_server()) as client:
        tools = (await client.list_tools()).tools
        schema = tools[0].input_schema
        assert schema["properties"]["kind"]["description"] == "vilket fel"
        assert tools[0].annotations.read_only_hint is True
        res = await client.call_tool("fails", {})
        assert res.is_error and "[scb] Hittades inte | HTTP 404" in res.content[0].text
        res = await client.call_tool("fails", {"kind": "input"})
        assert res.is_error and "Ogiltig variabel 'Regio'" in res.content[0].text


def test_compact_drops_empty():
    assert compact({"a": None, "b": [], "c": {"d": None}, "e": [1, {"f": None}], "g": 0}) == {
        "e": [1, {}],
        "g": 0,
    }


def test_inline_schema_refs():
    from fuzzy_mcp.core import inline_schema_refs

    schema = {
        "type": "object",
        "$defs": {"Ref": {"type": "object", "properties": {"code": {"type": "string"}}}},
        "properties": {"items": {"type": "array", "items": {"$ref": "#/$defs/Ref"}, "description": "lista"}},
    }
    out = inline_schema_refs(schema)
    assert "$defs" not in out and out["properties"]["items"]["items"]["properties"]["code"] == {"type": "string"}
    recursive = {"$defs": {"N": {"type": "object", "properties": {"next": {"$ref": "#/$defs/N"}}}}, "$ref": "#/$defs/N"}
    assert "$defs" in inline_schema_refs(recursive)  # recursion keeps its definitions


async def test_no_tool_schema_uses_refs():
    import json

    from mcp import Client

    from fuzzy_mcp.config import Settings
    from fuzzy_mcp.server import build_server

    async with Client(build_server(Settings(), rate_limits={})) as client:
        tools = (await client.list_tools()).tools
    assert len(tools) >= 80
    assert not [t.name for t in tools if "$ref" in json.dumps(t.input_schema)]


def test_fit_to_limit_trims_inner_values_before_records_and_keeps_rows_whole():
    import json

    from fuzzy_mcp.core import TRUNCATION_KEY, client_size, compact_json, fit_to_limit

    data = {
        "variables": [{"code": f"V{v}", "values": [f"kod {i} med text" for i in range(400)]} for v in range(3)],
        "rows": [["Område", i, 1.5] for i in range(50)],
        "geometry": {"coordinates": [[[600000.0 + i, 6700000.0] for i in range(10)]]},
        "title": "Ä",
    }
    out, cut = fit_to_limit(data, 6000)
    assert cut and client_size(compact_json(out)) <= 6000
    assert [v["code"] for v in out["variables"]] == ["V0", "V1", "V2"]  # records kept, inner values shortened
    assert all(len(v["values"]) < 400 for v in out["variables"])
    assert all(len(row) == 3 for row in out["rows"])  # table rows are never cut inside
    assert out["geometry"] == data["geometry"]  # positional data untouched
    fields = {c["falt"] for c in out[TRUNCATION_KEY]["kortade"]}
    assert "variables[].values" in fields
    assert len(data["variables"][0]["values"]) == 400  # input untouched
    json.loads(compact_json(out))
    small, cut = fit_to_limit({"a": [1, 2]}, 5000)
    assert not cut and small == {"a": [1, 2]}


def test_fit_to_limit_guarantees_size_or_reports_failure():
    from fuzzy_mcp.core import client_size, compact_json, fit_to_limit

    long_text, cut = fit_to_limit({"description": "x" * 20000}, 3000)
    assert cut and client_size(compact_json(long_text)) <= 3000 and "[kortad]" in long_text["description"]
    impossible, cut = fit_to_limit({"recipe": {"post_body": {"selection": ["a" * 150] * 100}}}, 1000)
    assert cut and impossible is None


async def test_output_limit_applies_to_tool_answers(router, make_client):
    import json

    from fuzzy_mcp.core import TRUNCATION_KEY

    from .conftest import load_fixture

    router.add("GET", r"/tables\?", load_fixture("scb_tables.json"))
    tables = load_fixture("scb_tables.json")
    tables["tables"] = tables["tables"] * 60
    router.routes[-1] = type(router.routes[-1])("GET", router.routes[-1].pattern, tables)
    async with make_client("scb", max_output_chars=3000) as client:
        result = await client.call_tool("scb_search_tables", {"query": "x", "page_size": 100})
    async with make_client("scb") as client:
        unlimited = await client.call_tool("scb_search_tables", {"query": "x", "page_size": 100})
    assert not result.is_error and len(result.content) == 1
    text = result.content[0].text
    assert len(json.dumps(text, ensure_ascii=False)) + 100 <= 3000 and TRUNCATION_KEY in json.loads(text)
    assert result.structured_content[TRUNCATION_KEY]["kortade"][0]["falt"] == "tables"
    # The cap is per server: a server built afterwards without a cap is not affected.
    assert TRUNCATION_KEY not in (unlimited.structured_content or {})
