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


def test_fit_to_limit_raw_only_note_does_not_ask_for_a_smaller_selection():
    from fuzzy_mcp.core import TRUNCATION_KEY

    out = _fit({"title": "t", "raw": {"rows": ["x" * 80] * 100}}, 600)
    assert out[TRUNCATION_KEY]["kortade"] == [] and out[TRUNCATION_KEY]["utelamnade_falt"] == ["raw"]
    assert "Begränsa urvalet" not in out[TRUNCATION_KEY]["meddelande"]


# --- issue #23: shortening a large answer must not take seconds (it blocked every session) -------------------------

LARGE_ANSWERS = {
    "200 listor": lambda: {f"l{i}": [f"varde-{j:05d}" for j in range(500)] for i in range(200)},
    "200 listor i en rad": lambda: {"rows": [{f"c{i}": [f"v{j}" for j in range(500)] for i in range(200)}]},
    "50 listor i ett objekt": lambda: {"a": {f"l{i}": [f"varde-{j:05d}" for j in range(500)] for i in range(50)}},
}


@pytest.mark.parametrize("shape", LARGE_ANSWERS)
def test_fit_to_limit_does_not_serialize_the_whole_answer_per_step(monkeypatch, shape):
    """Every step used to serialize the whole answer (1.5 MB) again, thousands of times: 30 s for 200 lists of 500
    values. The steps now measure only what they change. Counted, not timed, so that slow CI runners do not matter."""
    from fuzzy_mcp import core

    data = LARGE_ANSWERS[shape]()
    full = len(core.compact_json(data))
    serialize = core.compact_json
    whole: list[int] = []

    def counting(value):
        text = serialize(value)
        if len(text) > full // 10:
            whole.append(len(text))
        return text

    monkeypatch.setattr(core, "compact_json", counting)
    out, cut = core.fit_to_limit(data, 30000)
    assert cut and core.client_size(serialize(out)) <= 30000
    assert len(whole) <= 3, f"{len(whole)} serialiseringar av hela svaret"  # does it fit; first size of the copy


def _varied_answers(rng):
    """Small answers with the shapes fit_to_limit treats differently: records with lists of records inside (flags,
    ``returned``, paging fields), values, numbers, rows, protected and positional lists, URLs, long strings that need
    escaping, raw, the tool's own note and the paging contracts."""
    from fuzzy_mcp.core import CodesCut, CursorCut, Cut, OffsetCut, PageCut

    words = ["skola", "Göteborg", 'citat "x"', "back\\slash", "rad\nbrytning", "ctl\x01", "Ä€😀"]

    def text(lo=1, hi=6):
        return " ".join(rng.choice(words) for _ in range(rng.randint(lo, hi)))

    def records(n, depth):
        out = []
        for i in range(n):
            record = {"id": i, "code": f"K{i:03}", "name": text(), "truncated": False}
            if depth:
                key = rng.choice(["tags", "children"])
                inner = records(rng.randint(0, 6), depth - 1) if rng.random() < 0.4 else [text() for _ in range(9)]
                record.update({key: inner, f"{key}_truncated": False, "returned": len(inner)})
            if rng.random() < 0.1:
                record["next_offset"] = i + 1
            out.append(record)
        return out

    parts = {
        "items": lambda: records(rng.randint(2, 12), 2),
        "groups": lambda: [{"documents": records(rng.randint(2, 6), 1), "truncated": False} for _ in range(3)],
        "series": lambda: [{"name": text(), "values": [text() for _ in range(40)]} for _ in range(rng.randint(2, 6))],
        "values": lambda: [text() for _ in range(rng.randint(2, 60))],
        "numbers": lambda: [rng.random() for _ in range(20)] + ([text()] if rng.random() < 0.5 else []),
        "rows": lambda: [[text(1, 2), i, 0.5] for i in range(rng.randint(2, 30))],
        "description": lambda: text(30, 150),
        "request_url": lambda: "https://x.se/?q=" + text(5, 20),
        "notes": lambda: ["Svaret kortades till 50 rader.", text(5, 20)],
        "coordinates": lambda: [[600000.5, 6700000.5]] * 5,
        "variables": lambda: [{"code": "V", "values": [text() for _ in range(30)], "values_truncated": False}],
        "raw": lambda: {"data": records(20, 0)},
        "_kortat": lambda: {"meddelande": "Egen notis.", "kortade": [{"falt": "x", "fore": 9, "efter": 0}]},
    }
    contracts = [
        OffsetCut("page.items", stale_notes=("Svaret kortades",)),
        PageCut("page.items", size_argument="size"),
        CursorCut("page.items"),
        CodesCut("page.items", argument="codes"),
        CodesCut("page.items", code="id", hint=" H."),
        Cut("page.items", stale_notes=("Svaret kortades",)),
    ]
    for _ in range(50):
        data = {key: make() for key, make in parts.items() if rng.random() < 0.4}
        data.update(truncated=False, next_page=2)
        if "items" in data and rng.random() < 0.5:  # two lists that returned counts, in the same object
            data["values"] = [text() for _ in data["items"]]
            data["returned"] = len(data["items"])
        cuts: tuple = ()
        if rng.random() < 0.5:
            data["page"] = {"items": records(rng.randint(2, 40), 1), "offset": 40, "page": 2, "page_size": 50}
            data["page"].update(start_page=1, skip_matches=3, next_page=3, next_skip_matches=1, complete_scan=True)
            data["page"].update(returned=len(data["page"]["items"]), truncated=False, other=[text()] * 5)
            data["page"]["hint"] = {"lista": [text() for _ in range(9)]} if rng.random() < 0.5 else "h"
            cuts = (rng.choice(contracts),)
        yield data, cuts


def test_fit_to_limit_steps_match_measuring_the_whole_answer():
    """The incremental sizes take exactly the steps that serializing the whole answer at every step takes (the
    fallback, and fit_to_limit before #23): the same lists cut, flags, paging fields and note."""
    import random

    from fuzzy_mcp.core import _Shortening, client_size, compact_json

    checked = 0
    for data, cuts in _varied_answers(random.Random(23)):
        full = client_size(compact_json(data))
        for limit in sorted({2000, full // 4, full * 3 // 4}):
            if limit >= full:
                continue
            fast, fast_size = _Shortening(data, limit, cuts, exact=False).run()
            exact, exact_size = _Shortening(data, limit, cuts, exact=True).run()
            assert compact_json(fast) == compact_json(exact) and fast_size == exact_size
            assert exact_size == client_size(compact_json(exact))
            checked += 1
    assert checked > 100


def test_fit_to_limit_measures_the_whole_answer_if_the_steps_were_off(monkeypatch, caplog):
    """The result is measured whole once more; a disagreement redoes the shortening measuring every step whole."""
    from fuzzy_mcp import core

    data = {"items": [{"id": i, "t": "x" * 40} for i in range(100)], "returned": 100, "truncated": False}
    expected, _ = core.fit_to_limit(data, 2000)
    monkeypatch.setattr(core._Sizes, "answer", lambda self, note: 0)  # every step "fits"
    out, cut = core.fit_to_limit(data, 2000)
    assert cut and core.compact_json(out) == core.compact_json(expected)
    assert any("full mätning" in record.getMessage() for record in caplog.records)
