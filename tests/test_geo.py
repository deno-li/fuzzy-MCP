# SPDX-FileCopyrightText: 2026 Deniz Özer
#
# SPDX-License-Identifier: MIT

import pytest

from fuzzy_mcp.errors import InvalidInputError
from fuzzy_mcp.geo import sweref99tm_to_wgs84, wgs84_to_sweref99tm

# The same corner point of DeSO 1280C1070 as GeoServer (geodata.scb.se) returned it with srsName=EPSG:3006 and
# srsName=EPSG:4326 on 2026-10-10 (verifierat uttag, see tests/fixtures/scb_geodata_features_wgs84.json).
VERIFIED_EAST, VERIFIED_NORTH = 379844.1709, 6156729.8055
VERIFIED_LON, VERIFIED_LAT = 13.09577306, 55.5417832


def test_wgs84_to_sweref99tm_matches_geoserver_pair():
    east, north = wgs84_to_sweref99tm(VERIFIED_LAT, VERIFIED_LON)
    assert east == pytest.approx(VERIFIED_EAST, abs=0.05)
    assert north == pytest.approx(VERIFIED_NORTH, abs=0.05)


def test_sweref99tm_to_wgs84_matches_geoserver_pair():
    lat, lon = sweref99tm_to_wgs84(VERIFIED_EAST, VERIFIED_NORTH)
    assert lat == pytest.approx(VERIFIED_LAT, abs=1e-6)
    assert lon == pytest.approx(VERIFIED_LON, abs=1e-6)


@pytest.mark.parametrize(
    ("lat", "lon"),
    [
        (55.6, 13.0),  # Skåne
        (57.7, 11.97),  # Göteborg
        (59.3293, 18.0686),  # Stockholm
        (63.8, 20.3),  # Umeå
        (67.85, 20.22),  # Kiruna
        (65.6, 22.15),  # Luleå, far east of the central meridian
    ],
)
def test_round_trip(lat, lon):
    east, north = wgs84_to_sweref99tm(lat, lon)
    assert 200_000 <= east <= 1_000_000 and 6_100_000 <= north <= 7_700_000
    lat2, lon2 = sweref99tm_to_wgs84(east, north)
    assert lat2 == pytest.approx(lat, abs=1e-9)
    assert lon2 == pytest.approx(lon, abs=1e-9)


def test_verification_point_in_stockholm_round_trips():
    # The point used for the INTERSECTS check (DeSO 0180C4040) on 2026-10-10.
    lat, lon = sweref99tm_to_wgs84(674032, 6580822)
    assert 59.0 < lat < 59.6 and 17.8 < lon < 18.3
    east, north = wgs84_to_sweref99tm(lat, lon)
    assert east == pytest.approx(674032, abs=0.001) and north == pytest.approx(6580822, abs=0.001)


@pytest.mark.parametrize(
    ("func", "args", "field"),
    [
        (wgs84_to_sweref99tm, (13.09577306, 55.5417832), "lat"),  # swapped lat/lon
        (wgs84_to_sweref99tm, (54.9, 15.0), "lat"),
        (wgs84_to_sweref99tm, (60.0, 25.1), "lon"),
        (wgs84_to_sweref99tm, (float("nan"), 15.0), "lat"),
        (sweref99tm_to_wgs84, (6580822, 674032), "east"),  # swapped east/north
        (sweref99tm_to_wgs84, (199_999, 6_500_000), "east"),
        (sweref99tm_to_wgs84, (500_000, 7_700_001), "north"),
        (sweref99tm_to_wgs84, (float("inf"), 6_500_000), "east"),
    ],
)
def test_points_outside_sweden_are_rejected(func, args, field):
    with pytest.raises(InvalidInputError, match=rf"{field}=.* utanför det rimliga intervallet"):
        func(*args)


def test_range_message_uses_plain_digits():
    with pytest.raises(
        InvalidInputError, match=r"east=199999 ligger utanför det rimliga intervallet 200000–1000000 m "
    ):
        sweref99tm_to_wgs84(199_999, 6_500_000)
    with pytest.raises(InvalidInputError, match=r"lon=25.1 ligger utanför det rimliga intervallet 10–25 grader "):
        wgs84_to_sweref99tm(60.0, 25.1)


def test_non_numeric_input_is_rejected():
    with pytest.raises(InvalidInputError, match="lat måste vara ett tal"):
        wgs84_to_sweref99tm("59,33", 18.0)  # type: ignore[arg-type]
