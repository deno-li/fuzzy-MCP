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


@pytest.mark.parametrize(
    "source,key,prefix,version",
    [
        ("fohm", "fohm", "fohm", "v1"),
        ("skolverket.statistik", "skolverket_statistik", "skolverket_stat", "v1"),
        ("scb.statistik", "scb", "scb", "v2"),
        ("fohm", "fohm", "fohm", "v2"),
        ("skolverket.statistik", "skolverket_statistik", "skolverket_stat", "v2"),
    ],
)
async def test_query_recipes_are_complete_or_rejected(router, make_client, source, key, prefix, version):
    from fuzzy_mcp.config import DEFAULT_API_VERSIONS, DEFAULT_BASE_URLS
    from fuzzy_mcp.core import client_size

    # The original v1 bug reduced 1,000 codes to one, left cells=1000 and cut executable M code.
    # v2 must also reject a recipe when its protected POST body fits but its curl command does not.
    codes = [f"{i:06}" for i in range(1000 if version == "v1" else 2000)]
    if version == "v1":
        metadata = {
            "title": "Example table",
            "variables": [{"code": "Region", "text": "Region", "values": codes, "valueTexts": codes}],
        }
        router.add("GET", r"/sv/\?config$", {"maxCells": 100000})
        router.add("GET", r"Example\.px$", metadata)
        table_args = {"table_path": "Example.px"}
    else:
        metadata = {
            "label": "Example table",
            "id": ["Region"],
            "size": [len(codes)],
            "dimension": {
                "Region": {"label": "Region", "category": {"index": codes, "label": {code: code for code in codes}}}
            },
        }
        router.add("GET", r"/config$", {"maxDataCells": 100000})
        router.add("GET", r"/tables/TAB1/metadata", metadata)
        table_args = {"table_id": "TAB1"}
    settings = {
        "base_urls": {**DEFAULT_BASE_URLS, key: f"https://example.se/api/{version}"},
        "api_versions": {**DEFAULT_API_VERSIONS, key: version},
    }
    args = {**table_args, "selection": {"Region": codes}}
    small_args = {**table_args, "selection": {"Region": codes[:2]}}
    async with make_client(source, **settings) as client:
        original = await client.call_tool(f"{prefix}_build_query", args)
        small_original = await client.call_tool(f"{prefix}_build_query", small_args)
    assert not original.is_error and not small_original.is_error
    assert client_size(original.content[0].text) > 30000
    async with make_client(source, max_output_chars=30000, **settings) as client:
        oversized = await client.call_tool(f"{prefix}_build_query", args)
        small = await client.call_tool(f"{prefix}_build_query", small_args)
    assert oversized.is_error and oversized.structured_content is None
    assert "återanvändbar fråga" in oversized.content[0].text and "får inte kortas" in oversized.content[0].text
    assert not small.is_error
    assert small.structured_content == small_original.structured_content
    assert small.content == small_original.content  # URLs, body, executable strings and nested values all unchanged


# --- issue #20: truthful metadata and skip-free paging after the output cap shortens an answer ---------------------


def _fit(data, limit, *cuts):
    from fuzzy_mcp.core import client_size, compact_json, fit_to_limit

    out, cut = fit_to_limit(data, limit, cuts)
    assert cut and out is not None and client_size(compact_json(out)) <= limit
    return out


def test_fit_to_limit_sets_flags_where_the_list_was_cut():
    data = {
        "data": {"rows": [["Område", i, "x" * 30] for i in range(400)], "total_rows": 400, "truncated": False},
        "variables": [
            {"code": f"V{v}", "values": [f"kod {i}" for i in range(n)], "values_truncated": False}
            for v, n in enumerate((300, 2))
        ],
        "items": [{"id": i, "name": "n" * 40} for i in range(200)],
        "returned": 200,
    }
    out = _fit(data, 9000)
    assert out["data"]["truncated"] is True and out["data"]["total_rows"] == 400
    assert [v["code"] for v in out["variables"]] == ["V0", "V1"]  # variables keep their length
    for variable in out["variables"]:
        assert variable["values_truncated"] is (len(variable["values"]) < (300 if variable["code"] == "V0" else 2))
    assert out["returned"] == len(out["items"]) < 200


