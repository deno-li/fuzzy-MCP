# SPDX-FileCopyrightText: 2026 Deniz Özer
#
# SPDX-License-Identifier: MIT

"""Reusable prompts (workflows across sources) and argument completion."""

from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mcp.types import Completion, CompletionArgument, CompletionContext, PromptReference, ResourceTemplateReference
from pydantic import Field

from .core import Services
from .reference import codes

MAX_COMPLETIONS = 100


def _region_names() -> list[tuple[str, str]]:
    doc = codes.load("regioner")
    entries = [(k["kod"], k["namn"]) for k in doc["kommuner"]]
    entries += [(lan["kod"], lan["namn"]) for lan in doc["lan"]]
    return entries


def complete_region(prefix: str) -> Completion:
    """Complete a region name; for a comma list ("Gävle, Sand") only the last part is completed."""
    head, sep, last = prefix.rpartition(",")
    done = f"{head}{sep} " if sep else ""
    part = last.strip()
    needle = codes.normalize(part) if part else ""
    hits = [
        done + name
        for code, name in _region_names()
        if not needle or code.startswith(part) or codes.normalize(name).startswith(needle)
    ]
    return Completion(values=hits[:MAX_COMPLETIONS], total=len(hits), has_more=len(hits) > MAX_COMPLETIONS)


def complete_code_list(prefix: str) -> Completion:
    ids = [entry["id"] for entry in codes.list_code_lists() if entry["id"].startswith(prefix.strip())]
    return Completion(values=ids[:MAX_COMPLETIONS], total=len(ids), has_more=len(ids) > MAX_COMPLETIONS)


REGION_ARGUMENTS = {"kommun", "region", "kommuner"}


