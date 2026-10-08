# SPDX-FileCopyrightText: 2026 Deniz Özer
#
# SPDX-License-Identifier: MIT

"""MCP tools for a PxWebApi 2 installation.

SCB:s Statistikdatabas runs PxWebApi 2 today. Folkhälsomyndigheten and
Skolverket still run PxWeb API v1 (``/api/v2/config`` answered 404 when
verified 2026-09-25), but PxTools ships PxWebApi 2 as the successor, so the
tools are registered from a :class:`PxWebV2Source` description: moving an
installation to v2 is a configuration change, not new code.
"""

import re
import time
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult
from pydantic import BaseModel, Field

from ..core import READ_ONLY_OPEN, Services, clamp, personal_data, structured, tool_errors
from ..errors import InvalidInputError, UpstreamError
from .jsonstat import jsonstat_to_table
from .models import DataResult, TableMetadataView, to_view
from .powerquery import curl_post, power_query_get_csv
from .selection import PxVariable, expand_token, find_variable, is_expression, resolve_selection
from .v2 import (
    SEPARATOR_PARAMS,
    PxWebV2Client,
    build_get_url,
    build_post_url,
    build_selection_body,
    canonical_codelists,
    validate_output_params,
)

MAX_GET_URL = 2000
CATALOGUE_TTL_SECONDS = 6 * 3600
CATALOGUE_PAGE_SIZE = 5000
MAX_CATALOGUE_PAGES = 20
MAX_BROWSE_TABLES = 100
MAX_CODELIST_VALUES = 2000

Language = Literal["sv", "en"]
Selection = dict[str, list[str]]
OutputFormat = Literal["csv", "xlsx", "json-stat2", "px", "html", "json-px"]
OutputParam = Literal[
    "UseCodes",
    "UseTexts",
    "UseCodesAndTexts",
    "IncludeTitle",
    "SeparatorTab",
    "SeparatorSpace",
    "SeparatorSemicolon",
    "ExcludeZerosAndMissingValues",
]


@dataclass(frozen=True)
class PxWebV2Source:
    source: str  # HTTP source label (rate limits) and DataResult.source
    base_url_key: str  # key in Settings.base_urls
    prefix: str  # tool name prefix, e.g. "scb"
    name: str  # e.g. "Statistikdatabasen"
    publisher: str  # e.g. "SCB"
    citation: str
    default_max_cells: int = 150_000
    csv_encoding: int = 28591  # Power Query code page for csv (SCB: iso-8859-1)
    table_example: str = "TAB638"
    topics: str = ""
    top_subjects: str = ""
    subject_example: str = "['BE'] eller ['BE', 'BE0101']"
    variables_example: str = "Region, Kon, Alder, ContentsCode, Tid"
    selection_example: str = "{'Region': ['0180','1480'], 'ContentsCode': ['BE0101N1'], 'Tid': ['TOP(5)']}"
    codelist_example: str = "{'Region': 'vs_RegionLän07'}"
    codelist_data_example: str = "{'Alder': 'agg_Ålder10årJ'}"
    codelist_id_example: str = "'vs_RegionLän07' eller 'agg_RegionNUTS2_2008'"
    search_hint: str = (
        "Sökningen använder AND mellan ord. Svenska facktermer fungerar bäst. Trunkering med * fungerar på "
        "ordstammen. Kontrollera last_period: tabeller fryses ibland och serien fortsätter i ett nytt tabell-id."
    )
    notes: str = ""  # appended to the data/metadata tool descriptions


class PathElement(BaseModel):
    id: str
    label: str


class TableSummary(BaseModel):
    id: str
    label: str
    description: str | None = None
    updated: str | None = None
    first_period: str | None = None
    last_period: str | None = None
    time_unit: str | None = None
    variable_names: list[str] = Field(default_factory=list)
    subject_code: str | None = None
    paths: list[list[PathElement]] = Field(default_factory=list)
    discontinued: bool | None = None
    source: str | None = None


class TableSearchResult(BaseModel):
    query: str | None = None
    total: int
    page: int
    page_size: int
    total_pages: int
    tables: list[TableSummary]
    hint: str


class SubjectNode(BaseModel):
    id: str
    label: str
    table_count: int


class SubjectListing(BaseModel):
    path: list[PathElement]
    folders: list[SubjectNode]
    tables: list[TableSummary]
    table_count: int = Field(description="Antal tabeller direkt på nivån")
    tables_truncated: bool = False