def test_fit_to_limit_keeps_protected_lists_and_drops_raw_first():
    from fuzzy_mcp.core import TRUNCATION_KEY

    data = {
        "items": [{"id": i} for i in range(30)],
        "notes": [f"notis {i}" for i in range(20)],
        "filters": {"municipality_code": [f"{i:04}" for i in range(40)]},
        "raw": {"data": [{"x": "y" * 50} for _ in range(200)]},
    }
    out = _fit(data, 2500)
    assert "raw" not in out and out[TRUNCATION_KEY]["utelamnade_falt"] == ["raw"]
    assert out["notes"] == data["notes"] and out["filters"] == data["filters"] and out["items"] == data["items"]
    from fuzzy_mcp.core import fit_to_limit

    impossible, cut = fit_to_limit({"notes": ["n" * 100] * 40, "items": [1, 2]}, 1000)
    assert cut and impossible is None  # protected content alone over the cap is an error, never a silent cut


def test_fit_to_limit_never_shortens_urls_and_queries_and_terminates():
    from fuzzy_mcp.core import TRUNCATION_KEY

    url = "https://example.se/api?" + "&".join(f"p{i}=v{i}" for i in range(80))
    data = {"request_url": url, "solr_query": "q" * 600, "link": url, "description": "ord " * 1000}
    out = _fit(data, 2600)
    assert out["request_url"] == url and out["solr_query"] == data["solr_query"] and out["link"] == url
    assert out["description"].endswith(" … [kortad]")
    entry = next(e for e in out[TRUNCATION_KEY]["kortade"] if e["falt"] == "description")
    assert entry["enhet"] == "tecken" and entry["fore"] == 4000
    from fuzzy_mcp.core import fit_to_limit

    stuck, cut = fit_to_limit({"a": "x" * 211, "b": "y" * 211}, 300)
    assert cut and stuck is None  # a string at the floor is not picked again forever


def test_fit_to_limit_sums_cut_lists_per_path_and_merges_the_tools_own_note():
    from fuzzy_mcp.core import TRUNCATION_KEY

    data = {
        "groups": [{"documents": [{"name": "d" * 30} for _ in range(40)]} for _ in range(3)],
        TRUNCATION_KEY: {
            "meddelande": "Verktygets egen notis.",
            "kortade": [{"falt": "kategorier", "fore": 9, "efter": 0}],
        },
    }
    out = _fit(data, 2000)
    note = out[TRUNCATION_KEY]
    assert note["meddelande"].startswith("Verktygets egen notis. Svaret kortades")
    assert note["kortade"][0] == {"falt": "kategorier", "fore": 9, "efter": 0}
    entry = next(e for e in note["kortade"] if e["falt"] == "groups[].documents")
    assert entry["fore"] == 120 and entry["efter"] == sum(len(g["documents"]) for g in out["groups"])
    assert entry["listor"] == 3


def test_undeclared_cut_removes_paging_pointers():
    data = {
        "items": [{"id": i, "t": "x" * 40} for i in range(100)],
        "offset": 0,
        "next_offset": 100,
        "next_page": 2,
        "truncated": True,
    }
    out = _fit(data, 2000)
    assert out["truncated"] is True
    assert "next_offset" not in out and "next_page" not in out  # a dead end, never a gap


def test_offset_cut_points_at_the_first_item_not_shown():
    from fuzzy_mcp.core import TRUNCATION_KEY, OffsetCut

    rows = [{"id": i, "t": "x" * 40} for i in range(100)]
    last_page = {
        "datasets": rows,
        "offset": 850,
        "truncated": False,
        "returned": 100,
        "notes": ["Svaret kortades till 100 rader.", "annat"],
    }
    out = _fit(last_page, 2500, OffsetCut("datasets", stale_notes=("Svaret kortades till",)))
    kept = len(out["datasets"])
    assert out["truncated"] is True and out["returned"] == kept and out["next_offset"] == 850 + kept
    assert out["notes"] == ["annat"]
    assert out[TRUNCATION_KEY]["fortsatt"] == [{"falt": "datasets", "anrop": {"offset": 850 + kept}}]
    out = _fit({"items": rows, "offset": 0}, 2500, OffsetCut("items", next_field=None))
    assert "next_offset" not in out and out[TRUNCATION_KEY]["fortsatt"][0]["anrop"] == {"offset": len(out["items"])}
    out = _fit(
        {"part": {"items": rows, "offset": 0, "filters": {"status": ["AKTIV"]}}, "since": "2026-01-01"},
        2500,
        OffsetCut(
            "part.items", tool="other_tool", call=lambda root, holder, pos: {"since": root["since"], "offset": pos}
        ),
    )
    assert out[TRUNCATION_KEY]["fortsatt"] == [
        {
            "falt": "part.items",
            "anrop": {"since": "2026-01-01", "offset": len(out["part"]["items"])},
            "verktyg": "other_tool",
        }
    ]


