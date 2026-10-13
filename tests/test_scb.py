# SPDX-FileCopyrightText: 2026 Deniz Özer
#
# SPDX-License-Identifier: MIT

import json

import httpx2
import pytest

from fuzzy_mcp.core import TRUNCATION_KEY, client_size

from .conftest import call, call_error, load_fixture

pytestmark = pytest.mark.anyio

BASE = "https://statistikdatabasen.scb.se/api/v2"
CONFIG = {
    "apiVersion": "2.3.2",
    "defaultLanguage": "sv",
    "maxDataCells": 150000,
    "maxCallsPerTimeWindow": 30,
    "timeWindow": 10,
    "license": "https://creativecommons.org/share-your-work/public-domain/cc0/",
    "dataFormats": ["json-stat2", "csv", "px", "xlsx", "html", "json-px"],
}


def setup_tab638(router):
    router.add("GET", r"/config$", CONFIG)
    router.add("GET", r"/tables/TAB638/metadata", load_fixture("scb_metadata_tab638.json"))
    router.add("POST", r"/tables/TAB638/data", load_fixture("scb_data_tab638.json"))


async def test_search_tables(router, make_client):
    router.add("GET", r"/tables\?", load_fixture("scb_tables.json"))
    async with make_client("scb") as client:
        result = await call(client, "scb_search_tables", {"query": "folkmängd", "page_size": 500})
    params = router.last().url.params
    assert params["query"] == "folkmängd" and params["pageSize"] == "100" and params["lang"] == "sv"
    assert params["includeDiscontinued"] == "false"
    assert result["total"] == 3
    first = result["tables"][0]
    assert first["id"] == "TAB638" and first["last_period"] == "2024" and first["time_unit"] == "Annual"
    assert first["paths"][0][1] == {"id": "BE0101", "label": "Befolkningsstatistik"}


async def test_browse_subjects_builds_tree_from_paths(router, make_client):
    router.add("GET", r"/tables\?", load_fixture("scb_tables.json"))
    async with make_client("scb") as client:
        top = await call(client, "scb_browse_subjects")
        assert [(f["id"], f["table_count"]) for f in top["folders"]] == [("BE", 2), ("UF", 1)]
        leaf = await call(client, "scb_browse_subjects", {"path": ["be", "BE0101", "BE0101A"]})
        assert [t["id"] for t in leaf["tables"]] == ["TAB638", "TAB6471"] and leaf["folders"] == []
        assert [p["label"] for p in leaf["path"]] == ["Befolkning", "Befolkningsstatistik", "Folkmängd"]
        err = await call_error(client, "scb_browse_subjects", {"path": ["XX"]})
        assert "Hittade ingen ämnessökväg" in err
    # The catalogue is fetched once and cached.
    assert sum(1 for r in router.requests if "/tables?" in str(r.url)) == 1


async def test_metadata_view(router, make_client):
    setup_tab638(router)
    async with make_client("scb") as client:
        meta = await call(client, "scb_get_table_metadata", {"table_id": "TAB638", "max_values": 2})
    region = meta["variables"][0]
    assert region["kind"] == "geo" and region["elimination"] is True
    assert region["value_count"] == 4 and region["values_truncated"] is True and len(region["values"]) == 2
    assert [c["id"] for c in region["codelists"]] == ["vs_RegionLän07", "vs_RegionKommun07"]
    alder = meta["variables"][2]
    assert alder["codelists"][0]["id"] == "agg_Ålder5år"  # legacy "codeLists" spelling
    assert meta["variables"][4]["kind"] == "contents" and meta["variables"][5]["kind"] == "time"
    assert meta["official_statistics"] is True and meta["notes"]


