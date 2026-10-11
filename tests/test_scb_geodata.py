# SPDX-FileCopyrightText: 2026 Deniz Özer
#
# SPDX-License-Identifier: MIT

"""SCB:s öppna geodata (WFS).

Fixtures ``scb_geodata_describe_deso.json``, ``scb_geodata_describe_regso.json``, ``scb_geodata_features.json``
and the single feature in ``scb_geodata_intersects.json`` are real answers from geodata.scb.se recorded on
2026-10-10 (verifierat uttag). ``scb_geodata_features_wgs84.json`` has the real crs/properties and the real first
corner of DeSO 1280C1070 in EPSG:4326, followed by synthetic vertices.
"""

import json
import re
from pathlib import Path

import httpx2
import pytest

from fuzzy_mcp.geo import sweref99tm_to_wgs84
from fuzzy_mcp.sources.scb_geodata import (
    GeoAttribute,
    build_cql,
    coordinate_text,
    default_sort_attribute,
    point_cql,
)

from .conftest import call, call_error, load_fixture

pytestmark = pytest.mark.anyio

WFS = "https://geodata.scb.se/geoserver/stat/wfs"
CAPS = (Path(__file__).parent / "fixtures" / "scb_geodata_capabilities.xml").read_text(encoding="utf-8")
ATTRS = [GeoAttribute(name="desokod"), GeoAttribute(name="kommunkod"), GeoAttribute(name="sp_geometry", geometry=True)]
# Non-geometry attributes of DeSO_2025/RegSO_2025 in DescribeFeatureType order (2026-10-10).
DESO_DATA = (
    "objectid,objektidentitet,objekttyp,desokod,regsokod,lanskod,kommunkod,version,ansvarig_organisation,referensdatum"
)
# RegSO layers carry regsonamn instead of desokod.
REGSO_DATA = DESO_DATA.replace("desokod,regsokod", "regsokod,regsonamn")
DESO_0180C4040 = {
    "objectid": 2804,
    "objektidentitet": "745c9219-deb4-43ca-8556-69dd0d69ce09",
    "objekttyp": "deso",
    "desokod": "0180C4040",
    "regsokod": "0180R047",
    "lanskod": "01",
    "kommunkod": "0180",
    "version": "2025_v2",
    "ansvarig_organisation": "Statistiska centralbyrån",
    "referensdatum": "20250101",
}
EMPTY = {"type": "FeatureCollection", "features": [], "totalFeatures": 0, "numberMatched": 0, "numberReturned": 0}


def test_build_cql_quotes_and_validates():
    assert build_cql({"Kommunkod": ["2180", "2181"]}, ATTRS) == "(kommunkod = '2180' OR kommunkod = '2181')"
    assert build_cql({"desokod": "2180C*"}, ATTRS) == "(desokod LIKE '2180C%')"
    assert build_cql({"kommunkod": "x' OR '1'='1"}, ATTRS) == "(kommunkod = 'x'' OR ''1''=''1')"
    with pytest.raises(Exception, match=re.escape("Okänt attribut 'lan'. Lagrets attribut: desokod, kommunkod")):
        build_cql({"lan": "x"}, ATTRS)
    with pytest.raises(
        Exception,
        match=re.escape(
            "Attributet 'sp_geometry' är lagrets geometri och kan inte användas i filters, attributes eller sort_by; "
            "geometrin fås med include_geometry eller scb_geodata_download_url. Lagrets attribut: desokod, kommunkod"
        ),
    ):
        build_cql({"SP_GEOMETRY": "x"}, ATTRS)


def test_point_cql_writes_northing_before_easting():
    # Verified 2026-10-10: POINT(6580822 674032) found DeSO 0180C4040, POINT(674032 6580822) found nothing.
    assert point_cql("sp_geometry", 674032, 6580822) == "INTERSECTS(sp_geometry,POINT(6580822 674032))"
    assert point_cql("sp_geometry", 679180.062, 6562397.097) == "INTERSECTS(sp_geometry,POINT(6562397.097 679180.062))"
    assert coordinate_text(379844.17075651) == "379844.171" and coordinate_text(500000.0) == "500000"