def register(server: MCPServer[Any], services: Services) -> None:
    @server.prompt(name="analysera_kommun", title="Analysera en kommun")
    def analysera_kommun(
        kommun: Annotated[str, Field(description="Kommunens namn eller kod, t.ex. 'Gävle' eller '2180'")],
        tema: Annotated[
            str, Field(description="Fokus, t.ex. 'skola och utbildning', 'folkhälsa', 'befolkning'")
        ] = "skola, folkhälsa och befolkning",
    ) -> str:
        """Ta fram en faktabaserad lägesbild för en kommun genom att kombinera SCB, Skolverket och
        Folkhälsomyndigheten."""
        return f"""Gör en lägesbild för kommunen {kommun} med fokus på {tema}.

Arbetsgång:
1. Slå upp kommunkod och län med `ref_lookup_region` (kind='kommun'). Använd kommunkoden (4 siffror) och
   länskoden (2 siffror) som nycklar i alla följande anrop.
2. Befolkning (SCB): sök tabell med `scb_search_tables` (t.ex. 'folkmängd'), kontrollera variabler och
   koder med `scb_get_table_metadata` och hämta de senaste åren med `scb_get_table_data`
   (Region=[kommunkod, '00'], Tid=['TOP(5)']).
3. Skola (Skolverket): lista aktiva skolenheter med `skolverket_search_school_units` (municipality_code=kommunkod),
   och hämta statistik för relevanta skolenheter med `skolverket_pe_school_unit_statistics`.
4. Folkhälsa (Folkhälsomyndigheten): sök tabeller med `fohm_search_tables` (t.ex. 'psykisk*', 'levnadsvanor')
   och hämta kommun- eller länsvärden med `fohm_get_table_data`. Jämför med riket ('00').
5. Sammanfatta: viktigaste iakttagelser, jämförelse med länet och riket, tidstrender och osäkerheter
   (t.ex. små tal, prickade värden, ändrade definitioner).

Redovisa alltid källa (myndighet + tabell-id/endpoint) och period för varje siffra. Hitta inte på siffror –
om data saknas, säg det."""

    @server.prompt(name="jamfor_kommuner", title="Jämför kommuner")
    def jamfor_kommuner(
        kommuner: Annotated[str, Field(description="Kommaseparerade kommuner, t.ex. 'Gävle, Sandviken, Falun'")],
        indikator: Annotated[str, Field(description="Vad som ska jämföras, t.ex. 'behörighet till gymnasiet'")],
    ) -> str:
        """Jämför en indikator mellan flera kommuner med samma källa och period."""
        return f"""Jämför {indikator} mellan kommunerna: {kommuner}.

1. Översätt varje kommun till kommunkod med `ref_lookup_region`.
2. Hitta EN källa som har indikatorn för alla kommuner (SCB `scb_search_tables`, Folkhälsomyndigheten
   `fohm_search_tables`, Skolverket `skolverket_pe_*` eller `dataportal_search_datasets`).
3. Hämta samma period och samma mått för alla kommuner i ett anrop när det går (t.ex. Region=[koder]).
4. Presentera en tabell (kommun, värde, period, enhet) och kommentera skillnader och osäkerheter.
Ange källa och tabell-id."""

    @server.prompt(name="hitta_statistik", title="Hitta statistik")
    def hitta_statistik(
        fraga: Annotated[str, Field(description="Frågan du vill ha statistik för")],
    ) -> str:
        """Hitta rätt källa och tabell för en statistikfråga."""
        return f"""Hitta den bästa officiella källan för frågan: "{fraga}".

Sök parallellt och jämför träffarna:
- SCB: `scb_search_tables` (svenska facktermer fungerar bäst; ord kombineras med AND).
- Folkhälsomyndigheten: `fohm_search_tables`.
- Skolverket: skolenheter, läroplaner och skolstatistik via `skolverket_*`-verktygen.
- Övriga myndigheter: `dataportal_search_datasets`.

Välj tabell utifrån täckning (region, period, senaste uppdatering) och relevans. Visa 2–3 alternativ med
tabell-id, titel och period, och hämta sedan data för det bästa alternativet med en liten testfråga."""

    @server.prompt(name="power_bi_fraga", title="Bygg dataflöde för Power BI")
    def power_bi_fraga(
        beskrivning: Annotated[str, Field(description="Vilka data som ska in i Power BI/Excel")],
    ) -> str:
        """Bygg återanvändbara frågor (Power Query M) mot SCB eller Folkhälsomyndigheten."""
        return f"""Bygg ett återanvändbart dataflöde för Power BI/Excel: {beskrivning}.

1. Hitta tabellen och kontrollera variabler och koder (metadataverktygen).
2. Bygg frågan med `scb_build_query` (SCB, CSV via GET-URL, rullande uttryck som TOP(5) behålls) eller
   `fohm_build_query` (Folkhälsomyndigheten, POST).
3. Leverera Power Query-koden (M) och förklara hur den klistras in via Hämta data → Tom fråga → Avancerad
   redigerare. Beskriv vilka kolumner som blir dimensioner respektive mått, och föreslå en datummodell för
   tidsvariabeln (år, månad 2024M01, kvartal 2024K1)."""

    @server.prompt(name="skolenhet_profil", title="Profil för en skolenhet")
    def skolenhet_profil(
        skolenhetskod: Annotated[str, Field(description="Skolenhetskod (8 siffror) eller skolans namn")],
    ) -> str:
        """Sammanställ register-, utbuds- och statistikuppgifter för en skolenhet."""
        return f"""Sammanställ en profil för skolenheten {skolenhetskod}.

1. Om du fått ett namn: hitta skolenhetskoden med `skolverket_search_school_units` (name=...).
2. Registeruppgifter: `skolverket_get_school_unit` (status, skolformer, huvudman, adress, kommunkod).
3. Huvudman: `skolverket_get_organizer` (organisationsnummer från steg 2).
4. Utbud och statistik: `skolverket_pe_get_school_unit`, `skolverket_pe_school_unit_statistics` och vid
   gymnasieskola `skolverket_pe_school_unit_education_events`; Skolenkäten via `skolverket_pe_school_unit_surveys`.
5. Sätt in i sammanhang: kommunens nivå och riket (`skolverket_pe_national_statistics`).
Ange källa och läsår/period för varje uppgift."""

    @server.completion()
    async def complete(
        ref: PromptReference | ResourceTemplateReference,
        argument: CompletionArgument,
        context: CompletionContext | None,
    ) -> Completion | None:
        if isinstance(ref, PromptReference) and argument.name in REGION_ARGUMENTS:
            return complete_region(argument.value)
        if isinstance(ref, ResourceTemplateReference) and ref.uri == "fuzzy://codes/{name}" and argument.name == "name":
            return complete_code_list(argument.value)
        return None
