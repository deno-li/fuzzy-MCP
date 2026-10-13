# SPDX-FileCopyrightText: 2026 Deniz Özer
#
# SPDX-License-Identifier: MIT

import httpx2
import pytest

from fuzzy_mcp.config import DEFAULT_API_VERSIONS, DEFAULT_BASE_URLS, Settings
from fuzzy_mcp.core import TRUNCATION_KEY
from fuzzy_mcp.sources.fohm import normalize_table_path

from .conftest import call, call_error, load_fixture
from .test_scb import assert_no_skips, dataset, summary, table_pages, walk_search

pytestmark = pytest.mark.anyio

BASE = "https://fohm-app.folkhalsomyndigheten.se/Folkhalsodata/api/v1/sv"
TABLE = "A_Folkhalsodata/C_HBSC/Regionalt/Skolan/Mobbning/MobbningReg.px"


@pytest.mark.parametrize(
    "value,expected",
    [
        (TABLE, TABLE),
        ("/" + TABLE + "/", TABLE),
        ("MobbningReg.px", "A_Folkhalsodata/MobbningReg.px"),
        (f"{BASE}/{TABLE}", TABLE),
        (
            "https://fohm-app.folkhalsomyndigheten.se/Folkhalsodata/pxweb/sv/A_Folkhalsodata/"
            "A_Folkhalsodata__Z_ovrigdata__Arbete/Arbetsloshet_LanUTBNIVA.px/table/tableViewLayout1/",
            "A_Folkhalsodata/Z_ovrigdata/Arbete/Arbetsloshet_LanUTBNIVA.px",
        ),
        (
            "https://fohm-app.folkhalsomyndigheten.se/Folkhalsodata/pxweb/sv/A_Folkhalsodata/"
            "A_Folkhalsodata__C_HBSC__Regionalt__Levnadsvanor__Alkohol%20tobak%20och%20narkotika__Tobak/A_RokvanorReg.px/",
            "A_Folkhalsodata/C_HBSC/Regionalt/Levnadsvanor/Alkohol tobak och narkotika/Tobak/A_RokvanorReg.px",
        ),
    ],
)
def test_normalize_table_path(value, expected):
    assert normalize_table_path(value) == expected


async def test_browse_and_search(router, make_client):
    router.add(
        "GET",
        r"/sv/A_Folkhalsodata/C_HBSC$",
        [
            {"id": "Nationellt", "type": "l", "text": "Nationella resultat"},
            {"id": "Regionalt", "type": "l", "text": "Regionala resultat"},
        ],
    )
    router.add(
        "GET",
        r"/sv/A_Folkhalsodata\?query=mobbning",
        [
            {
                "id": "MobbningReg.px",
                "path": "/C_HBSC/Regionalt/Skolan/Mobbning",
                "title": "Mobbning",
                "score": 2.0,
                "published": "2023-05-30T08:00:00",
            },
            {"id": "MobbningReg.px", "path": "/Annan/Plats", "title": "Mobbning", "score": 1.0},
        ],
    )
    async with make_client("fohm") as client:
        listing = await call(client, "fohm_browse", {"path": "A_Folkhalsodata/C_HBSC"})
        assert [n["path"] for n in listing["nodes"]] == [
            "A_Folkhalsodata/C_HBSC/Nationellt",
            "A_Folkhalsodata/C_HBSC/Regionalt",
        ]
        result = await call(client, "fohm_search_tables", {"query": "mobbning"})
    assert result["query"] == "mobbning*"
    assert result["total"] == 1 and result["hits"][0]["path"] == TABLE
    assert "query=mobbning%2A" in str(router.requests[-1].url)


async def test_metadata_and_data_with_swedish_codes(router, make_client):
    router.add(
        "GET",
        r"/sv/\?config$",
        {"maxValues": 10000, "maxCells": 100000, "maxCalls": 1000, "timeWindow": 10, "CORS": True},
    )
    router.add("GET", r"MobbningReg\.px$", load_fixture("fohm_metadata_mobbning.json"))
    router.add("POST", r"MobbningReg\.px$", load_fixture("fohm_data_mobbning.json"))
    async with make_client("fohm") as client:
        meta = await call(client, "fohm_get_table_metadata", {"table_path": TABLE})
        assert [v["code"] for v in meta["variables"]] == [
            "Region",
            "Kategori",
            "Hur ofta",
            "Ålder",
            "Kön",
            "År",
        ]
        assert meta["variables"][5]["kind"] == "time"
        result = await call(
            client,
            "fohm_get_table_data",
            {
                "table_path": TABLE,
                "selection": {
                    "Region": ["00", "21"],
                    "hur ofta": ["2-3 ggr/månad"],
                    "Ålder": ["13"],
                    "Kön": ["1"],
                },
            },
        )
    body = router.json_body(-1)
    assert body["response"] == {"format": "json-stat2"}
    query = {q["code"]: q["selection"] for q in body["query"]}
    assert query["Hur ofta"] == {"filter": "item", "values": ["2-3 ggr/månad"]}
    assert query["År"]["values"] == ["2021-2022"]  # latest period chosen automatically
    assert query["Kategori"]["values"] == ["Mobbad i skolan senaste månaderna"]
    assert result["selection"]["defaulted"] == {
        "Kategori": ["Mobbad i skolan senaste månaderna"],
        "År": ["2021-2022"],
    }
    assert result["data"]["rows"][1][0] == "21 Gävleborgs län" and result["data"]["rows"][1][-1] == 5
    assert result["citation"].startswith("Källa: Folkhälsomyndigheten")
    config_request = next(r for r in router.requests if "config" in str(r.url))
    assert str(config_request.url).endswith("/sv/?config")