async def test_get_data_posts_resolved_selection(router, make_client):
    setup_tab638(router)
    async with make_client("scb") as client:
        result = await call(
            client,
            "scb_get_table_data",
            {
                "table_id": "TAB638",
                "selection": {
                    "Region": ["0180", "Göteborg"],
                    "ContentsCode": ["BE0101N1"],
                    "Tid": ["TOP(2)"],
                },
            },
        )
    request = router.last()
    assert request.method == "POST"
    assert request.url.params["outputFormat"] == "json-stat2" and request.url.params["lang"] == "sv"
    body = router.json_body()
    assert body == {
        "selection": [
            {"variableCode": "Region", "valueCodes": ["0180", "1480"]},
            {"variableCode": "ContentsCode", "valueCodes": ["BE0101N1"]},
            {"variableCode": "Tid", "valueCodes": ["2023", "2024"]},
        ]
    }
    assert result["selection"]["eliminated"] == ["Civilstand", "Alder", "Kon"]
    assert result["selection"]["cells"] == 4
    assert result["data"]["rows"][0] == ["Stockholm", "Folkmängd", "2023", 984748]
    assert result["citation"].startswith("Källa: SCB, Statistikdatabasen, tabell TAB638")


async def test_get_data_with_codelist_passes_codelist(router, make_client):
    setup_tab638(router)
    async with make_client("scb") as client:
        await call(
            client,
            "scb_get_table_data",
            {
                "table_id": "TAB638",
                "codelists": {"Region": "vs_RegionLän07"},
                "selection": {"Region": ["01"], "ContentsCode": ["BE0101N1"], "Tid": ["2024"]},
            },
        )
    meta_request = next(r for r in router.requests if "/metadata" in str(r.url))
    assert meta_request.url.params["codelist[Region]"] == "vs_RegionLän07"
    assert router.json_body()["selection"][0] == {
        "variableCode": "Region",
        "valueCodes": ["01"],
        "codelist": "vs_RegionLän07",
    }


async def test_too_many_cells_rejected_before_calling(router, make_client):
    setup_tab638(router)
    router.routes[0] = type(router.routes[0])("GET", router.routes[0].pattern, dict(CONFIG, maxDataCells=10))
    async with make_client("scb") as client:
        err = await call_error(
            client,
            "scb_get_table_data",
            {"table_id": "TAB638", "selection": {"Region": ["*"], "ContentsCode": ["*"], "Tid": ["*"]}},
        )
    assert "överskrider gränsen 10" in err
    assert not any(r.method == "POST" for r in router.requests)


async def test_upstream_problem_detail_is_reported(router, make_client):
    router.add("GET", r"/config$", CONFIG)
    router.add(
        "GET",
        r"/tables/TAB0/metadata",
        httpx2.Response(404, json={"type": "Parameter error", "title": "Non-existent table", "status": 404}),
    )
    async with make_client("scb") as client:
        err = await call_error(client, "scb_get_table_metadata", {"table_id": "TAB0"})
    assert "HTTP 404" in err and "Non-existent table" in err
    async with make_client("scb") as client:
        for bad in ("../x", "..", "."):
            err = await call_error(client, "scb_get_table_metadata", {"table_id": bad})
            assert "Ogiltigt id" in err
    assert not any("/tables/." in str(r.url) or str(r.url).rstrip("/").endswith("/api/v2") for r in router.requests)