def test_page_continuation_never_skips_and_always_progresses():
    from fuzzy_mcp.core import page_continuation

    for size in (1, 2, 7, 20, 25, 37, 100):
        for page in range(40):
            for kept in range(1, size):
                first_unseen = page * size + kept
                next_page, next_size = page_continuation(first_unseen, kept)
                start = next_page * next_size
                assert start <= first_unseen < start + next_size  # nothing skipped
                assert start > page * size  # progress
                assert (kept + 1) // 2 <= next_size <= kept
                if page == 0:
                    assert start == first_unseen  # exact on the first page


def test_page_cut_replaces_next_page_with_a_skip_free_continuation():
    from fuzzy_mcp.core import TRUNCATION_KEY, PageCut

    rows = [{"id": i, "t": "x" * 40} for i in range(20)]
    for first_page in (0, 1):
        data = {
            "rows": rows,
            "page": first_page + 3,
            "page_size": 20,
            "total_pages": 9,
            "truncated": True,
            "next_page": first_page + 4,
        }
        out = _fit(data, 1200, PageCut("rows", size_argument="size", first_page=first_page))
        kept = len(out["rows"])
        assert "next_page" not in out and out["truncated"] is True
        call = out[TRUNCATION_KEY]["fortsatt"][0]["anrop"]
        start = (call["page"] - first_page) * call["size"]
        assert start <= 3 * 20 + kept < start + call["size"]
    compact = PageCut("rows", when=("variant", "compact"))
    out = _fit({"rows": rows, "page": 0, "page_size": 20, "variant": "full", "next_page": 1}, 1200, compact)
    assert "fortsatt" not in out[TRUNCATION_KEY] and "next_page" not in out


def test_cursor_cut_never_skips_and_keeps_page_zero():
    from fuzzy_mcp.core import CursorCut

    rows = [{"id": i, "t": "x" * 40} for i in range(30)]
    data = {"events": rows, "page_size": 50, "start_page": 0, "skip_matches": 0, "complete_scan": True, "hint": "h"}
    out = _fit(data, 1500, CursorCut("events"))
    kept = len(out["events"])
    assert out["next_page"] == 0 and out["next_skip_matches"] == kept  # page 0 is a cursor, not the end
    assert out["complete_scan"] is False and "hint" not in out
    data = {"events": rows, "page_size": 10, "start_page": 2, "skip_matches": 25}  # skip beyond a page
    out = _fit(data, 1500, CursorCut("events"))
    page, skip = out["next_page"], out.get("next_skip_matches", 0)
    assert page * 10 + skip <= 2 * 10 + 10 + len(out["events"])  # at or before the first row not shown


def test_codes_cut_names_or_asks_for_the_items_left_out():
    from fuzzy_mcp.core import TRUNCATION_KEY, CodesCut

    courses = [{"code": f"K{i:03}", "text": "x" * 60} for i in range(40)]
    out = _fit({"courses": courses}, 1500, CodesCut("courses", argument="course_codes"))
    kept = len(out["courses"])
    assert out[TRUNCATION_KEY]["fortsatt"][0]["anrop"] == {"course_codes": [c["code"] for c in courses[kept:]]}
    out = _fit({"items": [{"version": i, "t": "x" * 60} for i in range(40)]}, 1500, CodesCut("items", code="version"))
    named = out[TRUNCATION_KEY]["utelamnade"][0]
    assert named["koder"] == list(range(len(out["items"]), 40)) and "fortsatt" not in out[TRUNCATION_KEY]
    out = _fit({"items": [{"t": "x" * 60} for _ in range(40)]}, 1500, CodesCut("items", code="version"))
    assert "utelamnade" not in out[TRUNCATION_KEY]  # no code to name: flags only


def test_fit_count_matches_brute_force():
    from fuzzy_mcp.core import client_size, compact_json, fit_count

    def build(n):
        return {"rows": [{"id": i, "t": "x" * (i % 7) * 10} for i in range(n)]}

    for limit in (0, 300, 1500, 4000, 100000):
        n = fit_count(build, 60, limit)
        fitting = [k for k in range(1, 61) if limit <= 0 or client_size(compact_json(build(k))) <= limit]
        assert n == (max(fitting) if fitting else 1)