async def test_cell_limit_and_errors(router, make_client):
    router.add("GET", r"/sv/\?config$", {"maxCells": 3})
    router.add("GET", r"MobbningReg\.px$", load_fixture("fohm_metadata_mobbning.json"))
    router.add("POST", r"MobbningReg\.px$", httpx2.Response(400, json={"error": "Parameter error"}))
    async with make_client("fohm") as client:
        err = await call_error(client, "fohm_get_table_data", {"table_path": TABLE, "selection": {"Region": ["*"]}})
        assert "överskrider gränsen 3" in err
        err = await call_error(
            client,
            "fohm_get_table_data",
            {
                "table_path": TABLE,
                "selection": {"Region": ["00"], "Hur ofta": ["2-3 ggr/månad"], "Ålder": ["13"]},
            },
        )
        assert "Parameter error" in err and "HTTP 400" in err
        err = await call_error(client, "fohm_get_table_data", {"table_path": TABLE, "selection": {"Län": ["01"]}})
        assert "Okänd variabel 'Län'" in err


async def test_build_query_returns_power_query(router, make_client):
    router.add("GET", r"/sv/\?config$", {"maxCells": 100000})
    router.add("GET", r"MobbningReg\.px$", load_fixture("fohm_metadata_mobbning.json"))
    async with make_client("fohm") as client:
        recipe = await call(client, "fohm_build_query", {"table_path": TABLE, "selection": {"Region": ["21"]}})
    assert recipe["url"] == f"{BASE}/{TABLE}"
    assert "Web.Contents" in recipe["power_query_m"] and '""format"":""csv""' in recipe["power_query_m"]
    assert recipe["cells"] == 1 * 1 * 2 * 3 * 1  # Region, Kategori, Hur ofta(all), Ålder(all), År(latest)
    assert all(r.method == "GET" for r in router.requests)


async def test_fohm_switches_to_pxwebapi2_by_configuration(router, make_client):
    """FUZZY_MCP_FOHM_API_VERSION=v2 registers the PxWebApi 2 tool set under the same prefix."""
    v2_base = "https://fohm.example/api/v2"
    router.add("GET", r"fohm\.example/api/v2/config$", {"apiVersion": "2.3.2", "maxDataCells": 100000})
    async with make_client(
        "fohm",
        base_urls={**DEFAULT_BASE_URLS, "fohm": v2_base},
        api_versions={"fohm": "v2", "skolverket_statistik": "v1"},
    ) as client:
        names = {t.name for t in (await client.list_tools()).tools}
        assert {"fohm_search_tables", "fohm_get_table_metadata", "fohm_build_query", "fohm_get_codelist"} <= names
        assert "fohm_list_databases" not in names
        config = await call(client, "fohm_get_config")
    assert config["apiVersion"] == "2.3.2"
    assert str(router.last().url) == f"{v2_base}/config"


def test_api_version_v2_requires_a_v2_base_url(monkeypatch):
    monkeypatch.setenv("FUZZY_MCP_FOHM_API_VERSION", "v2")
    with pytest.raises(ValueError, match="FUZZY_MCP_FOHM_BASE_URL"):
        Settings.from_env()
    monkeypatch.setenv("FUZZY_MCP_FOHM_BASE_URL", "https://fohm.example/api/v2")
    assert Settings.from_env().api_version("fohm") == "v2"
    monkeypatch.setenv("FUZZY_MCP_SKOLVERKET_STATISTIK_API_VERSION", "v3")
    with pytest.raises(ValueError, match="'v1' eller 'v2'"):
        Settings.from_env()


# PxWeb v1 declares no contract: the two tests below guard the core's generic rules on its answer shapes.