def test_default_sort_attribute_prefers_code_attributes():
    assert default_sort_attribute(ATTRS).name == "desokod"
    assert default_sort_attribute([GeoAttribute(name="objectid"), GeoAttribute(name="regsokod")]).name == "regsokod"
    assert default_sort_attribute([GeoAttribute(name="objectid"), GeoAttribute(name="namn")]).name == "objectid"


def _describe(request: httpx2.Request) -> httpx2.Response:
    layer = request.url.params["typeName"].split(":")[-1]
    fixture = "scb_geodata_describe_regso.json" if layer.startswith("RegSO") else "scb_geodata_describe_deso.json"
    return httpx2.Response(200, json=load_fixture(fixture))


def _routes(router, features="scb_geodata_features.json"):
    router.add("GET", r"request=GetCapabilities", httpx2.Response(200, text=CAPS, headers={"content-type": "text/xml"}))
    router.add("GET", r"request=DescribeFeatureType", _describe)
    router.add("GET", r"request=GetFeature", load_fixture(features) if isinstance(features, str) else features)


def _get_feature_requests(router) -> list[httpx2.Request]:
    return [r for r in router.requests if r.url.params.get("request") == "GetFeature"]


async def test_layers_describe_and_features(router, make_client):
    _routes(router)
    async with make_client("scb.geodata") as client:
        names = {t.name for t in (await client.list_tools()).tools}
        assert {"scb_geodata_layers", "scb_geodata_get_features", "scb_geodata_locate"} <= names
        assert "scb_search_tables" not in names
        layers = await call(client, "scb_geodata_layers", {"search": "tätort"})
        assert [layer["name"] for layer in layers["layers"]] == ["Tatorter_2023"]
        described = await call(client, "scb_geodata_describe_layer", {"layer": "stat:DeSO_2025"})
        assert [a["name"] for a in described["attributes"] if a["geometry"]] == ["sp_geometry"]
        assert ",".join(a["name"] for a in described["attributes"] if not a["geometry"]) == DESO_DATA
        assert described["attributes"][0] == {"name": "objectid", "type": "int", "geometry": False}
        result = await call(
            client, "scb_geodata_get_features", {"layer": "DeSO_2025", "filters": {"kommunkod": "2180"}, "limit": 1}
        )
    request = router.last()
    assert str(request.url).startswith(WFS)
    params = request.url.params
    assert params["typeName"] == "stat:DeSO_2025" and params["outputFormat"] == "application/json"
    assert params["propertyName"] == DESO_DATA
    assert params["CQL_FILTER"] == "(kommunkod = '2180')" and params["maxFeatures"] == "2"
    assert not {"startIndex", "sortBy", "srsName"} & set(params.keys())
    assert result["total"] == 6160 and result["returned"] == 1 and result["truncated"] is True
    assert result["offset"] == 0 and "srs" not in result
    assert result["features"][0] == {
        "objectid": 1,
        "objektidentitet": "2f346855-dd37-46f0-8e82-c905017489e8",
        "objekttyp": "deso",
        "desokod": "1280C1070",
        "regsokod": "1280R055",
        "lanskod": "12",
        "kommunkod": "1280",
        "version": "2025_v2",
        "ansvarig_organisation": "Statistiska centralbyrån",
        "referensdatum": "20250101",
    }


