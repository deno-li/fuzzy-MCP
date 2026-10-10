# SPDX-FileCopyrightText: 2026 Deniz Özer
#
# SPDX-License-Identifier: MIT

"""SWEREF 99 TM (EPSG:3006) <-> WGS84 (EPSG:4326) without external dependencies.

Gauss-Krüger (transverse Mercator) on the GRS80 ellipsoid with the parameters of SWEREF 99 TM: central
meridian 15° E, scale factor 0.9996, false easting 500 000 m, false northing 0 m. The series are carried to
n^4, which is more than enough at the sub-millimetre level inside Sweden.

The formulas are the project's own implementation ("härlett") of Lantmäteriet's published Gauss-Krüger
formula set (phi*, xi'/eta' and the beta/delta series); they are not copied from any library. They were
checked on 2026-10-10 against a coordinate pair that GeoServer (geodata.scb.se) produced for the same corner
point of DeSO 1280C1070 with srsName=EPSG:3006 and srsName=EPSG:4326: E 379844.1709, N 6156729.8055 <->
lon 13.09577306, lat 55.5417832 (difference below 5 mm and 1e-7 degrees). SWEREF 99 and WGS84 are treated as
the same datum (the difference is below a metre in Sweden and smaller than the precision of the sources).

Only points inside a generous box around Sweden are accepted; anything else is almost certainly swapped
axes or another coordinate system and is refused with a clear message instead of a silent wrong answer.
"""

import math

from .errors import InvalidInputError

# GRS80 ellipsoid (used by SWEREF 99).
GRS80_A = 6378137.0
GRS80_F = 1 / 298.257222101
# SWEREF 99 TM projection parameters.
CENTRAL_MERIDIAN_DEG = 15.0
SCALE = 0.9996
FALSE_EASTING = 500_000.0
FALSE_NORTHING = 0.0

# Plausibility limits (a box around Sweden with a margin).
LAT_RANGE = (55.0, 70.0)
LON_RANGE = (10.0, 25.0)
EAST_RANGE = (200_000.0, 1_000_000.0)
NORTH_RANGE = (6_100_000.0, 7_700_000.0)

_E2 = GRS80_F * (2 - GRS80_F)  # first eccentricity squared
_N = GRS80_F / (2 - GRS80_F)  # third flattening
_A_HAT = GRS80_A / (1 + _N) * (1 + _N**2 / 4 + _N**4 / 64)
_LAMBDA0 = math.radians(CENTRAL_MERIDIAN_DEG)

# Geodetic -> grid: conformal latitude coefficients and the beta series.
_A = _E2
_B = (5 * _E2**2 - _E2**3) / 6
_C = (104 * _E2**3 - 45 * _E2**4) / 120
_D = 1237 * _E2**4 / 1260
_BETA = (
    _N / 2 - 2 * _N**2 / 3 + 5 * _N**3 / 16 + 41 * _N**4 / 180,
    13 * _N**2 / 48 - 3 * _N**3 / 5 + 557 * _N**4 / 1440,
    61 * _N**3 / 240 - 103 * _N**4 / 140,
    49561 * _N**4 / 161280,
)
# Grid -> geodetic: the delta series and the inverse conformal latitude coefficients.
_DELTA = (
    _N / 2 - 2 * _N**2 / 3 + 37 * _N**3 / 96 - _N**4 / 360,
    _N**2 / 48 + _N**3 / 15 - 437 * _N**4 / 1440,
    17 * _N**3 / 480 - 37 * _N**4 / 840,
    4397 * _N**4 / 161280,
)
_A_STAR = _E2 + _E2**2 + _E2**3 + _E2**4
_B_STAR = -(7 * _E2**2 + 17 * _E2**3 + 30 * _E2**4) / 6
_C_STAR = (224 * _E2**3 + 889 * _E2**4) / 120
_D_STAR = -(4279 * _E2**4) / 1260


def _bound(value: float) -> str:
    """A limit as plain digits ('200000', '55'), never exponent notation."""
    return f"{value:.0f}" if value == int(value) else f"{value:g}"


def _check(value: float, name: str, low: float, high: float, unit: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise InvalidInputError(f"{name} måste vara ett tal, fick {value!r}") from exc
    if not math.isfinite(number) or not low <= number <= high:
        raise InvalidInputError(
            f"{name}={value!r} ligger utanför det rimliga intervallet {_bound(low)}–{_bound(high)} {unit} för Sverige "
            "(kontrollera koordinatsystem och axelordning)"
        )
    return number


def check_wgs84(lat: float, lon: float) -> tuple[float, float]:
    """``(lat, lon)`` as floats after the plausibility check, for callers that use the values unconverted."""
    return _check(lat, "lat", *LAT_RANGE, "grader"), _check(lon, "lon", *LON_RANGE, "grader")


def check_sweref99tm(east: float, north: float) -> tuple[float, float]:
    """``(east, north)`` as floats after the plausibility check, for callers that use the values unconverted."""
    return _check(east, "east", *EAST_RANGE, "m"), _check(north, "north", *NORTH_RANGE, "m")


def wgs84_to_sweref99tm(lat: float, lon: float) -> tuple[float, float]:
    """WGS84 latitude/longitude (degrees) -> SWEREF 99 TM ``(east, north)`` in metres."""
    lat, lon = check_wgs84(lat, lon)
    phi = math.radians(lat)
    lam = math.radians(lon)
    sin_phi = math.sin(phi)
    phi_star = phi - sin_phi * math.cos(phi) * (_A + _B * sin_phi**2 + _C * sin_phi**4 + _D * sin_phi**6)
    delta_lambda = lam - _LAMBDA0
    xi = math.atan(math.tan(phi_star) / math.cos(delta_lambda))
    eta = math.atanh(math.cos(phi_star) * math.sin(delta_lambda))
    north = xi
    east = eta
    for k, beta in enumerate(_BETA, start=1):
        north += beta * math.sin(2 * k * xi) * math.cosh(2 * k * eta)
        east += beta * math.cos(2 * k * xi) * math.sinh(2 * k * eta)
    return SCALE * _A_HAT * east + FALSE_EASTING, SCALE * _A_HAT * north + FALSE_NORTHING


def sweref99tm_to_wgs84(east: float, north: float) -> tuple[float, float]:
    """SWEREF 99 TM ``east``/``north`` (metres) -> WGS84 ``(lat, lon)`` in degrees."""
    east, north = check_sweref99tm(east, north)
    xi = (north - FALSE_NORTHING) / (SCALE * _A_HAT)
    eta = (east - FALSE_EASTING) / (SCALE * _A_HAT)
    xi_prime = xi
    eta_prime = eta
    for k, delta in enumerate(_DELTA, start=1):
        xi_prime -= delta * math.sin(2 * k * xi) * math.cosh(2 * k * eta)
        eta_prime -= delta * math.cos(2 * k * xi) * math.sinh(2 * k * eta)
    phi_star = math.asin(math.sin(xi_prime) / math.cosh(eta_prime))
    delta_lambda = math.atan(math.sinh(eta_prime) / math.cos(xi_prime))
    sin_star = math.sin(phi_star)
    phi = phi_star + sin_star * math.cos(phi_star) * (
        _A_STAR + _B_STAR * sin_star**2 + _C_STAR * sin_star**4 + _D_STAR * sin_star**6
    )
    return math.degrees(phi), math.degrees(_LAMBDA0 + delta_lambda)
