# SPDX-FileCopyrightText: 2026 Deniz Özer
#
# SPDX-License-Identifier: MIT

"""MCP tools and resources for the bundled reference data."""

from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ResourceNotFoundError
from pydantic import Field

from ..core import READ_ONLY_LOCAL, Services, clamp, tool_errors
from ..errors import InvalidInputError
from . import catalog, codes


def register(server: MCPServer[Any], services: Services) -> None:
    @server.tool(name="ref_lookup_region", title="Slå upp kommun/län", annotations=READ_ONLY_LOCAL)
    @tool_errors
    async def ref_lookup_region(
        query: Annotated[
            str, Field(description="Namn eller kod, t.ex. 'Göteborg', 'goteborg', '1480', 'AB' eller 'Skåne'")
        ],
        kind: Annotated[
            Literal["alla", "kommun", "lan"], Field(description="Begränsa till kommuner eller län")
        ] = "alla",
        limit: Annotated[int, Field(description="Max antal träffar (1–20)")] = 5,
    ) -> list[dict[str, Any]]:
        """Fuzzy-sök kommun- och länskoder (SCB:s regionala indelning) på namn eller kod.
        Tål stavfel och å/ä/ö-varianter. Kommunkoden (4 siffror) är nyckeln för att koppla ihop SCB
        (variabeln Region), Skolverket (kommunkod) och Folkhälsomyndigheten."""
        return codes.lookup_region(query, kind=kind, limit=clamp(limit, 1, 20))

    @server.tool(name="ref_list_municipalities", title="Kommuner i ett län", annotations=READ_ONLY_LOCAL)
    @tool_errors
    async def ref_list_municipalities(
        county_code: Annotated[
            str | None, Field(description="Länskod (2 siffror, t.ex. '14'). Utelämna för alla 290 kommuner.")
        ] = None,
    ) -> list[dict[str, Any]]:
        """Lista kommuner (kod, namn, länskod), valfritt filtrerat på län."""
        if county_code:
            return codes.municipalities_in_county(county_code.strip())
        return codes.load("regioner")["kommuner"]

    @server.tool(name="ref_list_code_lists", title="Lista kodlistor", annotations=READ_ONLY_LOCAL)
    @tool_errors
    async def ref_list_code_lists() -> list[dict[str, Any]]:
        """Lista medföljande kodlistor/referensdata (t.ex. regioner, betygsskala, skolformer, gymnasieprogram,
        SS 12000-uppräkningar) med beskrivning och källa."""
        return codes.list_code_lists()

    @server.tool(name="ref_get_code_list", title="Hämta kodlista", annotations=READ_ONLY_LOCAL)
    @tool_errors
    async def ref_get_code_list(
        name: Annotated[
            str, Field(description="Kodlistans id från ref_list_code_lists, t.ex. 'betygsskalor' eller 'skolformer'")
        ],
        search: Annotated[
            str | None,
            Field(
                description="Filtrera poster vars kod eller namn innehåller texten (gäller alla listor i dokumentet)"
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Hämta en hel kodlista (koder, namn, beskrivningar och källhänvisning)."""
        doc = codes.load(name.strip())
        if not search:
            return doc
        needle = codes.normalize(search)

        def matches(entry: Any) -> bool:
            if not isinstance(entry, dict):
                return needle in codes.normalize(str(entry))
            return any(
                needle in codes.normalize(str(entry.get(key, ""))) for key in ("kod", "namn", "code", "name", "id")
            )

        # Filter every list of entries ("koder", or e.g. "lan" and "kommuner" in regioner).
        return {
            key: [e for e in value if matches(e)]
            if isinstance(value, list) and value and isinstance(value[0], dict)
            else value
            for key, value in doc.items()
        } | {"search": search}

    @server.tool(name="ref_entity_catalog", title="Entitetskatalog och kopplingsnycklar", annotations=READ_ONLY_LOCAL)
    @tool_errors
    async def ref_entity_catalog(
        source: Annotated[
            Literal["scb", "fohm", "skolverket", "dataportal", "reference"] | None,
            Field(description="Begränsa till en källa; utelämna för alla"),
        ] = None,
    ) -> dict[str, Any]:
        """Katalog över alla dataentiteter som servern exponerar (tabeller, skolenheter, huvudmän, ämnen, kurser,
        program, utbildningstillfällen, dataset ...) med identifierare, verktyg och hur källorna kopplas ihop
        (kommunkod, länskod, skolenhetskod, organisationsnummer, skolform, kurs-/ämnes-/programkoder)."""
        return catalog.catalogue(source)

    @server.resource(
        "fuzzy://entities",
        name="entities",
        title="Entitetskatalog",
        description="Alla dataentiteter, deras nycklar och kopplingar mellan SCB, Skolverket, FoHM och dataportalen.",
        mime_type="application/json",
    )
    def entities_resource() -> dict[str, Any]:
        return catalog.catalogue()

    @server.resource(
        "fuzzy://crosswalk/bbic-icf",
        name="semantic-bridge",
        title="Konceptuell brygga till BBIC och ICF",
        description="Öppna indikatorer som belyser BBIC-domäner och ICF-komponenter (analysstöd, ej validerad).",
        mime_type="application/json",
    )
    def semantic_bridge_resource() -> dict[str, Any]:
        return catalog.semantic_bridge()

    @server.resource(
        "fuzzy://crosswalk/informationsmodell",
        name="information-model",
        title="Informationsmodell → öppna källor",
        description="Vilka begrepp en analys av öppna data behöver (indikatorvärde, period, geografi, "
        "organisation, skolform, kodöversättning, datakälla m.m.) och vilka verktyg och nycklar som ger dem.",
        mime_type="application/json",
    )
    def information_model_resource() -> dict[str, Any]:
        return catalog.information_model()

    @server.resource(
        "fuzzy://codes",
        name="code-lists",
        title="Kodlistor",
        description="Index över medföljande kodlistor och referensdata.",
        mime_type="application/json",
    )
    def code_list_index() -> list[dict[str, Any]]:
        return codes.list_code_lists()

    @server.resource(
        "fuzzy://codes/{name}",
        name="code-list",
        title="Kodlista",
        description="En kodlista i JSON, t.ex. fuzzy://codes/regioner.",
        mime_type="application/json",
    )
    def code_list(name: str) -> dict[str, Any]:
        try:
            return codes.load(name)
        except InvalidInputError as exc:
            raise ResourceNotFoundError(str(exc)) from exc