async def test_v1_search_cut_sets_truncated(router, make_client):
    """Fewer hits than limit: the tool's own truncated is false, so only the server's cut can make it true. Browse and
    the database list have no flag of their own; _kortat is their signal."""
    hits = [
        {
            "id": f"T{i:03}.px",
            "path": "/C_HBSC/Regionalt",
            "title": f"Tabell {i} om mobbning i skolan",
            "score": 100 - i,
        }
        for i in range(80)
    ]
    folders = [{"id": f"N{i:03}", "type": "l", "text": f"Mapp {i}"} for i in range(200)]
    router.add("GET", r"/sv/A_Folkhalsodata\?query=mobbning", hits)
    router.add("GET", r"/sv/A_Folkhalsodata/C_HBSC$", folders)
    router.add("GET", r"/sv/$", [{"dbid": f"DB{i:02}", "text": f"Databas {i} om folkhälsa"} for i in range(60)])
    for cap in (0, 4000):
        async with make_client("fohm", max_output_chars=cap) as client:
            result = await call(client, "fohm_search_tables", {"query": "mobbning", "limit": 100})
            listing = await call(client, "fohm_browse", {"path": "A_Folkhalsodata/C_HBSC"})
            databases = await call(client, "fohm_list_databases")
        assert result["total"] == 80 and result["truncated"] is (len(result["hits"]) < 80) is bool(cap)
        assert [hit["id"] for hit in result["hits"]] == [hit["id"] for hit in hits][: len(result["hits"])]
        assert (TRUNCATION_KEY in listing) is (len(listing["nodes"]) < 200) is bool(cap)
        assert listing["path"] == "A_Folkhalsodata/C_HBSC"
        assert (TRUNCATION_KEY in databases) is (len(databases["databases"]) < 60) is bool(cap)


@pytest.mark.parametrize("source,prefix", [("fohm", "fohm"), ("skolverket.statistik", "skolverket_stat")])
async def test_v1_table_data_cut_sets_truncated(router, make_client, source, prefix):
    """The issue #20 case on PxWeb v1: the cut answer says it is incomplete and keeps what explains its rows."""
    dimensions = {
        "Region": [f"{i:04}" for i in range(29)],
        "Kon": ["1", "2", "3"],
        "ContentsCode": ["BE0101N1"],
        "Tid": [str(2015 + i) for i in range(10)],
    }
    variables = [
        {"code": code, "text": code.lower(), "values": codes, "valueTexts": codes, "time": code == "Tid"}
        for code, codes in dimensions.items()
    ]
    data = dataset(dimensions, value=[i * 1.5 for i in range(870)], note=[f"Fotnot {i}" for i in range(45)])
    router.add("GET", r"/sv/\?config$", {"maxCells": 100000})
    router.add("GET", r"/T870\.px$", {"title": "Tabell", "variables": variables})
    router.add("POST", r"/T870\.px$", data)
    args = {"table_path": "T870.px", "selection": {"Region": ["*"], "Kon": ["*"], "Tid": ["*"]}, "max_rows": 1000}
    async with make_client(source) as client:
        whole = await call(client, f"{prefix}_get_table_data", args)
    async with make_client(source, max_output_chars=30000) as client:
        cut = await call(client, f"{prefix}_get_table_data", args)
    rows = cut["data"]["rows"]
    assert whole["data"]["truncated"] is False and len(whole["data"]["rows"]) == 870
    assert cut["data"]["truncated"] is True and cut["data"]["total_rows"] == 870 and 0 < len(rows) < 870
    assert rows == whole["data"]["rows"][: len(rows)] and cut["selection"] == whole["selection"]
    for key in ("dimensions", "columns", "notes"):
        assert cut["data"][key] == whole["data"][key]
    assert cut[TRUNCATION_KEY]["kortade"] == [{"falt": "data.rows", "fore": 870, "efter": len(rows)}]


@pytest.mark.parametrize("cap", [0, 3000])
@pytest.mark.parametrize(
    "source,key,prefix", [("fohm", "fohm", "fohm"), ("skolverket.statistik", "skolverket_statistik", "skolverket_stat")]
)
async def test_v2_search_walk(router, make_client, source, key, prefix, cap):
    """With *_API_VERSION=v2 the search is PxWebApi 2's paged one, and a cut page continues without gaps."""
    router.add("GET", r"example\.se/api/v2/tables\?", table_pages(237))
    settings = {
        "base_urls": {**DEFAULT_BASE_URLS, key: "https://example.se/api/v2"},
        "api_versions": {**DEFAULT_API_VERSIONS, key: "v2"},
    }
    async with make_client(source, max_output_chars=cap, **settings) as client:
        seen, cuts = await walk_search(client, f"{prefix}_search_tables", {"query": "elever", "page_size": 20}, cap)
    assert_no_skips(seen, [summary(i)["id"] for i in range(237)], max_dups=10 * cuts)
    assert (cuts > 0) is bool(cap)