class QueryRecipe(BaseModel):
    table_id: str
    get_url: str | None = Field(default=None, description="Delbar GET-URL (None om den blir för lång)")
    post_url: str
    post_body: dict[str, Any]
    power_query_m: str | None = Field(default=None, description="Power Query (M) som läser CSV via GET-URL:en")
    curl: str
    cells: int
    note: str | None = None


class CodelistValue(BaseModel):
    code: str | None = None
    label: str | None = None
    value_map: list[str] | None = Field(default=None, description="Ursprungliga koder som ingår i värdet")


class CodelistView(BaseModel):
    id: str | None = None
    label: str | None = None
    type: str | None = Field(default=None, description="Aggregation (agg_) eller Valueset (vs_)")
    elimination: bool | None = None
    value_count: int
    values: list[CodelistValue]
    values_truncated: bool = False


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _paths(raw: Any) -> list[list[PathElement]]:
    out: list[list[PathElement]] = []
    for path in raw or []:
        if isinstance(path, list):
            out.append(
                [
                    PathElement(id=str(p.get("id", "")), label=str(p.get("label", "")))
                    for p in path
                    if isinstance(p, dict)
                ]
            )
    return out


def to_summary(table: dict[str, Any]) -> TableSummary:
    return TableSummary(
        id=str(table.get("id", "")),
        label=str(table.get("label") or ""),
        description=table.get("description") or None,
        updated=table.get("updated"),
        first_period=table.get("firstPeriod"),
        last_period=table.get("lastPeriod"),
        time_unit=table.get("timeUnit"),
        variable_names=[str(v) for v in table.get("variableNames") or []],
        subject_code=table.get("subjectCode"),
        paths=_paths(table.get("paths")),
        discontinued=table.get("discontinued"),
        source=table.get("source"),
    )


def shareable_tokens(
    variables: list[PxVariable], selection: Selection | None, resolved: dict[str, list[str]]
) -> dict[str, list[str]]:
    """Tokens for shareable queries.

    The caller's expressions (e.g. TOP(5)) are kept so the query stays
    rolling; plain codes or labels become the resolved codes. A time
    variable that was filled in automatically becomes ``TOP(1)`` (latest
    period), other filled-in variables keep their concrete codes.
    """
    given: dict[str, list[str]] = {}
    for key, tokens in (selection or {}).items():
        variable = find_variable(variables, key)
        given[variable.code] = [tokens] if isinstance(tokens, str) else list(tokens)
    by_code = {v.code: v for v in variables}
    out: dict[str, list[str]] = {}
    for code, codes in resolved.items():
        variable = by_code[code]
        tokens = given.get(code)
        if not tokens:
            out[code] = ["TOP(1)"] if variable.is_time and len(codes) == 1 else list(codes)
            continue
        items: list[str] = []
        for token in tokens:
            token = str(token).strip()
            if is_expression(token) and token not in variable.codes():
                # expand_token already checked the expression against the server grammar.
                items.append(re.sub(r"\s+", "", token))
            else:
                items.extend(expand_token(token, variable))
        out[code] = list(dict.fromkeys(items))
    return out


class PxWebV2Service:
    """Per-server state for one installation: API client, cached limits and table catalogue."""

    def __init__(self, services: Services, spec: PxWebV2Source) -> None:
        self.services = services
        self.spec = spec
        self._client: PxWebV2Client | None = None
        self._max_cells: int | None = None
        self._catalogue: dict[str, tuple[float, list[TableSummary]]] = {}

    @property
    def base_url(self) -> str:
        return self.services.settings.base_url(self.spec.base_url_key)

    @property
    def client(self) -> PxWebV2Client:
        http = self.services.http
        if self._client is None or self._client.http is not http:
            self._client = PxWebV2Client(http, self.base_url, self.spec.source)
        return self._client

    async def max_cells(self) -> int:
        if self._max_cells is None:
            try:
                config = await self.client.config()
                self._max_cells = _int(config.get("maxDataCells"), self.spec.default_max_cells) or (
                    self.spec.default_max_cells
                )
            except (UpstreamError, AttributeError, TypeError, ValueError):
                self._max_cells = self.spec.default_max_cells
        return self._max_cells

    async def metadata(
        self, table_id: str, lang: str, codelists: dict[str, str] | None
    ) -> tuple[str, list[PxVariable], dict[str, Any]]:
        return await self.client.get_metadata(table_id, lang=lang, codelists=codelists)

    async def catalogue(self, lang: str) -> list[TableSummary]:
        """All tables as compact summaries (a few large /tables pages), cached for hours."""
        cached = self._catalogue.get(lang)
        if cached and time.monotonic() - cached[0] < CATALOGUE_TTL_SECONDS:
            return cached[1]
        tables: list[TableSummary] = []
        for page in range(1, MAX_CATALOGUE_PAGES + 1):
            data = await self.client.search_tables(lang=lang, page_number=page, page_size=CATALOGUE_PAGE_SIZE)
            if not isinstance(data, dict):
                raise UpstreamError(self.spec.source, "Oväntat svar från /tables")
            batch = [to_summary(t) for t in data.get("tables") or [] if isinstance(t, dict)]
            tables.extend(batch)
            total_pages = _int((data.get("page") or {}).get("totalPages"), 1)
            if page >= total_pages or not batch:
                break
        self._catalogue[lang] = (time.monotonic(), tables)
        return tables


