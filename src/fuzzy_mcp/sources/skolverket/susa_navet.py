# SPDX-FileCopyrightText: 2026 Deniz Özer
#
# SPDX-License-Identifier: MIT

"""Skolverket Susa-navet (EMIL 3): utbildningstillfällen, utbildningar och utbildningsanordnare.

Base: https://api.skolverket.se/susa-navet + ``/emil3`` (OpenAPI "Skolverket - Susa-navets öppna API" 3.0.2,
later revised to 3.0.3/3.0.4). GET only, JSON only, no authentication. Seven operations: ``/api-info``,
``/educationEvents[/{id}]``, ``/educationInfos[/{id}]`` and ``/educationProviders[/{id}]``.

List endpoints take ``schoolType`` (array), ``updatedSince``, ``page`` (0-based) and ``size`` (1–2000, default
100); ``/educationEvents`` also takes ``providerId``. There is no server-side text, municipality, date or
language filter, so those filters run client-side over a capped number of (cached) pages.

List items and by-id responses are wrappers ``{id, status, content}``. ``status`` is not enumerated in the spec
(only "ACTIVE" is known) and deleted records returned by ``updatedSince`` may come with ``content: null``; such
rows are flagged ``inactive`` and, when ``updatedSince`` is set, always pass the client-side filters so that delta
syncs see deletions. Rows that a client-side filter cannot test (missing location, start date, ...) are counted in
``excluded`` instead of disappearing silently. Texts are LangStrings
``{"strings": [{"lang", "value", "content"}]}`` where ``content == "HTML"`` (3.0.4+) marks HTML in ``value``; coded
values are ``{"type", "code"}`` objects.

E-mail addresses (``application.email``, ``emailAddresses``, mailto: links, e-mail keys in extensions) can be a named
person's address: with ``FUZZY_MCP_PERSONAL_DATA=off`` the by-id tools withhold them, also in ``raw``, and say so in
``notes``. Phone numbers and web addresses are always returned.
"""

import html
import json
import re
import unicodedata
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Annotated, Any
from urllib.parse import quote

from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult
from pydantic import BaseModel, Field

from ...core import READ_ONLY_OPEN, Services, clamp, compact, structured, tool_errors
from ...errors import InvalidInputError, UpstreamError
from ...reference import codes as region_codes

SOURCE = "skolverket"
BASE_URL_KEY = "susa_navet"
API_PATH = "/emil3"
CODES_URI = "fuzzy://skolverket/susa-navet/codes"

DEFAULT_LIMIT = 25
# Hosts cap tool output at ~25k tokens; 100 rows with titles cut to ROW_TITLE_CHARS stay well below that.
MAX_LIMIT = 100
ROW_TITLE_CHARS = 150
DEFAULT_PAGE_SIZE = 500
MAX_PAGE_SIZE = 2000  # API maximum for ``size``
# Raw list pages larger than this are not cached: a 2000-row page is several MB and only the matches are used.
CACHE_MAX_PAGE_SIZE = 200
DEFAULT_MAX_PAGES = 3
HARD_MAX_PAGES = 10
MAX_TEXT_CHARS = 4000
ACTIVE_STATUS = "ACTIVE"
# Id prefix (inferred pattern <kind>.<source>.<local id>) → tool that reads that kind; used for 404 hints only.
BY_ID_TOOLS = {
    "e.": "skolverket_susa_get_education_event",
    "i.": "skolverket_susa_get_education_info",
    "p.": "skolverket_susa_get_education_provider",
}
# With FUZZY_MCP_PERSONAL_DATA=off no e-mail address is returned (it can be a named person's address).
EMAIL_WITHHELD_NOTE = (
    "E-postadresser lämnas inte ut i den här installationen (FUZZY_MCP_PERSONAL_DATA=off); "
    "telefon och webbadress visas som vanligt."
)
# A bare e-mail address or a mailto: link (no ':' or '/' before '@', so URLs with user info do not match).
_EMAIL_VALUE = re.compile(r"(?i)(?:mailto:\S*|[^@\s/:<>]+@[^@\s/<>]+\.[^@\s/<>]+)")
# Ids are opaque (e.g. e.uoh.kth.dd1420.60090.20251, p.sv.68897220). The first character must be a letter or digit
# so that "." and ".." – dot segments that the HTTP client normalises away, which changes the request path – can never
# be sent; path/query separators and whitespace are rejected too. Everything else is percent-encoded.
ID_PATTERN = re.compile(r"^[^\W_][^\s/\\?#]{0,255}$")

Item = dict[str, Any]

# ---------------------------------------------------------------------------
# Code lists (spec §4). Labels marked "tolkning" are inferred, not documented.
# ---------------------------------------------------------------------------

SCHOOL_TYPES: dict[str, str] = {
    "AU": "Annan undervisning",
    "AUB": "Arbetsmarknadsutbildning",
    "FHS": "Folkhögskola",
    "FKLASS": "Förskoleklass",
    "FTH": "Fritidshem",
    "GR": "Grundskola",
    "GRAN": "Anpassad grundskola",
    "GY": "Gymnasieskola",
    "GYAN": "Anpassad gymnasieskola",
    "HS": "Högskola/universitet (inkl. polisutbildning)",
    "KKU": "Konst- och kulturutbildning",
    "KU": "Kulturskola",
    "NY": "Nationell yrkesutbildning",
    "OPPFTH": "Öppen fritidsverksamhet",
    "SAM": "Sameskola",
    "SP": "Specialskola",
    "STF": "Studieförbund",
    "VUXGR": "Kommunal vuxenutbildning på grundläggande nivå",
    "VUXGRAN": "Kommunal vuxenutbildning som anpassad utbildning på grundläggande nivå",
    "VUXGY": "Kommunal vuxenutbildning på gymnasial nivå",
    "VUXGYAN": "Kommunal vuxenutbildning som anpassad utbildning på gymnasial nivå",
    "VUXSFI": "Kommunal vuxenutbildning i svenska för invandrare",
    "YH": "Yrkeshögskola",
}
# "VUX" was only a placeholder for the komvux forms and was removed from the API in 3.0.3: expand it.
SCHOOL_TYPE_ALIASES: dict[str, tuple[str, ...]] = {"VUX": ("VUXGR", "VUXGRAN", "VUXGY", "VUXGYAN", "VUXSFI")}
# Forms known to carry data (Skolverket + JobTech whitelist); the others are listed in the spec but unverified.
POPULATED_SCHOOL_TYPES = ("HS", "YH", "AUB", "FHS", "KKU", "VUXGR", "VUXGY")

CODE_LABELS: dict[str, dict[str, str]] = {
    "C_SchoolType": {**SCHOOL_TYPES, "VUX": "Kommunal vuxenutbildning (borttagen i API-version 3.0.3)"},
    "C_Body": {
        "kommunal": "Kommunal huvudman",
        "region": "Region",
        "statlig": "Statlig huvudman",
        "enskild": "Enskild huvudman",
        "annan": "Annan huvudman",
    },
    "C_Configuration": {"kurs": "Kurs", "kurspaket": "Kurspaket", "program": "Program"},
    "C_EducationLevel": {
        "ISCED_01": "ISCED 01 – förskola för de yngsta barnen",
        "ISCED_02": "ISCED 02 – förskola",
        "ISCED_1": "ISCED 1 – primärnivå",
        "ISCED_2": "ISCED 2 – lägre sekundärnivå",
        "ISCED_3": "ISCED 3 – gymnasial nivå",
        "ISCED_4": "ISCED 4 – eftergymnasial, ej högskolenivå",
        "ISCED_5": "ISCED 5 – kort högskoleutbildning",
        "ISCED_6": "ISCED 6 – kandidat eller motsvarande",
        "ISCED_7": "ISCED 7 – master eller motsvarande",
        "ISCED_8": "ISCED 8 – forskarnivå",
    },
    "C_Credits": {
        "hp": "Högskolepoäng",
        "yh": "Yrkeshögskolepoäng",
        "gy": "Gymnasiepoäng",
        "fup": "Folkhögskolepoäng (tolkning)",
        "vp": "Verksamhetspoäng (tolkning)",
        "nyp": "Poäng för nationell yrkesutbildning (tolkning)",
    },
    "C_Qualification": {str(n): f"SeQF-nivå {n} (tolkning)" for n in range(1, 9)},
    "C_StudentAid": {
        "saknas": "Uppgift saknas",
        "nej": "Ger inte rätt till studiemedel",
        "ja": "Ger rätt till studiemedel",
        "A 1": "CSN avdelning A, kategori 1",
        "A 1 gr": "CSN avdelning A, kategori 1 (grundläggande nivå)",
        "A 1 gy": "CSN avdelning A, kategori 1 (gymnasial nivå)",
        "A 2": "CSN avdelning A, kategori 2",
        "A 3": "CSN avdelning A, kategori 3",
        "B 1": "CSN avdelning B, kategori 1",
        "B 2": "CSN avdelning B, kategori 2",
    },
    "C_TimeOfStudy": {
        "dag": "Dagtid",
        "eftermiddag": "Eftermiddag",
        "kväll": "Kvällstid",
        "veckoslut": "Helger",
        "blandat": "Blandad tid",
        "ingen": "Ingen fast undervisningstid",
    },
    "C_TimeType": {
        "hours": "timmar",
        "days": "dagar",
        "weeks": "veckor",
        "months": "månader",
        "years": "år",
        "semesters": "terminer",
    },
    "C_Audience": {
        "rektorer": "Rektorer",
        "lärare": "Lärare",
        "förskolelärare": "Förskollärare",
        "yrkeslärare": "Yrkeslärare",
    },
    "C_ExecutionCondition": {
        "0": "Utbildningen är etablerad men inte planerad",
        "1": "Beslutad, genomförs mellan angivet start- och slutdatum",
        "2": "Beslutad, start och slut ej fastställda – bestäms av den studerande",
        "3": "Beslutad, start och slut ej fastställda – bestäms av utbildningsanordnaren",
    },
}

SUBJECT_TYPES: dict[str, str] = {
    "UH_Subject": "Ämneskoder från UHR (universitet och högskola)",
    "AUB_Subject": "Arbetsmarknadsutbildning – koderna är SSYK-yrkeskoder",
    "FH_Subject": "Folkhögskolans ämneskoder",
    "SV_Subject": "Skolverkets ämnes- och kurskoder, t.ex. MODG1000",
    "F_Subject": "Ämneskoder (odokumenterat kodsystem)",
    "C_Subject_SUN": "Svensk utbildningsnomenklatur (SUN)",
    "C_Subject_ISCED97": "ISCED 97 ämnesområden",
    "C_Subject_ISCED2013": "ISCED-F 2013 ämnesområden",
}


