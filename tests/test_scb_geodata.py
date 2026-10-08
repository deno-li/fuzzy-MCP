# SPDX-FileCopyrightText: 2026 Deniz Özer
#
# SPDX-License-Identifier: MIT

from pathlib import Path

import httpx2
import pytest

from fuzzy_mcp.sources.scb_geodata import GeoAttribute, build_cql

from .conftest import call, call_error, load_fixture

pytestmark = pytest.mark.anyio

WFS = "https://geodata.scb.se/geoserver/stat/wfs"
CAPS = (Path(__file__).parent / "fixtures" / "scb_geodata_capabilities.xml").read_text(encoding="utf-8")
ATTRS = [GeoAttribute(name="desokod"), GeoAttribute(name="kommunkod"), GeoAttribute(name="geom", geometry=True)]


def test_build_cql_quotes_and_validates():
    assert build_cql({"Kommunkod": ["2180", "2181"]}, ATTRS) == "(kommunkod = '2180' OR kommunkod = '2181')"
    assert build_cql({"desokod": "2180C*"}, ATTRS) == "(desokod LIKE '2180C%')"
    assert build_cql({"kommunkod": "x' OR '1'='1"}, ATTRS) == "(kommunkod = 'x'' OR ''1''=''1')"
    with pytest.raises(Exception, match="Okänt attribut 'geom'"):
        build_cql({"geom": "x"}, ATTRS)


def _routes(router):
    router.add("GET", r"request=GetCapabilities", httpx2.Response(200, text=CAPS, headers={"content-type": "text/xml"}))
    router.add("GET", r"request=DescribeFeatureType", load_fixture("scb_geodata_describe_deso.json"))
    router.add("GET", r"request=GetFeature", load_fixture("scb_geodata_features.json"))


async def test_layers_describe_and_features(router, make_client):
    _routes(router)
    async with make_client("scb.geodata") as client:
        names = {t.name for t in (await client.list_tools()).tools}
        assert {"scb_geodata_layers", "scb_geodata_get_features"} <= names and "scb_search_tables" not in names
        layers = await call(client, "scb_geodata_layers", {"search": "tätort"})
        assert [layer["name"] for layer in layers["layers"]] == ["Tatorter_2023"]
        described = await call(client, "scb_geodata_describe_layer", {"layer": "stat:DeSO_2025"})
        assert [a["name"] for a in described["attributes"] if a["geometry"]] == ["geom"]
        result = await call(
            client, "scb_geodata_get_features", {"layer": "DeSO_2025", "filters": {"kommunkod": "2180"}, "limit": 1}
        )
    request = router.last()
    assert str(request.url).startswith(WFS)
    params = request.url.params
    assert params["typeName"] == "stat:DeSO_2025" and params["outputFormat"] == "application/json"
    assert params["propertyName"] == "desokod,regsokod,kommunkod,kommunnamn"
    assert params["CQL_FILTER"] == "(kommunkod = '2180')" and params["maxFeatures"] == "2"
    assert result["total"] == 3 and result["returned"] == 1 and result["truncated"] is True
    assert result["features"][0] == {
        "desokod": "2180C1010",
        "regsokod": "2180R001",
        "kommunkod": "2180",
        "kommunnamn": "Gävle",
    }


async def test_download_url_and_validation(router, make_client):
    _routes(router)
    async with make_client("scb.geodata") as client:
        link = await call(client, "scb_geodata_download_url", {"layer": "DeSO_2025", "format": "shape-zip"})
        assert link["url"].startswith(f"{WFS}?service=WFS&version=1.1.0&request=GetFeature&typeName=stat:DeSO_2025")
        assert "outputFormat=shape-zip" in link["url"] and "format_options=CHARSET:UTF-8" in link["url"]
        err = await call_error(client, "scb_geodata_get_features", {"layer": "../etc/passwd"})
        assert "Ogiltigt lagernamn" in err
        err = await call_error(client, "scb_geodata_get_features", {"layer": "DeSO_2025", "filters": {"lan": "21"}})
        assert "Okänt attribut 'lan'" in err


async def test_geometry_is_capped_in_count_and_size(router, make_client):
    ring = [[600000.0 + i, 6700000.0 + (i % 7)] for i in range(2500)]  # ~50 kB per polygon
    features = [
        {
            "type": "Feature",
            "properties": {"desokod": f"2180C10{i:02d}", "kommunkod": "2180"},
            "geometry": {"type": "MultiPolygon", "coordinates": [[ring]]},
        }
        for i in range(30)
    ]
    router.add("GET", r"request=GetCapabilities", httpx2.Response(200, text=CAPS, headers={"content-type": "text/xml"}))
    router.add("GET", r"request=DescribeFeatureType", load_fixture("scb_geodata_describe_deso.json"))
    router.add("GET", r"request=GetFeature", {"type": "FeatureCollection", "totalFeatures": 30, "features": features})
    async with make_client("scb.geodata") as client:
        result = await call(
            client, "scb_geodata_get_features", {"layer": "DeSO_2025", "include_geometry": True, "limit": 500}
        )
    assert router.last().url.params["maxFeatures"] == "11"
    assert result["returned"] == 10 and result["truncated"] is True
    with_geometry = [f for f in result["features"] if "geometry" in f]
    assert len(with_geometry) == 1
    assert result["features"][1]["bbox"] == [600000.0, 6700000.0, 602499.0, 6700006.0]
    assert any("scb_geodata_download_url" in n for n in result["notes"])
