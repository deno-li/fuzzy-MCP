# SPDX-FileCopyrightText: 2026 Deniz Özer
#
# SPDX-License-Identifier: MIT

"""SCB:s öppna geodata (GeoServer WFS 1.1.0).

Base: https://geodata.scb.se/geoserver/stat – WFS at ``/wfs`` with layers such
as ``stat:DeSO_2025`` (demografiska statistikområden), ``stat:RegSO_2025``,
``stat:Tatorter_2023`` and ``stat:befolkning_1km_2025``. Output formats per
SCB's guide (2025-03-03): json, csv, geopackage and shape-zip. Licence: CC0 1.0.

Attribute names are read from ``DescribeFeatureType`` at runtime instead of
being assumed, and geometry is left out unless asked for, so that results
stay small (DeSO has over 6 000 polygons).

Verified against the live service on 2026-10-10 (verifierat uttag; tests/fixtures/scb_geodata_describe_*.json and
scb_geodata_features.json are answers from that run, scb_geodata_intersects.json and scb_geodata_features_wgs84.json
are shaped like them):

* DeSO layers carry ``objectid, objektidentitet, objekttyp, desokod, regsokod, lanskod, kommunkod, version,
  ansvarig_organisation, referensdatum`` and the geometry ``sp_geometry`` (gml:Polygon); RegSO layers have
  ``regsokod, regsonamn`` instead of ``desokod``. Default SRS is ``urn:x-ogc:def:crs:EPSG:3006`` (SWEREF 99 TM).
* JSON GetFeature answers ``{type, features, totalFeatures, numberMatched, numberReturned, timeStamp, crs}``;
  with ``propertyName`` that leaves out the geometry both ``crs`` and ``geometry`` are null.
* Paging works with ``maxFeatures`` + ``startIndex`` + ``sortBy`` (sorting is required for stable pages).
* ``srsName=EPSG:4326`` is verified for the JSON output only; coordinates then come as ``[lon, lat]``.
* In CQL filters against the EPSG:3006 layers the axis order is NORTHING EASTING, i.e. ``POINT(N E)``:
  ``INTERSECTS(sp_geometry,POINT(674032 6580822))`` matched nothing while
  ``INTERSECTS(sp_geometry,POINT(6580822 674032))`` returned DeSO 0180C4040.
"""

import json
import math
import time
import unicodedata
import xml.etree.ElementTree as ET
from typing import Annotated, Any, Literal
from urllib.parse import urlencode

from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult
from pydantic import BaseModel, Field

from ..core import READ_ONLY_OPEN, Services, clamp, structured, tool_errors
from ..errors import InvalidInputError, UpstreamError
from ..geo import check_sweref99tm, check_wgs84, sweref99tm_to_wgs84, wgs84_to_sweref99tm
from .skolverket.skolenhetsregistret import Address, RegistryClient, SchoolUnitDetail, normalize_school_unit_code

SOURCE = "scb"
SCHOOL_UNIT_SOURCE = "skolverket.skolenhetsregistret"
CAPABILITIES_TTL_SECONDS = 6 * 3600
DOWNLOAD_FORMATS = {
    "geopackage": "geopackage",
    "shape-zip": "shape-zip",
    "csv": "csv",
    "geojson": "application/json",
}
_WFS_NS = {"wfs": "http://www.opengis.net/wfs"}
MAX_FEATURES = 1000
MAX_GEOMETRY_FEATURES = 10
# Keeps an answer well below the ~25k-token cap of MCP hosts (compact JSON, ~3–4 chars per token).
OUTPUT_CHAR_BUDGET = 60_000
_LAYER_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-:")

Srs = Literal["EPSG:3006", "EPSG:4326"]
DEFAULT_SRS: Srs = "EPSG:3006"
_SRS_HELP = "Koordinatsystem för geometrin: EPSG:3006 (SWEREF 99 TM, [E, N]) eller EPSG:4326 (WGS84, [lon, lat])"
LOCATE_DEFAULT_LAYERS: tuple[str, ...] = ("DeSO_2025", "RegSO_2025")
LOCATE_MAX_LAYERS = 4
LOCATE_MAX_FEATURES = 5
CITATION_SCB = "Källa: SCB, öppna geodata (CC0 1.0)"
CITATION_SCHOOL_UNIT = "Skolverket, Skolenhetsregistret"
VISIT_ADDRESS_TYPE = "BESOKSADRESS"
# When the register gives both a SWEREF 99 TM and a WGS84 pair for an address they should describe the same spot;
# a larger gap means one of them is wrong, and the projected pair (the register's primary one) is used.
REGISTER_COORDINATE_TOLERANCE_M = 50.0
# Verified 2026-10-10: the code lists vs_DeSO2025/vs_RegSO2025 (reference year 2024 onwards) spell the codes with a
# suffix, while the WFS attributes desokod/regsokod are the plain codes.
SSD_SUFFIX_NOTE = (
    "desokod/regsokod är lagrets rena koder; i Statistikdatabasen skrivs DeSO 2025- och RegSO 2025-koder med suffix "
    "_DeSO2025/_RegSO2025 (ref_lookup_deso ger dem under ssd_koder)."
)