async def test_attributes_offset_and_sorting(router, make_client):
    _routes(router)
    async with make_client("scb.geodata") as client:
        result = await call(
            client,
            "scb_geodata_get_features",
            {"layer": "DeSO_2025", "attributes": ["Kommunkod", "desokod", "kommunkod"], "offset": 2, "limit": 2},
        )
        params = router.last().url.params
        assert params["propertyName"] == "kommunkod,desokod"
        assert params["startIndex"] == "2" and params["sortBy"] == "desokod" and params["maxFeatures"] == "3"
        assert result["offset"] == 2
        assert any(n.startswith("Sortering vid offset: desokod (standard") for n in result["notes"])
        assert not any("unikt per objekt" in n for n in result["notes"])

        result = await call(
            client, "scb_geodata_get_features", {"layer": "DeSO_2025", "offset": 10, "sort_by": "kommunkod"}
        )
        params = router.last().url.params
        assert params["startIndex"] == "10" and params["sortBy"] == "kommunkod"
        assert "Sortering vid offset: kommunkod (sort_by)" in result["notes"]
        assert any(n.startswith("Bläddring är stabil bara om sort_by är unikt per objekt") for n in result["notes"])

        await call(client, "scb_geodata_get_features", {"layer": "DeSO_2025", "sort_by": "desokod"})
        params = router.last().url.params
        assert params["sortBy"] == "desokod" and "startIndex" not in params

        await call(client, "scb_geodata_get_features", {"layer": "RegSO_2025", "offset": 1})
        params = router.last().url.params
        assert params["sortBy"] == "regsokod" and params["propertyName"] == REGSO_DATA

        err = await call_error(client, "scb_geodata_get_features", {"layer": "DeSO_2025", "attributes": ["lan"]})
        assert "Okänt attribut 'lan'" in err and "desokod" in err and "sp_geometry" not in err
        err = await call_error(client, "scb_geodata_get_features", {"layer": "DeSO_2025", "sort_by": "sp_geometry"})
        assert "Attributet 'sp_geometry' är lagrets geometri" in err and "include_geometry" in err
        err = await call_error(
            client, "scb_geodata_get_features", {"layer": "DeSO_2025", "attributes": ["sp_geometry"]}
        )
        assert "Attributet 'sp_geometry' är lagrets geometri" in err
        err = await call_error(client, "scb_geodata_get_features", {"layer": "DeSO_2025", "offset": -1})
        assert "offset måste vara 0 eller större" in err


async def test_geometry_srs(router, make_client):
    _routes(router, "scb_geodata_features_wgs84.json")
    async with make_client("scb.geodata") as client:
        result = await call(
            client,
            "scb_geodata_get_features",
            {"layer": "DeSO_2025", "include_geometry": True, "srs": "EPSG:4326", "limit": 1},
        )
        params = router.last().url.params
        assert params["srsName"] == "EPSG:4326" and "propertyName" not in params
        assert result["srs"] == "EPSG:4326"
        assert result["features"][0]["geometry"]["coordinates"][0][0] == [13.09577306, 55.5417832]
        assert result["features"][0]["desokod"] == "1280C1070"
        assert any("[lon, lat]" in n for n in result["notes"])

        await call(
            client,
            "scb_geodata_get_features",
            {"layer": "DeSO_2025", "include_geometry": True, "attributes": ["desokod"], "limit": 1},
        )
        params = router.last().url.params
        assert params["propertyName"] == "desokod,sp_geometry" and params["srsName"] == "EPSG:3006"

        result = await call(client, "scb_geodata_get_features", {"layer": "DeSO_2025", "srs": "EPSG:4326"})
        assert "srsName" not in router.last().url.params and "srs" not in result
        assert any(n.startswith("srs='EPSG:4326' gäller bara geometrin") for n in result["notes"])


async def test_download_url_and_validation(router, make_client):
    _routes(router)
    async with make_client("scb.geodata") as client:
        link = await call(client, "scb_geodata_download_url", {"layer": "DeSO_2025", "format": "shape-zip"})
        assert link["url"].startswith(f"{WFS}?service=WFS&version=1.1.0&request=GetFeature&typeName=stat:DeSO_2025")
        assert "outputFormat=shape-zip" in link["url"] and "format_options=CHARSET:UTF-8" in link["url"]
        assert "srsName" not in link["url"] and link["srs"] == "EPSG:3006"
        link = await call(
            client, "scb_geodata_download_url", {"layer": "DeSO_2025", "format": "geojson", "srs": "EPSG:4326"}
        )
        assert "outputFormat=application%2Fjson" in link["url"] and "srsName=EPSG:4326" in link["url"]
        assert link["srs"] == "EPSG:4326"
        err = await call_error(
            client, "scb_geodata_download_url", {"layer": "DeSO_2025", "format": "csv", "srs": "EPSG:4326"}
        )
        assert "bara verifierat för format='geojson'" in err
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
    _routes(router, {"type": "FeatureCollection", "totalFeatures": 30, "features": features})
    async with make_client("scb.geodata") as client:
        result = await call(
            client, "scb_geodata_get_features", {"layer": "DeSO_2025", "include_geometry": True, "limit": 500}
        )
    params = router.last().url.params
    assert params["maxFeatures"] == "11" and params["srsName"] == "EPSG:3006" and "propertyName" not in params
    assert result["returned"] == 10 and result["truncated"] is True and result["srs"] == "EPSG:3006"
    with_geometry = [f for f in result["features"] if "geometry" in f]
    assert len(with_geometry) == 1
    assert result["features"][1]["bbox"] == [600000.0, 6700000.0, 602499.0, 6700006.0]
    assert any("scb_geodata_download_url" in n for n in result["notes"])


