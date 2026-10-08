# SPDX-FileCopyrightText: 2026 Deniz Özer
#
# SPDX-License-Identifier: MIT

"""MCP tools for a PxWeb API v1 installation, shared by Folkhälsomyndigheten
(Folkhälsodata) and Skolverkets statistikdatabas.

Each installation is described by a :class:`PxWebV1Source`; the tools are
registered with the source's prefix (e.g. ``fohm_get_table_data``).
"""

import re
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal
from urllib.parse import unquote, urlparse

from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult
from pydantic import BaseModel, Field

from ..core import READ_ONLY_OPEN, Services, clamp, structured, tool_errors
from ..errors import InvalidInputError, UpstreamError
from .jsonstat import jsonstat_to_table
from .models import DataResult, TableMetadataView, to_view
from .powerquery import curl_post, power_query_post_jsonstat
from .selection import PxVariable, resolve_selection
from .v1 import PxNode, PxSearchHit, PxWebV1Client, build_v1_query

Language = Literal["sv", "en"]
Selection = dict[str, list[str]]

_UI_MARKER = re.compile(r"/pxweb/[a-z]{2}/(?P<db>[^/]+)/(?P<rest>.*)$", re.IGNORECASE)
_API_MARKER = re.compile(r"/api/v1/[a-z]{2}/(?P<rest>.*)$", re.IGNORECASE)


@dataclass(frozen=True)
class PxWebV1Source:
    source: str  # HTTP source label and DataResult.source
    base_url_key: str  # key in Settings.base_urls
    prefix: str  # tool name prefix, e.g. "fohm"
    name: str  # e.g. "Folkhälsodata"
    publisher: str  # e.g. "Folkhälsomyndigheten"
    default_database: str
    default_max_cells: int
    citation: str
    path_example: str
    table_example: str
    selection_example: str
    subject_folders: dict[str, str] = field(default_factory=dict)
    notes: str = ""  # appended to data/metadata tool descriptions


class NodeList(BaseModel):
    path: str
    nodes: list[PxNode]
    hint: str = "type l = mapp (bläddra vidare med path), t = tabell (använd path i metadata-/dataverktygen)"


class SearchResult(BaseModel):
    query: str
    total: int
    hits: list[PxSearchHit]
    truncated: bool = False


class QueryRecipe(BaseModel):
    url: str
    body: dict[str, Any]
    power_query_m: str = Field(description="Power Query (M) för Power BI/Excel – hämtar samma urval som CSV")
    curl: str
    cells: int = Field(description="Beräknat antal celler (-1 om okänt, t.ex. vid jokertecken utan kodlista)")


def normalize_table_path(value: str, default_database: str) -> str:
    """Accept an API path, a full API URL, a web UI URL or a bare table file
    name and return ``database/folders/table.px``. Only the path is used –
    requests always go to the configured base URL."""
    text = value.strip()
    if not text:
        raise InvalidInputError(f"Ange en sökväg, t.ex. '{default_database}/...'")
    if text.startswith(("http://", "https://")):
        path = unquote(urlparse(text).path)
        if match := _UI_MARKER.search(path):
            rest = re.sub(r"/table/tableViewLayout\d+/?$", "", match.group("rest")).strip("/")
            parts = [p for p in rest.split("/") if p]
            # The web UI encodes folders as "<db>__<folder>__<folder>".
            if parts and "__" in parts[0]:
                folder = parts[0].split("__")
                if folder[0] == match.group("db"):
                    folder = folder[1:]
                parts = folder + parts[1:]
            elif parts and parts[0] == match.group("db"):
                parts = parts[1:]
            return "/".join([match.group("db"), *parts])
        if match := _API_MARKER.search(path):
            return match.group("rest").strip("/")
        raise InvalidInputError(f"Känner inte igen URL:en {value!r} som en PxWeb-adress")
    text = text.strip("/")
    if "/" not in text and text.lower().endswith(".px"):
        # Short form: PxWeb resolves the table through its search index.
        return f"{default_database}/{text}"
    return text


class _Client(PxWebV1Client):
    def __init__(self, services: Services, spec: PxWebV1Source) -> None:
        super().__init__(services.http, services.settings.base_url(spec.base_url_key), spec.source)
        self.spec = spec
        self._max_cells: int | None = None

    async def max_cells(self, lang: str) -> int:
        if self._max_cells is None:
            try:
                config = await self.get_config(lang)
                self._max_cells = int(config.get("maxCells") or self.spec.default_max_cells)
            except (UpstreamError, AttributeError, TypeError, ValueError):
                self._max_cells = self.spec.default_max_cells
        return self._max_cells