class GeoLayer(BaseModel):
    name: str
    title: str | None = None
    abstract: str | None = None


class GeoLayerList(BaseModel):
    layers: list[GeoLayer]
    hint: str = "Använd namnet (t.ex. 'DeSO_2025') i scb_geodata_describe_layer och scb_geodata_get_features."


class GeoAttribute(BaseModel):
    name: str
    type: str | None = None
    geometry: bool = False


class GeoLayerDescription(BaseModel):
    layer: str
    attributes: list[GeoAttribute]


class GeoFeatures(BaseModel):
    layer: str
    total: int | None = Field(default=None, description="Antal objekt som matchar filtret")
    returned: int
    offset: int = Field(default=0, description="Antal överhoppade objekt (startIndex)")
    truncated: bool = False
    srs: str | None = Field(default=None, description="Koordinatsystem för geometrin (bara när geometri ingår)")
    features: list[dict[str, Any]]
    request_url: str
    notes: list[str] = Field(default_factory=list)
    citation: str = CITATION_SCB


class GeoDownload(BaseModel):
    layer: str
    format: str
    srs: str = Field(default=DEFAULT_SRS, description="Koordinatsystem i filen")
    url: str
    note: str = "Ladda ner filen med t.ex. QGIS, ArcGIS eller Power BI (Web-källa)."


class SwerefPoint(BaseModel):
    east: float = Field(description="Östlig koordinat i SWEREF 99 TM (EPSG:3006), meter")
    north: float = Field(description="Nordlig koordinat i SWEREF 99 TM (EPSG:3006), meter")


class Wgs84Point(BaseModel):
    lat: float = Field(description="Latitud i WGS84 (EPSG:4326), grader")
    lon: float = Field(description="Longitud i WGS84 (EPSG:4326), grader")


class GeoPoint(BaseModel):
    sweref99tm: SwerefPoint
    wgs84: Wgs84Point
    harledd: Literal["wgs84", "sweref99tm"] | None = Field(
        default=None,
        description="Vilket koordinatpar som räknats om från det andra (Gauss-Krüger i fuzzy_mcp.geo); "
        "null när båda kommer från indata eller källan",
    )


class GeoSchoolUnit(BaseModel):
    kod: str = Field(description="Skolenhetskod")
    namn: str | None = None
    kommunkod: str | None = None
    adresstyp: str | None = Field(
        default=None,
        description="Adressen i Skolenhetsregistret vars koordinater använts: BESOKSADRESS, eller en annan typ "
        "(t.ex. POSTADRESS) bara när besöksadressen saknar koordinater",
    )


class GeoHit(BaseModel):
    layer: str
    properties: dict[str, Any] = Field(description="Objektets attribut, t.ex. desokod/regsokod, kommunkod, version")
    request_url: str


class GeoLocation(BaseModel):
    punkt: GeoPoint
    skolenhet: GeoSchoolUnit | None = None
    traffar: list[GeoHit]
    notes: list[str] = Field(default_factory=list)
    citation: str = CITATION_SCB


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def layer_name(value: str) -> str:
    name = value.strip()
    if name.startswith("stat:"):
        name = name[len("stat:") :]
    if not name or any(ch not in _LAYER_CHARS for ch in name):
        raise InvalidInputError(f"Ogiltigt lagernamn {value!r} (exempel: 'DeSO_2025')")
    return name