def codes_document() -> dict[str, Any]:
    """The static code-list document served as ``fuzzy://skolverket/susa-navet/codes``."""

    def values(type_name: str) -> list[dict[str, str]]:
        return [{"code": code, "label": label} for code, label in CODE_LABELS[type_name].items()]

    school_types = [
        {"code": code, "label": label, "populated": code in POPULATED_SCHOOL_TYPES}
        for code, label in SCHOOL_TYPES.items()
    ]
    return {
        "source": "Skolverket, Susa-navets öppna API (EMIL 3, SS 10700:2024), OpenAPI 3.0.2 och versionshistorik "
        "till 3.0.4",
        "code_format": 'Kodade värden är objekt {"type": "<C_...>", "code": "<värde>"}; type är kodlistans namn. '
        "Okända värden kan förekomma.",
        "lists": {
            "C_SchoolType": {
                "description": "Skolform (parametern school_type/schoolType och EducationInfo.type). populated = "
                "känd att innehålla data.",
                "values": school_types,
                "note": "VUX togs bort i version 3.0.3 och tolkas här som VUXGR, VUXGRAN, VUXGY, VUXGYAN, VUXSFI. "
                "Gymnasieskola och grundskola finns i listan men förväntas sakna data.",
            },
            "C_Body": {
                "description": "Huvudmannatyp (EducationProvider.responsibleBody.type)",
                "values": values("C_Body"),
            },
            "C_Configuration": {
                "description": "Utbildningens upplägg (EducationInfo.configuration)",
                "values": values("C_Configuration"),
            },
            "C_EducationLevel": {
                "description": "Utbildningsnivå enligt ISCED 2011 (EducationInfo.educationLevels)",
                "values": values("C_EducationLevel"),
                "note": "YH_EducationLevel och UH_EducationLevel har fria, odokumenterade koder.",
            },
            "C_Credits": {"description": "Poängsystem (EducationInfo.credits.system)", "values": values("C_Credits")},
            "C_Orientation": {
                "description": "Inriktning (EducationInfo.orientation)",
                "values": [{"code": c, "label": "Odokumenterad betydelse"} for c in ("1", "2", "3")],
            },
            "C_Qualification": {
                "description": "Kvalifikationsnivå (EducationInfo.qualificationLevel), sannolikt SeQF/EQF-nivå",
                "values": values("C_Qualification"),
            },
            "C_StudentAid": {
                "description": "Studiemedelsrätt (EducationInfo.eligibleForStudentAid); A/B = avdelning i CSN:s "
                "regelverk",
                "values": values("C_StudentAid"),
            },
            "C_TimeOfStudy": {
                "description": "Undervisningstid (EducationEvent.timeOfStudy)",
                "values": values("C_TimeOfStudy"),
                "note": "Värden från EMIL 2-manualen; fri sträng i EMIL 3, ej verifierad.",
            },
            "C_TimeType": {"description": "Tidsenhet för omfattning (extent.unit)", "values": values("C_TimeType")},
            "C_Audience": {
                "description": "Målgrupp för Skolverkets kompetensutvecklingskurser",
                "values": values("C_Audience"),
            },
            "C_ExecutionCondition": {
                "description": "Genomförandevillkor för start- och slutdatum (EducationEvent.execution.condition)",
                "values": values("C_ExecutionCondition"),
            },
            "C_SchoolYear": {
                "description": "Årskurser som anordnaren erbjuder (EducationProvider.years)",
                "values": [{"code": str(n), "label": f"Årskurs {n}"} for n in range(11)],
            },
            "SemesterType": {
                "description": "Termin i UH-tillägg",
                "values": [{"code": "vt", "label": "Vårtermin"}, {"code": "ht", "label": "Hösttermin"}],
            },
            "subject_types": {
                "description": "Kodsystem för EducationInfo.subjects[].type (koderna valideras inte)",
                "values": [{"code": k, "label": v} for k, v in SUBJECT_TYPES.items()],
            },
        },
        "identifiers": "Id har formen <typ>.<källa>.<lokalt id>: i = utbildning (EducationInfo), e = "
        "utbildningstillfälle (EducationEvent), p = anordnare (EducationProvider); källa t.ex. uoh (UHR), sv "
        "(Skolverket), af (Arbetsförmedlingen). Exempel: e.uoh.kth.dd1420.60090.20251, p.sv.68897220. Jämför id "
        "skiftlägesokänsligt.",
        "languages": "Språkkoder är ISO 639-2 med tre bokstäver, t.ex. swe och eng.",
        "municipality_codes": "Address.areaCode är SCB:s fyrsiffriga kommunkod (t.ex. 0180 Stockholm); de två "
        "första siffrorna är länskoden.",
        "wrapper_status": "Varje post är {id, status, content}. Endast status ACTIVE är känd; borttagna poster "
        "(via updatedSince) kan ha annan status och content = null.",
    }


# ---------------------------------------------------------------------------
# Output models
# ---------------------------------------------------------------------------


class SusaCode(BaseModel):
    type: str | None = None
    code: str | None = None
    label: str | None = None


class SusaAddress(BaseModel):
    municipality_code: str | None = Field(default=None, description="Kommunkod (areaCode, 4 siffror)")
    town: str | None = None
    street_address: str | None = None
    postal_code: str | None = None
    post_box: str | None = None
    country: str | None = None
    organization: str | None = None
    department: str | None = None
    study_location: str | None = None
    wgs84: str | None = None
    sweref99: str | None = None


class ExcludedCounts(BaseModel):
    """Rows that could not be tested against an active client-side filter because data was missing."""

    no_content: int | None = Field(
        default=None,
        description="Poster utan innehåll (borttagna/inaktiva). Med updated_since tas de i stället med som inactive.",
    )
    no_location: int | None = Field(default=None, description="Saknar adress med kommunkod (kommun-/länsfilter)")
    no_start_date: int | None = Field(
        default=None, description="Saknar startdatum, t.ex. genomförandevillkor 2/3 (start_from/start_to)"
    )
    no_language: int | None = Field(default=None, description="Saknar undervisningsspråk (language)")
    no_title: int | None = Field(default=None, description="Saknar titel/namn att söka i (text)")


class SearchCoverage(BaseModel):
    filters: dict[str, Any] = Field(default_factory=dict)
    total_upstream: int | None = Field(
        default=None, description="Antal poster som matchar serverfiltren (school_type, provider_id, updated_since)"
    )
    scanned: int = Field(default=0, description="Antal poster som genomsöktes klientside")
    pages_scanned: int = 0
    page_size: int = 0
    start_page: int = 0
    skip_matches: int | None = Field(default=None, description="Antal träffar på start_page som hoppades över")
    next_page: int | None = Field(
        default=None, description="Sida att fortsätta med (start_page); saknas när sista sidan är genomsökt"
    )
    next_skip_matches: int | None = Field(
        default=None,
        description="Skicka som skip_matches tillsammans med start_page=next_page: så många träffar på den sidan har "
        "redan returnerats",
    )
    complete_scan: bool = Field(
        default=False, description="Hela mängden (från sida 0 till sista sidan) genomsöktes i detta anrop"
    )
    matched: int = Field(
        default=0, description="Antal träffar bland de genomsökta posterna (utom de skip_matches första)"
    )
    returned: int = 0
    truncated: bool = False
    excluded: ExcludedCounts | None = Field(
        default=None, description="Poster som inte kunde prövas mot klientfiltren för att uppgifter saknas"
    )
    hint: str | None = None


class EventRow(BaseModel):
    id: str
    education_id: str | None = None
    provider_ids: list[str] | None = None
    title: str | None = None
    start: str | None = None
    end: str | None = None
    municipality_codes: list[str] | None = None
    towns: list[str] | None = None
    pace_percent: float | None = None
    distance: bool | None = None
    languages: list[str] | None = None
    application_last: str | None = None
    cancelled: bool | None = None
    url: str | None = None
    status: str | None = None
    inactive: bool | None = None


class EventSearchResult(SearchCoverage):
    events: list[EventRow] = Field(default_factory=list)


class InfoRow(BaseModel):
    id: str
    code: str | None = None
    title: str | None = None
    school_type: str | None = None
    configuration: str | None = None
    credits: float | None = None
    credits_system: str | None = None
    education_levels: list[str] | None = None
    is_vocational: bool | None = None
    url: str | None = None
    status: str | None = None
    inactive: bool | None = None


class InfoSearchResult(SearchCoverage):
    infos: list[InfoRow] = Field(default_factory=list)


class ProviderRow(BaseModel):
    id: str
    name: str | None = None
    organisation_number: str | None = None
    responsible_body_type: str | None = None
    municipality_codes: list[str] | None = None
    towns: list[str] | None = None
    url: str | None = None
    status: str | None = None
    inactive: bool | None = None


class ProviderSearchResult(SearchCoverage):
    providers: list[ProviderRow] = Field(default_factory=list)


class DistanceDetails(BaseModel):
    mandatory_sessions: int | None = None
    optional_sessions: int | None = None
    mandatory_remote_sessions: int | None = None
    optional_remote_sessions: int | None = None
    description: str | None = None


class FeeInfo(BaseModel):
    total_amount: float | None = None
    currency: str | None = None
    first_installment: float | None = None
    condition: str | None = None


class ApplicationInfo(BaseModel):
    code: str | None = None
    first: str | None = None
    last: str | None = None
    continuous: bool | None = None
    url: str | None = None
    email: str | None = None
    instruction: str | None = None
    address: SusaAddress | None = None


class EventDetail(BaseModel):
    id: str
    status: str | None = None
    inactive: bool | None = None
    education_id: str | None = Field(
        default=None, description="Id för utbildningen (skolverket_susa_get_education_info)"
    )
    provider_ids: list[str] = Field(default_factory=list)
    examining_body_ids: list[str] | None = None
    orderer_ids: list[str] | None = None
    title: str | None = None
    description: str | None = None
    url: str | None = None
    start: str | None = None
    end: str | None = None
    execution_condition: SusaCode | None = None
    cancelled: bool | None = None
    languages: list[str] | None = None
    locations: list[SusaAddress] | None = None
    pace_percent: float | None = None
    pace_individual: bool | None = None
    pace_note: str | None = None
    time_of_study: SusaCode | None = None
    distance: bool | None = None
    distance_details: DistanceDetails | None = None
    places: int | None = None
    apprenticeship: bool | None = None
    fees: list[FeeInfo] | None = None
    application: ApplicationInfo | None = None
    keywords: list[str] | None = None
    extensions: list[dict[str, Any]] | None = None
    last_edited: str | None = None
    expires: str | None = None
    notes: list[str] | None = None
    raw: dict[str, Any] | None = None


