# SPDX-FileCopyrightText: 2026 Deniz Özer
#
# SPDX-License-Identifier: MIT

import pytest

from .conftest import call, call_error, load_fixture

pytestmark = pytest.mark.anyio

BASE = "https://statistikdatabasen.skolverket.se/PxWeb/api/v1/sv"
KJT = "Skolverkets_statistikdatabas/Kommunala_jamforelsetal/Grundskola/Skolor_och_elever/Grundskola_skolor_elever.px"
UFA = "Skolverkets_statistikdatabas/Underlag_for_analys_inom_det_nationella_kvalitetssystemet/Grundskola/Grundskola.px"
CONFIG = {"maxValues": 1000, "maxCells": 1250000, "maxCalls": 10, "timeWindow": 10, "CORS": True}


async def test_databases_and_ui_url(router, make_client):
    router.add("GET", r"/sv/$", [{"dbid": "Skolverkets_statistikdatabas", "text": "Skolverkets statistikdatabas"}])
    router.add("GET", r"/sv/\?config$", CONFIG)
    router.add("GET", r"Grundskola_skolor_elever\.px$", load_fixture("skolverket_stat_kjt_metadata.json"))
    async with make_client("skolverket.statistik") as client:
        names = {t.name for t in (await client.list_tools()).tools}
        assert {"skolverket_stat_browse", "skolverket_stat_get_table_data"} <= names
        assert not any(n.startswith("fohm_") for n in names)
        dbs = await call(client, "skolverket_stat_list_databases")
        assert dbs["databases"][0]["id"] == "Skolverkets_statistikdatabas"
        ui = (
            "https://statistikdatabasen.skolverket.se/PxWeb/pxweb/sv/Skolverkets_statistikdatabas/"
            "Skolverkets_statistikdatabas__Kommunala_jamforelsetal__Grundskola__Skolor_och_elever/"
            "Grundskola_skolor_elever.px/"
        )
        meta = await call(client, "skolverket_stat_get_table_metadata", {"table_path": ui})
    assert meta["table_id"] == KJT
    assert [v["code"] for v in meta["variables"]] == ["variable", "level", "time"]
    assert all(v["elimination"] is False for v in meta["variables"])


async def test_kjt_data_defaults_and_extra_dimension(router, make_client):
    router.add("GET", r"/sv/\?config$", CONFIG)
    router.add("GET", r"Grundskola_skolor_elever\.px$", load_fixture("skolverket_stat_kjt_metadata.json"))
    router.add("POST", r"Grundskola_skolor_elever\.px$", load_fixture("skolverket_stat_data.json"))
    async with make_client("skolverket.statistik") as client:
        result = await call(
            client,
            "skolverket_stat_get_table_data",
            {
                "table_path": KJT,
                "selection": {"variable": ["2"], "level": ["Gävle [2180]", "00"], "time": ["TOP(2)"]},
            },
        )
    body = router.json_body()
    assert body["query"] == [
        {"code": "variable", "selection": {"filter": "item", "values": ["2"]}},
        {"code": "level", "selection": {"filter": "item", "values": ["2180", "00"]}},
        {"code": "time", "selection": {"filter": "item", "values": ["2024", "2025"]}},
    ]
    assert result["data"]["columns"] == ["variable", "level", "time", "value"]
    assert result["data"]["rows"][0] == ["Elever, antal i huvudmannens skolor", "Gävle [2180]", "2024/25", 10101]
    assert result["citation"].startswith("Källa: Skolverket, Skolverkets statistikdatabas")


async def test_ufa_level_without_code_list_is_passed_through(router, make_client):
    router.add("GET", r"/sv/\?config$", CONFIG)
    router.add("GET", r"Grundskola\.px$", load_fixture("skolverket_stat_ufa_metadata.json"))
    router.add("POST", r"Grundskola\.px$", load_fixture("skolverket_stat_data.json"))
    async with make_client("skolverket.statistik") as client:
        meta = await call(client, "skolverket_stat_get_table_metadata", {"table_path": UFA})
        level = meta["variables"][1]
        assert "value_count" not in level and level["values_withheld"] is True
        err = await call_error(
            client,
            "skolverket_stat_get_table_data",
            {"table_path": UFA, "selection": {"variable": ["0"], "time": ["2024"]}},
        )
        assert "'level'" in err and "saknar värdelista" in err
        result = await call(
            client,
            "skolverket_stat_get_table_data",
            {
                "table_path": UFA,
                "selection": {"variable": ["0"], "level": ["2120002338", "2120002338-87313898"], "time": ["2024"]},
            },
        )
        assert result["selection"]["unvalidated"] == ["level"] and result["selection"]["cells"] == 2
        assert router.json_body()["query"][1] == {
            "code": "level",
            "selection": {"filter": "item", "values": ["2120002338", "2120002338-87313898"]},
        }
        result = await call(
            client,
            "skolverket_stat_get_table_data",
            {"table_path": UFA, "selection": {"variable": ["0"], "level": ["*"], "time": ["2024"]}},
        )
        assert result["selection"]["cells"] == -1
        assert router.json_body()["query"][1]["selection"] == {"filter": "all", "values": ["*"]}
        recipe = await call(
            client,
            "skolverket_stat_build_query",
            {"table_path": UFA, "selection": {"variable": ["0"], "level": ["2120002338"], "time": ["TOP(1)"]}},
        )
    assert recipe["url"] == f"{BASE}/{UFA}"
    assert recipe["body"]["query"][2]["selection"] == {"filter": "item", "values": ["2025"]}