async def test_build_query_keeps_expressions_in_url(router, make_client):
    setup_tab638(router)
    async with make_client("scb") as client:
        recipe = await call(
            client,
            "scb_build_query",
            {
                "table_id": "TAB638",
                "selection": {
                    "Region": ["Stockholm", "01*"],
                    "ContentsCode": ["BE0101N1"],
                    "Tid": ["RANGE(2022,2024)"],
                },
                "output_format_params": ["UseTexts", "SeparatorSemicolon"],
            },
        )
    url = recipe["get_url"]
    assert url.startswith(f"{BASE}/tables/TAB638/data?lang=sv&outputFormat=csv")
    assert "valueCodes[Region]=0180,01*" in url
    assert "valueCodes[Tid]=[RANGE(2022,2024)]" in url
    assert "outputFormatParams=UseTexts&outputFormatParams=SeparatorSemicolon" in url
    assert recipe["post_body"]["selection"][2] == {"variableCode": "Tid", "valueCodes": ["RANGE(2022,2024)"]}
    assert "Csv.Document(Web.Contents(" in recipe["power_query_m"]
    # SCB serves csv as iso-8859-1 and the separator follows outputFormatParams.
    assert 'Delimiter=";"' in recipe["power_query_m"] and "Encoding=28591" in recipe["power_query_m"]
    assert recipe["post_url"].endswith(
        "outputFormat=csv&outputFormatParams=UseTexts&outputFormatParams=SeparatorSemicolon"
    )
    assert recipe["cells"] == 2 * 1 * 3  # Stockholm + 01* (01, 0180 → dedup) ... resolved locally
    async with make_client("scb") as client:
        err = await call_error(
            client,
            "scb_build_query",
            {"table_id": "TAB638", "output_format": "json-stat2", "output_format_params": ["UseTexts"]},
        )
    assert "outputFormatParams" in err


async def test_build_query_power_query_tab_title_and_rolling_time(router, make_client):
    setup_tab638(router)
    async with make_client("scb") as client:
        recipe = await call(
            client,
            "scb_build_query",
            {
                "table_id": "TAB638",
                # Label keys ("år") and lower-case codes are accepted and keep their expression.
                "selection": {"region": ["0180"], "ContentsCode": ["BE0101N1"], "år": ["TOP(2)"]},
                "output_format_params": ["SeparatorTab", "IncludeTitle"],
            },
        )
        assert "valueCodes[Tid]=TOP(2)" in recipe["get_url"]
        m = recipe["power_query_m"]
        assert 'Delimiter="#(tab)"' in m and "Table.Skip(Källa, 1)" in m
        assert "Table.PromoteHeaders(UtanTitel" in m
        # An omitted time variable stays rolling (latest period) instead of being frozen to 2024.
        rolling = await call(
            client,
            "scb_build_query",
            {"table_id": "TAB638", "selection": {"Region": ["0180"], "ContentsCode": ["BE0101N1"]}},
        )
        assert "valueCodes[Tid]=TOP(1)" in rolling["get_url"]
        assert rolling["post_body"]["selection"][-1] == {"variableCode": "Tid", "valueCodes": ["TOP(1)"]}


@pytest.mark.parametrize(
    ("output_format", "params", "message"),
    [
        ("xlsx", ["SeparatorSemicolon"], "Separator"),
        ("csv", ["UseCodes", "UseTexts"], "högst en"),
        ("csv", ["SeparatorTab", "SeparatorSpace"], "högst en"),
        ("px", ["IncludeTitle"], "outputFormatParams"),
    ],
)
async def test_build_query_rejects_params_the_server_refuses(router, make_client, output_format, params, message):
    setup_tab638(router)
    async with make_client("scb") as client:
        err = await call_error(
            client,
            "scb_build_query",
            {"table_id": "TAB638", "output_format": output_format, "output_format_params": params},
        )
    assert message in err


async def test_codelist_keys_are_normalised_to_variable_code(router, make_client):
    setup_tab638(router)
    async with make_client("scb") as client:
        await call(
            client,
            "scb_get_table_data",
            {
                "table_id": "TAB638",
                "codelists": {"region": "vs_RegionLän07"},
                "selection": {"Region": ["01"], "ContentsCode": ["BE0101N1"], "Tid": ["2024"]},
            },
        )
        assert router.json_body()["selection"][0]["codelist"] == "vs_RegionLän07"
        recipe = await call(
            client,
            "scb_build_query",
            {
                "table_id": "TAB638",
                "codelists": {"REGION": "vs_RegionLän07"},
                "selection": {"Region": ["01"], "ContentsCode": ["BE0101N1"], "Tid": ["2024"]},
            },
        )
    assert "codelist[Region]=vs_RegionL%C3%A4n07" in recipe["get_url"]
    assert recipe["post_body"]["selection"][0]["codelist"] == "vs_RegionLän07"