def register_pxweb_v2_source(server: MCPServer[Any], services: Services, spec: PxWebV2Source) -> None:
    settings = services.settings
    service = PxWebV2Service(services, spec)
    p = spec.prefix
    extra_notes = f" {spec.notes}" if spec.notes else ""

    def lang_or_default(lang: str | None) -> str:
        return lang or settings.default_language

    table_id_field = Field(description=f"Tabell-id, t.ex. '{spec.table_example}'")

    @server.tool(
        name=f"{p}_get_config",
        title=f"{spec.publisher}: API-konfiguration",
        description=f"Visa PxWebApi 2-konfigurationen för {spec.name} ({spec.publisher}): API-version, språk, max "
        "antal celler per anrop (maxDataCells), anropsgräns (maxCallsPerTimeWindow/timeWindow), licens och "
        "tillgängliga dataformat.",
        annotations=READ_ONLY_OPEN,
    )
    @tool_errors
    async def get_config() -> dict[str, Any]:
        config = await service.client.config()
        if not isinstance(config, dict):
            raise UpstreamError(spec.source, "Oväntat svar från /config")
        return config

    @server.tool(
        name=f"{p}_search_tables",
        title=f"{spec.publisher}: sök tabeller",
        description=f"Sök tabeller i {spec.publisher}:s {spec.name}"
        + (f" ({spec.topics})" if spec.topics else "")
        + ". Returnerar tabell-id, titel, tidsperiod, tidsenhet och ämnessökväg.",
        annotations=READ_ONLY_OPEN,
    )
    @tool_errors
    async def search_tables(
        query: Annotated[
            str | None,
            Field(
                description="Sökord (Lucene). Ord kombineras med AND; 'title:barn', 'folkmäng*', "
                "'konsumentpris~1'. Utelämna för att lista alla tabeller."
            ),
        ] = None,
        past_days: Annotated[
            int | None, Field(description="Bara tabeller uppdaterade de senaste N dagarna", ge=1)
        ] = None,
        include_discontinued: Annotated[bool, Field(description="Ta med avslutade tabeller")] = False,
        page: Annotated[int, Field(description="Sida (från 1)", ge=1)] = 1,
        page_size: Annotated[int, Field(description="Träffar per sida (1–100)")] = 20,
        lang: Language | None = None,
    ) -> Annotated[CallToolResult, TableSearchResult]:
        data = await service.client.search_tables(
            lang=lang_or_default(lang),
            query=query,
            past_days=past_days,
            include_discontinued=include_discontinued,
            page_number=page,
            page_size=clamp(page_size, 1, 100),
        )
        if not isinstance(data, dict):
            raise UpstreamError(spec.source, "Oväntat svar från /tables")
        info = data.get("page") or {}
        tables = [to_summary(t) for t in data.get("tables") or [] if isinstance(t, dict)]
        return structured(
            TableSearchResult(
                query=query,
                total=_int(info.get("totalElements"), len(tables)),
                page=_int(info.get("pageNumber"), page),
                page_size=_int(info.get("pageSize"), page_size),
                total_pages=_int(info.get("totalPages"), 1),
                tables=tables,
                hint=spec.search_hint,
            )
        )

    @server.tool(
        name=f"{p}_browse_subjects",
        title=f"{spec.publisher}: bläddra i ämnesträdet",
        description=f"Bläddra i ämnesträdet för {spec.name} (ämnesområde → statistikprodukt → tabellgrupp). "
        "Trädet byggs från tabellkatalogen eftersom PxWebApi 2 saknar navigeringsendpoint; första anropet kan ta "
        "några sekunder.",
        annotations=READ_ONLY_OPEN,
    )
    @tool_errors
    async def browse_subjects(
        path: Annotated[
            list[str] | None,
            Field(
                description=f"Sökväg av mapp-id från roten, t.ex. {spec.subject_example}. Utelämna för "
                "ämnesområdena på toppnivån" + (f" ({spec.top_subjects})." if spec.top_subjects else ".")
            ),
        ] = None,
        max_tables: Annotated[
            int, Field(description=f"Max antal tabeller att lista på nivån (0–{MAX_BROWSE_TABLES})")
        ] = 50,
        lang: Language | None = None,
    ) -> Annotated[CallToolResult, SubjectListing]:
        wanted = [w.strip().casefold() for w in path or [] if w.strip()]
        tables = await service.catalogue(lang_or_default(lang))
        folders: dict[str, SubjectNode] = {}
        here: list[TableSummary] = []
        labels: list[PathElement] = []
        for table in tables:
            children: dict[str, PathElement] = {}
            at_level = False
            for table_path in table.paths:
                if [e.id.casefold() for e in table_path[: len(wanted)]] != wanted or len(table_path) < len(wanted):
                    continue
                if not labels and wanted:
                    labels = table_path[: len(wanted)]
                if len(table_path) > len(wanted):
                    child = table_path[len(wanted)]
                    children.setdefault(child.id, child)
                else:
                    at_level = True
            for child in children.values():
                node = folders.setdefault(child.id, SubjectNode(id=child.id, label=child.label, table_count=0))
                node.table_count += 1
            if at_level:
                here.append(table)
        if wanted and not labels:
            raise InvalidInputError(f"Hittade ingen ämnessökväg {path!r}. Börja från toppnivån utan path.")
        cap = clamp(max_tables, 0, MAX_BROWSE_TABLES)
        return structured(
            SubjectListing(
                path=labels,
                folders=sorted(folders.values(), key=lambda f: f.id),
                tables=here[:cap],
                table_count=len(here),
                tables_truncated=len(here) > cap,
            )
        )

    @server.tool(
        name=f"{p}_get_table",
        title=f"{spec.publisher}: tabellinformation",
        description=f"Katalogposten för en tabell i {spec.name}: titel, första/sista period, tidsenhet, uppdaterad, "
        "ämnessökväg och variabelnamn.",
        annotations=READ_ONLY_OPEN,
    )
    @tool_errors
    async def get_table(
        table_id: Annotated[str, table_id_field],
        lang: Language | None = None,
    ) -> Annotated[CallToolResult, TableSummary]:
        data = await service.client.get_table(table_id, lang=lang_or_default(lang))
        if not isinstance(data, dict):
            raise UpstreamError(spec.source, "Oväntat svar från /tables/{id}")
        return structured(to_summary(data))

    @server.tool(
        name=f"{p}_get_table_metadata",
        title=f"{spec.publisher}: tabellmetadata",
        description=f"Variabler ({spec.variables_example} ...) med värdekoder och texter, om variabeln kan "
        "elimineras, vilka kodlistor (agg_/vs_) som finns, enhet/referensperiod/måttyp per innehåll och länkar till "
        f"definitioner och statistikens webbsida (META-ID).{extra_notes}",
        annotations=READ_ONLY_OPEN,
    )
    @tool_errors
    async def get_table_metadata(
        table_id: Annotated[str, table_id_field],
        codelists: Annotated[
            dict[str, str] | None,
            Field(description=f"Kodlista per variabel, t.ex. {spec.codelist_example} (skiftlägeskänsligt id)"),
        ] = None,
        max_values: Annotated[int, Field(description="Max antal värden att visa per variabel (0 = alla)")] = 60,
        include_contacts: Annotated[
            bool,
            Field(
                description="Ta med kontaktpersoner (namn, telefon, e-post – personuppgift). Annars visas bara "
                "kontaktorganisationen."
            ),
        ] = False,
        lang: Language | None = None,
    ) -> Annotated[CallToolResult, TableMetadataView]:
        include_contacts = personal_data(services, include_contacts)
        title, variables, info = await service.metadata(table_id, lang_or_default(lang), codelists)
        variable_links = info.get("variable_links") or {}
        contents = info.get("contents") or {}
        return structured(
            TableMetadataView(
                source=spec.source,
                table_id=table_id.strip(),
                title=title,
                updated=info.get("updated"),
                first_period=info.get("first_period"),
                last_period=info.get("last_period"),
                variables=[to_view(v, max(0, max_values), variable_links.get(v.code)) for v in variables],
                notes=info.get("notes") or [],
                contacts=(info.get("contacts") if include_contacts else info.get("contact_organizations")) or [],
                official_statistics=info.get("official_statistics"),
                links=info.get("links") or [],
                contents_info=dict(list(contents.items())[:100]),
                extra={k: v for k, v in (info.get("px") or {}).items() if v},
            )
        )

    @server.tool(
        name=f"{p}_get_table_data",
        title=f"{spec.publisher}: hämta data",
        description=f"Hämta data ur en tabell i {spec.name} som rader (en rad per cell, JSON-stat 2). Urvalet "
        "valideras mot metadata och API:ts cellgräns (maxDataCells) innan anropet; svaret visar vilka koder som "
        f"användes.{extra_notes}",
        annotations=READ_ONLY_OPEN,
    )
    @tool_errors
    async def get_table_data(
        table_id: Annotated[str, table_id_field],
        selection: Annotated[
            Selection | None,
            Field(
                description=f"Urval {{variabelkod: [värdekoder/uttryck]}}, t.ex. {spec.selection_example}. "
                "Uttryck: '*', '01*', '????', 'TOP(n)', 'BOTTOM(n)', 'RANGE(a,b)', 'FROM(a)', 'TO(b)'. Utelämnade "
                "variabler elimineras om möjligt, annars väljs senaste perioden (tid) respektive alla värden."
            ),
        ] = None,
        codelists: Annotated[
            dict[str, str] | None,
            Field(
                description=f"Kodlista per variabel, t.ex. {spec.codelist_data_example}; urvalet avser då listans koder"
            ),
        ] = None,
        max_rows: Annotated[int, Field(description="Max antal rader i svaret (1–1000)")] = 200,
        label_mode: Annotated[
            Literal["label", "code", "both"], Field(description="Visa värdetexter, koder eller båda")
        ] = "label",
        drop_empty: Annotated[bool, Field(description="Hoppa över celler utan värde (t.ex. '..')")] = False,
        lang: Language | None = None,
    ) -> Annotated[CallToolResult, DataResult]:
        language = lang_or_default(lang)
        _, variables, _ = await service.metadata(table_id, language, codelists)
        lists = canonical_codelists(variables, codelists)
        resolved = resolve_selection(variables, selection, max_cells=await service.max_cells())
        response = await service.client.get_data(table_id, resolved.values, lang=language, codelists=lists)
        data = jsonstat_to_table(
            response.data,
            max_rows=clamp(max_rows, 1, settings.max_rows),
            label_mode=label_mode,
            drop_empty=drop_empty,
        )
        return structured(
            DataResult(
                source=spec.source,
                table_id=table_id.strip(),
                selection=resolved.compacted(),
                data=data,
                citation=f"{spec.citation}, tabell {table_id.strip()}" + (f" ({data.title})" if data.title else ""),
                request_url=response.url,
                notices=response.notices,
            )
        )

    @server.tool(
        name=f"{p}_get_default_selection",
        title=f"{spec.publisher}: standardurval",
        description=f"Tabellens standardurval i {spec.name} (det urval webbgränssnittet visar först) med konkreta "
        "värdekoder.",
        annotations=READ_ONLY_OPEN,
    )
    @tool_errors
    async def get_default_selection(
        table_id: Annotated[str, table_id_field],
        lang: Language | None = None,
    ) -> dict[str, Any]:
        data = await service.client.get_default_selection(table_id, lang=lang_or_default(lang))
        if not isinstance(data, dict):
            raise UpstreamError(spec.source, "Oväntat svar från /defaultselection")
        return {"selection": data.get("selection") or [], "placement": data.get("placement")}

    @server.tool(
        name=f"{p}_get_codelist",
        title=f"{spec.publisher}: kodlista",
        description=f"Hämta en kodlista i {spec.name} (aggregering agg_* eller värdemängd vs_*): koder, texter och "
        "vilka ursprungliga koder som ingår (value_map).",
        annotations=READ_ONLY_OPEN,
    )
    @tool_errors
    async def get_codelist(
        codelist_id: Annotated[
            str, Field(description=f"Kodlistans id från metadata, t.ex. {spec.codelist_id_example}")
        ],
        max_values: Annotated[int, Field(description=f"Max antal värden i svaret (1–{MAX_CODELIST_VALUES})")] = 500,
        lang: Language | None = None,
    ) -> Annotated[CallToolResult, CodelistView]:
        data = await service.client.get_codelist(codelist_id, lang=lang_or_default(lang))
        if not isinstance(data, dict):
            raise UpstreamError(spec.source, "Oväntat svar från /codelists")
        raw_values = [v for v in data.get("values") or [] if isinstance(v, dict)]
        cap = clamp(max_values, 1, MAX_CODELIST_VALUES)
        return structured(
            CodelistView(
                id=data.get("id"),
                label=data.get("label"),
                type=data.get("type"),
                elimination=data.get("elimination"),
                value_count=len(raw_values),
                values=[
                    CodelistValue(
                        code=None if v.get("code") is None else str(v.get("code")),
                        label=None if v.get("label") is None else str(v.get("label")),
                        value_map=[str(x) for x in v.get("valueMap") or []] or None,
                    )
                    for v in raw_values[:cap]
                ],
                values_truncated=len(raw_values) > cap,
            )
        )

    @server.tool(
        name=f"{p}_build_query",
        title=f"{spec.publisher}: bygg återanvändbar fråga",
        description=f"Bygg en delbar GET-URL och en POST-fråga för ett urval i {spec.name}, med Power Query (M) för "
        "Power BI/Excel och curl. Uttryck som TOP(5) behålls i URL:en så att frågan hämtar senaste perioderna vid "
        "uppdatering; en automatiskt vald tidsperiod blir TOP(1). Hämtar ingen data, bara metadata för att "
        "validera urvalet.",
        annotations=READ_ONLY_OPEN,
    )
    @tool_errors
    async def build_query(
        table_id: Annotated[str, table_id_field],
        selection: Annotated[Selection | None, Field(description=f"Urval som i {p}_get_table_data")] = None,
        codelists: Annotated[dict[str, str] | None, Field(description="Kodlista per variabel")] = None,
        output_format: Annotated[OutputFormat, Field(description="Format för GET-URL:en och POST-frågan")] = "csv",
        output_format_params: Annotated[
            list[OutputParam] | None,
            Field(
                description="T.ex. ['UseTexts', 'SeparatorSemicolon']. Gäller csv/xlsx/html; Separator* bara csv; "
                "högst en av UseCodes/UseTexts/UseCodesAndTexts."
            ),
        ] = None,
        lang: Language | None = None,
    ) -> Annotated[CallToolResult, QueryRecipe]:
        params = validate_output_params(output_format, list(output_format_params or []))
        language = lang_or_default(lang)
        _, variables, _ = await service.metadata(table_id, language, codelists)
        lists = canonical_codelists(variables, codelists)
        resolved = resolve_selection(variables, selection, max_cells=await service.max_cells())
        tokens = shareable_tokens(variables, selection, resolved.values)
        get_url = build_get_url(
            service.base_url,
            table_id,
            tokens,
            lang=language,
            output_format=output_format,
            output_format_params=params,
            codelists=lists,
        )
        note = None
        get_url_out: str | None = get_url
        if len(get_url) > MAX_GET_URL:
            note = "GET-URL:en blir längre än ~2 000 tecken (servern svarar då 404); använd POST-frågan."
            get_url_out = None
        post_url = build_post_url(
            service.base_url, table_id, lang=language, output_format=output_format, output_format_params=params
        )
        body = build_selection_body(tokens, lists)
        power_query = None
        if get_url_out and output_format == "csv":
            separator = next((SEPARATOR_PARAMS[p] for p in params if p in SEPARATOR_PARAMS), ",")
            power_query = power_query_get_csv(
                get_url_out,
                delimiter=separator,
                encoding=spec.csv_encoding,
                skip_title="IncludeTitle" in params,
            )
        return structured(
            QueryRecipe(
                table_id=table_id.strip(),
                get_url=get_url_out,
                post_url=post_url,
                post_body=body,
                power_query_m=power_query,
                curl=curl_post(post_url, body),
                cells=resolved.cells,
                note=note,
            )
        )