class CreditsInfo(BaseModel):
    value: float | None = None
    min: float | None = None
    system: SusaCode | None = None


class ExtentInfo(BaseModel):
    length: int | None = None
    max: int | None = None
    unit: SusaCode | None = None
    note: str | None = None


class InfoDetail(BaseModel):
    id: str
    status: str | None = None
    inactive: bool | None = None
    code: str | None = None
    education_base: str | None = None
    title: str | None = None
    description: str | None = None
    url: str | None = None
    school_type: SusaCode | None = None
    education_levels: list[SusaCode] | None = None
    orientation: SusaCode | None = None
    audience: SusaCode | None = None
    configuration: SusaCode | None = None
    subjects: list[SusaCode] | None = None
    is_vocational: bool | None = None
    result_is_degree: bool | None = None
    degrees: list[str] | None = None
    qualification_level: SusaCode | None = None
    credits: CreditsInfo | None = None
    extent: ExtentInfo | None = None
    eligibility: str | None = None
    recommended_prior_knowledge: str | None = None
    eligible_for_student_aid: SusaCode | None = None
    extensions: list[dict[str, Any]] | None = None
    last_edited: str | None = None
    expires: str | None = None
    notes: list[str] | None = None
    raw: dict[str, Any] | None = None


class PhoneInfo(BaseModel):
    number: str | None = None
    function: str | None = None


class ResponsibleBodyInfo(BaseModel):
    type: SusaCode | None = None
    name: str | None = None


class ProviderDetail(BaseModel):
    id: str
    status: str | None = None
    inactive: bool | None = None
    name: str | None = None
    description: str | None = None
    url: str | None = None
    organisation_number: str | None = None
    responsible_body: ResponsibleBodyInfo | None = None
    owner: str | None = None
    contact_address: SusaAddress | None = None
    visit_addresses: list[SusaAddress] | None = None
    email_addresses: list[str] | None = None
    phones: list[PhoneInfo] | None = None
    school_years: list[str] | None = None
    extensions: list[dict[str, Any]] | None = None
    last_edited: str | None = None
    expires: str | None = None
    notes: list[str] | None = None
    raw: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Tolerant parsing helpers
# ---------------------------------------------------------------------------

SWEDISH = ("swe", "sv", "sve")
ENGLISH = ("eng", "en")
# Markers that may appear in LangStringNode.content (3.0.4 added "HTML"); anything else there is legacy text.
_CONTENT_MARKERS = frozenset({"HTML", "TEXT", "PLAIN", "MARKDOWN"})
_HTML_TAG = re.compile(
    r"<\s*/?\s*(?:p|br|div|span|li|ul|ol|a|b|i|u|em|strong|h[1-6]|table|tr|td|th|small|sup|sub)\b[^<>]*>", re.I
)
_BLOCK_TAG = re.compile(r"<\s*(?:br|/p|/div|/li|/h[1-6]|/tr|/ul|/ol)\b[^<>]*>", re.I)
_LI_TAG = re.compile(r"<\s*li\b[^<>]*>", re.I)
_ANY_TAG = re.compile(r"<[^<>]+>")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATETIME = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?$")


def as_list(value: Any) -> list[Any]:
    """Since 3.0.1 multi-valued fields are arrays, but tolerate a single object (dict-or-list)."""
    if value is None:
        return []
    if isinstance(value, list):
        return [v for v in value if v is not None]
    return [value]


def as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _str(value: Any) -> str | None:
    if value is None or isinstance(value, (dict, list)):
        return None
    text = str(value).strip()
    return text or None


def _float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int | None:
    number = _float(value)
    return int(number) if number is not None else None


def _bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    return None


def _unique(values: Iterable[str | None]) -> list[str]:
    out: list[str] = []
    for value in values:
        if value and value not in out:
            out.append(value)
    return out


def _nonempty(values: list[Any]) -> list[Any] | None:
    return values or None


def strip_html(text: str) -> str:
    text = _BLOCK_TAG.sub("\n", text)
    text = _LI_TAG.sub("\n- ", text)
    text = _ANY_TAG.sub("", text)
    text = html.unescape(text)
    lines = (" ".join(line.split()) for line in text.splitlines())
    return "\n".join(line for line in lines if line).strip()


def _truncate(text: str | None, limit: int | None) -> str | None:
    if text is None or limit is None or len(text) <= limit:
        return text
    return text[:limit].rstrip() + " …[förkortad]"


def lang_nodes(value: Any) -> list[dict[str, Any]]:
    """Nodes of a LangString. Tolerates ``strings`` as dict or list, bare strings and lists of LangStrings."""
    if value is None:
        return []
    if isinstance(value, str):
        return [{"value": value}]
    if isinstance(value, list):
        return [node for item in value for node in lang_nodes(item)]
    if isinstance(value, dict):
        if "strings" in value:
            return [node for item in as_list(value["strings"]) for node in lang_nodes(item)]
        if "value" in value or "lang" in value or "content" in value:
            return [value]
    return []


def _node_text(node: dict[str, Any]) -> str | None:
    value = node.get("value")
    content = node.get("content")
    if value is None and isinstance(content, str) and content.strip().upper() not in _CONTENT_MARKERS:
        value = content  # the legacy EMIL 2 rendering kept the text in ``content``
    if value is None or isinstance(value, (dict, list)):
        return None
    text = str(value)
    is_html = isinstance(content, str) and content.strip().upper() == "HTML"
    # Before 3.0.4 HTML was not flagged, so also strip when the value clearly contains HTML tags.
    text = strip_html(text) if is_html or _HTML_TAG.search(text) else text.strip()
    return text or None


def _pick(
    nodes: list[dict[str, Any]], prefer: tuple[str, ...], reader: Callable[[dict[str, Any]], str | None]
) -> str | None:
    candidates = [(str(node.get("lang") or "").strip().lower(), reader(node)) for node in nodes]
    candidates = [(lang, text) for lang, text in candidates if text]
    for wanted in prefer:
        for lang, text in candidates:
            if lang == wanted:
                return text
    for wanted in SWEDISH:
        for lang, text in candidates:
            if lang == wanted:
                return text
    return candidates[0][1] if candidates else None


def text_of(value: Any, prefer: tuple[str, ...] = SWEDISH, limit: int | None = None) -> str | None:
    """Text of a LangString: the preferred language (Swedish by default), else the first non-empty node."""
    return _truncate(_pick(lang_nodes(value), prefer, _node_text), limit)


def all_texts(value: Any) -> list[str]:
    return [text for node in lang_nodes(value) if (text := _node_text(node))]


def texts_of(value: Any, prefer: tuple[str, ...] = SWEDISH) -> list[str]:
    """Every text of a multi-valued LangString such as ``keywords`` or ``degrees`` (the spec types both as ONE
    LangString with one node per value): the nodes in the preferred language, else Swedish, else all languages.
    Nodes without ``lang`` are always kept. A list of LangStrings is tolerated as well (defensive)."""
    nodes = [
        (str(node.get("lang") or "").strip().lower(), text) for node in lang_nodes(value) if (text := _node_text(node))
    ]
    for wanted in (*prefer, *SWEDISH):
        if any(lang == wanted for lang, _ in nodes):
            return _unique(text for lang, text in nodes if lang in (wanted, ""))
    return _unique(text for _, text in nodes)


def _url_nodes(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, str):
        return [{"value": value}]
    if isinstance(value, list):
        return [node for item in value for node in _url_nodes(item)]
    if isinstance(value, dict):
        if "urls" in value:
            return [node for item in as_list(value["urls"]) for node in _url_nodes(item)]
        if "value" in value:
            return [value]
    return []


def _url_value(node: dict[str, Any]) -> str | None:
    url = _str(node.get("value"))
    if url and "://" not in url and not url.lower().startswith("mailto:"):
        url = "https://" + url.lstrip("/")  # some values lack a scheme
    return url


def url_of(value: Any, prefer: tuple[str, ...] = SWEDISH) -> str | None:
    return _pick(_url_nodes(value), prefer, _url_value)


def code_of(value: Any, default_type: str | None = None) -> SusaCode | None:
    """``{type, code}`` object (also tolerates a bare code string). Unknown values are kept without label."""
    if value is None:
        return None
    if isinstance(value, dict):
        type_name = _str(value.get("type")) or default_type
        code = _str(value.get("code"))
    else:
        type_name, code = default_type, _str(value)
    if code is None and type_name is None:
        return None
    label = CODE_LABELS.get(type_name or "", {}).get(code or "") if code else None
    return SusaCode(type=type_name, code=code, label=label)


def _code_value(value: Any) -> str | None:
    if isinstance(value, dict):
        return _str(value.get("code"))
    return _str(value)


def area_code(value: Any) -> str | None:
    code = _str(value)
    if code and code.isdigit() and len(code) == 3:
        code = code.zfill(4)  # leading zero lost when sent as a number
    return code


def address_of(value: Any, prefer: tuple[str, ...]) -> SusaAddress | None:
    data = as_dict(value)
    if not data:
        return None
    post_box = data.get("postBox")
    address = SusaAddress(
        municipality_code=area_code(data.get("areaCode")),
        town=_str(data.get("town")),
        street_address=_str(data.get("streetAddress")),
        postal_code=_str(data.get("postalCode")),
        post_box=_str(as_list(post_box)[0]) if as_list(post_box) else None,
        country=_str(data.get("country")),
        organization=text_of(data.get("organization"), prefer),
        department=text_of(data.get("department"), prefer),
        study_location=text_of(data.get("studyLocation"), prefer),
        wgs84=_str(data.get("wgs84")),
        sweref99=_str(data.get("sweref99")),
    )
    return address if address.model_dump(exclude_none=True) else None


def _area_codes(addresses: Iterable[Any]) -> list[str]:
    return _unique(area_code(as_dict(a).get("areaCode")) for a in addresses)


def _towns(addresses: Iterable[Any]) -> list[str]:
    return _unique(_str(as_dict(a).get("town")) for a in addresses)


