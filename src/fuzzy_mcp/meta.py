# SPDX-FileCopyrightText: 2026 Deniz Özer
#
# SPDX-License-Identifier: MIT

"""Server-wide tools and resources: source overview and API lifecycle notices."""

from typing import Any

from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel, Field

from .core import READ_ONLY_LOCAL, Services


class SourceInfo(BaseModel):
    key: str
    name: str
    publisher: str
    base_url: str
    enabled: bool
    api: str = Field(description="API-typ och version")
    documentation: str
    license: str
    tool_prefix: str


SOURCES: list[dict[str, Any]] = [
    {
        "key": "scb.statistik",
        "name": "Statistikdatabasen",
        "publisher": "Statistiska centralbyrån (SCB)",
        "url_key": "scb",
        "api": "PxWebApi 2.0 (REST, JSON-stat 2)",
        "documentation": "https://www.scb.se/vara-tjanster/oppna-data/pxwebapi/pxwebapi-v2/",
        "license": "CC0 1.0 – rekommenderad källhänvisning: 'Källa: SCB'",
        "tool_prefix": "scb_",
    },
    {
        "key": "scb.geodata",
        "name": "Öppna geodata (DeSO, RegSO, tätorter m.m.)",
        "publisher": "Statistiska centralbyrån (SCB)",
        "url_key": "scb_geodata",
        "api": "OGC WFS 1.1.0 (GeoServer, GeoJSON/CSV/GeoPackage)",
        "documentation": "https://www.scb.se/vara-tjanster/oppna-data/oppna-geodata/",
        "license": "CC0 1.0",
        "tool_prefix": "scb_geodata_",
    },
    {
        "key": "fohm",
        "name": "Folkhälsodata",
        "publisher": "Folkhälsomyndigheten",
        "url_key": "fohm",
        "api": "PxWeb API v1 (REST, JSON-stat 2)",
        "documentation": "https://www.folkhalsomyndigheten.se/statistik-och-data/",
        "license": "Licens anges inte i API:et – ange 'Källa: Folkhälsomyndigheten' samt tabell",
        "tool_prefix": "fohm_",
    },
    {
        "key": "skolverket.skolenhetsregistret",
        "name": "Skolenhetsregistret",
        "publisher": "Skolverket",
        "url_key": "skolenhetsregistret",
        "api": "REST/JSON",
        "documentation": "https://api.skolverket.se/skolenhetsregistret/swagger-ui/index.html",
        "license": "CC0 1.0 – rekommenderad källhänvisning: 'Källa: Skolverket'",
        "tool_prefix": "skolverket_",
    },
    {
        "key": "skolverket.syllabus",
        "name": "Läroplan/Syllabus",
        "publisher": "Skolverket",
        "url_key": "syllabus",
        "api": "REST/JSON",
        "documentation": "https://api.skolverket.se/syllabus/swagger-ui/index.html",
        "license": "CC0 1.0 – rekommenderad källhänvisning: 'Källa: Skolverket'",
        "tool_prefix": "skolverket_",
    },
    {
        "key": "skolverket.planerad",
        "name": "Planerad utbildning",
        "publisher": "Skolverket",
        "url_key": "planned_educations",
        "api": "REST/HAL+JSON",
        "documentation": "https://api.skolverket.se/planned-educations/swagger-ui/index.html",
        "license": "CC0 1.0 – rekommenderad källhänvisning: 'Källa: Skolverket'",
        "tool_prefix": "skolverket_pe_",
    },
    {
        "key": "skolverket.susa",
        "name": "Susa-navet",
        "publisher": "Skolverket",
        "url_key": "susa_navet",
        "api": "REST/JSON",
        "documentation": "https://api.skolverket.se/susa-navet/swagger-ui/index.html",
        "license": "CC0 1.0 – rekommenderad källhänvisning: 'Källa: Skolverket'",
        "tool_prefix": "skolverket_susa_",
    },
    {
        "key": "skolverket.statistik",
        "name": "Skolverkets statistikdatabas",
        "publisher": "Skolverket",
        "url_key": "skolverket_statistik",
        "api": "PxWeb API v1 (REST, JSON-stat 2)",
        "documentation": "https://statistikdatabasen.skolverket.se/PxWeb/pxweb/sv/Skolverkets_statistikdatabas/",
        "license": "CC0 1.0 enligt Sveriges dataportal – rekommenderad källhänvisning: 'Källa: Skolverket'",
        "tool_prefix": "skolverket_stat_",
    },
    {
        "key": "dataportal",
        "name": "Sveriges dataportal",
        "publisher": "Myndigheten för digital förvaltning (DIGG)",
        "url_key": "dataportal",
        "api": "EntryStore REST/Solr (DCAT-AP-SE)",
        "documentation": "https://www.dataportal.se/",
        "license": "Licens för portalens metadata anges inte av Digg – följ varje datasets egen dcterms:license",
        "tool_prefix": "dataportal_",
    },
]


def list_sources(services: Services) -> list[SourceInfo]:
    settings = services.settings
    return [
        SourceInfo(
            key=s["key"],
            name=s["name"],
            publisher=s["publisher"],
            base_url=settings.base_url(s["url_key"]),
            enabled=settings.source_enabled(s["key"]),
            api=(
                "PxWebApi 2.0 (REST, JSON-stat 2) – aktiverad via konfiguration"
                if s["url_key"] in settings.api_versions and settings.api_version(s["url_key"]) == "v2"
                else s["api"]
            ),
            documentation=s["documentation"],
            license=s["license"],
            tool_prefix=s["tool_prefix"],
        )
        for s in SOURCES
    ]


def register(server: MCPServer[Any], services: Services) -> None:
    @server.tool(name="fuzzy_list_sources", title="Lista datakällor", annotations=READ_ONLY_LOCAL)
    async def fuzzy_list_sources() -> list[SourceInfo]:
        """Lista alla datakällor (myndighet, API, bas-URL, licens, verktygsprefix) och om de är aktiverade."""
        return list_sources(services)

    @server.tool(name="fuzzy_api_notices", title="API-livscykelnotiser", annotations=READ_ONLY_LOCAL)
    async def fuzzy_api_notices() -> dict[str, Any]:
        """Visa Deprecation/Sunset-varningar som API:erna skickat under sessionen (DIGG:s REST API-profil).
        Används för att upptäcka när en API-version håller på att fasas ut."""
        notices = list(services.http.recent_notices) if services._http is not None else []
        return {"notices": notices, "count": len(notices)}

    @server.resource(
        "fuzzy://sources",
        name="sources",
        title="Datakällor",
        description="Översikt över alla datakällor, bas-URL:er och licenser.",
        mime_type="application/json",
    )
    def sources_resource() -> list[dict[str, Any]]:
        return [s.model_dump() for s in list_sources(services)]