async def test_metadata_related_links_and_contents_info(router, make_client):
    meta = load_fixture("scb_metadata_tab638.json")
    meta["link"] = {
        "related": [
            {
                "extension": {"relation": "statistics-homepage", "metaid": "STATPROD:BE0101"},
                "href": "https://www.scb.se/BE0101",
                "label": "Statistikens webbsida",
                "type": "text/html",
            },
            {"label": "utan href"},
        ]
    }
    meta["dimension"]["Region"]["link"] = {
        "related": [
            {
                "extension": {"relation": "definitions", "metaid": "urn:scb:klass:region"},
                "href": "https://www.scb.se/klass/region",
                "label": "Klassifikation för region",
                "type": "text/html",
            }
        ]
    }
    contents = meta["dimension"]["ContentsCode"]
    contents["category"]["unit"] = {"BE0101N1": {"base": "antal", "decimals": 0}}
    contents["extension"].update(
        {"refperiod": {"BE0101N1": "31 december respektive år"}, "measuringType": {"BE0101N1": "Stock"}}
    )
    router.add("GET", r"/tables/TAB638/metadata", meta)
    async with make_client("scb") as client:
        view = await call(client, "scb_get_table_metadata", {"table_id": "TAB638"})
    assert view["links"] == [
        {
            "href": "https://www.scb.se/BE0101",
            "label": "Statistikens webbsida",
            "type": "text/html",
            "relation": "statistics-homepage",
            "metaid": "STATPROD:BE0101",
        }
    ]
    assert view["variables"][0]["links"][0]["relation"] == "definitions"
    assert view["contents_info"]["BE0101N1"] == {
        "base": "antal",
        "decimals": 0,
        "refperiod": "31 december respektive år",
        "measuring_type": "Stock",
    }


async def test_browse_counts_table_once_per_folder_and_caps_listing(router, make_client):
    tables = load_fixture("scb_tables.json")
    tables["tables"][0]["paths"].append(
        [{"id": "AM", "label": "Arbetsmarknad"}, {"id": "AM0101", "label": "Sysselsättning"}]
    )
    tables["tables"][0]["paths"].append(
        [{"id": "AM", "label": "Arbetsmarknad"}, {"id": "AM0102", "label": "Arbetslöshet"}]
    )
    router.add("GET", r"/tables\?", tables)
    async with make_client("scb") as client:
        top = await call(client, "scb_browse_subjects")
        assert ("AM", 1) in [(f["id"], f["table_count"]) for f in top["folders"]]
        am = await call(client, "scb_browse_subjects", {"path": ["AM"]})
        assert [(f["id"], f["table_count"]) for f in am["folders"]] == [("AM0101", 1), ("AM0102", 1)]
        leaf = await call(client, "scb_browse_subjects", {"path": ["BE", "BE0101", "BE0101A"], "max_tables": 1})
    assert leaf["table_count"] == 2 and leaf["tables_truncated"] is True and len(leaf["tables"]) == 1


async def test_catalogue_survives_bad_total_pages(router, make_client):
    tables = load_fixture("scb_tables.json")
    tables["page"] = {"pageNumber": 1, "pageSize": 5000, "totalElements": 3, "totalPages": "x"}
    router.add("GET", r"/tables\?", tables)
    async with make_client("scb") as client:
        top = await call(client, "scb_browse_subjects")
    assert [f["id"] for f in top["folders"]] == ["BE", "UF"]