def _id_list(value: Any) -> list[str]:
    return _unique(_str(v) for v in as_list(value))


def simplify(value: Any, prefer: tuple[str, ...]) -> Any:
    """Generic compaction for extension objects: LangStrings → text, LangUrls → URL, None/empty dropped.
    Upstream keys are kept as-is because extension schemas vary (and ProviderExtension is undocumented)."""
    if isinstance(value, dict):
        keys = set(value)
        if keys and keys <= {"strings"}:
            return text_of(value, prefer)
        if keys and keys <= {"urls"}:
            return url_of(value, prefer)
        return compact({key: simplify(item, prefer) for key, item in value.items()})
    if isinstance(value, list):
        return [simplify(item, prefer) for item in value]
    return value


@dataclass
class Unwrapped:
    id: str | None
    status: str | None
    content: dict[str, Any] | None

    @property
    def inactive(self) -> bool:
        if self.content is None:
            return True
        return self.status is not None and self.status.strip().upper() != ACTIVE_STATUS

    def row_status(self) -> tuple[str | None, bool | None]:
        """(status, inactive) for compact rows: only set when the record is not ACTIVE."""
        if not self.inactive:
            return None, None
        return self.status or "content saknas", True

    def identifier(self, fallback: str = "") -> str:
        return _str(as_dict(self.content).get("identifier")) or self.id or fallback


def unwrap(item: Any) -> Unwrapped:
    """Split a ``{id, status, content}`` wrapper; also accepts a bare domain object (defensive)."""
    if not isinstance(item, dict):
        return Unwrapped(None, None, None)
    if "content" in item or "status" in item:
        content = item.get("content")
        return Unwrapped(_str(item.get("id")), _str(item.get("status")), content if isinstance(content, dict) else None)
    if "identifier" in item:
        return Unwrapped(_str(item.get("identifier")), None, item)
    return Unwrapped(_str(item.get("id")), None, None)


def same_id(a: str | None, b: str | None) -> bool:
    """Ids are opaque but compared case-insensitively (consumers lower-case them before matching)."""
    return a is not None and b is not None and a.strip().casefold() == b.strip().casefold()


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


def _split_values(value: str | list[str] | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("["):
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, list):
                return _split_values([str(v) for v in parsed])
        return [part.strip() for part in re.split(r"[,;\s]+", text) if part.strip()]
    return [part for item in value for part in _split_values(str(item))]


def normalize_school_types(value: str | list[str] | None) -> list[str]:
    out: list[str] = []
    for raw in _split_values(value):
        code = raw.upper()
        expanded = SCHOOL_TYPE_ALIASES.get(code, (code,))
        for item in expanded:
            if item not in SCHOOL_TYPES:
                raise InvalidInputError(
                    f"Okänd skolform {raw!r}. Giltiga koder: {', '.join(SCHOOL_TYPES)} "
                    f"(med data: {', '.join(POPULATED_SCHOOL_TYPES)})"
                )
            if item not in out:
                out.append(item)
    return out


def _municipality_items(value: str | list[str] | None) -> list[str]:
    # Like _split_values, but names may contain spaces ("Upplands Väsby"), so only "0180 2281" splits on blanks.
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("["):
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, list):
                return _municipality_items([str(v) for v in parsed])
        parts = [part.strip() for part in re.split(r"[,;]", text) if part.strip()]
        return [p for part in parts for p in (part.split() if re.fullmatch(r"[\d\s]+", part) else [part])]
    return [part for item in value for part in _municipality_items(str(item))]


def normalize_municipality_codes(value: str | list[str] | None) -> list[str]:
    """Four-digit kommunkod, or a municipality name resolved through the bundled SCB region list."""
    out: list[str] = []
    for item in _municipality_items(value):
        if item.isdigit():
            if len(item) not in (3, 4):
                raise InvalidInputError(f"Kommunkoden {item!r} ska ha fyra siffror, t.ex. 0180 (Stockholm)")
            code = item.zfill(4)
        else:
            hits = region_codes.lookup_region(item, kind="kommun", limit=3)
            if not hits or hits[0].get("likhet", 0) < 0.9:
                suggestions = ", ".join(f"{h['kod']} {h['namn']}" for h in hits) or "inga"
                raise InvalidInputError(
                    f"Kunde inte tolka {item!r} som en kommun. Ange kommunkod (4 siffror). Förslag: {suggestions}"
                )
            code = str(hits[0]["kod"])
        if code not in out:
            out.append(code)
    return out


def normalize_county_codes(value: str | list[str] | None) -> list[str]:
    out: list[str] = []
    for item in _split_values(value):
        if not item.isdigit() or len(item) not in (1, 2):
            raise InvalidInputError(f"Länskoden {item!r} ska ha två siffror, t.ex. 22 (Västernorrlands län)")
        code = item.zfill(2)
        if code not in out:
            out.append(code)
    return out