# --- scb_geodata_locate -----------------------------------------------------------------------------------


def _intersects_by_layer(request: httpx2.Request) -> httpx2.Response:
    """DeSO layers answer with the verified hit for POINT(6580822 674032); other layers answer empty."""
    layer = request.url.params["typeName"].split(":")[-1]
    body = load_fixture("scb_geodata_intersects.json") if layer.startswith("DeSO") else EMPTY
    return httpx2.Response(200, json=body)


def _point(cql: str) -> tuple[float, float]:
    """(north, east) from an INTERSECTS(sp_geometry,POINT(N E)) filter."""
    match = re.fullmatch(r"INTERSECTS\(sp_geometry,POINT\(([0-9.]+) ([0-9.]+)\)\)", cql)
    assert match, cql
    return float(match.group(1)), float(match.group(2))


async def test_locate_sweref_point(router, make_client):
    _routes(router, _intersects_by_layer)
    async with make_client("scb.geodata") as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        schema = tools["scb_geodata_locate"].input_schema["properties"]
        assert schema["layers"]["default"] == ["DeSO_2025", "RegSO_2025"]
        assert tools["scb_geodata_locate"].annotations.read_only_hint is True
        result = await call(client, "scb_geodata_locate", {"east": 674032, "north": 6580822})
    requests = _get_feature_requests(router)
    assert [r.url.params["typeName"] for r in requests] == ["stat:DeSO_2025", "stat:RegSO_2025"]
    deso, regso = (r.url.params for r in requests)
    assert deso["CQL_FILTER"] == "INTERSECTS(sp_geometry,POINT(6580822 674032))"
    assert deso["propertyName"] == DESO_DATA and "sp_geometry" not in deso["propertyName"]
    assert deso["maxFeatures"] == "5" and deso["outputFormat"] == "application/json"
    assert regso["CQL_FILTER"] == deso["CQL_FILTER"] and regso["propertyName"] == REGSO_DATA
    assert result["traffar"] == [
        {"layer": "DeSO_2025", "properties": DESO_0180C4040, "request_url": str(requests[0].url)}
    ]
    assert result["punkt"]["sweref99tm"] == {"east": 674032.0, "north": 6580822.0}
    assert result["punkt"]["harledd"] == "wgs84"
    wgs84 = result["punkt"]["wgs84"]
    assert 59.33 < wgs84["lat"] < 59.331 and 18.059 < wgs84["lon"] < 18.06
    assert "skolenhet" not in result
    assert result["citation"] == "Källa: SCB, öppna geodata (CC0 1.0)"
    assert any(n.startswith("Ingen träff i RegSO_2025") for n in result["notes"])
    assert any("omräknad från SWEREF 99 TM" in n for n in result["notes"])
    # The hit is DeSO 2025 (version '2025_v2'): Statistikdatabasen spells the code with a suffix.
    assert any("suffix _DeSO2025/_RegSO2025 (ref_lookup_deso ger dem under ssd_koder)" in n for n in result["notes"])


async def test_locate_wgs84_point_is_converted(router, make_client):
    _routes(router, "scb_geodata_intersects.json")
    async with make_client("scb.geodata") as client:
        result = await call(
            client, "scb_geodata_locate", {"lat": 55.5417832, "lon": 13.09577306, "layers": ["stat:DeSO_2025"]}
        )
    north, east = _point(router.last().url.params["CQL_FILTER"])
    # GeoServer's own conversion of this corner of DeSO 1280C1070 (2026-10-10): E 379844.1709, N 6156729.8055.
    assert east == pytest.approx(379844.1709, abs=0.05) and north == pytest.approx(6156729.8055, abs=0.05)
    assert result["punkt"]["harledd"] == "sweref99tm"
    assert result["punkt"]["wgs84"] == {"lat": 55.5417832, "lon": 13.09577306}
    assert result["punkt"]["sweref99tm"]["east"] == pytest.approx(east, abs=0.001)
    assert any("omräknad från WGS84" in n for n in result["notes"])
    assert [hit["layer"] for hit in result["traffar"]] == ["DeSO_2025"]


