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
"""

import json
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

SOURCE = "scb"
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
    truncated: bool = False
    features: list[dict[str, Any]]
    request_url: str
    notes: list[str] = Field(default_factory=list)
    citation: str = "Källa: SCB, öppna geodata (CC0 1.0)"


class GeoDownload(BaseModel):
    layer: str
    format: str
    url: str
    note: str = "Ladda ner filen med t.ex. QGIS, ArcGIS eller Power BI (Web-källa)."


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


def build_cql(filters: dict[str, Any], attributes: list[GeoAttribute]) -> str | None:
    """CQL filter from {attribute: value | [values]}; values ending in '*'
    become a LIKE prefix match. Attribute names must exist in the layer."""
    if not filters:
        return None
    by_lower = {a.name.lower(): a for a in attributes if not a.geometry}
    clauses: list[str] = []
    for raw_name, raw_values in filters.items():
        attribute = by_lower.get(str(raw_name).lower())
        if attribute is None:
            valid = ", ".join(sorted(by_lower[k].name for k in by_lower))
            raise InvalidInputError(f"Okänt attribut {raw_name!r}. Lagrets attribut: {valid}")
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


def _bbox(geometry: Any) -> list[float] | None:
    """[minx, miny, maxx, maxy] of a GeoJSON geometry (layer coordinates, SWEREF 99 TM for SCB)."""
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


def register(server: MCPServer[Any], services: Services) -> None:
    geo = GeodataService(services)

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
        """Visa ett geodatalagers attribut (fältnamn och typer), t.ex. DeSO-kod, kommunkod och län. Använd
        attributnamnen som filter i scb_geodata_get_features."""
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
    ) -> Annotated[CallToolResult, GeoFeatures]:
        """Hämta objekt ur ett geodatalager, t.ex. alla DeSO-områden i en kommun med koder och namn. Utan
        geometri som standard; koderna (t.ex. DeSO '2180C1010') kopplar till SCB-statistik på DeSO/RegSO-nivå."""
        name = layer_name(layer)
        attributes = await geo.describe(name)
        cql = build_cql(filters or {}, attributes)
        cap = clamp(limit, 1, MAX_GEOMETRY_FEATURES if include_geometry else MAX_FEATURES)
        params: dict[str, Any] = {
            "service": "WFS",
            "version": "1.1.0",
            "request": "GetFeature",
            "typeName": f"stat:{name}",
            "outputFormat": "application/json",
            "maxFeatures": cap + 1,
        }
        if not include_geometry:
            params["propertyName"] = ",".join(a.name for a in attributes if not a.geometry)
        if cql:
            params["CQL_FILTER"] = cql
        res = await services.http.get_json(SOURCE, geo.wfs_url, params=params)
        data = res.data if isinstance(res.data, dict) else {}
        raw_features = data.get("features") or []
        features: list[dict[str, Any]] = []
        notes: list[str] = []
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
                truncated=len(raw_features) > cap or budget_hit,
                features=features,
                request_url=res.url,
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
    ) -> Annotated[CallToolResult, GeoDownload]:
        """Bygg en nedladdningslänk (WFS GetFeature) för ett helt lager eller ett filtrerat urval i GeoPackage,
        Shape (zip), CSV eller GeoJSON – för QGIS, ArcGIS eller Power BI. Hämtar ingen data."""
        name = layer_name(layer)
        params: dict[str, Any] = {
            "service": "WFS",
            "version": "1.1.0",
            "request": "GetFeature",
            "typeName": f"stat:{name}",
            "outputFormat": DOWNLOAD_FORMATS[format],
        }
        if format == "shape-zip":
            params["format_options"] = "CHARSET:UTF-8"
        if filters:
            params["CQL_FILTER"] = build_cql(filters, await geo.describe(name))
        url = f"{geo.wfs_url}?{urlencode(params, safe=':,')}"
        return structured(GeoDownload(layer=name, format=format, url=url))