def normalize_updated_since(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    text = value.strip()
    try:
        if _DATE.match(text):
            date.fromisoformat(text)
            return text
        if _DATETIME.match(text):
            text = text.replace(" ", "T")
            if text.count(":") == 1:
                text += ":00"
            datetime.fromisoformat(text)
            return text
    except ValueError:
        pass
    raise InvalidInputError(
        f"Ogiltigt updated_since {value!r}: ange ÅÅÅÅ-MM-DD eller ÅÅÅÅ-MM-DDTHH:MM:SS (svensk lokal tid, utan tidszon)"
    )


def normalize_date(value: str | None, name: str) -> str | None:
    if value is None or not value.strip():
        return None
    text = value.strip()
    try:
        if _DATE.match(text):
            date.fromisoformat(text)
            return text
    except ValueError:
        pass
    raise InvalidInputError(f"Ogiltigt datum för {name}: {value!r} (ange ÅÅÅÅ-MM-DD)")


LANGUAGE_ALIASES = {
    "sv": "swe",
    "sve": "swe",
    "svenska": "swe",
    "swedish": "swe",
    "en": "eng",
    "engelska": "eng",
    "english": "eng",
}


def normalize_language(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    text = value.strip().lower()
    text = LANGUAGE_ALIASES.get(text, text)
    if not re.fullmatch(r"[a-z]{3}", text):
        raise InvalidInputError(
            f"Ogiltig språkkod {value!r}: ange ISO 639-2 med tre bokstäver, t.ex. 'swe' eller 'eng'"
        )
    return text


def fold(text: str) -> str:
    """Case- and diacritic-insensitive form used for client-side text search."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def search_terms(text: str | None) -> list[str]:
    return fold(text).split() if text and text.strip() else []


def matches_terms(texts: Iterable[str], terms: list[str]) -> bool:
    haystack = fold(" ".join(texts))
    return all(term in haystack for term in terms)


def _event_date(content: dict[str, Any], key: str) -> str | None:
    value = _str(as_dict(content.get("execution")).get(key))
    return value[:10] if value else None


def _has_distance(content: dict[str, Any]) -> bool:
    # Presence of ``distance`` marks a distance course (Sundsvall: present → Distance, absent → Classroom).
    distance = content.get("distance")
    return distance is not None and distance is not False


# ---------------------------------------------------------------------------
# Row and detail builders
# ---------------------------------------------------------------------------


def event_row(item: Item, prefer: tuple[str, ...]) -> EventRow:
    wrapped = unwrap(item)
    content = wrapped.content or {}
    status, inactive = wrapped.row_status()
    locations = as_list(content.get("locations"))
    pace = as_dict(content.get("paceOfStudy"))
    cancelled = _bool(content.get("isCancelled"))
    return EventRow(
        id=wrapped.identifier(),
        education_id=_str(content.get("education")),
        provider_ids=_nonempty(_id_list(content.get("providers"))),
        title=text_of(content.get("title"), prefer, ROW_TITLE_CHARS),
        start=_event_date(content, "start"),
        end=_event_date(content, "end"),
        municipality_codes=_nonempty(_area_codes(locations)),
        towns=_nonempty(_towns(locations)),
        pace_percent=_float(pace.get("percentage")),
        distance=_has_distance(content) if wrapped.content is not None else None,
        languages=_nonempty(_unique(_str(v) for v in as_list(content.get("languageOfInstructions")))),
        application_last=_str(as_dict(content.get("application")).get("last")),
        cancelled=True if cancelled else None,
        url=url_of(content.get("url"), prefer),
        status=status,
        inactive=inactive,
    )


def info_row(item: Item, prefer: tuple[str, ...]) -> InfoRow:
    wrapped = unwrap(item)
    content = wrapped.content or {}
    status, inactive = wrapped.row_status()
    credits = as_dict(content.get("credits"))
    return InfoRow(
        id=wrapped.identifier(),
        code=_str(content.get("code")),
        title=text_of(content.get("title"), prefer, ROW_TITLE_CHARS),
        school_type=_code_value(content.get("type")),
        configuration=_code_value(content.get("configuration")),
        credits=_float(credits.get("credits")),
        credits_system=_code_value(credits.get("system")),
        education_levels=_nonempty(_unique(_code_value(v) for v in as_list(content.get("educationLevels")))),
        is_vocational=_bool(content.get("isVocational")),
        url=url_of(content.get("url"), prefer),
        status=status,
        inactive=inactive,
    )


def _provider_addresses(content: dict[str, Any]) -> list[Any]:
    return [content.get("contactAddress"), *as_list(content.get("visitAddresses"))]


def provider_row(item: Item, prefer: tuple[str, ...]) -> ProviderRow:
    wrapped = unwrap(item)
    content = wrapped.content or {}
    status, inactive = wrapped.row_status()
    addresses = [a for a in _provider_addresses(content) if a]
    return ProviderRow(
        id=wrapped.identifier(),
        name=text_of(content.get("name"), prefer, ROW_TITLE_CHARS),
        organisation_number=_str(content.get("organisationNumber")),
        responsible_body_type=_code_value(as_dict(content.get("responsibleBody")).get("type")),
        municipality_codes=_nonempty(_area_codes(addresses)),
        towns=_nonempty(_towns(addresses)),
        url=url_of(content.get("url"), prefer),
        status=status,
        inactive=inactive,
    )


def _extensions(content: dict[str, Any], prefer: tuple[str, ...]) -> list[dict[str, Any]] | None:
    out = [simplify(ext, prefer) for ext in as_list(content.get("extensions")) if isinstance(ext, dict)]
    return _nonempty([ext for ext in out if ext])


def is_email_key(key: Any) -> bool:
    """Keys holding e-mail addresses: ``email`` (application), ``emailAddresses``/``emailAddress`` (provider) and
    any e-mail key in extensions."""
    return isinstance(key, str) and "email" in key.casefold()


def is_email_value(value: Any) -> bool:
    return isinstance(value, str) and _EMAIL_VALUE.fullmatch(value.strip()) is not None


def contains_email(value: Any) -> bool:
    """True when the record holds an e-mail address (under an e-mail key or as a bare/mailto value)."""
    if isinstance(value, dict):
        return any((is_email_key(k) and v not in (None, "", [], {})) or contains_email(v) for k, v in value.items())
    if isinstance(value, list):
        return any(contains_email(v) for v in value)
    return is_email_value(value)


def without_emails(value: Any) -> Any:
    """Deep copy without e-mail keys and e-mail values (bare addresses, mailto: links), wherever they occur.
    Never mutates the (cached) input."""
    if isinstance(value, dict):
        return {k: without_emails(v) for k, v in value.items() if not (is_email_key(k) or is_email_value(v))}
    if isinstance(value, list):
        return [without_emails(v) for v in value if not is_email_value(v)]
    return value


def withhold_emails(data: Item, emails: bool) -> tuple[Item, list[str]]:
    """The record to flatten (and to return as ``raw``) plus notes: unchanged when ``emails`` is allowed, otherwise
    without e-mail addresses and with a note when there was one (FUZZY_MCP_PERSONAL_DATA=off)."""
    if emails or not contains_email(data):
        return data, []
    return without_emails(data), [EMAIL_WITHHELD_NOTE]


def _id_notes(requested: str, wrapped: Unwrapped) -> list[str] | None:
    notes: list[str] = []
    returned = wrapped.identifier()
    if returned and not same_id(returned, requested):
        notes.append(f"API:t returnerade id {returned!r} för förfrågan {requested!r}")
    if wrapped.inactive:
        notes.append(
            f"Posten är inte aktiv (status {wrapped.status or 'saknas'}"
            + (", content saknas" if wrapped.content is None else "")
            + ") – den kan vara borttagen."
        )
    return notes or None


def event_detail(
    data: Item, requested: str, prefer: tuple[str, ...], include_raw: bool, emails: bool = True
) -> EventDetail:
    data, withheld = withhold_emails(data, emails)
    wrapped = unwrap(data)
    content = wrapped.content or {}
    pace = as_dict(content.get("paceOfStudy"))
    distance = content.get("distance")
    distance_data = as_dict(distance)
    application = as_dict(content.get("application"))
    execution = as_dict(content.get("execution"))
    fees = [
        FeeInfo(
            total_amount=_float(as_dict(fee).get("totalAmount")),
            currency=_str(as_dict(fee).get("currency")),
            first_installment=_float(as_dict(fee).get("firstInstallment")),
            condition=text_of(as_dict(fee).get("condition"), prefer),
        )
        for fee in as_list(content.get("fees"))
        if isinstance(fee, dict)
    ]
    application_info = (
        ApplicationInfo(
            code=_str(application.get("code")),
            first=_str(application.get("first")),
            last=_str(application.get("last")),
            continuous=_bool(application.get("continuous")),
            url=url_of(application.get("url"), prefer),
            email=_str(application.get("email")),
            instruction=text_of(application.get("instruction"), prefer, MAX_TEXT_CHARS),
            address=address_of(application.get("address"), prefer),
        )
        if application
        else None
    )
    locations = [a for loc in as_list(content.get("locations")) if (a := address_of(loc, prefer))]
    cancelled = _bool(content.get("isCancelled"))
    return EventDetail(
        id=wrapped.identifier(requested),
        status=wrapped.status,
        inactive=True if wrapped.inactive else None,
        education_id=_str(content.get("education")),
        provider_ids=_id_list(content.get("providers")),
        examining_body_ids=_nonempty(_id_list(content.get("examiningBodies"))),
        orderer_ids=_nonempty(_id_list(content.get("orderers"))),
        title=text_of(content.get("title"), prefer),
        description=text_of(content.get("description"), prefer, MAX_TEXT_CHARS),
        url=url_of(content.get("url"), prefer),
        start=_event_date(content, "start"),
        end=_event_date(content, "end"),
        execution_condition=code_of(execution.get("condition"), "C_ExecutionCondition"),
        cancelled=cancelled,
        languages=_nonempty(_unique(_str(v) for v in as_list(content.get("languageOfInstructions")))),
        locations=_nonempty(locations),
        pace_percent=_float(pace.get("percentage")),
        pace_individual=_bool(pace.get("individual")),
        pace_note=text_of(pace.get("note"), prefer),
        time_of_study=code_of(content.get("timeOfStudy"), "C_TimeOfStudy"),
        distance=_has_distance(content) if wrapped.content is not None else None,
        distance_details=DistanceDetails(
            mandatory_sessions=_int(distance_data.get("noOfMandatorySessions")),
            optional_sessions=_int(distance_data.get("noOfOptionalSessions")),
            mandatory_remote_sessions=_int(distance_data.get("noOfMandatoryRemoteSessions")),
            optional_remote_sessions=_int(distance_data.get("noOfOptionalRemoteSessions")),
            description=text_of(distance_data.get("description"), prefer, MAX_TEXT_CHARS),
        )
        if distance_data
        else None,
        places=_int(content.get("places")),
        apprenticeship=_bool(content.get("isApprenticeship")),
        fees=_nonempty(fees),
        application=application_info,
        keywords=_nonempty(texts_of(content.get("keywords"), prefer)),
        extensions=_extensions(content, prefer),
        last_edited=_str(content.get("lastEdited")),
        expires=_str(content.get("expires")),
        notes=_nonempty([*(_id_notes(requested, wrapped) or []), *withheld]),
        raw=data if include_raw else None,
    )


def info_detail(
    data: Item, requested: str, prefer: tuple[str, ...], include_raw: bool, emails: bool = True
) -> InfoDetail:
    data, withheld = withhold_emails(data, emails)
    wrapped = unwrap(data)
    content = wrapped.content or {}
    credits = as_dict(content.get("credits"))
    extent = as_dict(content.get("extent"))
    return InfoDetail(
        id=wrapped.identifier(requested),
        status=wrapped.status,
        inactive=True if wrapped.inactive else None,
        code=_str(content.get("code")),
        education_base=_str(content.get("educationBase")),
        title=text_of(content.get("title"), prefer),
        description=text_of(content.get("description"), prefer, MAX_TEXT_CHARS),
        url=url_of(content.get("url"), prefer),
        school_type=code_of(content.get("type"), "C_SchoolType"),
        education_levels=_nonempty([c for v in as_list(content.get("educationLevels")) if (c := code_of(v))]),
        orientation=code_of(content.get("orientation"), "C_Orientation"),
        audience=code_of(content.get("audience"), "C_Audience"),
        configuration=code_of(content.get("configuration"), "C_Configuration"),
        # The spec's required list says ``subject`` but the property is ``subjects``: accept both.
        subjects=_nonempty([c for v in as_list(content.get("subjects", content.get("subject"))) if (c := code_of(v))]),
        is_vocational=_bool(content.get("isVocational")),
        result_is_degree=_bool(content.get("resultIsDegree")),
        degrees=_nonempty(texts_of(content.get("degrees"), prefer)),
        qualification_level=code_of(content.get("qualificationLevel"), "C_Qualification"),
        credits=CreditsInfo(
            value=_float(credits.get("credits")),
            min=_float(credits.get("min")),
            system=code_of(credits.get("system"), "C_Credits"),
        )
        if credits
        else None,
        extent=ExtentInfo(
            length=_int(extent.get("length")),
            max=_int(extent.get("max")),
            unit=code_of(extent.get("unit"), "C_TimeType"),
            note=text_of(extent.get("note"), prefer),
        )
        if extent
        else None,
        eligibility=text_of(content.get("eligibility"), prefer, MAX_TEXT_CHARS),
        recommended_prior_knowledge=text_of(content.get("recommendedPriorKnowledge"), prefer, MAX_TEXT_CHARS),
        eligible_for_student_aid=code_of(content.get("eligibleForStudentAid"), "C_StudentAid"),
        extensions=_extensions(content, prefer),
        last_edited=_str(content.get("lastEdited")),
        expires=_str(content.get("expires")),
        notes=_nonempty([*(_id_notes(requested, wrapped) or []), *withheld]),
        raw=data if include_raw else None,
    )


def provider_detail(
    data: Item, requested: str, prefer: tuple[str, ...], include_raw: bool, emails: bool = True
) -> ProviderDetail:
    data, withheld = withhold_emails(data, emails)
    wrapped = unwrap(data)
    content = wrapped.content or {}
    body = as_dict(content.get("responsibleBody"))
    phones = [
        PhoneInfo(
            number=text_of(as_dict(p).get("number"), prefer), function=text_of(as_dict(p).get("function"), prefer)
        )
        for p in as_list(content.get("phones"))
    ]
    visit = [a for v in as_list(content.get("visitAddresses")) if (a := address_of(v, prefer))]
    return ProviderDetail(
        id=wrapped.identifier(requested),
        status=wrapped.status,
        inactive=True if wrapped.inactive else None,
        name=text_of(content.get("name"), prefer),
        description=text_of(content.get("description"), prefer, MAX_TEXT_CHARS),
        url=url_of(content.get("url"), prefer),
        organisation_number=_str(content.get("organisationNumber")),
        responsible_body=ResponsibleBodyInfo(
            type=code_of(body.get("type"), "C_Body"), name=text_of(body.get("name"), prefer)
        )
        if body
        else None,
        owner=text_of(content.get("owner"), prefer),
        contact_address=address_of(content.get("contactAddress"), prefer),
        visit_addresses=_nonempty(visit),
        # The spec's required list says ``emailAddress`` but the property is ``emailAddresses``: accept both.
        email_addresses=_nonempty(_id_list(content.get("emailAddresses", content.get("emailAddress")))),
        phones=_nonempty([p for p in phones if p.number or p.function]),
        school_years=_nonempty(_unique(_code_value(y) for y in as_list(content.get("years")))),
        extensions=_extensions(content, prefer),
        last_edited=_str(content.get("lastEdited")),
        expires=_str(content.get("expires")),
        notes=_nonempty([*(_id_notes(requested, wrapped) or []), *withheld]),
        raw=data if include_raw else None,
    )


# ---------------------------------------------------------------------------
# Client-side filters
# ---------------------------------------------------------------------------

# Verdicts of a row check. Rows that cannot be tested because data is missing get a reason instead (the keys of
# ExcludedCounts) and are counted, so that a filtered result never silently drops them.
MATCH = "match"
MATCH_INACTIVE = "match_inactive"  # deleted/inactive row passed through because updated_since is set
NO_MATCH = "no_match"
NO_CONTENT = "no_content"
NO_LOCATION = "no_location"
NO_START_DATE = "no_start_date"
NO_LANGUAGE = "no_language"
NO_TITLE = "no_title"
EXCLUSION_LABELS = {
    NO_CONTENT: "utan innehåll (borttagna/inaktiva)",
    NO_LOCATION: "utan adress med kommunkod",
    NO_START_DATE: "utan startdatum",
    NO_LANGUAGE: "utan undervisningsspråk",
    NO_TITLE: "utan titel/namn",
}

Check = Callable[[Item], str]


@dataclass
class ClientFilters:
    terms: list[str] = field(default_factory=list)
    municipalities: list[str] = field(default_factory=list)
    counties: list[str] = field(default_factory=list)
    start_from: str | None = None
    start_to: str | None = None
    distance_only: bool = False
    language: str | None = None
    # With updated_since the caller is syncing changes: deleted/inactive rows must reach them whatever the filters.
    include_inactive: bool = False

    @property
    def active(self) -> bool:
        return bool(self.terms or self.area or self.start_from or self.start_to or self.distance_only or self.language)

    @property
    def area(self) -> bool:
        return bool(self.municipalities or self.counties)

    def area_verdict(self, codes: list[str]) -> str | None:
        """None when the area filter is off or passes, else NO_MATCH or NO_LOCATION."""
        if not self.area:
            return None
        if not codes:
            return NO_LOCATION
        if self.municipalities and not set(codes) & set(self.municipalities):
            return NO_MATCH
        if self.counties and not any(code[:2] in self.counties for code in codes):
            return NO_MATCH
        return None

    def terms_verdict(self, texts: list[str]) -> str | None:
        if not self.terms:
            return None
        texts = [text for text in texts if text]
        if not texts:
            return NO_TITLE
        return None if matches_terms(texts, self.terms) else NO_MATCH


def _content_to_check(filters: ClientFilters, item: Item) -> dict[str, Any] | str:
    """The row's content to test, or a final verdict: MATCH without client filters, MATCH_INACTIVE for an inactive
    row when updated_since is set, NO_CONTENT when there is nothing to test."""
    wrapped = unwrap(item)
    if not filters.active:
        return MATCH
    if filters.include_inactive and wrapped.inactive:
        return MATCH_INACTIVE
    return wrapped.content if wrapped.content is not None else NO_CONTENT


def _combine(verdicts: Iterable[str | None]) -> str:
    """NO_MATCH wins over missing data (the row fails anyway); otherwise the first missing-data reason, else MATCH."""
    missing: str | None = None
    for verdict in verdicts:
        if verdict == NO_MATCH:
            return NO_MATCH
        missing = missing or verdict
    return missing or MATCH


def event_check(filters: ClientFilters) -> Check:
    def check(item: Item) -> str:
        content = _content_to_check(filters, item)
        if isinstance(content, str):
            return content
        verdicts: list[str | None] = [filters.area_verdict(_area_codes(as_list(content.get("locations"))))]
        if filters.start_from or filters.start_to:
            start = _event_date(content, "start")
            if start is None:
                verdicts.append(NO_START_DATE)
            elif (filters.start_from and start < filters.start_from) or (filters.start_to and start > filters.start_to):
                return NO_MATCH
        if filters.distance_only and not _has_distance(content):
            return NO_MATCH
        if filters.language:
            languages = {str(v).strip().lower() for v in as_list(content.get("languageOfInstructions"))}
            if not languages:
                verdicts.append(NO_LANGUAGE)
            elif filters.language not in languages:
                return NO_MATCH
        verdicts.append(filters.terms_verdict([*all_texts(content.get("title")), *all_texts(content.get("keywords"))]))
        return _combine(verdicts)

    return check


def info_check(filters: ClientFilters) -> Check:
    def check(item: Item) -> str:
        content = _content_to_check(filters, item)
        if isinstance(content, str):
            return content
        return _combine([filters.terms_verdict([*all_texts(content.get("title")), _str(content.get("code")) or ""])])

    return check


def provider_check(filters: ClientFilters) -> Check:
    def check(item: Item) -> str:
        content = _content_to_check(filters, item)
        if isinstance(content, str):
            return content
        texts = [*all_texts(content.get("name")), _str(content.get("organisationNumber")) or ""]
        return _combine(
            [
                filters.area_verdict(_area_codes(a for a in _provider_addresses(content) if a)),
                filters.terms_verdict(texts),
            ]
        )

    return check


# ---------------------------------------------------------------------------
# Paging
# ---------------------------------------------------------------------------


@dataclass
class ScanResult:
    matches: list[Item]
    matched: int
    scanned: int
    pages_scanned: int
    total: int | None
    next_page: int | None
    next_skip: int
    page_size: int
    start_page: int
    skip_matches: int
    filtering: bool
    limit: int
    excluded: dict[str, int] = field(default_factory=dict)
    inactive_included: int = 0


def coverage(scan: ScanResult, filters: dict[str, Any], returned: int, noun: str) -> dict[str, Any]:
    # Pages before start_page were not scanned in this call, so only a scan from page 0 to the end is complete.
    complete = scan.next_page is None and scan.start_page == 0
    truncated = scan.matched > returned or scan.next_page is not None
    hints: list[str] = []
    if scan.next_page is not None:
        total = f" av {scan.total}" if scan.total is not None else ""
        resume = f"start_page={scan.next_page}"
        if scan.next_skip:
            resume += f", skip_matches={scan.next_skip}"
        # Without client filters the API page size is ``limit``, so page numbers only hold for the same limit.
        sizing = f"page_size={scan.page_size}" if scan.filtering else f"limit={scan.limit}"
        hint = (
            f"Genomsökte {scan.scanned}{total} {noun} ({scan.pages_scanned} sidor). Fortsätt med {resume} och "
            f"{sizing} (samma filter)"
        )
        if scan.matched > returned:
            hint += (
                f"; {scan.matched - returned} träffar på de genomsökta sidorna har inte returnerats än och kommer "
                "först i nästa anrop (eller höj limit)."
            )
        elif scan.filtering:
            hint += ", höj max_pages, eller snäva in med school_type/provider_id/updated_since."
        else:
            hint += "."
        hints.append(hint)
    elif scan.start_page > 0:
        hints.append(
            f"Sista sidan nåddes. Sidorna före start_page={scan.start_page} ingick inte i detta anrop "
            "(complete_scan är därför false)."
        )
    if scan.excluded:
        parts = ", ".join(f"{count} {EXCLUSION_LABELS[reason]}" for reason, count in scan.excluded.items())
        hint = f"{sum(scan.excluded.values())} poster kunde inte prövas mot klientfiltren och togs inte med ({parts})."
        if NO_CONTENT in scan.excluded:
            hint += " Ange updated_since för att få borttagna poster med, flaggade inactive."
        hints.append(hint)
    if scan.inactive_included:
        hints.append(
            f"{scan.inactive_included} borttagna/inaktiva poster (inactive) togs med oavsett klientfiltren eftersom "
            "updated_since är satt."
        )
    return {
        "filters": compact(filters),
        "total_upstream": scan.total,
        "scanned": scan.scanned,
        "pages_scanned": scan.pages_scanned,
        "page_size": scan.page_size,
        "start_page": scan.start_page,
        "skip_matches": scan.skip_matches or None,
        "next_page": scan.next_page,
        "next_skip_matches": scan.next_skip if scan.next_page is not None and scan.next_skip else None,
        "complete_scan": complete,
        "matched": scan.matched,
        "returned": returned,
        "truncated": truncated,
        "excluded": ExcludedCounts(**scan.excluded) if scan.excluded else None,
        "hint": " ".join(hints) or None,
    }


def _prefer(services: Services) -> tuple[str, ...]:
    return ENGLISH if services.settings.default_language == "en" else SWEDISH


@dataclass
class Paging:
    limit: int
    max_pages: int
    page_size: int
    start_page: int
    skip_matches: int


def _paging_args(limit: int, max_pages: int, page_size: int, start_page: int, skip_matches: int) -> Paging:
    if start_page < 0:
        raise InvalidInputError("start_page måste vara 0 eller större (sidnumreringen börjar på 0)")
    if skip_matches < 0:
        raise InvalidInputError("skip_matches måste vara 0 eller större")
    return Paging(
        limit=clamp(limit, 1, MAX_LIMIT),
        max_pages=clamp(max_pages, 1, HARD_MAX_PAGES),
        page_size=clamp(page_size, 1, MAX_PAGE_SIZE),
        start_page=start_page,
        skip_matches=skip_matches,
    )


def validate_id(raw_id: str | None, prefix: str) -> str:
    ident = (raw_id or "").strip()
    if not ident:
        raise InvalidInputError(f"Ange ett id, t.ex. '{prefix}uoh.kth.dd1420.60090.20251'")
    if not ID_PATTERN.match(ident):
        raise InvalidInputError(
            f"Ogiltigt id {ident!r}: id ska börja med en bokstav eller siffra och får inte innehålla blanksteg, '/', "
            f"'\\', '?' eller '#', t.ex. '{prefix}uoh.kth.dd1420.60090.20251'"
        )
    return ident


def register(server: MCPServer[Any], services: Services) -> None:
    def api_url(path: str) -> str:
        return f"{services.settings.base_url(BASE_URL_KEY)}{API_PATH}{path}"

    async def scan(
        path: str, item_key: str, params: dict[str, Any], check: Check, *, filtering: bool, paging: Paging
    ) -> ScanResult:
        """Page through a list endpoint, testing every row with ``check``.

        When ``limit`` is reached on a page that still holds unreturned matches, the scan stops there and reports
        that page as ``next_page`` together with the number of its matches already returned (``next_skip``), which
        the caller passes back as ``skip_matches``; no match is ever skipped by following next_page."""
        limit = paging.limit
        # Without client-side filters every row matches, so one API page of exactly ``limit`` rows is enough.
        size = paging.page_size if filtering else limit
        use_cache = size <= CACHE_MAX_PAGE_SIZE
        page = paging.start_page
        skip = paging.skip_matches
        matches: list[Item] = []
        matched = scanned = pages_scanned = inactive_included = 0
        excluded: dict[str, int] = {}
        total: int | None = None
        next_page: int | None = None
        next_skip = 0
        while True:
            response = await services.http.get_json(
                SOURCE, api_url(path), params={**params, "page": page, "size": size}, use_cache=use_cache
            )
            data = response.data
            if not isinstance(data, dict):
                raise UpstreamError(SOURCE, "Oväntat svar från Susa-navet (inte ett JSON-objekt)", url=response.url)
            items = [item for item in as_list(data.get(item_key)) if isinstance(item, dict)]
            meta = as_dict(data.get("page"))
            total = _int(meta.get("totalElements"))
            total_pages = _int(meta.get("totalPages"))
            pages_scanned += 1
            scanned += len(items)
            page_hits = 0  # matches on this page, including the ``skip`` ones returned by an earlier call
            overflow_at: int | None = None
            for item in items:
                verdict = check(item)
                if verdict == NO_MATCH:
                    continue
                if verdict not in (MATCH, MATCH_INACTIVE):
                    excluded[verdict] = excluded.get(verdict, 0) + 1
                    continue
                page_hits += 1
                if page_hits <= skip:
                    continue
                matched += 1
                if len(matches) < limit:
                    matches.append(item)
                    inactive_included += verdict == MATCH_INACTIVE
                elif overflow_at is None:
                    overflow_at = page_hits - 1
            skip = 0
            if overflow_at is not None:
                # Resume on this same page after the matches already returned.
                next_page, next_skip = page, overflow_at
                break
            page += 1
            # ``page``/``totalPages`` drive paging; ``links`` (rel values undocumented) are ignored.
            exhausted = (
                not items
                or (total_pages is not None and page >= total_pages)
                or (total_pages is None and len(items) < size)
            )
            if exhausted:
                break
            if len(matches) >= limit or pages_scanned >= paging.max_pages:
                next_page = page
                break
        if total is None and next_page is None and paging.start_page == 0:
            total = scanned
        return ScanResult(
            matches=matches,
            matched=matched,
            scanned=scanned,
            pages_scanned=pages_scanned,
            total=total,
            next_page=next_page,
            next_skip=next_skip,
            page_size=size,
            start_page=paging.start_page,
            skip_matches=paging.skip_matches,
            filtering=filtering,
            limit=limit,
            excluded=excluded,
            inactive_included=inactive_included,
        )

    async def fetch_by_id(path: str, raw_id: str, noun: str, prefix: str) -> tuple[Item, str]:
        ident = validate_id(raw_id, prefix)
        try:
            response = await services.http.get_json(SOURCE, api_url(f"{path}/{quote(ident, safe='')}"))
        except UpstreamError as exc:
            if exc.status == 404:
                hint = ""
                other = ident[:2].lower()
                if other in BY_ID_TOOLS and other != prefix:
                    hint = f" Id:t börjar med {ident[:2]!r} – prova {BY_ID_TOOLS[other]}."
                raise UpstreamError(
                    SOURCE, f"Hittade inget {noun} med id {ident!r}.{hint}", status=404, url=exc.url, detail=exc.detail
                ) from exc
            raise
        data = response.data
        # A by-id answer is a {id, status, content} wrapper (or, defensively, a bare object with ``identifier``);
        # anything else (e.g. a list envelope) must not be reported as an invented inactive record.
        if not isinstance(data, dict) or not {"id", "status", "content", "identifier"} & set(data):
            raise UpstreamError(
                SOURCE, f"Oväntat svar från Susa-navet för {noun} {ident!r} (ingen id/status/content)", url=response.url
            )
        return data, ident

    school_type_field = Field(
        description="Skolform(er) (schoolType), t.ex. 'YH', 'HS', 'FHS', 'AUB', 'KKU', 'VUXGY', 'VUXGR', 'VUXSFI'. "
        "Lista eller kommaseparerat. 'VUX' tolkas som alla komvux-former. Se fuzzy://skolverket/susa-navet/codes."
    )
    updated_since_field = Field(
        description="Endast poster som är nya, ändrade eller borttagna från och med detta datum/tid (updatedSince), "
        "'ÅÅÅÅ-MM-DD' eller 'ÅÅÅÅ-MM-DDTHH:MM:SS' (svensk lokal tid). Borttagna/inaktiva poster tas då alltid med, "
        "flaggade inactive, även när klientfilter (text, kommun m.m.) används."
    )
    limit_field = Field(description=f"Max antal rader i svaret (1–{MAX_LIMIT})")
    max_pages_field = Field(description="Max antal sidor att hämta från API:t per anrop (1–10, standard 3)")
    page_size_field = Field(
        description="Poster per API-sida vid klientfiltrering (1–2000, standard 500). Utan klientfilter är "
        "sidstorleken alltid limit."
    )
    start_page_field = Field(
        description="Första API-sida (0-baserad). Använd next_page från ett tidigare svar för att fortsätta."
    )
    skip_matches_field = Field(
        description="Antal träffar på start_page som redan returnerats och ska hoppas över. Skicka next_skip_matches "
        "från ett tidigare svar tillsammans med start_page=next_page, annars tappas träffar."
    )
    municipality_field = Field(
        description="Kommunkod(er) (4 siffror, t.ex. '2281' Sundsvall) eller kommunnamn – filtreras klientside på "
        "adressernas areaCode. Poster utan kommunkod räknas i excluded.no_location."
    )
    county_field = Field(
        description="Länskod(er) (2 siffror, t.ex. '22'), klientside på areaCode:s två första siffror. Poster utan "
        "kommunkod räknas i excluded.no_location."
    )
    include_raw_field = Field(description="Ta med hela det ursprungliga API-svaret i fältet raw")

    @server.tool(
        name="skolverket_susa_search_education_events",
        title="Susa-navet: sök utbildningstillfällen",
        annotations=READ_ONLY_OPEN,
    )
    @tool_errors
    async def skolverket_susa_search_education_events(
        school_type: Annotated[str | list[str] | None, school_type_field] = None,
        provider_id: Annotated[
            str | None,
            Field(description="Anordnarens id (providerId), t.ex. 'p.uoh.kth' – filtreras i API:t"),
        ] = None,
        updated_since: Annotated[str | None, updated_since_field] = None,
        municipality_code: Annotated[str | list[str] | None, municipality_field] = None,
        county_code: Annotated[str | list[str] | None, county_field] = None,
        text: Annotated[
            str | None,
            Field(
                description="Fritext (klientside): alla ord måste finnas i tillfällets titel eller nyckelord "
                "(skiftläges- och accentokänsligt)"
            ),
        ] = None,
        start_from: Annotated[
            str | None,
            Field(
                description="Endast tillfällen som startar detta datum eller senare (ÅÅÅÅ-MM-DD). Tillfällen utan "
                "startdatum (t.ex. genomförandevillkor 2/3, flexibel start) räknas i excluded.no_start_date."
            ),
        ] = None,
        start_to: Annotated[
            str | None,
            Field(
                description="Endast tillfällen som startar senast detta datum (ÅÅÅÅ-MM-DD). Tillfällen utan "
                "startdatum räknas i excluded.no_start_date."
            ),
        ] = None,
        distance_only: Annotated[bool, Field(description="Endast distansutbildningar (distance education)")] = False,
        language: Annotated[
            str | None,
            Field(description="Undervisningsspråk (ISO 639-2), t.ex. 'swe' eller 'eng'; 'sv'/'en' accepteras"),
        ] = None,
        limit: Annotated[int, limit_field] = DEFAULT_LIMIT,
        max_pages: Annotated[int, max_pages_field] = DEFAULT_MAX_PAGES,
        page_size: Annotated[int, page_size_field] = DEFAULT_PAGE_SIZE,
        start_page: Annotated[int, start_page_field] = 0,
        skip_matches: Annotated[int, skip_matches_field] = 0,
    ) -> Annotated[CallToolResult, EventSearchResult]:
        """Sök utbildningstillfällen (education events, kurs-/programomgångar) i Skolverkets Susa-navet: högskola,
        yrkeshögskola, folkhögskola, arbetsmarknadsutbildning, konst- och kulturutbildning och komvux.

        API:t filtrerar bara på skolform, anordnare och updated_since. Kommun/län, fritext, startdatum, distans och
        språk filtreras klientside över högst max_pages sidor – svaret anger total_upstream, scanned, complete_scan
        och next_page/next_skip_matches (fortsätt med start_page/skip_matches) så att du vet hur stor del som
        genomsökts. Poster som saknar uppgiften ett filter kräver räknas i excluded i stället för att tyst
        försvinna; med updated_since tas borttagna poster alltid med (inactive). Snäva in med school_type (och
        provider_id) för bästa täckning. Varje rad har tillfällets id, utbildningens id (education_id →
        skolverket_susa_get_education_info) och anordnarnas id (provider_ids → skolverket_susa_get_education_provider).
        Tillfällen saknar ibland titel; sök då utbildningar med skolverket_susa_search_education_infos."""
        paging = _paging_args(limit, max_pages, page_size, start_page, skip_matches)
        school_types = normalize_school_types(school_type)
        since = normalize_updated_since(updated_since)
        provider = provider_id.strip() if provider_id and provider_id.strip() else None
        filters = ClientFilters(
            terms=search_terms(text),
            municipalities=normalize_municipality_codes(municipality_code),
            counties=normalize_county_codes(county_code),
            start_from=normalize_date(start_from, "start_from"),
            start_to=normalize_date(start_to, "start_to"),
            distance_only=distance_only,
            language=normalize_language(language),
            include_inactive=since is not None,
        )
        if filters.start_from and filters.start_to and filters.start_from > filters.start_to:
            raise InvalidInputError("start_from måste vara före eller lika med start_to")
        params: dict[str, Any] = {"schoolType": school_types or None, "providerId": provider, "updatedSince": since}
        result = await scan(
            "/educationEvents", "educationEvents", params, event_check(filters), filtering=filters.active, paging=paging
        )
        prefer = _prefer(services)
        rows = [event_row(item, prefer) for item in result.matches]
        applied = {
            "school_type": school_types,
            "provider_id": provider,
            "updated_since": since,
            "municipality_codes": filters.municipalities,
            "county_codes": filters.counties,
            "text": text.strip() if text else None,
            "start_from": filters.start_from,
            "start_to": filters.start_to,
            "distance_only": filters.distance_only or None,
            "language": filters.language,
        }
        return structured(EventSearchResult(events=rows, **coverage(result, applied, len(rows), "tillfällen")))

    @server.tool(
        name="skolverket_susa_search_education_infos",
        title="Susa-navet: sök utbildningar",
        annotations=READ_ONLY_OPEN,
    )
    @tool_errors
    async def skolverket_susa_search_education_infos(
        school_type: Annotated[str | list[str] | None, school_type_field] = None,
        text: Annotated[
            str | None,
            Field(
                description="Fritext (klientside): alla ord måste finnas i utbildningens titel eller kurskod "
                "(skiftläges- och accentokänsligt)"
            ),
        ] = None,
        updated_since: Annotated[str | None, updated_since_field] = None,
        limit: Annotated[int, limit_field] = DEFAULT_LIMIT,
        max_pages: Annotated[int, max_pages_field] = DEFAULT_MAX_PAGES,
        page_size: Annotated[int, page_size_field] = DEFAULT_PAGE_SIZE,
        start_page: Annotated[int, start_page_field] = 0,
        skip_matches: Annotated[int, skip_matches_field] = 0,
    ) -> Annotated[CallToolResult, InfoSearchResult]:
        """Sök utbildningar (education infos: kurser, program, kurspaket) i Susa-navet – titel, kurskod, skolform,
        poäng (hp/yh-poäng), upplägg och nivå. Använd id:t i skolverket_susa_get_education_info för beskrivning,
        behörighet, ämnen och studiemedelsrätt.

        API:t filtrerar bara på skolform och updated_since; fritexten matchas klientside över högst max_pages sidor.
        Svaret anger total_upstream, scanned, complete_scan, excluded och next_page/next_skip_matches (fortsätt med
        start_page/skip_matches) för täckningen. Med updated_since tas borttagna poster alltid med (inactive).
        Tillfällen (datum, ort) finns i skolverket_susa_search_education_events."""
        paging = _paging_args(limit, max_pages, page_size, start_page, skip_matches)
        school_types = normalize_school_types(school_type)
        since = normalize_updated_since(updated_since)
        filters = ClientFilters(terms=search_terms(text), include_inactive=since is not None)
        result = await scan(
            "/educationInfos",
            "educationInfos",
            {"schoolType": school_types or None, "updatedSince": since},
            info_check(filters),
            filtering=filters.active,
            paging=paging,
        )
        prefer = _prefer(services)
        rows = [info_row(item, prefer) for item in result.matches]
        applied = {"school_type": school_types, "updated_since": since, "text": text.strip() if text else None}
        return structured(InfoSearchResult(infos=rows, **coverage(result, applied, len(rows), "utbildningar")))

    @server.tool(
        name="skolverket_susa_search_education_providers",
        title="Susa-navet: sök utbildningsanordnare",
        annotations=READ_ONLY_OPEN,
    )
    @tool_errors
    async def skolverket_susa_search_education_providers(
        school_type: Annotated[str | list[str] | None, school_type_field] = None,
        text: Annotated[
            str | None,
            Field(
                description="Fritext (klientside): alla ord måste finnas i anordnarens namn eller organisationsnummer "
                "(skiftläges- och accentokänsligt)"
            ),
        ] = None,
        municipality_code: Annotated[str | list[str] | None, municipality_field] = None,
        county_code: Annotated[str | list[str] | None, county_field] = None,
        updated_since: Annotated[str | None, updated_since_field] = None,
        limit: Annotated[int, limit_field] = DEFAULT_LIMIT,
        max_pages: Annotated[int, max_pages_field] = DEFAULT_MAX_PAGES,
        page_size: Annotated[int, page_size_field] = DEFAULT_PAGE_SIZE,
        start_page: Annotated[int, start_page_field] = 0,
        skip_matches: Annotated[int, skip_matches_field] = 0,
    ) -> Annotated[CallToolResult, ProviderSearchResult]:
        """Sök utbildningsanordnare (education providers: lärosäten, folkhögskolor, YH-anordnare, kommuner m.fl.) i
        Susa-navet. Raderna har id, namn, organisationsnummer, huvudmannatyp, kommunkoder och webbadress.

        API:t filtrerar bara på skolform och updated_since; namn och kommun/län filtreras klientside över högst
        max_pages sidor (täckningen anges i svaret, fortsätt med start_page/skip_matches). Anordnare utan kommunkod
        räknas i excluded; med updated_since tas borttagna poster alltid med (inactive). Anordnarens
        utbildningstillfällen hämtas med skolverket_susa_search_education_events(provider_id=...)."""
        paging = _paging_args(limit, max_pages, page_size, start_page, skip_matches)
        school_types = normalize_school_types(school_type)
        since = normalize_updated_since(updated_since)
        filters = ClientFilters(
            terms=search_terms(text),
            municipalities=normalize_municipality_codes(municipality_code),
            counties=normalize_county_codes(county_code),
            include_inactive=since is not None,
        )
        result = await scan(
            "/educationProviders",
            "educationProviders",
            {"schoolType": school_types or None, "updatedSince": since},
            provider_check(filters),
            filtering=filters.active,
            paging=paging,
        )
        prefer = _prefer(services)
        rows = [provider_row(item, prefer) for item in result.matches]
        applied = {
            "school_type": school_types,
            "updated_since": since,
            "text": text.strip() if text else None,
            "municipality_codes": filters.municipalities,
            "county_codes": filters.counties,
        }
        return structured(ProviderSearchResult(providers=rows, **coverage(result, applied, len(rows), "anordnare")))

    @server.tool(
        name="skolverket_susa_get_education_event",
        title="Susa-navet: utbildningstillfälle",
        annotations=READ_ONLY_OPEN,
    )
    @tool_errors
    async def skolverket_susa_get_education_event(
        event_id: Annotated[str, Field(description="Utbildningstillfällets id, t.ex. 'e.uoh.kth.dd1420.60090.20251'")],
        include_raw: Annotated[bool, include_raw_field] = False,
    ) -> Annotated[CallToolResult, EventDetail]:
        """Hämta ett utbildningstillfälle (education event) i Susa-navet: titel, beskrivning, start/slut,
        studietakt, distans, orter med kommunkod, undervisningsspråk, platser, avgifter, ansökningsperiod och
        UH-/komvux-tillägg (t.ex. studieavgift, startvecka). Länkar till utbildningen (education_id) och
        anordnarna (provider_ids). E-post lämnas inte ut när personuppgifter är avstängda i installationen."""
        data, ident = await fetch_by_id("/educationEvents", event_id, "utbildningstillfälle", "e.")
        return structured(
            event_detail(data, ident, _prefer(services), include_raw, services.settings.allow_personal_data)
        )

    @server.tool(
        name="skolverket_susa_get_education_info",
        title="Susa-navet: utbildning",
        annotations=READ_ONLY_OPEN,
    )
    @tool_errors
    async def skolverket_susa_get_education_info(
        education_id: Annotated[
            str, Field(description="Utbildningens id (education_id), t.ex. 'i.uoh.uu.1te686.14483.20242'")
        ],
        include_raw: Annotated[bool, include_raw_field] = False,
    ) -> Annotated[CallToolResult, InfoDetail]:
        """Hämta en utbildning (education info: kurs, program eller kurspaket) i Susa-navet: titel, kurskod,
        beskrivning, skolform, nivå (ISCED), poäng, omfattning, ämnen, examen, behörighet, förkunskaper,
        studiemedelsrätt (CSN) och kvalifikationsnivå. Kodade värden ges som {type, code, label}."""
        data, ident = await fetch_by_id("/educationInfos", education_id, "utbildning", "i.")
        return structured(
            info_detail(data, ident, _prefer(services), include_raw, services.settings.allow_personal_data)
        )

    @server.tool(
        name="skolverket_susa_get_education_provider",
        title="Susa-navet: utbildningsanordnare",
        annotations=READ_ONLY_OPEN,
    )
    @tool_errors
    async def skolverket_susa_get_education_provider(
        provider_id: Annotated[str, Field(description="Anordnarens id, t.ex. 'p.uoh.kth' eller 'p.sv.68897220'")],
        include_raw: Annotated[bool, include_raw_field] = False,
    ) -> Annotated[CallToolResult, ProviderDetail]:
        """Hämta en utbildningsanordnare (education provider) i Susa-navet: namn, organisationsnummer, huvudman
        (kommunal/region/statlig/enskild/annan), kontakt- och besöksadresser med kommunkod, e-post, telefon och
        webbadress. Anordnarens tillfällen: skolverket_susa_search_education_events(provider_id=...). E-post
        lämnas inte ut när personuppgifter är avstängda i installationen."""
        data, ident = await fetch_by_id("/educationProviders", provider_id, "anordnare", "p.")
        return structured(
            provider_detail(data, ident, _prefer(services), include_raw, services.settings.allow_personal_data)
        )

    @server.tool(name="skolverket_susa_api_info", title="Susa-navet: API-information", annotations=READ_ONLY_OPEN)
    @tool_errors
    async def skolverket_susa_api_info() -> dict[str, Any]:
        """Visa information om Susa-navets API (utgivare, API- och EMIL-schemaversion, releasedatum, status och
        dokumentationslänk) från /emil3/api-info. Bra för att kontrollera att API:t svarar och vilken version som
        körs."""
        response = await services.http.get_json(SOURCE, api_url("/api-info"))
        data = as_dict(response.data)
        known = {
            "apiPublisher": "publisher",
            "apiName": "name",
            "apiVersion": "api_version",
            "emilSchemaVersion": "emil_schema_version",
            "apiReleased": "released",
            "apiDocumentation": "documentation",
            "apiStatus": "status",
        }
        info: dict[str, Any] = {target: data.get(source) for source, target in known.items()}
        info["other"] = {k: v for k, v in data.items() if k not in known} or None
        info["base_url"] = api_url("")
        info["codes_resource"] = CODES_URI
        return compact(info)

    @server.resource(
        CODES_URI,
        name="skolverket-susa-navet-codes",
        title="Susa-navet: kodlistor",
        description="Kodlistor för Susa-navet (EMIL 3): skolformer, huvudmän, poängsystem, utbildningsnivåer, "
        "kvalifikationsnivå, studiemedel, inriktning, undervisningstid, terminer, genomförandevillkor m.m.",
        mime_type="application/json",
    )
    def susa_navet_codes() -> dict[str, Any]:
        return codes_document()