async def test_locate_school_unit_uses_register_coordinates(router, make_client):
    _routes(router, _intersects_by_layer)
    router.add("GET", r"/v2/school-units/12345678", load_fixture("skolenhetsregistret_school_unit.json"))
    async with make_client("scb.geodata,skolverket.skolenhetsregistret") as client:
        result = await call(client, "scb_geodata_locate", {"school_unit_code": " 12345678 ", "layers": ["DeSO_2025"]})
    unit_request = next(r for r in router.requests if "/school-units/" in r.url.path)
    assert unit_request.url.path == "/skolenhetsregistret/v2/school-units/12345678"
    # coordinateSweRefE "679180,062" / coordinateSweRefN "6562397,097" from the register, written POINT(N E).
    assert router.last().url.params["CQL_FILTER"] == "INTERSECTS(sp_geometry,POINT(6562397.097 679180.062))"
    assert result["skolenhet"] == {
        "kod": "12345678",
        "namn": "Storskolan",
        "kommunkod": "0180",
        "adresstyp": "BESOKSADRESS",
    }
    assert result["punkt"]["sweref99tm"] == {"east": 679180.062, "north": 6562397.097}
    assert result["punkt"]["wgs84"] == {"lat": 59.16286545184274, "lon": 18.134313905557242}
    assert "harledd" not in result["punkt"]
    assert not any("inte besöksadressen" in n for n in result["notes"])
    assert result["citation"] == "Källa: SCB, öppna geodata (CC0 1.0); Skolverket, Skolenhetsregistret"
    assert result["traffar"][0]["properties"]["desokod"] == "0180C4040"
    dumped = json.dumps(result, ensure_ascii=False)
    assert "Förnamn" not in dumped and "storskolan@" not in dumped and "Rektorsexpeditionen" not in dumped


async def test_locate_school_unit_falls_back_to_wgs84(router, make_client):
    _routes(router, "scb_geodata_intersects.json")
    router.add("GET", r"/v2/school-units/43038662", load_fixture("skolenhetsregistret_school_unit_minimal.json"))
    no_coordinates = load_fixture("skolenhetsregistret_school_unit_minimal.json")
    no_coordinates["data"]["schoolUnitCode"] = "11111111"
    del no_coordinates["data"]["attributes"]["addresses"][0]["geoCoordinates"]
    router.add("GET", r"/v2/school-units/11111111", no_coordinates)
    async with make_client("scb.geodata,skolverket") as client:
        result = await call(client, "scb_geodata_locate", {"school_unit_code": "43038662", "layers": ["DeSO_2025"]})
        north, east = _point(router.last().url.params["CQL_FILTER"])
        assert result["punkt"]["harledd"] == "sweref99tm"
        assert result["punkt"]["wgs84"] == {"lat": 59.393258, "lon": 17.662399}
        assert result["punkt"]["sweref99tm"] == {"east": pytest.approx(east), "north": pytest.approx(north)}
        assert 645_000 < east < 655_000 and 6_585_000 < north < 6_595_000  # Färentuna, Ekerö
        assert result["skolenhet"] == {
            "kod": "43038662",
            "namn": "Färentuna skola",
            "kommunkod": "0125",
            "adresstyp": "BESOKSADRESS",
        }
        err = await call_error(client, "scb_geodata_locate", {"school_unit_code": "11111111"})
        assert "saknar koordinater i Skolenhetsregistret" in err
        err = await call_error(client, "scb_geodata_locate", {"school_unit_code": "123"})
        assert "Ogiltig skolenhetskod" in err


def _school_unit_with(visit_geo: dict | None, postal_geo: dict | None) -> dict:
    """The Storskolan fixture (addresses: POSTADRESS 'Box 123', BESOKSADRESS) with the geoCoordinates replaced."""
    unit = load_fixture("skolenhetsregistret_school_unit.json")
    postal, visit = unit["data"]["attributes"]["addresses"]
    assert postal["type"] == "POSTADRESS" and visit["type"] == "BESOKSADRESS"
    for address, geo in ((visit, visit_geo), (postal, postal_geo)):
        address.pop("geoCoordinates", None)
        if geo is not None:
            address["geoCoordinates"] = geo
    return unit


SWEREF_BOX = {"coordinateSweRefN": "6562397,097", "coordinateSweRefE": "679180,062"}
WGS84_VISIT = {"latitude": "59.393258", "longitude": "17.662399"}  # Färentuna, 35 km from SWEREF_BOX


