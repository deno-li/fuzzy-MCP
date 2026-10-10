# SPDX-FileCopyrightText: 2026 Deniz Özer
#
# SPDX-License-Identifier: MIT

"""MCP tools and resources for the bundled reference data."""

from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ResourceNotFoundError
from pydantic import Field

from ..core import READ_ONLY_LOCAL, TRUNCATION_KEY, Services, clamp, client_size, compact_json, tool_errors
from ..errors import InvalidInputError
from . import catalog, codes, deso

# A code list answer is kept within the size Eneo cuts a tool result at (see core.TRUNCATION_KEY). The DeSO/RegSO
# lists are 90–290 kB as files, so their lookup tables are filtered by ``search`` or left out with a note.
CODE_LIST_LIMIT = 32_768
TABLE_ROWS = 100  # a top-level kod → namn table or list of rows with more entries than this is a lookup table
POST_KEYS = ("kod", "namn", "code", "name", "id")


def _is_table(value: Any) -> bool:
    """A lookup table (str → str) or list of rows (list of lists) with more than TABLE_ROWS entries: the
    collections of the DeSO/RegSO lists that hold no posts (``deso``, ``regso``, ``forandringar``)."""
    if isinstance(value, dict):
        return len(value) > TABLE_ROWS and all(isinstance(item, str) for item in value.values())
    return isinstance(value, list) and len(value) > TABLE_ROWS and all(isinstance(item, list) for item in value)


def _filter_code_list(doc: dict[str, Any], search: str) -> dict[str, Any]:
    """Keep the posts whose kod/namn, the table entries whose key or value and the rows whose cells contain
    ``search`` (normalized as in :func:`codes.normalize`); every other field is kept whole."""
    needle = codes.normalize(search)

    def hit(*cells: Any) -> bool:
        return any(needle in codes.normalize(str(cell)) for cell in cells)

    def post_hit(entry: Any) -> bool:
        return hit(*(entry.get(key, "") for key in POST_KEYS)) if isinstance(entry, dict) else hit(entry)

    out: dict[str, Any] = {}
    for key, value in doc.items():
        if isinstance(value, list) and value and isinstance(value[0], dict):
            out[key] = [entry for entry in value if post_hit(entry)]
        elif _is_table(value) and isinstance(value, dict):
            out[key] = {code: name for code, name in value.items() if hit(code, name)}
        elif _is_table(value):
            out[key] = [row for row in value if hit(*row)]
        else:
            out[key] = value
    return out | {"search": search}


def register(server: MCPServer[Any], services: Services) -> None:
    @server.tool(name="ref_lookup_region", title="Slå upp kommun/län", annotations=READ_ONLY_LOCAL)
    @tool_errors
    async def ref_lookup_region(
        query: Annotated[
            str,
            Field(
                description="Namn eller kod, t.ex. 'Göteborg', 'goteborg', '1480', 'AB' eller 'Skåne'", max_length=100
            ),
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

    @server.tool(name="ref_lookup_deso", title="Slå upp DeSO- eller RegSO-kod", annotations=READ_ONLY_LOCAL)
    @tool_errors
    async def ref_lookup_deso(
        code: Annotated[
            str,
            Field(
                description="DeSO-kod (t.ex. '0114C1010') eller RegSO-kod (t.ex. '0114R001'); versaler krävs inte",
                max_length=20,
            ),
        ],
    ) -> dict[str, Any]:
        """Slå upp en DeSO- eller RegSO-kod i SCB:s nyckelfiler: kommun, kategori, RegSO per version och
        jämförbarhet mellan DeSO 2018 och DeSO 2025. För DeSO ges RegSO-kod och -namn i varje version koden
        finns i (DeSO 2018/RegSO 2020, DeSO 2025/RegSO 2025), alla rader ur SCB:s förändringslogg och ett i kod
        härlett omdöme (jamforbarhet): oförändrad, ändrad gräns, upphört (ersatts_av, summerbar) eller nytt
        (bildat_av, summerbar_till, syskon). För RegSO ges namn och DeSO-koder per version samt om namnet
        ändrats. Lokala data, inga nätanrop; svaret anger fil och datum."""
        return deso.lookup(code)

    @server.tool(name="ref_list_deso", title="DeSO i en kommun", annotations=READ_ONLY_LOCAL)
    @tool_errors
    async def ref_list_deso(
        municipality: Annotated[
            str,
            Field(
                description="Kommunkod (4 siffror, t.ex. '0180') eller kommunnamn (t.ex. 'Upplands Väsby')",
                max_length=100,
            ),
        ],
        version: Annotated[
            Literal["2025", "2018"],
            Field(description="DeSO-version: '2025' (med RegSO 2025) eller '2018' (med RegSO 2020)"),
        ] = "2025",
        regso: Annotated[
            str | None,
            Field(
                description="Begränsa till ett RegSO: kod (t.ex. '0180R001') eller exakt namn inom kommunen",
                max_length=100,
            ),
        ] = None,
        category: Annotated[
            Literal["A", "B", "C"] | None,
            Field(description="Begränsa till DeSO-kategori A, B eller C (kodens femte tecken)"),
        ] = None,
    ) -> dict[str, Any]:
        """Lista alla DeSO-koder i en kommun grupperade per RegSO (kod, namn, antal) för DeSO 2025 eller
        DeSO 2018, med antal per kategori A/B/C. Valfritt filter på ett RegSO eller en kategori. Koderna är
        urvalsvärden för SCB:s tabeller på DeSO/RegSO-nivå och geodatalagren; använd samma årsversion i
        båda. Lokala data ur SCB:s kopplingsfil, inga nätanrop."""
        return deso.list_deso(municipality, version=version, regso=regso, category=category)

    @server.tool(name="ref_list_code_lists", title="Lista kodlistor", annotations=READ_ONLY_LOCAL)
    @tool_errors
    async def ref_list_code_lists() -> list[dict[str, Any]]:
        """Lista medföljande kodlistor och referensdata med beskrivning, källa och verifiering. Omfattar bl.a.
        regioner, betygsskala, skolformer, gymnasieprogram, DeSO/RegSO och
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
                description="Filtrera poster vars kod eller namn innehåller texten; gäller alla listor i dokumentet, "
                "i uppslagstabellerna deso/regso kod eller namn och i förändringsloggen varje cell (t.ex. kommunkod)"
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Hämta en hel kodlista (koder, namn, beskrivningar och källhänvisning). DeSO/RegSO-listorna är stora:
        uppslagstabellerna (deso, regso, forandringar) utelämnas med en not i _kortat om svaret skulle överstiga
        32 768 tecken; ange search (kommunkod eller kod) eller använd ref_lookup_deso/ref_list_deso."""
        doc = codes.load(name.strip())
        tables = [key for key, value in doc.items() if _is_table(value)]
        if search:
            doc = _filter_code_list(doc, search)
        if not tables or client_size(compact_json(doc)) <= CODE_LIST_LIMIT:
            return doc
        note = {
            "meddelande": f"Svaret skulle överstiga {CODE_LIST_LIMIT} tecken, så uppslagstabellerna "
            f"{', '.join(tables)} är utelämnade. Ange search (t.ex. kommunkod eller kod) för ett urval, använd "
            "ref_lookup_deso/ref_list_deso för DeSO och RegSO, eller läs hela filen som resursen "
            f"fuzzy://codes/{doc.get('id', name.strip())}.",
            "kortade": [{"falt": key, "fore": len(doc[key]), "efter": 0} for key in tables],
        }
        return {key: value for key, value in doc.items() if key not in tables} | {TRUNCATION_KEY: note}

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