async def test_get_codelist_is_capped(router, make_client):
    values = [{"code": f"{i:04d}", "label": f"Kommun {i}", "valueMap": [f"{i:04d}"]} for i in range(30)]
    router.add(
        "GET",
        r"/codelists/vs_RegionKommun07",
        {"id": "vs_RegionKommun07", "label": "Kommuner", "type": "Valueset", "values": values},
    )
    async with make_client("scb") as client:
        result = await call(client, "scb_get_codelist", {"codelist_id": "vs_RegionKommun07", "max_values": 5})
    assert result["value_count"] == 30 and result["values_truncated"] is True and len(result["values"]) == 5
    assert result["values"][0] == {"code": "0000", "label": "Kommun 0", "value_map": ["0000"]}


async def test_metadata_contact_persons_only_on_request(router, make_client):
    setup_tab638(router)
    async with make_client("scb") as client:
        default = await call(client, "scb_get_table_metadata", {"table_id": "TAB638"})
        assert default["contacts"] == ["SCB"]
        assert "Efternamn" not in str(default)
        opted = await call(client, "scb_get_table_metadata", {"table_id": "TAB638", "include_contacts": True})
    assert opted["contacts"] == ["Förnamn Efternamn, SCB#010-000 00 00"]


async def test_contact_persons_refused_when_personal_data_is_off(router, make_client):
    setup_tab638(router)
    async with make_client("scb", allow_personal_data=False) as client:
        err = await call_error(client, "scb_get_table_metadata", {"table_id": "TAB638", "include_contacts": True})
        assert "FUZZY_MCP_PERSONAL_DATA=off" in err
        default = await call(client, "scb_get_table_metadata", {"table_id": "TAB638"})
    assert default["contacts"] == ["SCB"]


# --- issue #20: truthful flags and skip-free paging when the server caps the answer (FUZZY_MCP_MAX_OUTPUT_CHARS) ---