async def test_locate_school_unit_prefers_the_visit_address_over_a_postal_address_with_sweref(router, make_client):
    """The visit address has only lat/lon while the postal address (a box) has SWEREF 99 TM: the point is the visit
    address, converted – not the box 35 km away."""
    _routes(router, "scb_geodata_intersects.json")
    router.add("GET", r"/v2/school-units/12345678", _school_unit_with(WGS84_VISIT, SWEREF_BOX))
    async with make_client("scb.geodata,skolverket") as client:
        result = await call(client, "scb_geodata_locate", {"school_unit_code": "12345678", "layers": ["DeSO_2025"]})
    north, east = _point(router.last().url.params["CQL_FILTER"])
    assert 645_000 < east < 655_000 and 6_585_000 < north < 6_595_000
    assert result["punkt"]["harledd"] == "sweref99tm"
    assert result["punkt"]["wgs84"] == {"lat": 59.393258, "lon": 17.662399}
    assert result["skolenhet"]["adresstyp"] == "BESOKSADRESS"
    assert not any("inte besöksadressen" in n for n in result["notes"])


async def test_locate_school_unit_reports_when_another_address_is_used(router, make_client):
    """Only the postal address has coordinates: they are used, and the answer says so."""
    _routes(router, "scb_geodata_intersects.json")
    router.add("GET", r"/v2/school-units/12345678", _school_unit_with(None, SWEREF_BOX))
    async with make_client("scb.geodata,skolverket") as client:
        result = await call(client, "scb_geodata_locate", {"school_unit_code": "12345678", "layers": ["DeSO_2025"]})
    assert router.last().url.params["CQL_FILTER"] == "INTERSECTS(sp_geometry,POINT(6562397.097 679180.062))"
    assert result["skolenhet"]["adresstyp"] == "POSTADRESS"
    assert (
        "Koordinaterna kommer från adressen av typ POSTADRESS, inte besöksadressen: besöksadressen saknar "
        "koordinater i Skolenhetsregistret."
    ) in result["notes"]
    assert "Box 123" not in json.dumps(result, ensure_ascii=False)


async def test_locate_school_unit_uses_sweref_when_the_register_pairs_disagree(router, make_client):
    """The visit address carries both pairs but they are 35 km apart: SWEREF 99 TM is used, WGS84 is derived from
    it and the answer says why. With both pairs on the same spot nothing is derived and no note is given."""
    _routes(router, "scb_geodata_intersects.json")
    router.add("GET", r"/v2/school-units/12345678", _school_unit_with({**SWEREF_BOX, **WGS84_VISIT}, None))
    lat, lon = sweref99tm_to_wgs84(679180.062, 6562397.097)
    same_spot = {**SWEREF_BOX, "latitude": f"{lat:.6f}", "longitude": f"{lon:.6f}"}
    consistent = _school_unit_with(same_spot, None)
    consistent["data"]["schoolUnitCode"] = "22222222"
    router.add("GET", r"/v2/school-units/22222222", consistent)
    async with make_client("scb.geodata,skolverket") as client:
        result = await call(client, "scb_geodata_locate", {"school_unit_code": "12345678", "layers": ["DeSO_2025"]})
        assert router.last().url.params["CQL_FILTER"] == "INTERSECTS(sp_geometry,POINT(6562397.097 679180.062))"
        assert result["punkt"]["harledd"] == "wgs84"
        assert abs(result["punkt"]["wgs84"]["lat"] - lat) < 1e-6 and abs(result["punkt"]["wgs84"]["lon"] - lon) < 1e-6
        gap_notes = [n for n in result["notes"] if "ligger" in n and "m isär" in n]
        assert len(gap_notes) == 1 and "SWEREF 99 TM används" in gap_notes[0]
        gap = re.search(r"ligger (\d+) m isär", gap_notes[0])
        assert gap is not None and 30_000 < int(gap.group(1)) < 40_000, gap_notes[0]

        result = await call(client, "scb_geodata_locate", {"school_unit_code": "22222222", "layers": ["DeSO_2025"]})
        assert router.last().url.params["CQL_FILTER"] == "INTERSECTS(sp_geometry,POINT(6562397.097 679180.062))"
        assert "harledd" not in result["punkt"]
        assert result["punkt"]["wgs84"] == {"lat": round(lat, 6), "lon": round(lon, 6)}
        assert not any("m isär" in n or "omräknad" in n for n in result["notes"])