def register_pxweb_v1_source(server: MCPServer[Any], services: Services, spec: PxWebV1Source) -> None:
    settings = services.settings
    holder: dict[str, _Client] = {}
    p = spec.prefix

    def client() -> _Client:
        # Recreated after the lifespan closes the shared HTTP client.
        current = holder.get("c")
        if current is None or current.http is not services.http:
            current = _Client(services, spec)
            holder["c"] = current
        return current

    def lang_or_default(lang: str | None) -> str:
        return lang or settings.default_language

    def path_of(value: str) -> str:
        return normalize_table_path(value, spec.default_database)

    async def load_metadata(path: str, lang: str) -> tuple[str, str, list[PxVariable]]:
        table_path = path_of(path)
        title, variables, _ = await client().get_metadata(lang, table_path)
        return table_path, title, variables

    folders = "; ".join(f"{k}: {v}" for k, v in spec.subject_folders.items())

    @server.tool(
        name=f"{p}_list_databases",
        title=f"{spec.publisher}: lista databaser",
        description=f"Lista databaserna i {spec.name} ({spec.publisher}, PxWeb API v1) och de kända ämnesmapparna"
        + (f" ({folders})." if folders else "."),
        annotations=READ_ONLY_OPEN,
    )
    @tool_errors
    async def list_databases(lang: Language | None = None) -> dict[str, Any]:
        databases = await client().list_databases(lang_or_default(lang))
        return {"databases": databases, "known_subject_folders": spec.subject_folders}

    @server.tool(
        name=f"{p}_browse",
        title=f"{spec.publisher}: bläddra i {spec.name}",
        description=f"Lista mappar (type 'l') och tabeller (type 't') på en nivå i {spec.name}. Använd fältet "
        "'path' för att gå vidare eller för att hämta metadata för en tabell.",
        annotations=READ_ONLY_OPEN,
    )
    @tool_errors
    async def browse(
        path: Annotated[str, Field(description=f"Mapp att lista, t.ex. '{spec.path_example}'")] = spec.default_database,
        lang: Language | None = None,
    ) -> Annotated[CallToolResult, NodeList]:
        normalized = path_of(path)
        nodes = await client().list_level(lang_or_default(lang), normalized)
        return structured(NodeList(path=normalized, nodes=nodes))

    @server.tool(
        name=f"{p}_search_tables",
        title=f"{spec.publisher}: sök tabeller",
        description=f"Sök tabeller i {spec.name} på titel, variabler och värden (Lucene-syntax). Svaret innehåller "
        f"tabellens 'path' som används i {p}_get_table_metadata och {p}_get_table_data.",
        annotations=READ_ONLY_OPEN,
    )
    @tool_errors
    async def search_tables(
        query: Annotated[
            str,
            Field(
                description="Sökord. Trunkera med *, t.ex. 'psykisk*', 'title:elev*', 'betyg AND grundskola'. "
                "Ett ensamt ord trunkeras automatiskt."
            ),
        ],
        path: Annotated[str, Field(description="Mapp att söka under")] = spec.default_database,
        search_in: Annotated[
            Literal["default", "title", "codes", "values", "variables"],
            Field(description="Begränsa sökningen till fält; 'codes' söker värdekoder, t.ex. en kommunkod"),
        ] = "default",
        limit: Annotated[int, Field(description="Max antal träffar (1–100)")] = 25,
        lang: Language | None = None,
    ) -> Annotated[CallToolResult, SearchResult]:
        text = query.strip()
        if not text:
            raise InvalidInputError("Ange ett sökord")
        if re.fullmatch(r"[\w-]+", text) and len(text) >= 3:
            text = f"{text}*"
        hits = await client().search(
            lang_or_default(lang),
            path_of(path),
            text,
            search_filter=None if search_in == "default" else search_in,
        )
        unique: dict[str, PxSearchHit] = {}
        for hit in hits:
            unique.setdefault(hit.id, hit)
        ordered = sorted(unique.values(), key=lambda h: -(h.score or 0))
        cap = clamp(limit, 1, 100)
        return structured(
            SearchResult(query=text, total=len(ordered), hits=ordered[:cap], truncated=len(ordered) > cap)
        )

    @server.tool(
        name=f"{p}_get_table_metadata",
        title=f"{spec.publisher}: tabellmetadata",
        description=f"Visa en tabells variabler i {spec.name} med värdekoder och värdetexter och om variabeln kan "
        f"elimineras. Koderna används i urvalet till {p}_get_table_data. {spec.notes}".strip(),
        annotations=READ_ONLY_OPEN,
    )
    @tool_errors
    async def get_table_metadata(
        table_path: Annotated[
            str,
            Field(
                description=f"Tabellens sökväg, en {spec.name}-URL eller bara tabellfilen, t.ex. '{spec.table_example}'"
            ),
        ],
        max_values: Annotated[int, Field(description="Max antal värden att visa per variabel (0 = alla)")] = 60,
        lang: Language | None = None,
    ) -> Annotated[CallToolResult, TableMetadataView]:
        table_path, title, variables = await load_metadata(table_path, lang_or_default(lang))
        return structured(
            TableMetadataView(
                source=spec.source,
                table_id=table_path,
                title=title,
                variables=[to_view(v, max(0, max_values)) for v in variables],
            )
        )

    @server.tool(
        name=f"{p}_get_table_data",
        title=f"{spec.publisher}: hämta data",
        description=f"Hämta data ur en tabell i {spec.name} som rader (en rad per cell). Urvalet kontrolleras mot "
        f"metadata och cellgränsen innan anropet. {spec.notes}".strip(),
        annotations=READ_ONLY_OPEN,
    )
    @tool_errors
    async def get_table_data(
        table_path: Annotated[str, Field(description="Tabellens sökväg, URL eller tabellfil")],
        selection: Annotated[
            Selection | None,
            Field(
                description=f"Urval {{variabelkod: [värdekoder/uttryck]}}, t.ex. {spec.selection_example}. "
                "Uttryck: '*', '01*', 'TOP(n)', 'BOTTOM(n)', 'RANGE(a,b)', 'FROM(a)', 'TO(b)'. Utelämnade variabler "
                "elimineras om möjligt; annars väljs senaste tidsperioden respektive alla värden (högst 100). "
                "Variabler med values_withheld=true (värdena listas inte i metadata) tar bara koder, "
                "'*', 'prefix*'/'*suffix' eller 'TOP(n)'."
            ),
        ] = None,
        max_rows: Annotated[int, Field(description="Max antal rader i svaret (1–1000)")] = 200,
        label_mode: Annotated[
            Literal["label", "code", "both"], Field(description="Visa värdetexter, koder eller båda")
        ] = "label",
        drop_empty: Annotated[bool, Field(description="Hoppa över celler utan värde")] = False,
        lang: Language | None = None,
    ) -> Annotated[CallToolResult, DataResult]:
        language = lang_or_default(lang)
        table, _, variables = await load_metadata(table_path, language)
        resolved = resolve_selection(variables, selection, max_cells=await client().max_cells(language))
        response = await client().get_data(language, table, resolved.values, raw_variables=set(resolved.unvalidated))
        data = jsonstat_to_table(
            response.data,
            max_rows=clamp(max_rows, 1, settings.max_rows),
            label_mode=label_mode,
            drop_empty=drop_empty,
        )
        return structured(
            DataResult(
                source=spec.source,
                table_id=table,
                selection=resolved.compacted(),
                data=data,
                citation=f"{spec.citation}, tabell {table}" + (f" ({data.title})" if data.title else ""),
                request_url=response.url,
                notices=response.notices,
            )
        )

    @server.tool(
        name=f"{p}_build_query",
        title=f"{spec.publisher}: bygg återanvändbar fråga",
        description="Bygg en återanvändbar fråga (URL + JSON-kropp) för urvalet, med färdig Power Query (M) för "
        "Power BI/Excel och ett curl-kommando. Hämtar ingen data, bara metadata för att validera urvalet.",
        annotations=READ_ONLY_OPEN,
    )
    @tool_errors
    async def build_query(
        table_path: Annotated[str, Field(description="Tabellens sökväg, URL eller tabellfil")],
        selection: Annotated[Selection | None, Field(description=f"Urval som i {p}_get_table_data")] = None,
        lang: Language | None = None,
    ) -> Annotated[CallToolResult, QueryRecipe]:
        language = lang_or_default(lang)
        table, _, variables = await load_metadata(table_path, language)
        resolved = resolve_selection(variables, selection, max_cells=await client().max_cells(language))
        url = client().url(language, table)
        body = build_v1_query(resolved.values, raw_variables=set(resolved.unvalidated))
        return structured(
            QueryRecipe(
                url=url,
                body=body,
                power_query_m=power_query_post_jsonstat(url, body),
                curl=curl_post(url, body),
                cells=resolved.cells,
            )
        )