def summary(i: int) -> dict:
    return {
        "id": f"TAB{i:04}",
        "label": f"Tabell {i} om folkmängd efter region, ålder och kön",
        "description": "Beskrivning av tabellen. " * (i // 12 + i * 7 % 11),  # growing, uneven: cuts on many pages
        "updated": "2025-02-21T07:00:00Z",
        "firstPeriod": "1968",
        "lastPeriod": "2024",
        "timeUnit": "Annual",
        "variableNames": ["region", "kön", "ålder", "tabellinnehåll", "år"],
        "subjectCode": "BE",
        "paths": [[{"id": "BE", "label": "Befolkning"}, {"id": "BE0101", "label": "Befolkningsstatistik"}]],
    }


def table_pages(count: int, *, echo_page_size: bool = True):
    """GET /tables responder that pages ``count`` tables like PxWebApi 2 (pageNumber from 1)."""

    def respond(request: httpx2.Request) -> httpx2.Response:
        number, size = int(request.url.params["pageNumber"]), int(request.url.params["pageSize"])
        info = {"pageNumber": number, "totalElements": count, "totalPages": -(-count // size)}
        if echo_page_size:
            info["pageSize"] = size
        tables = [summary(i) for i in range((number - 1) * size, min(count, number * size))]
        return httpx2.Response(200, json={"tables": tables, "page": info})

    return respond


async def walk_search(client, tool: str, args: dict, cap: int) -> tuple[list[str], int]:
    """Page through a table search as documented: next_page with the same page size, else the server's
    _kortat.fortsatt (and keep its page size). Checks every answer; returns the ids seen and how many answers the
    server cut."""
    seen: list[str] = []
    cuts = 0
    args = dict(args)
    for _ in range(300):
        result = await client.call_tool(tool, args)
        text = result.content[0].text
        assert not result.is_error, text
        answer = json.loads(text)
        assert answer == result.structured_content and (not cap or client_size(text) <= cap)
        seen += [table["id"] for table in answer["tables"]]
        note = answer.get(TRUNCATION_KEY)
        if note:
            cuts += 1
            # A cut page never claims to be complete, and next_page would skip the rest of it.
            assert answer["truncated"] is True and "next_page" not in answer
            assert [entry["falt"] for entry in note["kortade"]] == ["tables"]
        else:
            more = answer["page"] < answer["total_pages"]
            assert answer["truncated"] is more and answer.get("next_page") == (answer["page"] + 1 if more else None)
        if "next_page" in answer:
            args["page"] = answer["next_page"]
        elif note and "fortsatt" in note:
            args.update(note["fortsatt"][0]["anrop"])
        else:
            return seen, cuts
    raise AssertionError("bläddringen tog aldrig slut")


def assert_no_skips(seen: list[str], expected: list[str], max_dups: int) -> None:
    assert list(dict.fromkeys(seen)) == expected  # every item, first seen in order: nothing skipped
    assert len(seen) - len(expected) <= max_dups


async def follow_next_page(client, tool: str, args: dict) -> list[str]:
    """A client that only reads next_page."""
    seen: list[str] = []
    while True:
        answer = await call(client, tool, args)
        seen += [table["id"] for table in answer["tables"]]
        if "next_page" not in answer:
            return seen
        args = {**args, "page": answer["next_page"]}


@pytest.mark.parametrize("cap", [0, 12000, 6000, 3000])
async def test_search_tables_walk_never_skips(router, make_client, cap):
    router.add("GET", r"/tables\?", table_pages(237))
    expected = [summary(i)["id"] for i in range(237)]
    args = {"query": "folkmängd", "page_size": 20}
    async with make_client("scb", max_output_chars=cap) as client:
        seen, cuts = await walk_search(client, "scb_search_tables", args, cap)
        prefix = await follow_next_page(client, "scb_search_tables", args)
        last = await call(client, "scb_search_tables", {**args, "page": 12})
    assert_no_skips(seen, expected, max_dups=10 * cuts)
    assert prefix == expected[: len(prefix)]  # stops at a cut page instead of skipping its rest
    if not cap:
        assert seen == expected and cuts == 0
        assert last["truncated"] is False and "next_page" not in last and len(last["tables"]) == 17
        return
    assert cuts > 0 and len(prefix) < len(expected)
    # The last page has no next page, but once cut it is not complete, and fortsatt starts at or before the first
    # table not shown.
    kept = len(last["tables"])
    assert last["truncated"] is True and "next_page" not in last and kept < 17
    resume = last[TRUNCATION_KEY]["fortsatt"][0]
    assert resume["falt"] == "tables" and set(resume["anrop"]) == {"page", "page_size"}
    start = (resume["anrop"]["page"] - 1) * resume["anrop"]["page_size"]
    assert start <= 220 + kept < start + resume["anrop"]["page_size"]


async def test_search_tables_page_size_fallback_clamped(router, make_client):
    router.add("GET", r"/tables\?", table_pages(237, echo_page_size=False))
    async with make_client("scb") as client:
        result = await call(client, "scb_search_tables", {"page": 2, "page_size": 500})
    assert router.last().url.params["pageSize"] == "100"
    assert result["page_size"] == 100 and result["total_pages"] == 3  # the size requested, not the input
    assert result["truncated"] is True and result["next_page"] == 3 and result["tables"][0]["id"] == "TAB0100"
    async with make_client("scb", max_output_chars=6000) as client:
        cut = await call(client, "scb_search_tables", {"page": 2, "page_size": 500})
    anrop = cut[TRUNCATION_KEY]["fortsatt"][0]["anrop"]
    start = (anrop["page"] - 1) * anrop["page_size"]
    assert start <= 100 + len(cut["tables"]) < start + anrop["page_size"]  # resumes inside page 2, not after 500


WIDE_TABLE = ["ContentsCode", "Tid", *(f"Kategori{i:02}" for i in range(18))]


@pytest.mark.parametrize(
    "default",
    [
        {
            "selection": [
                {"variableCode": "Region", "valueCodes": [f"{i:04}" for i in range(290)]},
                {"variableCode": "Kon", "valueCodes": ["1", "2"]},
                {"variableCode": "Tid", "valueCodes": [str(1968 + i) for i in range(57)]},
            ],
            "placement": {"heading": ["ContentsCode", "Tid"], "stub": ["Region", "Kon"]},
        },
        # A wide table: the generic rules keep the selection whole, but could drop variables from placement.
        {
            "selection": [{"variableCode": code, "valueCodes": ["1"]} for code in WIDE_TABLE],
            "placement": {"heading": WIDE_TABLE[:2], "stub": WIDE_TABLE[2:]},
        },
    ],
)
async def test_default_selection_whole_or_rejected(router, make_client, default):
    """A shortened default selection would be another selection: it is returned whole or rejected, never cut."""
    router.add("GET", r"/tables/TAB638/defaultselection", default)
    args = {"table_id": "TAB638"}
    async with make_client("scb") as client:
        whole = await client.call_tool("scb_get_default_selection", args)
    size = client_size(whole.content[0].text)
    async with make_client("scb", max_output_chars=size) as client:
        fits = await client.call_tool("scb_get_default_selection", args)
    assert fits.content == whole.content and fits.structured_content == default
    for cap in (size - 1, size // 2):
        async with make_client("scb", max_output_chars=cap) as client:
            rejected = await client.call_tool("scb_get_default_selection", args)
        assert rejected.is_error and rejected.structured_content is None
        assert "får inte kortas" in rejected.content[0].text


# Table data, metadata, codelists and the subject tree need no contract of their own: the tests below guard the core's
# generic rules (flags, protected dimensions, selection echo and notes) on these answer shapes.


def dataset(dimensions: dict[str, list[str]], **extra) -> dict:
    """A JSON-stat 2.0 dataset (PxWebApi 2 metadata, or data with ``value``) with labelled categories."""
    return {
        "version": "2.0",
        "class": "dataset",
        "label": "Folkmängd",
        "id": list(dimensions),
        "size": [len(codes) for codes in dimensions.values()],
        "role": {"time": ["Tid"], "metric": ["ContentsCode"]},
        "dimension": {
            code: {
                "label": code.lower(),
                "category": {
                    "index": {c: i for i, c in enumerate(codes)},
                    "label": {c: f"{code.lower()} {c}" for c in codes},
                },
                "extension": {"elimination": code != "ContentsCode"},
            }
            for code, codes in dimensions.items()
        },
        **extra,
    }


async def test_get_table_data_cut_sets_truncated(router, make_client):
    """Issue #20: 870 cells with max_rows=1000 under a 30 000 character cap. The cut answer says it is incomplete
    and keeps everything needed to read the rows it shows."""
    dimensions = {
        "Region": [f"{i:04}" for i in range(29)],
        "Kon": ["1", "2", "3"],
        "ContentsCode": ["BE0101N1"],
        "Tid": [str(2015 + i) for i in range(10)],
    }
    data = dataset(dimensions, value=[i * 1.5 for i in range(870)], note=[f"Fotnot {i}" for i in range(45)])
    router.add("GET", r"/config$", CONFIG)
    router.add("GET", r"/tables/TAB870/metadata", dataset(dimensions))
    router.add("POST", r"/tables/TAB870/data", httpx2.Response(200, json=data, headers={"Deprecation": "true"}))
    args = {
        "table_id": "TAB870",
        "selection": {"Region": ["*"], "Kon": ["*"], "ContentsCode": ["BE0101N1"], "Tid": ["TOP(10)"]},
        "max_rows": 1000,
    }
    async with make_client("scb") as client:
        whole = await call(client, "scb_get_table_data", args)
    async with make_client("scb", max_output_chars=30000) as client:
        result = await client.call_tool("scb_get_table_data", args)
    assert not result.is_error and client_size(result.content[0].text) <= 30000
    assert whole["data"]["truncated"] is False and len(whole["data"]["rows"]) == 870
    cut = result.structured_content
    rows = cut["data"]["rows"]
    assert cut["data"]["truncated"] is True and cut["data"]["total_rows"] == 870 and 0 < len(rows) < 870
    assert rows == whole["data"]["rows"][: len(rows)]
    # Everything that explains the rows is whole: the 4 dimensions, the selection echo, footnotes and notices.
    for key in ("dimensions", "columns", "notes"):
        assert cut["data"][key] == whole["data"][key]
    assert len(cut["data"]["dimensions"]) == 4 and cut["data"]["notes"][-1].startswith("… ytterligare 5 fotnoter")
    assert cut["selection"] == whole["selection"] and cut["notices"] == whole["notices"] == ["Deprecation: true"]
    note = cut[TRUNCATION_KEY]
    assert note["kortade"] == [{"falt": "data.rows", "fore": 870, "efter": len(rows)}]
    assert "fortsatt" not in note and note["meddelande"].endswith("Begränsa urvalet för att få allt.")


async def test_get_table_metadata_flags_cut_variables(router, make_client):
    dimensions = {
        "Region": [f"{i:04}" for i in range(300)],
        "Kon": ["1", "2"],
        "ContentsCode": ["BE0101N1", "BE0101N2"],
        "Tid": [str(1975 + i) for i in range(50)],
    }
    router.add("GET", r"/tables/TAB300/metadata", dataset(dimensions))
    async with make_client("scb") as client:
        whole = await call(client, "scb_get_table_metadata", {"table_id": "TAB300", "max_values": 0})
    assert not any(variable["values_truncated"] for variable in whole["variables"])
    for max_values in (0, 60):
        async with make_client("scb", max_output_chars=3000) as client:
            cut = await call(client, "scb_get_table_metadata", {"table_id": "TAB300", "max_values": max_values})
        assert [variable["code"] for variable in cut["variables"]] == list(dimensions)  # no variable is dropped
        for variable, full in zip(cut["variables"], whole["variables"], strict=True):
            assert variable["value_count"] == len(dimensions[variable["code"]])
            assert variable["values"] == full["values"][: len(variable["values"])]
            assert variable["values_truncated"] is (len(variable["values"]) < variable["value_count"])
        # Tid fits max_values=60, so only the server's cut can make its list incomplete.
        assert cut["variables"][3]["values_truncated"] is True
        assert any(entry["falt"] == "variables[].values" for entry in cut[TRUNCATION_KEY]["kortade"])


async def test_codelist_and_subjects_flags(router, make_client):
    values = [
        {"code": f"G{i:03}", "label": f"Grupp {i}", "valueMap": [f"{i:03}{j}" for j in range(5)]} for i in range(300)
    ]
    router.add(
        "GET",
        r"/codelists/agg_Grupper",
        {"id": "agg_Grupper", "label": "Grupper", "type": "Aggregation", "values": values},
    )
    router.add("GET", r"/tables\?", table_pages(60))
    for cap in (0, 3000):
        async with make_client("scb", max_output_chars=cap) as client:
            codelist = await call(client, "scb_get_codelist", {"codelist_id": "agg_Grupper"})
            listing = await call(client, "scb_browse_subjects", {"path": ["BE", "BE0101"], "max_tables": 100})
        assert codelist["value_count"] == 300 and listing["table_count"] == 60
        assert codelist["values_truncated"] is (len(codelist["values"]) < 300) is bool(cap)
        assert listing["tables_truncated"] is (len(listing["tables"]) < 60) is bool(cap)
        for value, raw in zip(codelist["values"], values, strict=False):
            assert value["value_map"] == raw["valueMap"]  # which codes a group holds is never shortened
        assert [element["id"] for element in listing["path"]] == ["BE", "BE0101"]