async def test_locate_school_unit_skips_implausible_register_coordinates(router, make_client):
    """SWEREF 99 TM '0'/'0' in the register (to_float gives 0.0, not None) is not an input error: the WGS84 pair of
    the same address is used; with no plausible pair at all the error names the register's values."""
    _routes(router, "scb_geodata_intersects.json")
    zero = {"coordinateSweRefN": "0", "coordinateSweRefE": "0"}
    router.add("GET", r"/v2/school-units/12345678", _school_unit_with({**zero, **WGS84_VISIT}, None))
    only_zero = _school_unit_with(zero, None)
    only_zero["data"]["schoolUnitCode"] = "22222222"
    router.add("GET", r"/v2/school-units/22222222", only_zero)
    async with make_client("scb.geodata,skolverket") as client:
        result = await call(client, "scb_geodata_locate", {"school_unit_code": "12345678", "layers": ["DeSO_2025"]})
        assert result["punkt"]["harledd"] == "sweref99tm"
        assert result["punkt"]["wgs84"] == {"lat": 59.393258, "lon": 17.662399}
        assert 645_000 < result["punkt"]["sweref99tm"]["east"] < 655_000
        err = await call_error(client, "scb_geodata_locate", {"school_unit_code": "22222222"})
    assert "Skolenhetsregistret anger orimliga koordinater för skolenheten 22222222 (Storskolan)" in err
    assert "E=0.0, N=0.0, lat=None, lon=None" in err and "east/north eller lat/lon" in err
    assert "utanför det rimliga intervallet" not in err


async def test_locate_school_unit_requires_the_register_source(router, make_client):
    _routes(router, "scb_geodata_intersects.json")
    router.add("GET", r"/v2/school-units/12345678", load_fixture("skolenhetsregistret_school_unit.json"))
    async with make_client("scb.geodata") as client:
        err = await call_error(client, "scb_geodata_locate", {"school_unit_code": "12345678"})
    assert "aktivera källan skolverket.skolenhetsregistret" in err
    assert not any("/school-units/" in r.url.path for r in router.requests)


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ({}, "exakt ett sätt"),
        ({"east": 674032}, "både east och north"),
        ({"north": 6580822}, "både east och north"),
        ({"lat": 59.33}, "både lat och lon"),
        ({"school_unit_code": "12345678", "east": 674032, "north": 6580822}, "exakt ett sätt"),
        ({"east": 674032, "north": 6580822, "lat": 59.33, "lon": 18.06}, "exakt ett sätt"),
        ({"lat": 13.09577306, "lon": 55.5417832}, "lat=13.09577306 ligger utanför det rimliga intervallet"),
        ({"east": 6580822, "north": 674032}, "east=6580822.0 ligger utanför det rimliga intervallet"),
        ({"east": 674032, "north": 6580822, "layers": []}, "Ange minst ett lager"),
        ({"east": 674032, "north": 6580822, "layers": ["../etc"]}, "Ogiltigt lagernamn"),
        (
            {"east": 674032, "north": 6580822, "layers": ["DeSO_2025", "DeSO_2018", "RegSO_2025", "RegSO_2020", "X"]},
            "Högst 4 lager",
        ),
    ],
)
async def test_locate_rejects_bad_input(router, make_client, args, message):
    _routes(router, "scb_geodata_intersects.json")
    async with make_client("scb.geodata") as client:
        err = await call_error(client, "scb_geodata_locate", args)
    assert message in err
    assert not _get_feature_requests(router)


async def test_locate_without_hit_explains(router, make_client):
    _routes(router, EMPTY)
    async with make_client("scb.geodata") as client:
        result = await call(client, "scb_geodata_locate", {"east": 300000, "north": 6200000, "layers": ["DeSO_2025"]})
    assert result["traffar"] == []
    assert result["notes"] == [
        "WGS84-koordinaten är omräknad från SWEREF 99 TM (Gauss-Krüger, fuzzy_mcp.geo).",
        "Ingen träff i DeSO_2025: punkten ligger utanför lagrets polygoner (t.ex. i vatten) eller utanför Sverige",
    ]