def cql_literal(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def data_attributes(attributes: list[GeoAttribute]) -> list[GeoAttribute]:
    """The layer's non-geometry attributes in DescribeFeatureType order."""
    return [a for a in attributes if not a.geometry]


def geometry_attribute(layer: str, attributes: list[GeoAttribute]) -> GeoAttribute:
    geometry = next((a for a in attributes if a.geometry), None)
    if geometry is None:
        raise InvalidInputError(f"Lagret {layer!r} saknar geometriattribut och kan inte användas för punktsökning")
    return geometry


def resolve_attribute(name: Any, attributes: list[GeoAttribute]) -> GeoAttribute:
    """The non-geometry attribute called ``name`` (case-insensitive), or InvalidInputError naming the valid ones."""
    wanted = str(name).strip().lower()
    by_lower = {a.name.lower(): a for a in data_attributes(attributes)}
    attribute = by_lower.get(wanted)
    if attribute is None:
        valid = ", ".join(sorted(a.name for a in by_lower.values()))
        geometry = next((a for a in attributes if a.geometry and a.name.lower() == wanted), None)
        if geometry is not None:
            raise InvalidInputError(
                f"Attributet {geometry.name!r} är lagrets geometri och kan inte användas i filters, attributes eller "
                f"sort_by; geometrin fås med include_geometry eller scb_geodata_download_url. Lagrets attribut: {valid}"
            )
        raise InvalidInputError(f"Okänt attribut {name!r}. Lagrets attribut: {valid}")
    return attribute


def resolve_attributes(names: list[str] | None, attributes: list[GeoAttribute]) -> list[GeoAttribute] | None:
    """Validated attribute selection (duplicates dropped, layer spelling), or None for "all"."""
    if not names:
        return None
    selected: list[GeoAttribute] = []
    for name in names:
        attribute = resolve_attribute(name, attributes)
        if attribute not in selected:
            selected.append(attribute)
    return selected


def default_sort_attribute(attributes: list[GeoAttribute]) -> GeoAttribute:
    """Sort key for paging when none is given: the first ``…kod`` attribute (desokod, regsokod), else the first."""
    data = data_attributes(attributes)
    if not data:
        raise InvalidInputError("Lagret saknar attribut att sortera på")
    return next((a for a in data if a.name.lower().endswith("kod")), data[0])


def build_cql(filters: dict[str, Any], attributes: list[GeoAttribute]) -> str | None:
    """CQL filter from {attribute: value | [values]}; values ending in '*'
    become a LIKE prefix match. Attribute names must exist in the layer."""
    if not filters:
        return None
    clauses: list[str] = []
    for raw_name, raw_values in filters.items():
        attribute = resolve_attribute(raw_name, attributes)
        values = raw_values if isinstance(raw_values, list) else [raw_values]
        if not values:
            raise InvalidInputError(f"Tomt filter för {attribute.name!r}")
        parts = []
        for value in values:
            text = str(value)
            if text.endswith("*"):
                prefix = text[:-1].replace("%", "\\%").replace("_", "\\_")
                parts.append(f"{attribute.name} LIKE {cql_literal(prefix + '%')}")
            else:
                parts.append(f"{attribute.name} = {cql_literal(text)}")
        clauses.append("(" + " OR ".join(parts) + ")")
    return " AND ".join(clauses)


def coordinate_text(value: float) -> str:
    """Metres with at most three decimals and no trailing zeros ('674032', '679180.062')."""
    return f"{value:.3f}".rstrip("0").rstrip(".")


def point_cql(geometry_attribute_name: str, east: float, north: float) -> str:
    """INTERSECTS filter for a SWEREF 99 TM point, written POINT(N E).

    Observed against geodata.scb.se on 2026-10-10 (WFS 1.1.0, layer CRS urn:x-ogc:def:crs:EPSG:3006):
    POINT(674032 6580822) (E N) matched nothing, POINT(6580822 674032) (N E) returned DeSO 0180C4040 (Stockholm).
    GeoServer reads the CQL point in the axis order of the layer CRS (northing first for EPSG:3006 in WFS 1.1.0);
    the order here follows that observation, and the Källkontroll checks wfs.intersects.* guard it.
    """
    return f"INTERSECTS({geometry_attribute_name},POINT({coordinate_text(north)} {coordinate_text(east)}))"


class GeodataService:
    def __init__(self, services: Services) -> None:
        self.services = services
        self._layers: tuple[float, list[GeoLayer]] | None = None
        self._descriptions: dict[str, list[GeoAttribute]] = {}

    @property
    def wfs_url(self) -> str:
        return self.services.settings.base_url("scb_geodata") + "/wfs"

    async def layers(self) -> list[GeoLayer]:
        if self._layers and time.monotonic() - self._layers[0] < CAPABILITIES_TTL_SECONDS:
            return self._layers[1]
        res = await self.services.http.request(
            SOURCE,
            "GET",
            self.wfs_url,
            params={"service": "WFS", "version": "1.1.0", "request": "GetCapabilities"},
            headers={"Accept": "application/xml, text/xml"},
            expect="text",
        )
        try:
            root = ET.fromstring(res.data)
        except ET.ParseError as exc:
            raise UpstreamError(SOURCE, "Kunde inte tolka WFS GetCapabilities", url=res.url) from exc
        layers: list[GeoLayer] = []
        for feature_type in root.iter("{http://www.opengis.net/wfs}FeatureType"):
            name = feature_type.findtext("wfs:Name", namespaces=_WFS_NS) or ""
            if not name:
                continue
            layers.append(
                GeoLayer(
                    name=name.split(":", 1)[-1],
                    title=feature_type.findtext("wfs:Title", namespaces=_WFS_NS),
                    abstract=(feature_type.findtext("wfs:Abstract", namespaces=_WFS_NS) or "")[:300] or None,
                )
            )
        self._layers = (time.monotonic(), layers)
        return layers

    async def describe(self, layer: str) -> list[GeoAttribute]:
        if layer in self._descriptions:
            return self._descriptions[layer]
        res = await self.services.http.get_json(
            SOURCE,
            self.wfs_url,
            params={
                "service": "WFS",
                "version": "1.1.0",
                "request": "DescribeFeatureType",
                "typeName": f"stat:{layer}",
                "outputFormat": "application/json",
            },
        )
        types = (res.data or {}).get("featureTypes") if isinstance(res.data, dict) else None
        if not types:
            raise UpstreamError(SOURCE, f"Lagret {layer!r} finns inte eller saknar beskrivning", url=res.url)
        attributes = [
            GeoAttribute(
                name=str(p.get("name")),
                type=p.get("localType") or p.get("type"),
                geometry=str(p.get("type", "")).startswith("gml:"),
            )
            for p in types[0].get("properties") or []
            if p.get("name")
        ]
        self._descriptions[layer] = attributes
        return attributes

    async def get_features(self, layer: str, **extra: Any) -> tuple[dict[str, Any], str]:
        """GetFeature as JSON: ``(body, request url)``. ``extra`` adds WFS parameters (propertyName, CQL_FILTER…)."""
        params: dict[str, Any] = {
            "service": "WFS",
            "version": "1.1.0",
            "request": "GetFeature",
            "typeName": f"stat:{layer}",
            "outputFormat": "application/json",
            **extra,
        }
        res = await self.services.http.get_json(SOURCE, self.wfs_url, params=params)
        return (res.data if isinstance(res.data, dict) else {}), res.url


def _bbox(geometry: Any) -> list[float] | None:
    """[minx, miny, maxx, maxy] of a GeoJSON geometry in the coordinates of the answer (``srs``)."""
    xs: list[float] = []
    ys: list[float] = []

    def walk(node: Any) -> None:
        if isinstance(node, list) and len(node) >= 2 and all(isinstance(v, int | float) for v in node[:2]):
            xs.append(float(node[0]))
            ys.append(float(node[1]))
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(geometry.get("coordinates") if isinstance(geometry, dict) else None)
    return [min(xs), min(ys), max(xs), max(ys)] if xs else None


def _feature_properties(data: dict[str, Any]) -> list[dict[str, Any]]:
    return [dict(f.get("properties") or {}) for f in data.get("features") or [] if isinstance(f, dict)]


def _has_coordinates(address: Address) -> bool:
    return (address.sweref99tm_e is not None and address.sweref99tm_n is not None) or (
        address.latitude is not None and address.longitude is not None
    )


def _plausible_point(address: Address) -> tuple[GeoPoint | None, str | None]:
    """The address's position from the coordinates that pass the plausibility check for Sweden, and a note when
    the register's two pairs disagree: SWEREF 99 TM first, else WGS84 (converted); ``(None, None)`` when no pair
    is plausible (the register sometimes carries '0')."""
    sweref = wgs84 = None
    if address.sweref99tm_e is not None and address.sweref99tm_n is not None:
        try:
            sweref = check_sweref99tm(address.sweref99tm_e, address.sweref99tm_n)
        except InvalidInputError:
            sweref = None
    if address.latitude is not None and address.longitude is not None:
        try:
            wgs84 = check_wgs84(address.latitude, address.longitude)
        except InvalidInputError:
            wgs84 = None
    if sweref is not None and wgs84 is not None:
        east, north = wgs84_to_sweref99tm(*wgs84)
        gap = math.hypot(east - sweref[0], north - sweref[1])
        if gap <= REGISTER_COORDINATE_TOLERANCE_M:
            point = GeoPoint(
                sweref99tm=SwerefPoint(east=sweref[0], north=sweref[1]),
                wgs84=Wgs84Point(lat=wgs84[0], lon=wgs84[1]),
            )
            return point, None
        return _point_from_sweref(*sweref), (
            f"Skolenhetsregistrets SWEREF 99 TM- och WGS84-koordinater för adressen ligger {gap:.0f} m isär; "
            "SWEREF 99 TM används och WGS84 räknas om därifrån."
        )
    if sweref is not None:
        return _point_from_sweref(*sweref), None
    if wgs84 is not None:
        return _point_from_wgs84(*wgs84), None
    return None, None


def point_from_school_unit(detail: SchoolUnitDetail) -> tuple[GeoPoint, Address, list[str]]:
    """The school unit's position, the address it was read from and notes about the register's coordinates: the
    visit address when it has any coordinates, otherwise the first other address with coordinates. SWEREF 99 TM
    from the register is used when plausible, else the register's WGS84 pair (converted). No personal data is
    read."""
    addresses = [a for a in (detail.visit_address, *detail.other_addresses) if a is not None and _has_coordinates(a)]
    unit = f"{detail.school_unit_code} ({detail.name or 'utan namn'})"
    if not addresses:
        raise InvalidInputError(
            f"Skolenheten {unit} saknar koordinater i Skolenhetsregistret; ange punkten med east/north eller lat/lon "
            "i stället"
        )
    address = addresses[0]
    point, note = _plausible_point(address)
    if point is None:
        raise InvalidInputError(
            f"Skolenhetsregistret anger orimliga koordinater för skolenheten {unit}: E={address.sweref99tm_e}, "
            f"N={address.sweref99tm_n}, lat={address.latitude}, lon={address.longitude}; ange punkten med "
            "east/north eller lat/lon i stället"
        )
    return point, address, [note] if note else []


def _point_from_sweref(east: float, north: float) -> GeoPoint:
    lat, lon = sweref99tm_to_wgs84(east, north)
    return GeoPoint(
        sweref99tm=SwerefPoint(east=east, north=north),
        wgs84=Wgs84Point(lat=round(lat, 7), lon=round(lon, 7)),
        harledd="wgs84",
    )


def _point_from_wgs84(lat: float, lon: float) -> GeoPoint:
    east, north = wgs84_to_sweref99tm(lat, lon)
    return GeoPoint(
        sweref99tm=SwerefPoint(east=round(east, 3), north=round(north, 3)),
        wgs84=Wgs84Point(lat=lat, lon=lon),
        harledd="sweref99tm",
    )


def _locate_layers(layers: list[str] | tuple[str, ...]) -> list[str]:
    names: list[str] = []
    for value in layers:
        name = layer_name(value)
        if name not in names:
            names.append(name)
    if not names:
        raise InvalidInputError(f"Ange minst ett lager, t.ex. {list(LOCATE_DEFAULT_LAYERS)}")
    if len(names) > LOCATE_MAX_LAYERS:
        raise InvalidInputError(f"Högst {LOCATE_MAX_LAYERS} lager per anrop (fick {len(names)})")
    return names


def register(server: MCPServer[Any], services: Services) -> None:
    geo = GeodataService(services)
    school_units = RegistryClient(services)

    @server.tool(name="scb_geodata_layers", title="SCB: geodatalager", annotations=READ_ONLY_OPEN)
    @tool_errors
    async def scb_geodata_layers(
        search: Annotated[
            str | None, Field(description="Filtrera på namn/titel, t.ex. 'deso', 'regso', 'tätort'")
        ] = None,
    ) -> Annotated[CallToolResult, GeoLayerList]:
        """Lista SCB:s öppna geodatalager (WFS): DeSO och RegSO (statistikområden), tätorter, småorter,
        befolkning per km-ruta, arbetsplats-, handels- och grönområden m.m., med årsversioner."""
        layers = await geo.layers()
        if search:
            needle = _fold(search)
            layers = [layer for layer in layers if needle in _fold(f"{layer.name} {layer.title or ''}")]
        return structured(GeoLayerList(layers=layers))

    @server.tool(name="scb_geodata_describe_layer", title="SCB: lagrets attribut", annotations=READ_ONLY_OPEN)
    @tool_errors
    async def scb_geodata_describe_layer(
        layer: Annotated[str, Field(description="Lagernamn, t.ex. 'DeSO_2025' eller 'RegSO_2025'")],
    ) -> Annotated[CallToolResult, GeoLayerDescription]:
        """Visa ett geodatalagers attribut (fältnamn och typer) som desokod, regsokod, kommunkod, lanskod och version.
        DeSO 2025 har objectid, objektidentitet, objekttyp, desokod, regsokod, lanskod, kommunkod, version,
        ansvarig_organisation, referensdatum samt geometrin sp_geometry; RegSO har regsonamn i stället för desokod.
        Använd attributnamnen som filter, attributes och sort_by i scb_geodata_get_features."""
        name = layer_name(layer)
        return structured(GeoLayerDescription(layer=name, attributes=await geo.describe(name)))

    @server.tool(name="scb_geodata_get_features", title="SCB: hämta geoobjekt", annotations=READ_ONLY_OPEN)
    @tool_errors
    async def scb_geodata_get_features(
        layer: Annotated[str, Field(description="Lagernamn, t.ex. 'DeSO_2025'")],
        filters: Annotated[
            dict[str, str | list[str]] | None,
            Field(
                description="Filter {attribut: värde eller [värden]}; ett värde som slutar på * matchar prefix. "
                "Attributnamnen kommer från scb_geodata_describe_layer (t.ex. kommunkod = '2180')."
            ),
        ] = None,
        attributes: Annotated[
            list[str] | None,
            Field(
                description="Urval av attribut att returnera (utan geometri), t.ex. ['desokod', 'regsokod']; "
                "standard är lagrets alla attribut utom geometrin"
            ),
        ] = None,
        include_geometry: Annotated[
            bool,
            Field(
                description=f"Ta med geometri (GeoJSON). Högst {MAX_GEOMETRY_FEATURES} objekt och en storleksgräns; "
                "använd scb_geodata_download_url för fler polygoner"
            ),
        ] = False,
        limit: Annotated[
            int, Field(description=f"Max antal objekt (1–{MAX_FEATURES}; med geometri 1–{MAX_GEOMETRY_FEATURES})")
        ] = 100,
        offset: Annotated[
            int,
            Field(
                description="Antal objekt att hoppa över (startIndex) för att bläddra; sidorna sorteras på sort_by "
                "eller, om det saknas, lagrets kodattribut (desokod/regsokod)"
            ),
        ] = 0,
        sort_by: Annotated[str | None, Field(description="Attribut att sortera på (stigande), t.ex. 'desokod'")] = None,
        srs: Annotated[Srs, Field(description=f"{_SRS_HELP}; används bara med include_geometry")] = DEFAULT_SRS,
    ) -> Annotated[CallToolResult, GeoFeatures]:
        """Hämta objekt ur ett geodatalager, som alla DeSO-områden i en kommun med koder (DeSO har bara koder; RegSO
        har namn). Utan geometri som standard; koderna (t.ex. DeSO '2180C1010') kopplar till SCB-statistik på
        DeSO/RegSO-nivå. Bläddra med limit/offset (sorterat); geometri kan fås i SWEREF 99 TM (standard, [E, N])
        eller WGS84 (srs='EPSG:4326', [lon, lat])."""
        name = layer_name(layer)
        if offset < 0:
            raise InvalidInputError(f"offset måste vara 0 eller större (fick {offset})")
        described = await geo.describe(name)
        cql = build_cql(filters or {}, described)
        selected = resolve_attributes(attributes, described)
        cap = clamp(limit, 1, MAX_GEOMETRY_FEATURES if include_geometry else MAX_FEATURES)
        params: dict[str, Any] = {"maxFeatures": cap + 1}
        notes: list[str] = []
        sort_attribute = resolve_attribute(sort_by, described) if sort_by else None
        if offset > 0:
            params["startIndex"] = offset
            if sort_attribute is None:
                sort_attribute = default_sort_attribute(described)
                notes.append(f"Sortering vid offset: {sort_attribute.name} (standard; ange sort_by för annan ordning)")
            else:
                notes.append(f"Sortering vid offset: {sort_attribute.name} (sort_by)")
                notes.append(
                    "Bläddring är stabil bara om sort_by är unikt per objekt (som desokod/regsokod); annars kan "
                    "objekt saknas eller upprepas mellan sidorna."
                )
        if sort_attribute is not None:
            params["sortBy"] = sort_attribute.name
        if include_geometry:
            if selected is not None:
                params["propertyName"] = ",".join(
                    [*(a.name for a in selected), geometry_attribute(name, described).name]
                )
            params["srsName"] = srs
            if srs == "EPSG:4326":
                notes.append("Geometrin är i EPSG:4326 (WGS84): koordinater som [lon, lat].")
        else:
            params["propertyName"] = ",".join(a.name for a in (selected or data_attributes(described)))
            if srs != DEFAULT_SRS:
                notes.append(f"srs={srs!r} gäller bara geometrin; ange include_geometry=true för att få den.")
        if cql:
            params["CQL_FILTER"] = cql
        data, url = await geo.get_features(name, **params)
        raw_features = data.get("features") or []
        features: list[dict[str, Any]] = []
        used = 0
        budget_hit = False
        geometry_dropped = 0
        for feature in raw_features[:cap]:
            if not isinstance(feature, dict):
                continue
            item = dict(feature.get("properties") or {})
            size = len(json.dumps(item, ensure_ascii=False, separators=(",", ":")))
            if features and used + size > OUTPUT_CHAR_BUDGET:
                budget_hit = True
                break
            used += size
            geometry = feature.get("geometry") if include_geometry else None
            if geometry is not None:
                geometry_size = len(json.dumps(geometry, separators=(",", ":")))
                if used + geometry_size > OUTPUT_CHAR_BUDGET:
                    item["bbox"] = _bbox(geometry)
                    geometry_dropped += 1
                else:
                    item["geometry"] = geometry
                    used += geometry_size
            features.append(item)
        if geometry_dropped:
            notes.append(
                f"Geometrin utelämnades för {geometry_dropped} objekt (för stor för svaret); bbox anges i stället. "
                "Hämta hela geometrin via scb_geodata_download_url."
            )
        if budget_hit:
            notes.append(f"Svaret kortades till {len(features)} objekt för att hålla storleken nere; filtrera mer.")
        total = data.get("totalFeatures") or data.get("numberMatched")
        return structured(
            GeoFeatures(
                layer=name,
                total=total if isinstance(total, int) else None,
                returned=len(features),
                offset=offset,
                truncated=len(raw_features) > cap or budget_hit,
                srs=srs if include_geometry else None,
                features=features,
                request_url=url,
                notes=notes,
            )
        )

    @server.tool(name="scb_geodata_download_url", title="SCB: nedladdningslänk för geodata", annotations=READ_ONLY_OPEN)
    @tool_errors
    async def scb_geodata_download_url(
        layer: Annotated[str, Field(description="Lagernamn, t.ex. 'DeSO_2025'")],
        format: Annotated[
            Literal["geopackage", "shape-zip", "csv", "geojson"], Field(description="Filformat")
        ] = "geopackage",
        filters: Annotated[
            dict[str, str | list[str]] | None,
            Field(description="Valfritt filter som i scb_geodata_get_features, t.ex. {'kommunkod': '2180'}"),
        ] = None,
        srs: Annotated[
            Srs, Field(description=f"{_SRS_HELP}. EPSG:4326 är verifierat bara för format='geojson'")
        ] = DEFAULT_SRS,
    ) -> Annotated[CallToolResult, GeoDownload]:
        """Bygg en nedladdningslänk (WFS GetFeature) för ett helt lager eller ett filtrerat urval i GeoPackage,
        Shape (zip), CSV eller GeoJSON – för QGIS, ArcGIS eller Power BI. Hämtar ingen data."""
        name = layer_name(layer)
        if srs != DEFAULT_SRS and format != "geojson":
            raise InvalidInputError(
                f"srs={srs!r} är bara verifierat för format='geojson' (WFS-svar i JSON, 2026-10-10); "
                f"för format={format!r} levereras filen i SWEREF 99 TM (EPSG:3006) – räkna om i GIS-verktyget"
            )
        params: dict[str, Any] = {
            "service": "WFS",
            "version": "1.1.0",
            "request": "GetFeature",
            "typeName": f"stat:{name}",
            "outputFormat": DOWNLOAD_FORMATS[format],
        }
        if format == "shape-zip":
            params["format_options"] = "CHARSET:UTF-8"
        if srs != DEFAULT_SRS:
            params["srsName"] = srs
        if filters:
            params["CQL_FILTER"] = build_cql(filters, await geo.describe(name))
        url = f"{geo.wfs_url}?{urlencode(params, safe=':,')}"
        return structured(GeoDownload(layer=name, format=format, srs=srs, url=url))

    @server.tool(
        name="scb_geodata_locate",
        title="SCB: vilket DeSO/RegSO ligger en punkt eller skolenhet i",
        annotations=READ_ONLY_OPEN,
    )
    @tool_errors
    async def scb_geodata_locate(
        school_unit_code: Annotated[
            str | None,
            Field(
                description="Skolenhetskod (8 siffror): punkten blir skolenhetens besöksadress enligt "
                "Skolenhetsregistret; en annan adress används bara när besöksadressen saknar koordinater, och "
                "svaret anger då adresstypen (kräver källan skolverket.skolenhetsregistret)"
            ),
        ] = None,
        east: Annotated[
            float | None, Field(description="Östlig koordinat i SWEREF 99 TM (EPSG:3006), meter; tillsammans med north")
        ] = None,
        north: Annotated[
            float | None, Field(description="Nordlig koordinat i SWEREF 99 TM (EPSG:3006), meter; tillsammans med east")
        ] = None,
        lat: Annotated[
            float | None, Field(description="Latitud i WGS84 (EPSG:4326), grader; tillsammans med lon")
        ] = None,
        lon: Annotated[
            float | None, Field(description="Longitud i WGS84 (EPSG:4326), grader; tillsammans med lat")
        ] = None,
        layers: Annotated[
            list[str],
            Field(
                description=f"Lager att söka i (högst {LOCATE_MAX_LAYERS}), t.ex. DeSO_2025, RegSO_2025 (gäller "
                "statistik fr.o.m. 2024) eller DeSO_2018, RegSO_2020 (t.o.m. 2023)"
            ),
        ] = LOCATE_DEFAULT_LAYERS,
    ) -> Annotated[CallToolResult, GeoLocation]:
        """Slå upp vilket DeSO- och RegSO-område (eller annat polygonlager) en punkt ligger i. Punkten anges på
        exakt ett sätt: skolenhetskod (koordinater ur Skolenhetsregistret), east/north i SWEREF 99 TM eller lat/lon
        i WGS84 (räknas om till SWEREF 99 TM). Koderna i träffarna (desokod, regsokod) är lagrets rena koder; slå upp
        dem med ref_lookup_deso, som under ssd_koder ger koden så som Statistikdatabasen skriver den (DeSO 2025/RegSO
        2025 har suffix _DeSO2025/_RegSO2025 fr.o.m. referensår 2024)."""
        ways = [
            school_unit_code is not None,
            east is not None or north is not None,
            lat is not None or lon is not None,
        ]
        if sum(ways) != 1:
            raise InvalidInputError(
                "Ange punkten på exakt ett sätt: school_unit_code, east+north (SWEREF 99 TM) eller lat+lon (WGS84)"
            )
        names = _locate_layers(layers)
        school_unit: GeoSchoolUnit | None = None
        notes: list[str] = []
        if school_unit_code is not None:
            if not services.settings.source_enabled(SCHOOL_UNIT_SOURCE):
                raise InvalidInputError(
                    f"Uppslag via skolenhetskod kräver Skolenhetsregistret: aktivera källan {SCHOOL_UNIT_SOURCE} "
                    "(FUZZY_MCP_SOURCES) eller ange east/north eller lat/lon"
                )
            code = normalize_school_unit_code(school_unit_code)
            detail, _ = await school_units.school_unit(code, None, personal=False, emails=False)
            point, address, point_notes = point_from_school_unit(detail)
            notes.extend(point_notes)
            school_unit = GeoSchoolUnit(
                kod=detail.school_unit_code,
                namn=detail.name,
                kommunkod=detail.municipality_code,
                adresstyp=address.type,
            )
            if (address.type or "").upper() != VISIT_ADDRESS_TYPE:
                notes.append(
                    f"Koordinaterna kommer från adressen av typ {address.type or 'okänd'}, inte besöksadressen: "
                    "besöksadressen saknar koordinater i Skolenhetsregistret."
                )
        elif east is not None or north is not None:
            if east is None or north is None:
                raise InvalidInputError("Ange både east och north (SWEREF 99 TM, meter)")
            point = _point_from_sweref(*check_sweref99tm(east, north))
        else:
            if lat is None or lon is None:
                raise InvalidInputError("Ange både lat och lon (WGS84, grader)")
            point = _point_from_wgs84(*check_wgs84(lat, lon))
        if point.harledd == "sweref99tm":
            notes.append("SWEREF 99 TM-koordinaten är omräknad från WGS84 (Gauss-Krüger, fuzzy_mcp.geo).")
        elif point.harledd == "wgs84":
            notes.append("WGS84-koordinaten är omräknad från SWEREF 99 TM (Gauss-Krüger, fuzzy_mcp.geo).")
        hits: list[GeoHit] = []
        for name in names:
            described = await geo.describe(name)
            geometry_name = geometry_attribute(name, described).name
            data, url = await geo.get_features(
                name,
                maxFeatures=LOCATE_MAX_FEATURES,
                propertyName=",".join(a.name for a in data_attributes(described)),
                CQL_FILTER=point_cql(geometry_name, point.sweref99tm.east, point.sweref99tm.north),
            )
            found = _feature_properties(data)
            hits.extend(GeoHit(layer=name, properties=properties, request_url=url) for properties in found)
            if not found:
                notes.append(
                    f"Ingen träff i {name}: punkten ligger utanför lagrets polygoner (t.ex. i vatten) eller "
                    "utanför Sverige"
                )
            elif len(found) > 1:
                notes.append(f"{len(found)} träffar i {name}: punkten ligger på en gräns mellan områden")
        if any(str(hit.properties.get("version", "")).startswith("2025") for hit in hits):
            notes.append(SSD_SUFFIX_NOTE)
        citation = CITATION_SCB if school_unit is None else f"{CITATION_SCB}; {CITATION_SCHOOL_UNIT}"
        return structured(GeoLocation(punkt=point, skolenhet=school_unit, traffar=hits, notes=notes, citation=citation))
