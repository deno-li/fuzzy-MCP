# SPDX-FileCopyrightText: 2026 Deniz Özer
#
# SPDX-License-Identifier: MIT

"""Smoke tests against the REAL public APIs.

Skipped by default. Run them from a machine with internet access:

    FUZZY_MCP_LIVE_TESTS=1 python -m pytest -q -m live

They make a handful of small requests per source and check the response
shapes the server relies on. Run them before a release and whenever an
agency changes its API.
"""

import os

import pytest
from mcp import Client

from fuzzy_mcp.config import Settings
from fuzzy_mcp.server import build_server

from .conftest import call

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.live,
    pytest.mark.skipif(os.environ.get("FUZZY_MCP_LIVE_TESTS") != "1", reason="sätt FUZZY_MCP_LIVE_TESTS=1"),
]


def live_client() -> Client:
    return Client(build_server(Settings.from_env()))


async def test_scb_live():
    async with live_client() as client:
        config = await call(client, "scb_get_config")
        assert config["maxDataCells"] > 0
        found = await call(client, "scb_search_tables", {"query": "folkmängd", "page_size": 5})
        assert found["total"] > 0 and found["tables"][0]["id"].startswith("TAB")
        meta = await call(client, "scb_get_table_metadata", {"table_id": "TAB638", "max_values": 3})
        assert {v["code"] for v in meta["variables"]} >= {"Region", "ContentsCode", "Tid"}
        data = await call(
            client,
            "scb_get_table_data",
            {"table_id": "TAB638", "selection": {"Region": ["0180"], "ContentsCode": ["BE0101N1"], "Tid": ["TOP(1)"]}},
        )
        assert data["data"]["rows"] and isinstance(data["data"]["rows"][0][-1], int | float)


async def test_fohm_live():
    async with live_client() as client:
        databases = await call(client, "fohm_list_databases")
        assert any(d["id"] == "A_Folkhalsodata" for d in databases["databases"])
        listing = await call(client, "fohm_browse", {"path": "A_Folkhalsodata"})
        assert listing["nodes"]
        hits = await call(client, "fohm_search_tables", {"query": "mobbning"})
        assert hits["total"] > 0
        table = hits["hits"][0]["path"]
        meta = await call(client, "fohm_get_table_metadata", {"table_path": table, "max_values": 2})
        assert meta["variables"]
        selection = {
            v["code"]: ["TOP(1)"] if v["kind"] == "time" else [v["values"][0]["code"]]
            for v in meta["variables"]
            if v["values"]
        }
        data = await call(client, "fohm_get_table_data", {"table_path": table, "selection": selection})
        assert data["data"]["total_rows"] >= 1


async def test_scb_live_verified_selection():
    """TAB637 (medelålder) with Region=2180, Kon=1+2, ContentsCode=BE0101G9 was verified on 2026-09-28."""
    async with live_client() as client:
        data = await call(
            client,
            "scb_get_table_data",
            {
                "table_id": "TAB637",
                "selection": {
                    "Region": ["2180"],
                    "Kon": ["1+2"],
                    "ContentsCode": ["BE0101G9"],
                    "Tid": ["2024", "2025"],
                },
            },
        )
        assert len(data["data"]["rows"]) == 2


async def test_skolverket_statistikdatabas_live():
    table = (
        "Skolverkets_statistikdatabas/Kommunala_jamforelsetal/Grundskola/Skolor_och_elever/Grundskola_skolor_elever.px"
    )
    async with live_client() as client:
        databases = await call(client, "skolverket_stat_list_databases")
        assert any(d["id"] == "Skolverkets_statistikdatabas" for d in databases["databases"])
        meta = await call(client, "skolverket_stat_get_table_metadata", {"table_path": table, "max_values": 3})
        assert [v["code"] for v in meta["variables"]] == ["variable", "level", "time"]
        data = await call(
            client,
            "skolverket_stat_get_table_data",
            {"table_path": table, "selection": {"variable": ["2"], "level": ["2180", "00"], "time": ["TOP(2)"]}},
        )
        assert data["data"]["total_rows"] == 4


async def test_skolverket_api_live():
    async with live_client() as client:
        units = await call(client, "skolverket_search_school_units", {"municipality_code": ["2180"], "limit": 5})
        assert units["total"] > 0
        code = units["items"][0]["school_unit_code"] if "items" in units else None
        assert code is None or len(code) == 8
        subjects = await call(client, "skolverket_list_subjects", {"schooltype": "GY", "limit": 5})
        assert subjects
        info = await call(client, "skolverket_susa_api_info")
        assert info


async def test_scb_geodata_live():
    async with live_client() as client:
        layers = await call(client, "scb_geodata_layers", {"search": "deso"})
        names = [layer["name"] for layer in layers["layers"]]
        assert "DeSO_2025" in names or "DeSO_2018" in names
        layer = "DeSO_2025" if "DeSO_2025" in names else "DeSO_2018"
        described = await call(client, "scb_geodata_describe_layer", {"layer": layer})
        assert any(a["geometry"] for a in described["attributes"])
        features = await call(client, "scb_geodata_get_features", {"layer": layer, "limit": 3})
        assert features["returned"] == 3


async def test_scb_geodata_locate_live():
    """INTERSECTS(sp_geometry,POINT(N E)) for east=674032, north=6580822 (Stockholm) gave DeSO 0180C4040 with
    regsokod 0180R047 on 2026-10-10. If SCB republishes the layer the point may land elsewhere; then this fails."""
    async with live_client() as client:
        result = await call(client, "scb_geodata_locate", {"east": 674032, "north": 6580822, "layers": ["DeSO_2025"]})
        assert result["punkt"]["sweref99tm"] == {"east": 674032.0, "north": 6580822.0}
        hits = [hit for hit in result["traffar"] if hit["layer"] == "DeSO_2025"]
        if hits:
            assert hits[0]["properties"]["desokod"] == "0180C4040"
            assert hits[0]["properties"]["regsokod"] == "0180R047"
