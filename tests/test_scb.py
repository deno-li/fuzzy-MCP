# SPDX-FileCopyrightText: 2026 Deniz Özer
#
# SPDX-License-Identifier: MIT

import httpx2
import pytest

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
