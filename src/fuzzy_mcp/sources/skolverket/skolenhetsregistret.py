# SPDX-FileCopyrightText: 2026 Deniz Özer
#
# SPDX-License-Identifier: MIT

"""Skolverket – Skolenhetsregistret, öppet API v2.

Base: https://api.skolverket.se/skolenhetsregistret (config key ``skolenhetsregistret``); every path is
``/v2/...``. GET only, plain ``application/json`` (no vendor media type), no authentication, CC0 1.0.

Upstream quirks handled here (see the research contract):

* List endpoints return ``{"meta": {"extractDate"}, "data": {"type", "attributes": [...]}}`` – the rows sit
  in ``data.attributes`` – and have no paging: the whole filtered set comes back in one response, so paging
  and name search are done client-side over the (cached) list.
* Items of ``/v2/school-units`` only carry ``schoolUnitCode``, ``name`` and ``status``.
* Detail responses put the id on ``data`` (not in ``data.attributes``); a school unit's organizer is a single
  ``included`` object.
* Coordinates are strings; SWEREF 99 TM uses comma decimals (``"6562397,097"``).
* Array query parameters are sent as repeated keys (``status=AKTIV&status=VILANDE``).
* v2 has no endpoints for code lists; they are served statically from the OpenAPI enums.

Personal data: ``headMaster`` (the principal's name) and ``careOfAddress`` (c/o names, e.g. a family name) are
personuppgifter. They are left out of the flattened output and stripped from ``raw`` unless the caller opts in
with ``include_personal_data=true``. E-mail addresses (school unit, organizer, education provider) can be a person's
direct address: with ``FUZZY_MCP_PERSONAL_DATA=off`` they are withheld everywhere (flattened output and ``raw``),
otherwise they are returned as before. Phone numbers and web addresses are always returned.
"""

import copy
import json
import math
import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from typing import Annotated, Any, Literal

import anyio
from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult
from pydantic import BaseModel, Field

from ...core import READ_ONLY_OPEN, Services, clamp, personal_data, structured, tool_errors
from ...errors import InvalidInputError, UpstreamError

SOURCE = "skolverket"
BASE_URL_KEY = "skolenhetsregistret"
API_VERSION = "v2"
CODES_URI = "fuzzy://skolverket/skolenhetsregistret/codes"

DEFAULT_LIMIT = 50
# Hard row caps per list type: a full page must stay well under the ~25k-token host cap (worst case with long
# names ≈ 55 kB of compact JSON). Contract rows are ~3x and education-provider rows ~2x as wide as organizer rows.
MAX_LIMIT_SCHOOL_UNITS = 300
MAX_LIMIT_ORGANIZERS = 300
MAX_LIMIT_EDUCATION_PROVIDERS = 200
MAX_LIMIT_CONTRACTS = 150
MAX_DETAIL_ROWS = 25
DETAIL_CONCURRENCY = 4
# skolverket_school_unit_changes queries up to four registers in one call; their rows share this budget
# (compact JSON bytes of the returned items), split evenly between the requested registers.
CHANGES_DEFAULT_LIMIT = 50
CHANGES_MAX_BYTES = 60_000

ALL_STATUSES = ("AKTIV", "VILANDE", "UPPHORT", "PLANERAD")
# Upstream keys holding names of individual people (personuppgifter).
PERSONAL_DATA_KEYS = frozenset({"headMaster", "careOfAddress"})
# With FUZZY_MCP_PERSONAL_DATA=off no e-mail address is returned (it can be a person's direct address).
EMAIL_WITHHELD_NOTE = (
    "E-postadresser lämnas inte ut i den här installationen (FUZZY_MCP_PERSONAL_DATA=off); "
    "telefon och webbadress visas som vanligt."
)
# A bare e-mail address or a mailto: link (no ':' or '/' before '@', so URLs with user info do not match).
_EMAIL_VALUE = re.compile(r"(?i)(?:mailto:\S*|[^@\s/:<>]+@[^@\s/<>]+\.[^@\s/<>]+)")

# ---------------------------------------------------------------------------
# Code lists (v2 enums from the OpenAPI spec; Swedish labels)
# ---------------------------------------------------------------------------


def _codes(*entries: tuple[str, str] | tuple[str, str, str]) -> list[dict[str, str]]:
    out = []
    for entry in entries:
        item = {"code": entry[0], "label": entry[1]}
        if len(entry) > 2:
            item["description"] = entry[2]
        out.append(item)
    return out


CODE_LISTS: dict[str, dict[str, Any]] = {
    "school_unit_status": {
        "title": "Skolenhetsstatus (CodeSchoolUnitStatus)",
        "used_in": ["status (skolenhet)", "filter: status"],
        "codes": _codes(
            ("AKTIV", "Aktiv"),
            ("VILANDE", "Vilande"),
            (
                "UPPHORT",
                "Upphörd",
                "Nedlagd skolenhet. Koden stavas UPPHORT (inte UPPHÖRD). Finns i v2:s enum; att listan faktiskt "
                "returnerar sådana enheter är inte verifierat.",
            ),
            ("PLANERAD", "Planerad"),
        ),
    },
    "school_type": {
        "title": "Skolform (CodeSchoolType)",
        "used_in": ["schoolTypes", "filter: school_type"],
        "note": "Förskola ingår inte i registret; det börjar på förskoleklass (FKLASS).",
        "codes": _codes(
            ("FKLASS", "Förskoleklass"),
            ("FTH", "Fritidshem"),
            ("OPPFTH", "Öppen fritidsverksamhet"),
            ("GR", "Grundskola"),
            ("GRAN", "Anpassad grundskola", "Tidigare grundsärskola."),
            ("SP", "Specialskola"),
            ("SAM", "Sameskola"),
            ("GY", "Gymnasieskola"),
            ("GYAN", "Anpassad gymnasieskola", "Tidigare gymnasiesärskola."),
            ("VUX", "Kommunal vuxenutbildning", "Komvux; skolformsdelar i school_type_part_vux."),
        ),
    },
    "school_type_part_vux": {
        "title": "Skolformsdel inom kommunal vuxenutbildning (CodeSchoolTypePartVux)",
        "used_in": ["schoolTypeProperties.vux.schoolTypeParts", "utbildningsanordnare", "entreprenader"],
        "codes": _codes(
            ("VUXGR", "Kommunal vuxenutbildning på grundläggande nivå"),
            ("VUXGY", "Kommunal vuxenutbildning på gymnasial nivå"),
            ("VUXGRAN", "Kommunal vuxenutbildning som anpassad utbildning på grundläggande nivå"),
            ("VUXGYAN", "Kommunal vuxenutbildning som anpassad utbildning på gymnasial nivå"),
            ("VUXSFI", "Kommunal vuxenutbildning i svenska för invandrare", "Sfi."),
        ),
    },
    "school_unit_type": {
        "title": "Skolenhetstyp (CodeSchoolUnitType)",
        "used_in": ["schoolUnitType", "filter: school_unit_type"],
        "codes": _codes(
            ("SKOLENHET", "Skolenhet"),
            ("CENTRAL", "Central insamlingsenhet", "Ingen undervisning; filtrera på SKOLENHET för att utesluta."),
            ("UTLAND", "Utlandsskolenhet", "Svensk skola i utlandet."),
        ),
    },
    "orientation_type": {
        "title": "Inriktningstyp (CodeOrientationType)",
        "used_in": ["orientationType"],
        "codes": _codes(
            ("ALLMAN", "Allmän"),
            ("WALDORF", "Waldorf"),
            ("KONFESSIONELL", "Konfessionell"),
            ("RIKSINTERNAT", "Riksinternat"),
            ("INTERNATIONELL", "Internationell"),
            ("EJ_RELEVANT", "Ej relevant"),
        ),
    },
    "organizer_type": {
        "title": "Huvudmannatyp (CodeOrganizerType)",
        "used_in": ["organizerType", "filter: organizer_type"],
        "codes": _codes(
            ("KOMMUN", "Kommunal"),
            ("REGION", "Region"),
            ("STAT", "Statlig"),
            ("SAME", "Sameskolan"),
            ("ENSKILD", "Enskild", "Enskild huvudman (fristående skola)."),
            ("SPECIAL", "Specialskola"),
            ("KOMMFORB", "Kommunalförbund"),
            ("HMANUTL", "Huvudman för svensk utlandsskola"),
        ),
    },
    "legal_entity_status": {
        "title": "Status hos Skatteverket (CodeLegalEntityStatus)",
        "used_in": ["legalEntityStatus (huvudman)"],
        "codes": _codes(
            ("INKLUDERAD_SKV", "Ingår i populationen och är registrerad i Skatteverkets organisationsnummerregister"),
            ("INKLUDERAD_ANNAN", "Ingår i populationen och är registrerad på annat sätt"),
            ("EJ_LANGRE_INKLUDERAD", "Ingår inte längre i populationen"),
        ),
    },
    "company_status": {
        "title": "Status hos Bolagsverket (CodeCompanyStatus)",
        "used_in": ["companyStatus (huvudman)"],
        "codes": _codes(
            ("ALDRIG_AKTIV", "Har aldrig varit verksam"),
            ("AKTIV", "Är verksam"),
            ("EJ_LANGRE_AKTIV", "Är ej längre verksam"),
        ),
    },
    "provider_type": {
        "title": "Uppgiftslämnartyp för e-post/telefon (CodeProviderType)",
        "used_in": ["email[].providerType", "phoneNumber[].providerType"],
        "codes": _codes(
            ("SCB", "SCB:s allmänna företagsregister"),
            ("SKOLVERKET", "Skolverket"),
            ("SV", "Skolverket", "Förekommer i specifikationens exempel, saknas i enum."),
        ),
    },
    "report_type": {
        "title": "Rapporteringstyp för centrala insamlingsenheter (CodeReportType)",
        "used_in": ["reportsStudents", "reportsPersonell"],
        "codes": _codes(
            ("NEJ", "Nej"),
            ("KOMMUNAL", "Kommunal verksamhet"),
            ("ENSKILD", "Enskild verksamhet"),
        ),
    },
    "address_type": {
        "title": "Adresstyp (AddressTypeEnum)",
        "used_in": ["addresses[].type", "address.type"],
        "codes": _codes(
            ("BESOKSADRESS", "Besöksadress"),
            ("LEVERANSADRESS", "Leveransadress"),
            ("POSTADRESS", "Postadress"),
            ("UTLANDSADRESS", "Utlandsadress"),
        ),
    },
    "contract_status": {
        "title": "Status för entreprenad (fritext i API:t)",
        "used_in": ["status (entreprenad)"],
        "codes": _codes(("aktiv", "Aktiv entreprenad"), ("inaktiv", "Inaktiv entreprenad")),
    },
}

# Extra spellings accepted in filters (matched after diacritic folding).
_ALIASES: dict[str, dict[str, str]] = {
    "school_unit_status": {"nedlagd": "UPPHORT", "upphord": "UPPHORT"},
    "school_type": {
        "komvux": "VUX",
        "vuxenutbildning": "VUX",
        "grundsarskola": "GRAN",
        "gymnasiesarskola": "GYAN",
        "fsk": "FKLASS",
        "fritids": "FTH",
    },
    "organizer_type": {"fristaende": "ENSKILD", "kommunal": "KOMMUN", "statlig": "STAT", "kommunalforbund": "KOMMFORB"},
}


def fold(text: str) -> str:
    """Casefold, strip diacritics and punctuation separators, collapse spaces."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    plain = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return " ".join(re.sub(r"[-_/.,()]", " ", plain).split())


def _build_lookup() -> dict[str, dict[str, str]]:
    lookup: dict[str, dict[str, str]] = {}
    for key, spec in CODE_LISTS.items():
        table: dict[str, str] = {}
        for entry in spec["codes"]:
            table.setdefault(fold(entry["label"]), entry["code"])
        for alias, code in _ALIASES.get(key, {}).items():
            table[fold(alias)] = code
        for entry in spec["codes"]:  # codes win over labels
            table[fold(entry["code"])] = entry["code"]
        lookup[key] = table
    return lookup


_LOOKUP = _build_lookup()
_LABELS = {key: {e["code"]: e["label"] for e in spec["codes"]} for key, spec in CODE_LISTS.items()}


def label(list_key: str, code: str | None) -> str | None:
    if code is None:
        return None
    return _LABELS[list_key].get(code) or _LABELS[list_key].get(code.upper())


def as_list(value: str | list[str] | None) -> list[str]:
    """Accept a list, a single value or a comma separated string."""
    if value is None:
        return []
    items = [value] if isinstance(value, str) else list(value)
    out: list[str] = []
    for item in items:
        for part in str(item).split(","):
            part = part.strip()
            if part:
                out.append(part)
    return out


def normalize_codes(value: str | list[str] | None, list_key: str, param: str) -> list[str]:
    """Validate filter values against a code list (case-insensitive, labels and a few aliases accepted)."""
    out: list[str] = []
    table = _LOOKUP[list_key]
    for raw in as_list(value):
        code = table.get(fold(raw))
        if code is None:
            valid = ", ".join(f"{e['code']} ({e['label']})" for e in CODE_LISTS[list_key]["codes"])
            raise InvalidInputError(f"Okänt värde {raw!r} för {param}. Giltiga koder: {valid}. Se {CODES_URI}.")
        if code not in out:
            out.append(code)
    return out


def resolve_status(value: str | list[str] | None, default: tuple[str, ...]) -> list[str]:
    """``None`` → default; ``[]`` or 'ALLA'/'ALL'/'*' → all four enum values, sent explicitly.

    An unfiltered list has only been observed to contain AKTIV and VILANDE units, so "all" is never
    expressed by omitting the parameter."""
    if value is None:
        return list(default)
    items = as_list(value)
    if not items or any(fold(i) in ("alla", "all", "*") for i in items):
        return list(ALL_STATUSES)
    return normalize_codes(items, "school_unit_status", "status")


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------

# ASCII digits only: `\d` also matches full-width and Arabic-Indic digits, which would be sent upstream verbatim
# and silently match nothing.
_DATE_RE = re.compile(r"^([0-9]{4})-?([0-9]{2})-?([0-9]{2})$")


def iso_date(value: str | None, param: str) -> str | None:
    """Accept YYYY-MM-DD (spec form) or YYYYMMDD and return YYYY-MM-DD."""
    if value is None or not value.strip():
        return None
    text = value.strip()
    if "T" in text:
        text = text.split("T", 1)[0]
    match = _DATE_RE.match(text)
    try:
        if not match:
            raise ValueError(text)
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3))).isoformat()
    except ValueError as exc:
        raise InvalidInputError(f"Ogiltigt datum {value!r} för {param}: använd ÅÅÅÅ-MM-DD, t.ex. 2025-01-31") from exc


def normalize_school_unit_code(value: str, what: str = "skolenhetskod") -> str:
    text = re.sub(r"\s", "", str(value))
    if not re.fullmatch(r"[0-9]{8}", text):
        raise InvalidInputError(f"Ogiltig {what} {value!r}: ska vara exakt 8 siffror, t.ex. '43038662'")
    return text


def normalize_organization_number(value: str, param: str = "organisationsnummer") -> str:
    """Normalise 'NNNNNN-NNNN', 'NNNNNNNNNN' or '16NNNNNNNNNN' to ten digits."""
    digits = re.sub(r"[\s-]", "", str(value))
    if len(digits) == 12 and digits.startswith("16"):
        digits = digits[2:]
    if not re.fullmatch(r"[0-9]{10}", digits):
        raise InvalidInputError(
            f"Ogiltigt {param} {value!r}: ange 10 siffror med eller utan bindestreck, t.ex. '212000-0126'"
        )
    return digits


def normalize_municipality_code(value: str) -> str:
    text = re.sub(r"\s", "", str(value))
    if re.fullmatch(r"[0-9]{3}", text):  # leading zero lost, e.g. 180 → 0180
        text = "0" + text
    if not re.fullmatch(r"[0-9]{4}", text):
        raise InvalidInputError(
            f"Ogiltig kommunkod {value!r}: ska vara 4 siffror, t.ex. '0180' (Stockholm). "
            "Slå upp koder från namn med ref_lookup_region."
        )
    return text


# ---------------------------------------------------------------------------
# Parsing helpers (defensive: tolerate missing fields, dict-or-list, numbers as strings)
# ---------------------------------------------------------------------------


def text_or_none(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    if isinstance(value, int | float):
        return str(value)
    return None


def to_float(value: Any) -> float | None:
    """Coordinates come as strings; SWEREF uses comma decimals."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        number = float(value)
    elif isinstance(value, str):
        cleaned = re.sub(r"[\s ]", "", value).replace(",", ".")
        if not cleaned:
            return None
        try:
            number = float(cleaned)
        except ValueError:
            return None
    else:
        return None
    return number if math.isfinite(number) else None


def to_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "j", "ja", "1"):
            return True
        if lowered in ("false", "n", "nej", "0"):
            return False
    return None


def dict_list(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [v for v in value if isinstance(v, dict)]
    return []


def str_list(value: Any) -> list[str]:
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    return [s for s in (text_or_none(v) for v in items) if s]


def as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def postal_code(value: Any) -> str | None:
    """Upstream sends both '234 67' and '17998'; normalise to 'NNN NN'."""
    text = text_or_none(value)
    if text is None:
        return None
    digits = re.sub(r"\s", "", text)
    if re.fullmatch(r"[0-9]{5}", digits):
        return f"{digits[:3]} {digits[3:]}"
    return text


def first_text(value: Any, *keys: str) -> str | None:
    """A plain string, or the first entry of a list of strings/objects (defensive)."""
    if isinstance(value, list):
        for item in value:
            found = first_text(item, *keys)
            if found:
                return found
        return None
    if isinstance(value, dict):
        for key in keys:
            found = text_or_none(value.get(key))
            if found:
                return found
        return None
    return text_or_none(value)


# ---------------------------------------------------------------------------
# Output models
# ---------------------------------------------------------------------------


class Address(BaseModel):
    type: str | None = Field(default=None, description="BESOKSADRESS, POSTADRESS, LEVERANSADRESS, UTLANDSADRESS")
    care_of: str | None = Field(
        default=None, description="c/o-namn (personuppgift); bara med include_personal_data=true"
    )
    street: str | None = None
    postal_code: str | None = None
    locality: str | None = Field(default=None, description="Postort")
    country: str | None = None
    continent: str | None = None
    latitude: float | None = Field(default=None, description="WGS84")
    longitude: float | None = Field(default=None, description="WGS84")
    sweref99tm_n: float | None = Field(default=None, description="SWEREF 99 TM (EPSG:3006) norr")
    sweref99tm_e: float | None = Field(default=None, description="SWEREF 99 TM (EPSG:3006) öst")


class EntityMeta(BaseModel):
    extract_date: str | None = Field(default=None, description="Uttagsdatum för registret")
    created: str | None = Field(default=None, description="När entiteten skapades")
    modified: str | None = Field(default=None, description="Senaste ändring av entitetens egna attribut")


class OrganizerRef(BaseModel):
    organization_number: str | None = None
    name: str | None = None
    organizer_type: str | None = None
    organizer_type_label: str | None = None


class SchoolTypeInfo(BaseModel):
    code: str
    label: str | None = None
    grades: list[str] | None = Field(default=None, description="Årskurser (GR, GRAN, SAM, SP)")
    programmes: list[str] | None = Field(default=None, description="Programkoder (GY, GYAN), se Syllabus-API:t")
    csn_code: str | None = None
    school_type_parts: list[str] | None = Field(default=None, description="Skolformsdelar för VUX")


class SchoolUnitRow(BaseModel):
    school_unit_code: str
    name: str | None = None
    status: str | None = None
    school_unit_type: str | None = None
    school_types: list[str] | None = None
    municipality_code: str | None = None
    organizer_organization_number: str | None = None
    organizer_name: str | None = None
    city: str | None = Field(default=None, description="Postort för besöksadressen")
    latitude: float | None = None
    longitude: float | None = None


class SchoolUnitSearchResult(BaseModel):
    total: int = Field(description="Antal träffar efter alla filter")
    offset: int = 0
    limit: int = DEFAULT_LIMIT
    truncated: bool = False
    next_offset: int | None = None
    extract_date: str | None = None
    filters: dict[str, Any] = Field(default_factory=dict)
    items: list[SchoolUnitRow] = Field(default_factory=list)
    request_url: str | None = None
    notes: list[str] = Field(default_factory=list)


class SchoolUnitDetail(BaseModel):
    school_unit_code: str
    name: str | None = None
    school_name: str | None = Field(default=None, description="Namn på skolan som enheten tillhör")
    status: str | None = None
    status_label: str | None = None
    school_unit_type: str | None = None
    orientation_type: str | None = None
    municipality_code: str | None = None
    school_types: list[SchoolTypeInfo] = Field(default_factory=list)
    organizer: OrganizerRef | None = None
    head_master: str | None = Field(
        default=None, description="Rektorns namn (personuppgift); bara med include_personal_data=true"
    )
    email: str | None = Field(
        default=None,
        description="Skolenhetens e-post (kan i enstaka fall vara personlig); saknas när personuppgifter är "
        "avstängda i installationen",
    )
    phone: str | None = Field(default=None, description="Skolenhetens telefon (kan i enstaka fall vara personlig)")
    website: str | None = None
    visit_address: Address | None = None
    latitude: float | None = None
    longitude: float | None = None
    other_addresses: list[Address] = Field(default_factory=list)
    special_support_school: bool | None = Field(default=None, description="Resursskola")
    hospital_school: bool | None = Field(default=None, description="Sjukhusskola")
    reports_students: str | None = None
    reports_personnel: str | None = None
    municipal_adult_education_provided_internally: bool | None = Field(
        default=None, description="Komvux i egen regi (municipalAdultEducationProvidedInternally)"
    )
    start_date: str | None = None
    end_date: str | None = None
    as_of_date: str | None = None
    meta: EntityMeta | None = None
    self_url: str | None = None
    notes: list[str] | None = None
    raw: dict[str, Any] | None = None


class OrganizerRow(BaseModel):
    organization_number: str
    name: str | None = None
    organizer_type: str | None = None


class OrganizerSearchResult(BaseModel):
    total: int
    offset: int = 0
    limit: int = DEFAULT_LIMIT
    truncated: bool = False
    next_offset: int | None = None
    extract_date: str | None = None
    filters: dict[str, Any] = Field(default_factory=dict)
    items: list[OrganizerRow] = Field(default_factory=list)
    request_url: str | None = None


class Contact(BaseModel):
    value: str
    provider_type: str | None = Field(default=None, description="SCB eller SKOLVERKET")


class CodeName(BaseModel):
    code: str | None = None
    name: str | None = None


class ContractRef(BaseModel):
    organizer_organization_number: str | None = None
    education_provider_code: str | None = None


class OrganizerDetail(BaseModel):
    organization_number: str
    name: str | None = None
    organizer_type: str | None = None
    organizer_type_label: str | None = None
    website: str | None = None
    emails: list[Contact] = Field(default_factory=list)
    phones: list[Contact] = Field(default_factory=list)
    address: Address | None = None
    company_form: CodeName | None = None
    legal_entity_status: str | None = None
    company_status: str | None = None
    is_international: bool | None = None
    municipalities: list[CodeName] = Field(default_factory=list, description="Säteskommun (kommunkod + namn)")
    regions: list[CodeName] = Field(default_factory=list)
    school_types: list[str] = Field(default_factory=list)
    high_school_association_id: str | None = None
    school_unit_codes: list[str] = Field(default_factory=list)
    school_unit_count: int = 0
    contracts: list[ContractRef] = Field(default_factory=list)
    as_of_date: str | None = None
    meta: EntityMeta | None = None
    self_url: str | None = None
    notes: list[str] | None = None
    raw: dict[str, Any] | None = None


class EducationProviderRow(BaseModel):
    education_provider_code: str
    organization_number: str | None = None
    name: str | None = None
    folk_high_school_name: str | None = None
    grading_right: bool | None = None


class EducationProviderSearchResult(BaseModel):
    total: int
    offset: int = 0
    limit: int = DEFAULT_LIMIT
    truncated: bool = False
    next_offset: int | None = None
    extract_date: str | None = None
    filters: dict[str, Any] = Field(default_factory=dict)
    items: list[EducationProviderRow] = Field(default_factory=list)
    request_url: str | None = None


class CodeLabel(BaseModel):
    code: str
    label: str | None = None


class EducationProviderDetail(BaseModel):
    education_provider_code: str
    organization_number: str | None = Field(
        default=None,
        description="Saknas i detaljsvaret enligt specifikationen (fylls bara om API:t ändå skickar det); "
        "finns i listan, se skolverket_search_education_providers",
    )
    name: str | None = None
    folk_high_school_name: str | None = None
    address: Address | None = None
    emails: list[Contact] = Field(default_factory=list)
    phones: list[Contact] = Field(default_factory=list)
    company_form: CodeName | None = None
    grading_rights: bool | None = None
    grading_rights_from: str | None = None
    grading_rights_to: str | None = None
    school_type_parts: list[CodeLabel] = Field(default_factory=list)
    contracts: list[ContractRef] = Field(default_factory=list)
    as_of_date: str | None = None
    meta: EntityMeta | None = None
    self_url: str | None = None
    notes: list[str] | None = None
    raw: dict[str, Any] | None = None


class ContractRow(BaseModel):
    organizer_organization_number: str | None = None
    organizer_name: str | None = None
    education_provider_organization_number: str | None = None
    education_provider_name: str | None = None
    education_provider_code: str | None = None
    folk_high_school_name: str | None = None
    status: str | None = None


class ContractSearchResult(BaseModel):
    total: int
    offset: int = 0
    limit: int = DEFAULT_LIMIT
    truncated: bool = False
    next_offset: int | None = None
    extract_date: str | None = None
    filters: dict[str, Any] = Field(default_factory=dict)
    items: list[ContractRow] = Field(default_factory=list)
    request_url: str | None = None


class ContractSchoolTypePart(BaseModel):
    code: str | None = None
    label: str | None = None
    valid_from: str | None = None
    valid_to: str | None = None


class ContractDetail(BaseModel):
    organizer_organization_number: str
    education_provider_code: str
    organizer_name: str | None = None
    education_provider_organization_number: str | None = None
    education_provider_name: str | None = None
    folk_high_school_name: str | None = None
    status: str | None = None
    school_type_parts: list[ContractSchoolTypePart] = Field(default_factory=list)
    school_unit_codes: list[str] = Field(default_factory=list)
    as_of_date: str | None = None
    meta: EntityMeta | None = None
    self_url: str | None = None
    raw: dict[str, Any] | None = None


class ChangesResult(BaseModel):
    since: str
    truncated: bool = Field(default=False, description="Minst ett register har fler ändringar än som returnerats")
    school_units: SchoolUnitSearchResult | None = None
    organizers: OrganizerSearchResult | None = None
    education_providers: EducationProviderSearchResult | None = None
    contracts: ContractSearchResult | None = None
    note: str = (
        "meta_modified_after gäller från och med datumet och avser ändringar i entitetens egna attribut. "
        "Hämta detaljer med skolverket_get_school_unit / skolverket_get_organizer."
    )
    notes: list[str] = Field(default_factory=list)


ListResult = SchoolUnitSearchResult | OrganizerSearchResult | EducationProviderSearchResult | ContractSearchResult


# ---------------------------------------------------------------------------
# Flattening
# ---------------------------------------------------------------------------


def is_email_key(key: str) -> bool:
    """Upstream keys holding e-mail addresses (``email`` on school units, organizers and providers)."""
    return "email" in key.casefold()


def is_email_value(value: Any) -> bool:
    return isinstance(value, str) and _EMAIL_VALUE.fullmatch(value.strip()) is not None


def contains_email(value: Any) -> bool:
    """True when the payload holds an e-mail address (under an e-mail key or as a bare/mailto value)."""
    if isinstance(value, dict):
        return any((is_email_key(k) and v not in (None, "", [], {})) or contains_email(v) for k, v in value.items())
    if isinstance(value, list):
        return any(contains_email(v) for v in value)
    return is_email_value(value)


def redact_personal(value: Any, *, names: bool = True, emails: bool = False) -> Any:
    """Deep copy of an upstream payload without the keys that hold names of individual people
    (``headMaster``, ``careOfAddress``) when ``names``, and without e-mail keys and e-mail values when ``emails``,
    wherever they occur. Never mutates the (cached) input."""
    if isinstance(value, dict):
        return {
            k: redact_personal(v, names=names, emails=emails)
            for k, v in value.items()
            if not (names and k in PERSONAL_DATA_KEYS) and not (emails and (is_email_key(k) or is_email_value(v)))
        }
    if isinstance(value, list):
        return [redact_personal(v, names=names, emails=emails) for v in value if not (emails and is_email_value(v))]
    return value


def raw_output(payload: dict[str, Any], personal: bool, emails: bool = True) -> dict[str, Any]:
    """The upstream payload for ``raw`` (always a copy): without names of people unless personal data was opted
    in, and without e-mail addresses when ``emails`` is False (FUZZY_MCP_PERSONAL_DATA=off)."""
    if personal and emails:
        return copy.deepcopy(payload)
    return redact_personal(payload, names=not personal, emails=not emails)


def email_notes(payload: dict[str, Any], emails: bool) -> list[str] | None:
    """The note for a detail whose e-mail addresses were withheld (only when there was one to withhold)."""
    return [EMAIL_WITHHELD_NOTE] if not emails and contains_email(payload) else None


def parse_address(raw: Any, personal: bool = False) -> Address | None:
    if not isinstance(raw, dict):
        return None
    geo = as_dict(raw.get("geoCoordinates"))
    return Address(
        type=text_or_none(raw.get("type")),
        # c/o names are personuppgifter (often a family name); only on explicit opt-in.
        care_of=text_or_none(raw.get("careOfAddress")) if personal else None,
        street=text_or_none(raw.get("streetAddress")),
        postal_code=postal_code(raw.get("postalCode")),
        locality=text_or_none(raw.get("locality")),
        country=text_or_none(raw.get("country")),
        continent=text_or_none(raw.get("continent")),
        latitude=to_float(geo.get("latitude")),
        longitude=to_float(geo.get("longitude")),
        sweref99tm_n=to_float(geo.get("coordinateSweRefN")),
        sweref99tm_e=to_float(geo.get("coordinateSweRefE")),
    )


def split_addresses(
    raw: Any, personal: bool = False
) -> tuple[Address | None, list[Address], float | None, float | None]:
    """Return (visit address, other addresses, lat, lon). The visit address is the BESOKSADRESS entry,
    else the first address with coordinates, else the first address."""
    addresses = [a for a in (parse_address(x, personal) for x in dict_list(raw)) if a is not None]
    visit = next((a for a in addresses if (a.type or "").upper() == "BESOKSADRESS"), None)
    if visit is None:
        visit = next((a for a in addresses if a.latitude is not None), None)
    if visit is None and addresses:
        visit = addresses[0]
    others = [a for a in addresses if a is not visit]
    with_coords = visit if visit is not None and visit.latitude is not None else None
    if with_coords is None:
        with_coords = next((a for a in addresses if a.latitude is not None and a.longitude is not None), None)
    lat = with_coords.latitude if with_coords else None
    lon = with_coords.longitude if with_coords else None
    return visit, others, lat, lon


def parse_meta(raw: Any) -> EntityMeta | None:
    meta = as_dict(raw)
    if not meta:
        return None
    return EntityMeta(
        extract_date=text_or_none(meta.get("extractDate")),
        created=text_or_none(meta.get("created")),
        modified=text_or_none(meta.get("modified")),
    )


def parse_school_types(codes: Any, properties: Any) -> list[SchoolTypeInfo]:
    # schoolTypeProperties keys are lowercase (gr, gran, sam, sp, gy, gyan, vux). Only the unit's own
    # schoolTypes are listed; the property keys are a fallback when schoolTypes is missing.
    props = {str(k).lower(): v for k, v in as_dict(properties).items()}
    order: list[str] = []
    for code in str_list(codes) or [k.upper() for k in props]:
        if code.upper() not in order:
            order.append(code.upper())
    out = []
    for code in order:
        prop = as_dict(props.get(code.lower()))
        out.append(
            SchoolTypeInfo(
                code=code,
                label=label("school_type", code),
                grades=str_list(prop.get("grades")) or None,
                programmes=str_list(prop.get("programmes")) or None,
                csn_code=text_or_none(prop.get("csnCode")),
                school_type_parts=str_list(prop.get("schoolTypeParts")) or None,
            )
        )
    return out


def parse_included_organizer(included: Any) -> OrganizerRef | None:
    # `included` is a single object per the spec; accept a list defensively. Its `type` is
    # "organization" in the spec example but "organizer" in real-world fixtures – not relied on.
    for item in dict_list(included):
        attrs = as_dict(item.get("attributes"))
        number = text_or_none(item.get("organizationNumber")) or text_or_none(attrs.get("organizationNumber"))
        if number is None:
            href = text_or_none(as_dict(item.get("links")).get("href")) or ""
            match = re.search(r"/organizers/([0-9]{10})", href)
            number = match.group(1) if match else None
        name = text_or_none(attrs.get("displayName")) or text_or_none(item.get("displayName"))
        org_type = text_or_none(attrs.get("organizerType")) or text_or_none(item.get("organizerType"))
        if number is None and name is None:
            continue
        return OrganizerRef(
            organization_number=number,
            name=name,
            organizer_type=org_type,
            organizer_type_label=label("organizer_type", org_type),
        )
    return None


def flatten_school_unit(
    payload: dict[str, Any], code: str, personal: bool = False, emails: bool = True
) -> SchoolUnitDetail:
    """Flatten a school-unit detail. ``personal`` opts in to the principal's name and c/o names; ``emails=False``
    (FUZZY_MCP_PERSONAL_DATA=off) withholds the e-mail address."""
    data = as_dict(payload.get("data"))
    attrs = as_dict(data.get("attributes"))
    visit, others, lat, lon = split_addresses(attrs.get("addresses"), personal)
    status = text_or_none(attrs.get("status"))
    # Spec spells the start date `startdate` (lowercase d); accept `startDate` too.
    start = text_or_none(attrs.get("startdate")) or text_or_none(attrs.get("startDate"))
    return SchoolUnitDetail(
        school_unit_code=text_or_none(data.get("schoolUnitCode")) or text_or_none(attrs.get("schoolUnitCode")) or code,
        name=text_or_none(attrs.get("displayName")) or text_or_none(attrs.get("name")),
        school_name=text_or_none(attrs.get("schoolName")),
        status=status,
        status_label=label("school_unit_status", status),
        school_unit_type=text_or_none(attrs.get("schoolUnitType")),
        orientation_type=text_or_none(attrs.get("orientationType")),
        municipality_code=text_or_none(attrs.get("municipalityCode")),
        school_types=parse_school_types(attrs.get("schoolTypes"), attrs.get("schoolTypeProperties")),
        organizer=parse_included_organizer(payload.get("included")),
        # headMaster = "Namn på skolenhetens rektor": personuppgift, only on explicit opt-in.
        head_master=first_text(attrs.get("headMaster"), "name", "displayName") if personal else None,
        email=first_text(attrs.get("email"), "email") if emails else None,
        phone=first_text(attrs.get("phoneNumber"), "phone", "phoneNumber"),
        website=first_text(attrs.get("url"), "url", "href"),
        visit_address=visit,
        latitude=lat,
        longitude=lon,
        other_addresses=others,
        special_support_school=to_bool(attrs.get("specialSupportSchool")),
        hospital_school=to_bool(attrs.get("hospitalSchool")),
        reports_students=text_or_none(attrs.get("reportsStudents")),
        # Upstream spelling is `reportsPersonell` (sic); accept the corrected spelling too.
        reports_personnel=text_or_none(attrs.get("reportsPersonell")) or text_or_none(attrs.get("reportsPersonnel")),
        # SchoolUnitInfo (attributes): boolean, nullable, "Komvux i egen regi."
        municipal_adult_education_provided_internally=to_bool(attrs.get("municipalAdultEducationProvidedInternally")),
        start_date=start,
        end_date=text_or_none(attrs.get("endDate")) or text_or_none(attrs.get("enddate")),
        meta=parse_meta(payload.get("meta")),
        self_url=text_or_none(as_dict(payload.get("links")).get("href")),
        notes=email_notes(payload, emails),
    )


def parse_contacts(value: Any, key: str) -> list[Contact]:
    """Organizer/provider e-mail and phone are arrays of {providerType, email|phone}; accept strings too."""
    out: list[Contact] = []
    items = value if isinstance(value, list) else [value]
    for item in items:
        if isinstance(item, dict):
            text = text_or_none(item.get(key)) or text_or_none(item.get("value"))
            if text:
                out.append(Contact(value=text, provider_type=text_or_none(item.get("providerType"))))
        else:
            text = text_or_none(item)
            if text:
                out.append(Contact(value=text))
    return out


def parse_code_name(value: Any, code_key: str = "code") -> CodeName | None:
    item = as_dict(value)
    code = text_or_none(item.get(code_key))
    name = text_or_none(item.get("displayName")) or text_or_none(item.get("name"))
    if code is None and name is None:
        return None
    return CodeName(code=code, name=name)


def _relationship(relationships: Any, *names: str) -> dict[str, Any]:
    rels = as_dict(relationships)
    for name in names:
        if isinstance(rels.get(name), dict):
            return rels[name]
    return {}


def _related_hrefs(relation: dict[str, Any]) -> list[str]:
    links = as_dict(relation.get("links"))
    return [h for h in (text_or_none(link.get("href")) for link in dict_list(links.get("related"))) if h]


def related_school_unit_codes(relationships: Any) -> list[str]:
    # Organizer uses `schoolunit`, contract uses `schoolUnit` – read both.
    relation = _relationship(relationships, "schoolunit", "schoolUnit", "schoolunits", "schoolUnits")
    codes = [c for c in (text_or_none(d.get("schoolUnitCode")) for d in dict_list(relation.get("data"))) if c]
    if not codes:
        for href in _related_hrefs(relation):
            match = re.search(r"/school-units/([0-9]{8})(?:$|[/?])", href)
            if match:
                codes.append(match.group(1))
    return list(dict.fromkeys(codes))


def related_contracts(relationships: Any) -> list[ContractRef]:
    # Spec schema says `contract` with `data` as an array; its example says `contracts` with a single object.
    relation = _relationship(relationships, "contract", "contracts")
    refs: list[ContractRef] = []
    seen: set[tuple[str | None, str | None]] = set()
    for item in dict_list(relation.get("data")):
        key = (text_or_none(item.get("organizationNumber")), text_or_none(item.get("educationProviderCode")))
        if any(key) and key not in seen:
            seen.add(key)
            refs.append(ContractRef(organizer_organization_number=key[0], education_provider_code=key[1]))
    if not refs:
        for href in _related_hrefs(relation):
            match = re.search(r"/contracts/([0-9]{10})/([0-9]{8})", href)
            if match and (match.group(1), match.group(2)) not in seen:
                seen.add((match.group(1), match.group(2)))
                refs.append(
                    ContractRef(organizer_organization_number=match.group(1), education_provider_code=match.group(2))
                )
    return refs


def flatten_organizer(
    payload: dict[str, Any], number: str, personal: bool = False, emails: bool = True
) -> OrganizerDetail:
    data = as_dict(payload.get("data"))
    attrs = as_dict(data.get("attributes"))
    org_type = text_or_none(attrs.get("organizerType"))
    codes = related_school_unit_codes(data.get("relationships"))
    return OrganizerDetail(
        organization_number=text_or_none(data.get("organizationNumber")) or number,
        name=text_or_none(attrs.get("displayName")),
        organizer_type=org_type,
        organizer_type_label=label("organizer_type", org_type),
        website=first_text(attrs.get("url"), "url", "href"),
        emails=parse_contacts(attrs.get("email"), "email") if emails else [],
        phones=parse_contacts(attrs.get("phoneNumber"), "phone"),
        address=parse_address(attrs.get("address"), personal),
        company_form=parse_code_name(attrs.get("companyForm")),
        legal_entity_status=text_or_none(attrs.get("legalEntityStatus")),
        company_status=text_or_none(attrs.get("companyStatus")),
        is_international=to_bool(attrs.get("isInternational")),
        municipalities=[
            c for c in (parse_code_name(m, "municipalityCode") for m in dict_list(attrs.get("municipalities"))) if c
        ],
        regions=[c for c in (parse_code_name(r, "regionCode") for r in dict_list(attrs.get("regions"))) if c],
        school_types=str_list(attrs.get("schoolTypes")),
        high_school_association_id=text_or_none(attrs.get("highSchoolAssociationId")),
        school_unit_codes=codes,
        school_unit_count=len(codes),
        contracts=related_contracts(data.get("relationships")),
        meta=parse_meta(payload.get("meta")),
        self_url=text_or_none(as_dict(payload.get("links")).get("href")),
        notes=email_notes(payload, emails),
    )


def flatten_education_provider(
    payload: dict[str, Any], code: str, personal: bool = False, emails: bool = True
) -> EducationProviderDetail:
    data = as_dict(payload.get("data"))
    attrs = as_dict(data.get("attributes"))
    parts = str_list(attrs.get("schoolTypeParts"))
    return EducationProviderDetail(
        education_provider_code=text_or_none(data.get("educationProviderCode")) or code,
        # Not part of EducationProviderInfo(ResponseData) in the spec – only the list has organizationNumber.
        # Read defensively in case upstream adds it.
        organization_number=text_or_none(data.get("organizationNumber"))
        or text_or_none(attrs.get("organizationNumber")),
        name=text_or_none(attrs.get("displayName")),
        folk_high_school_name=text_or_none(attrs.get("folkHighschoolName")),
        address=parse_address(attrs.get("address"), personal),
        emails=parse_contacts(attrs.get("email"), "email") if emails else [],
        phones=parse_contacts(attrs.get("phoneNumber"), "phone"),
        company_form=parse_code_name(attrs.get("companyForm")),
        # Detail key is `gradingRights` (plural); the list uses `gradingRight`.
        grading_rights=to_bool(attrs.get("gradingRights", attrs.get("gradingRight"))),
        grading_rights_from=text_or_none(attrs.get("gradingRightsFrom")),
        grading_rights_to=text_or_none(attrs.get("gradingRightsTo")),
        school_type_parts=[CodeLabel(code=p, label=label("school_type_part_vux", p)) for p in parts],
        contracts=related_contracts(data.get("relationships")),
        meta=parse_meta(payload.get("meta")),
        self_url=text_or_none(as_dict(payload.get("links")).get("href")),
        notes=email_notes(payload, emails),
    )


def flatten_contract(payload: dict[str, Any], number: str, code: str) -> ContractDetail:
    data = as_dict(payload.get("data"))
    attrs = as_dict(data.get("attributes"))
    parts = []
    for item in attrs.get("schoolTypeParts") or []:
        if isinstance(item, dict):
            part = text_or_none(item.get("schoolTypePart"))
            parts.append(
                ContractSchoolTypePart(
                    code=part,
                    label=label("school_type_part_vux", part),
                    valid_from=text_or_none(item.get("validFrom")),
                    valid_to=text_or_none(item.get("validTo")),
                )
            )
        elif text_or_none(item):
            part = text_or_none(item)
            parts.append(ContractSchoolTypePart(code=part, label=label("school_type_part_vux", part)))
    return ContractDetail(
        organizer_organization_number=text_or_none(data.get("organizationNumber")) or number,
        education_provider_code=text_or_none(data.get("educationProviderCode")) or code,
        organizer_name=text_or_none(attrs.get("organizerName")),
        education_provider_organization_number=text_or_none(attrs.get("educationProviderOrganizationNumber")),
        education_provider_name=text_or_none(attrs.get("educationProviderName")),
        folk_high_school_name=text_or_none(attrs.get("folkHighschoolName")),
        status=text_or_none(attrs.get("status")),
        school_type_parts=parts,
        school_unit_codes=related_school_unit_codes(data.get("relationships")),
        meta=parse_meta(payload.get("meta")),
        self_url=text_or_none(as_dict(payload.get("links")).get("href")),
    )


def school_unit_row(item: dict[str, Any], municipalities: list[str], organizations: list[str]) -> SchoolUnitRow | None:
    """List items only have schoolUnitCode/name/status. Other fields are read if upstream ever adds them,
    or inferred from a single-valued server-side filter (every returned unit matches it)."""
    code = text_or_none(item.get("schoolUnitCode"))
    if code is None:
        return None
    visit, _, lat, lon = split_addresses(item.get("addresses"))
    return SchoolUnitRow(
        school_unit_code=code,
        name=text_or_none(item.get("name")) or text_or_none(item.get("displayName")),
        status=text_or_none(item.get("status")),
        school_unit_type=text_or_none(item.get("schoolUnitType")),
        school_types=str_list(item.get("schoolTypes")) or None,
        municipality_code=text_or_none(item.get("municipalityCode"))
        or (municipalities[0] if len(municipalities) == 1 else None),
        organizer_organization_number=text_or_none(item.get("organizationNumber"))
        or (organizations[0] if len(organizations) == 1 else None),
        city=visit.locality if visit else None,
        latitude=lat,
        longitude=lon,
    )


def apply_detail(row: SchoolUnitRow, detail: SchoolUnitDetail) -> None:
    row.name = row.name or detail.name
    row.status = detail.status or row.status
    row.school_unit_type = detail.school_unit_type or row.school_unit_type
    row.school_types = [t.code for t in detail.school_types] or row.school_types
    row.municipality_code = detail.municipality_code or row.municipality_code
    if detail.organizer is not None:
        row.organizer_organization_number = detail.organizer.organization_number or row.organizer_organization_number
        row.organizer_name = detail.organizer.name
    if detail.visit_address is not None:
        row.city = detail.visit_address.locality or row.city
    if detail.latitude is not None and detail.longitude is not None:
        row.latitude, row.longitude = detail.latitude, detail.longitude


# ---------------------------------------------------------------------------
# Client-side name matching and paging
# ---------------------------------------------------------------------------


class NameMatcher:
    """Case-insensitive, diacritic-tolerant match: every word of the query must occur in the name."""

    def __init__(self, query: str | None) -> None:
        self.query = fold(query) if query else ""
        self.words = self.query.split()

    def __bool__(self) -> bool:
        return bool(self.words)

    def rank(self, *names: str | None) -> int | None:
        best: int | None = None
        for name in names:
            if not name:
                continue
            folded = fold(name)
            if not all(word in folded for word in self.words):
                continue
            if folded == self.query:
                score = 0
            elif folded.startswith(self.query):
                score = 1
            elif self.query in folded:
                score = 2
            else:
                score = 3
            best = score if best is None else min(best, score)
        return best

    def filter(self, items: list[dict[str, Any]], *keys: str) -> list[dict[str, Any]]:
        if not self:
            return items
        ranked = []
        for index, item in enumerate(items):
            score = self.rank(*(text_or_none(item.get(k)) for k in keys))
            if score is not None:
                ranked.append((score, index, item))
        ranked.sort(key=lambda t: (t[0], t[1]))
        return [item for _, _, item in ranked]


@dataclass
class Page:
    window: list[dict[str, Any]]
    total: int
    offset: int
    limit: int
    truncated: bool
    next_offset: int | None


def paginate(items: list[dict[str, Any]], limit: int, offset: int, max_limit: int) -> Page:
    lim = clamp(limit, 1, max_limit)
    start = max(0, offset)
    window = items[start : start + lim]
    end = start + len(window)
    truncated = end < len(items)
    return Page(window, len(items), start, lim, truncated, end if truncated else None)


def json_size(model: BaseModel) -> int:
    """Bytes of the compact JSON a model contributes to a tool result."""
    data = model.model_dump(mode="json", exclude_none=True)
    return len(json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode())


def fit_to_budget(result: ListResult, budget: int) -> bool:
    """Drop trailing rows so the result's items stay within ``budget`` bytes of compact JSON. Returns True
    when rows were dropped (``truncated``/``next_offset`` are then set so the caller can continue)."""
    used = 0
    kept = 0
    for row in result.items:
        size = json_size(row)
        if used + size > budget:
            break
        used += size
        kept += 1
    if kept == len(result.items):
        return False
    result.items = result.items[:kept]  # type: ignore[assignment]
    result.truncated = True
    result.next_offset = result.offset + kept
    return True


# ---------------------------------------------------------------------------
# HTTP access
# ---------------------------------------------------------------------------


@dataclass
class ListResponse:
    items: list[dict[str, Any]]
    extract_date: str | None
    url: str


def _unwrap(item: dict[str, Any]) -> dict[str, Any]:
    """Merge a JSON:API-style nested ``attributes`` object into the row (defensive)."""
    nested = item.get("attributes")
    if isinstance(nested, dict):
        merged = {k: v for k, v in item.items() if k != "attributes"}
        merged.update(nested)
        return merged
    return item


class RegistryClient:
    def __init__(self, services: Services) -> None:
        self.services = services

    @property
    def base(self) -> str:
        return f"{self.services.settings.base_url(BASE_URL_KEY)}/{API_VERSION}"

    async def get_list(self, path: str, params: dict[str, Any]) -> ListResponse:
        response = await self.services.http.get_json(SOURCE, f"{self.base}/{path}", params=params)
        body = response.data
        if not isinstance(body, dict):
            raise UpstreamError(
                SOURCE, "Oväntat svar från Skolenhetsregistret", status=response.status, url=response.url
            )
        data = body.get("data")
        rows: Any = data.get("attributes") if isinstance(data, dict) else data
        items = [_unwrap(row) for row in dict_list(rows)]
        extract = text_or_none(as_dict(body.get("meta")).get("extractDate"))
        return ListResponse(items=items, extract_date=extract, url=response.url)

    async def get_detail(self, path: str, params: dict[str, Any], what: str) -> dict[str, Any]:
        try:
            response = await self.services.http.get_json(SOURCE, f"{self.base}/{path}", params=params)
        except UpstreamError as exc:
            # v2 answers 404; v1 used 410 for removed units and clients still treat both alike.
            if exc.status in (404, 410):
                raise UpstreamError(
                    SOURCE,
                    f"{what} hittades inte i Skolenhetsregistret (kontrollera koden, eller om as_of_date ligger "
                    "utanför tiden då den fanns)",
                    status=exc.status,
                    url=exc.url,
                    detail=exc.detail,
                ) from exc
            raise
        body = response.data
        if not isinstance(body, dict) or not isinstance(body.get("data"), dict):
            raise UpstreamError(
                SOURCE,
                "Oväntat svar från Skolenhetsregistret (saknar 'data')",
                status=response.status,
                url=response.url,
            )
        return body

    async def school_unit(
        self, code: str, as_of: str | None, personal: bool = False, emails: bool = True
    ) -> tuple[SchoolUnitDetail, dict[str, Any]]:
        payload = await self.get_detail(f"school-units/{code}", {"search_date": as_of}, f"Skolenheten {code}")
        return flatten_school_unit(payload, code, personal, emails), payload


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

CodeFilter = str | list[str] | None

_SCHOOL_TYPE_HELP = "FKLASS, FTH, OPPFTH, GR, GRAN, SP, SAM, GY, GYAN, VUX"
_STATUS_HELP = "AKTIV, VILANDE, UPPHORT, PLANERAD"
_ORGANIZER_TYPE_HELP = "KOMMUN, REGION, STAT, SAME, ENSKILD, SPECIAL, KOMMFORB, HMANUTL"


def _limit(maximum: int) -> Any:
    return Field(description=f"Max antal rader i svaret (1–{maximum}; större värden kapas)")


SchoolUnitLimit = Annotated[int, _limit(MAX_LIMIT_SCHOOL_UNITS)]
OrganizerLimit = Annotated[int, _limit(MAX_LIMIT_ORGANIZERS)]
EducationProviderLimit = Annotated[int, _limit(MAX_LIMIT_EDUCATION_PROVIDERS)]
ContractLimit = Annotated[int, _limit(MAX_LIMIT_CONTRACTS)]
Offset = Annotated[int, Field(description="Hoppa över så många träffar (för bläddring, se next_offset)", ge=0)]
ModifiedSince = Annotated[
    str | None,
    Field(description="Bara poster ändrade från och med detta datum (ÅÅÅÅ-MM-DD), skickas som meta_modified_after"),
]
AsOfDate = Annotated[
    str | None,
    Field(description="Historik: uppgifterna som de var detta datum (ÅÅÅÅ-MM-DD), skickas som search_date"),
]
IncludeRawPlain = Annotated[bool, Field(description="Ta med det obearbetade API-svaret i fältet raw")]
IncludeRaw = Annotated[
    bool,
    Field(
        description="Ta med det obearbetade API-svaret i fältet raw (personuppgifter tas bort om inte "
        "include_personal_data=true; e-post tas alltid bort när personuppgifter är avstängda i installationen)"
    ),
]
IncludePersonalDataUnit = Annotated[
    bool,
    Field(
        description="Ta med personuppgifter: rektorns namn (head_master, principal/headMaster) och c/o-namn i "
        "adresser (care_of). Personuppgift – sätt bara true när namnet uttryckligen behövs. Gäller även raw."
    ),
]
IncludePersonalDataAddress = Annotated[
    bool,
    Field(
        description="Ta med c/o-namn i adressen (care_of, kan vara en persons namn). Personuppgift – sätt bara "
        "true när det uttryckligen behövs. Gäller även raw."
    ),
]


def register(server: MCPServer[Any], services: Services) -> None:
    api = RegistryClient(services)

    @server.resource(
        CODES_URI,
        name="skolenhetsregistret-codes",
        title="Skolenhetsregistret: kodlistor",
        description="Kodlistor i Skolenhetsregistret v2 (status, skolformer inkl. komvux-delar, skolenhetstyper, "
        "huvudmannatyper m.m.) med svenska benämningar.",
        mime_type="application/json",
    )
    def skolenhetsregistret_codes() -> dict[str, Any]:
        return {
            "source": "Skolverket – Skolenhetsregistret, öppet API v2",
            "base_url": api.base,
            "note": "API v2 saknar endpoints för kodlistor; värdena kommer från OpenAPI-specifikationens enum. "
            "Filtervärden i verktygen skolverket_* matchas skiftlägesokänsligt mot kod eller benämning.",
            "school_type_property_keys": "schoolTypeProperties använder gemener: gr, gran, sam, sp, gy, gyan, vux",
            "code_lists": CODE_LISTS,
        }

    async def search_school_units(
        *,
        name: str | None,
        municipality: CodeFilter,
        school_type: CodeFilter,
        status: CodeFilter,
        status_default: tuple[str, ...],
        school_unit_type: CodeFilter,
        organization: CodeFilter,
        modified_since: str | None,
        limit: int,
        offset: int,
    ) -> SchoolUnitSearchResult:
        municipalities = list(dict.fromkeys(normalize_municipality_code(v) for v in as_list(municipality)))
        organizations = list(dict.fromkeys(normalize_organization_number(v) for v in as_list(organization)))
        filters: dict[str, Any] = {
            "municipality_code": municipalities or None,
            "school_type": normalize_codes(school_type, "school_type", "school_type") or None,
            "status": resolve_status(status, status_default) or None,
            "school_unit_type": normalize_codes(school_unit_type, "school_unit_type", "school_unit_type") or None,
            "organization_number": organizations or None,
            "meta_modified_after": iso_date(modified_since, "modified_since"),
        }
        matcher = NameMatcher(name)
        listing = await api.get_list("school-units", filters)
        matched = matcher.filter(listing.items, "name", "displayName")
        page = paginate(matched, limit, offset, MAX_LIMIT_SCHOOL_UNITS)
        rows = [r for r in (school_unit_row(i, municipalities, organizations) for i in page.window) if r]
        echo = {k: v for k, v in filters.items() if v is not None}
        if matcher:
            echo["name"] = name
        return SchoolUnitSearchResult(
            total=page.total,
            offset=page.offset,
            limit=page.limit,
            truncated=page.truncated,
            next_offset=page.next_offset,
            extract_date=listing.extract_date,
            filters=echo,
            items=rows,
            request_url=listing.url,
        )

    async def enrich(result: SchoolUnitSearchResult) -> None:
        limiter = anyio.CapacityLimiter(DETAIL_CONCURRENCY)
        failed: list[str] = []

        async def one(row: SchoolUnitRow) -> None:
            async with limiter:
                try:
                    detail, _ = await api.school_unit(row.school_unit_code, None)
                except UpstreamError:
                    failed.append(row.school_unit_code)
                    return
            apply_detail(row, detail)

        async with anyio.create_task_group() as group:
            for row in result.items:
                group.start_soon(one, row)
        if failed:
            result.notes.append(f"Detaljer kunde inte hämtas för: {', '.join(sorted(failed))}")

    async def search_organizers(
        *, name: str | None, organizer_type: CodeFilter, modified_since: str | None, limit: int, offset: int
    ) -> OrganizerSearchResult:
        filters: dict[str, Any] = {
            "organizer_type": normalize_codes(organizer_type, "organizer_type", "organizer_type") or None,
            "meta_modified_after": iso_date(modified_since, "modified_since"),
        }
        matcher = NameMatcher(name)
        listing = await api.get_list("organizers", filters)
        page = paginate(matcher.filter(listing.items, "displayName", "name"), limit, offset, MAX_LIMIT_ORGANIZERS)
        rows = [
            OrganizerRow(
                organization_number=number,
                name=text_or_none(i.get("displayName")) or text_or_none(i.get("name")),
                organizer_type=text_or_none(i.get("organizerType")),
            )
            for i in page.window
            if (number := text_or_none(i.get("organizationNumber")))
        ]
        echo = {k: v for k, v in filters.items() if v is not None}
        if matcher:
            echo["name"] = name
        return OrganizerSearchResult(
            total=page.total,
            offset=page.offset,
            limit=page.limit,
            truncated=page.truncated,
            next_offset=page.next_offset,
            extract_date=listing.extract_date,
            filters=echo,
            items=rows,
            request_url=listing.url,
        )

    async def search_education_providers(
        *, name: str | None, grading_rights: bool | None, modified_since: str | None, limit: int, offset: int
    ) -> EducationProviderSearchResult:
        filters: dict[str, Any] = {
            "grading_rights": grading_rights,
            "meta_modified_after": iso_date(modified_since, "modified_since"),
        }
        matcher = NameMatcher(name)
        listing = await api.get_list("education-providers", filters)
        page = paginate(
            matcher.filter(listing.items, "displayName", "folkHighschoolName"),
            limit,
            offset,
            MAX_LIMIT_EDUCATION_PROVIDERS,
        )
        rows = [
            EducationProviderRow(
                education_provider_code=code,
                organization_number=text_or_none(i.get("organizationNumber")),
                name=text_or_none(i.get("displayName")),
                folk_high_school_name=text_or_none(i.get("folkHighschoolName")),
                # List key is `gradingRight` (singular); accept the detail spelling too.
                grading_right=to_bool(i.get("gradingRight", i.get("gradingRights"))),
            )
            for i in page.window
            if (code := text_or_none(i.get("educationProviderCode")))
        ]
        echo = {k: v for k, v in filters.items() if v is not None}
        if matcher:
            echo["name"] = name
        return EducationProviderSearchResult(
            total=page.total,
            offset=page.offset,
            limit=page.limit,
            truncated=page.truncated,
            next_offset=page.next_offset,
            extract_date=listing.extract_date,
            filters=echo,
            items=rows,
            request_url=listing.url,
        )

    async def search_contracts(
        *,
        name: str | None,
        organizer: str | None,
        provider: str | None,
        status: str | None,
        modified_since: str | None,
        limit: int,
        offset: int,
    ) -> ContractSearchResult:
        filters: dict[str, Any] = {
            "organizer_organization_number": normalize_organization_number(organizer, "organizer_organization_number")
            if organizer
            else None,
            "education_provider_organization_number": normalize_organization_number(
                provider, "education_provider_organization_number"
            )
            if provider
            else None,
            "meta_modified_after": iso_date(modified_since, "modified_since"),
        }
        wanted_status = normalize_codes(status, "contract_status", "status")
        matcher = NameMatcher(name)
        listing = await api.get_list("contracts", filters)
        items = listing.items
        if wanted_status:
            items = [i for i in items if (text_or_none(i.get("status")) or "").casefold() in wanted_status]
        items = matcher.filter(items, "organizerName", "educationProviderName", "folkHighschoolName")
        page = paginate(items, limit, offset, MAX_LIMIT_CONTRACTS)
        rows = [
            ContractRow(
                organizer_organization_number=text_or_none(i.get("organizerOrganizationNumber")),
                organizer_name=text_or_none(i.get("organizerName")),
                education_provider_organization_number=text_or_none(i.get("educationProviderOrganizationNumber")),
                education_provider_name=text_or_none(i.get("educationProviderName")),
                education_provider_code=text_or_none(i.get("educationProviderCode")),
                folk_high_school_name=text_or_none(i.get("folkHighschoolName")),
                status=text_or_none(i.get("status")),
            )
            for i in page.window
        ]
        echo = {k: v for k, v in filters.items() if v is not None}
        if wanted_status:
            echo["status"] = wanted_status
        if matcher:
            echo["name"] = name
        return ContractSearchResult(
            total=page.total,
            offset=page.offset,
            limit=page.limit,
            truncated=page.truncated,
            next_offset=page.next_offset,
            extract_date=listing.extract_date,
            filters=echo,
            items=rows,
            request_url=listing.url,
        )

    @server.tool(
        name="skolverket_search_school_units",
        title="Skolverket: sök skolenheter (Skolenhetsregistret)",
        annotations=READ_ONLY_OPEN,
    )
    @tool_errors
    async def skolverket_search_school_units(
        name: Annotated[
            str | None,
            Field(
                description="Del av skolenhetens namn (school name). Matchas på klientsidan, skiftlägesokänsligt och "
                "tolerant för å/ä/ö; alla ord måste förekomma, t.ex. 'farentuna skola'."
            ),
        ] = None,
        municipality_code: Annotated[
            CodeFilter,
            Field(
                description="Kommunkod(er) (municipality code), 4 siffror, t.ex. ['0180', '1480']. "
                "Slå upp koder med ref_lookup_region."
            ),
        ] = None,
        school_type: Annotated[
            CodeFilter,
            Field(
                description=f"Skolform(er) (school type): {_SCHOOL_TYPE_HELP}. Benämning går också, t.ex. 'grundskola'."
            ),
        ] = None,
        status: Annotated[
            CodeFilter,
            Field(
                description=f"Status: {_STATUS_HELP}. Utelämnad = bara AKTIV; 'ALLA' = alla fyra statusar "
                "(skickas explicit som upprepad status-parameter)."
            ),
        ] = None,
        school_unit_type: Annotated[
            CodeFilter,
            Field(description="Skolenhetstyp: SKOLENHET, CENTRAL (insamlingsenhet utan undervisning), UTLAND"),
        ] = None,
        organization_number: Annotated[
            CodeFilter,
            Field(description="Huvudmannens organisationsnummer (organizer), 10 siffror, t.ex. '2120000126'"),
        ] = None,
        modified_since: ModifiedSince = None,
        limit: SchoolUnitLimit = DEFAULT_LIMIT,
        offset: Offset = 0,
        with_details: Annotated[
            bool,
            Field(
                description=f"Hämta detaljer för varje rad i svaret (skolformer, kommun, huvudman, ort, koordinater). "
                f"Ett anrop per skolenhet; max {MAX_DETAIL_ROWS} rader."
            ),
        ] = False,
    ) -> Annotated[CallToolResult, SchoolUnitSearchResult]:
        """Sök skolenheter i Skolverkets skolenhetsregister (Skolenhetsregistret, API v2). Filtren kommun,
        skolform, status, skolenhetstyp, huvudman och ändringsdatum körs i API:t och kan kombineras (OR inom ett
        filter, AND mellan filter); namnsökningen görs här över den cachade listan eftersom API:t saknar
        fritextsökning.

        Registrets lista innehåller bara skolenhetskod, namn och status. Kommunkod/huvudman fylls i när du
        filtrerat på exakt en kommun/huvudman; sätt with_details=true (eller använd skolverket_get_school_unit)
        för skolformer, ort och koordinater. Förskolor finns inte i registret. Skolenhetskoden (8 siffror) är
        samma nyckel som i Planerad utbildning och statistiken."""
        result = await search_school_units(
            name=name,
            municipality=municipality_code,
            school_type=school_type,
            status=status,
            status_default=("AKTIV",),
            school_unit_type=school_unit_type,
            organization=organization_number,
            modified_since=modified_since,
            limit=clamp(limit, 1, MAX_DETAIL_ROWS) if with_details else limit,
            offset=offset,
        )
        if with_details:
            if limit > MAX_DETAIL_ROWS:
                result.notes.append(f"with_details begränsar limit till {MAX_DETAIL_ROWS}.")
            await enrich(result)
        return structured(result)

    @server.tool(
        name="skolverket_get_school_unit",
        title="Skolverket: skolenhet (Skolenhetsregistret)",
        annotations=READ_ONLY_OPEN,
    )
    @tool_errors
    async def skolverket_get_school_unit(
        school_unit_code: Annotated[str, Field(description="Skolenhetskod (school unit code), 8 siffror")],
        as_of_date: AsOfDate = None,
        include_raw: IncludeRaw = False,
        include_personal_data: IncludePersonalDataUnit = False,
    ) -> Annotated[CallToolResult, SchoolUnitDetail]:
        """Hämta en skolenhet ur Skolenhetsregistret: namn, status, skolformer med årskurser/program/komvux-delar,
        kommunkod, huvudman (organisationsnummer, namn, typ), skolenhetens kontaktuppgifter (e-post, telefon,
        webb), besöksadress med WGS84-koordinater (latitude/longitude) och SWEREF 99 TM, inriktning, komvux i
        egen regi, start-/slutdatum och ändringsdatum. Med as_of_date fås uppgifterna som de var ett visst datum
        (historik).

        Rektorns namn och c/o-namn i adresser är personuppgifter och tas bara med när include_personal_data=true
        (då även i raw). Skolenhetens e-post och telefon kan i enstaka fall vara en persons direktkontakt; e-post
        lämnas inte ut när personuppgifter är avstängda i installationen (se notes)."""
        include_personal_data = personal_data(services, include_personal_data)
        emails = services.settings.allow_personal_data
        code = normalize_school_unit_code(school_unit_code)
        as_of = iso_date(as_of_date, "as_of_date")
        detail, payload = await api.school_unit(code, as_of, include_personal_data, emails)
        detail.as_of_date = as_of
        if include_raw:
            detail.raw = raw_output(payload, include_personal_data, emails)
        return structured(detail)

    @server.tool(
        name="skolverket_search_organizers",
        title="Skolverket: sök huvudmän (Skolenhetsregistret)",
        annotations=READ_ONLY_OPEN,
    )
    @tool_errors
    async def skolverket_search_organizers(
        name: Annotated[
            str | None,
            Field(description="Del av huvudmannens namn (organizer name), matchas på klientsidan, tål å/ä/ö"),
        ] = None,
        organizer_type: Annotated[
            CodeFilter,
            Field(description=f"Huvudmannatyp(er) (organizer type): {_ORGANIZER_TYPE_HELP}"),
        ] = None,
        modified_since: ModifiedSince = None,
        limit: OrganizerLimit = DEFAULT_LIMIT,
        offset: Offset = 0,
    ) -> Annotated[CallToolResult, OrganizerSearchResult]:
        """Sök huvudmän (organizers: kommuner, regioner, enskilda huvudmän m.fl.) i Skolenhetsregistret.
        Returnerar organisationsnummer, namn och huvudmannatyp. Typ och ändringsdatum filtreras i API:t,
        namnet här (API:t saknar namnsökning). Namn kommer från SCB:s företagsregister och kan vara versaler."""
        return structured(
            await search_organizers(
                name=name, organizer_type=organizer_type, modified_since=modified_since, limit=limit, offset=offset
            )
        )

    @server.tool(
        name="skolverket_get_organizer",
        title="Skolverket: huvudman (Skolenhetsregistret)",
        annotations=READ_ONLY_OPEN,
    )
    @tool_errors
    async def skolverket_get_organizer(
        organization_number: Annotated[
            str,
            Field(description="Huvudmannens organisationsnummer, 10 siffror, t.ex. '2120000126' eller '212000-0126'"),
        ],
        as_of_date: AsOfDate = None,
        include_raw: IncludeRaw = False,
        include_personal_data: IncludePersonalDataAddress = False,
    ) -> Annotated[CallToolResult, OrganizerDetail]:
        """Hämta en huvudman (organizer) ur Skolenhetsregistret: namn, huvudmannatyp, bolagsform, status hos
        Skatteverket/Bolagsverket, säteskommun, region, skolformer, kontaktuppgifter, adress samt
        skolenhetskoderna för huvudmannens skolenheter och eventuella komvux-entreprenader. Namn och status
        för skolenheterna fås med skolverket_search_school_units(organization_number=...). c/o-namn i adressen
        (personuppgift) tas bara med när include_personal_data=true. E-post lämnas inte ut när personuppgifter
        är avstängda i installationen (se notes)."""
        include_personal_data = personal_data(services, include_personal_data)
        emails = services.settings.allow_personal_data
        number = normalize_organization_number(organization_number)
        as_of = iso_date(as_of_date, "as_of_date")
        payload = await api.get_detail(f"organizers/{number}", {"search_date": as_of}, f"Huvudmannen {number}")
        detail = flatten_organizer(payload, number, include_personal_data, emails)
        detail.as_of_date = as_of
        if include_raw:
            detail.raw = raw_output(payload, include_personal_data, emails)
        return structured(detail)

    @server.tool(
        name="skolverket_search_education_providers",
        title="Skolverket: sök utbildningsanordnare (komvux)",
        annotations=READ_ONLY_OPEN,
    )
    @tool_errors
    async def skolverket_search_education_providers(
        name: Annotated[
            str | None,
            Field(description="Del av anordnarens eller folkhögskolans namn, matchas på klientsidan, tål å/ä/ö"),
        ] = None,
        grading_rights: Annotated[
            bool | None, Field(description="true = bara anordnare med betygsrätt, false = bara utan")
        ] = None,
        modified_since: ModifiedSince = None,
        limit: EducationProviderLimit = DEFAULT_LIMIT,
        offset: Offset = 0,
    ) -> Annotated[CallToolResult, EducationProviderSearchResult]:
        """Sök utbildningsanordnare (education providers) inom kommunal vuxenutbildning i Skolenhetsregistret:
        anordnarkod (8 siffror), organisationsnummer, namn, folkhögskolenamn och betygsrätt. Betygsrätt och
        ändringsdatum filtreras i API:t, namnet här."""
        return structured(
            await search_education_providers(
                name=name, grading_rights=grading_rights, modified_since=modified_since, limit=limit, offset=offset
            )
        )

    @server.tool(
        name="skolverket_get_education_provider",
        title="Skolverket: utbildningsanordnare (komvux)",
        annotations=READ_ONLY_OPEN,
    )
    @tool_errors
    async def skolverket_get_education_provider(
        education_provider_code: Annotated[str, Field(description="Anordnarkod (education provider code), 8 siffror")],
        as_of_date: AsOfDate = None,
        include_raw: IncludeRaw = False,
        include_personal_data: IncludePersonalDataAddress = False,
    ) -> Annotated[CallToolResult, EducationProviderDetail]:
        """Hämta en utbildningsanordnare inom komvux: namn, adress, kontaktuppgifter, bolagsform, betygsrätt med
        giltighetsperiod, skolformsdelar (VUXGR, VUXGY, VUXSFI ...) och entreprenadavtal (huvudmannens
        organisationsnummer + anordnarkod, används i skolverket_get_contract). Detaljsvaret saknar anordnarens
        organisationsnummer; det finns i skolverket_search_education_providers. c/o-namn i adressen
        (personuppgift) tas bara med när include_personal_data=true. E-post lämnas inte ut när personuppgifter
        är avstängda i installationen (se notes)."""
        include_personal_data = personal_data(services, include_personal_data)
        emails = services.settings.allow_personal_data
        code = normalize_school_unit_code(education_provider_code, "anordnarkod")
        as_of = iso_date(as_of_date, "as_of_date")
        payload = await api.get_detail(
            f"education-providers/{code}", {"search_date": as_of}, f"Utbildningsanordnaren {code}"
        )
        detail = flatten_education_provider(payload, code, include_personal_data, emails)
        detail.as_of_date = as_of
        if include_raw:
            detail.raw = raw_output(payload, include_personal_data, emails)
        return structured(detail)

    @server.tool(
        name="skolverket_search_contracts",
        title="Skolverket: sök komvux-entreprenader",
        annotations=READ_ONLY_OPEN,
    )
    @tool_errors
    async def skolverket_search_contracts(
        organizer_organization_number: Annotated[
            str | None, Field(description="Huvudmannens organisationsnummer (10 siffror)")
        ] = None,
        education_provider_organization_number: Annotated[
            str | None, Field(description="Utbildningsanordnarens organisationsnummer (10 siffror)")
        ] = None,
        status: Annotated[str | None, Field(description="'aktiv' eller 'inaktiv' (filtreras på klientsidan)")] = None,
        name: Annotated[
            str | None,
            Field(description="Del av huvudmannens, anordnarens eller folkhögskolans namn (klientsidan, tål å/ä/ö)"),
        ] = None,
        modified_since: ModifiedSince = None,
        limit: ContractLimit = DEFAULT_LIMIT,
        offset: Offset = 0,
    ) -> Annotated[CallToolResult, ContractSearchResult]:
        """Sök avtal om kommunal vuxenutbildning på entreprenad (contracts) mellan en huvudman och en
        utbildningsanordnare. Organisationsnummer och ändringsdatum filtreras i API:t; status och namn här."""
        return structured(
            await search_contracts(
                name=name,
                organizer=organizer_organization_number,
                provider=education_provider_organization_number,
                status=status,
                modified_since=modified_since,
                limit=limit,
                offset=offset,
            )
        )

    @server.tool(
        name="skolverket_get_contract",
        title="Skolverket: komvux-entreprenad",
        annotations=READ_ONLY_OPEN,
    )
    @tool_errors
    async def skolverket_get_contract(
        organization_number: Annotated[str, Field(description="Huvudmannens organisationsnummer (10 siffror)")],
        education_provider_code: Annotated[str, Field(description="Utbildningsanordnarens anordnarkod (8 siffror)")],
        as_of_date: AsOfDate = None,
        include_raw: IncludeRawPlain = False,
    ) -> Annotated[CallToolResult, ContractDetail]:
        """Hämta ett entreprenadavtal inom komvux: parternas namn och organisationsnummer, status,
        skolformsdelar med giltighetsperiod (validFrom/validTo) och skolenhetskoder som avtalet gäller."""
        number = normalize_organization_number(organization_number)
        code = normalize_school_unit_code(education_provider_code, "anordnarkod")
        as_of = iso_date(as_of_date, "as_of_date")
        payload = await api.get_detail(
            f"contracts/{number}/{code}", {"search_date": as_of}, f"Entreprenaden {number}/{code}"
        )
        detail = flatten_contract(payload, number, code)
        detail.as_of_date = as_of
        if include_raw:
            # The contract schema has no personal fields; redaction is a defensive no-op.
            detail.raw = raw_output(payload, personal=False)
        return structured(detail)

    @server.tool(
        name="skolverket_school_unit_changes",
        title="Skolverket: ändringar i Skolenhetsregistret",
        annotations=READ_ONLY_OPEN,
    )
    @tool_errors
    async def skolverket_school_unit_changes(
        since: Annotated[str, Field(description="Ändrade från och med detta datum (ÅÅÅÅ-MM-DD)")],
        entities: Annotated[
            list[Literal["school_units", "organizers", "education_providers", "contracts"]] | None,
            Field(description="Vilka register som ska kontrolleras. Standard: skolenheter och huvudmän."),
        ] = None,
        municipality_code: Annotated[
            CodeFilter, Field(description="Begränsa skolenheterna till kommunkod(er), 4 siffror")
        ] = None,
        status: Annotated[
            CodeFilter,
            Field(
                description=f"Begränsa skolenheterna till status: {_STATUS_HELP}. Standard: alla fyra "
                "(skickas explicit)."
            ),
        ] = None,
        limit: Annotated[
            int,
            Field(
                description=f"Max antal rader per register (skolenheter 1–{MAX_LIMIT_SCHOOL_UNITS}, huvudmän "
                f"1–{MAX_LIMIT_ORGANIZERS}, anordnare 1–{MAX_LIMIT_EDUCATION_PROVIDERS}, entreprenader "
                f"1–{MAX_LIMIT_CONTRACTS}). Hela svaret hålls dessutom under ca {CHANGES_MAX_BYTES // 1000} kB; "
                "se truncated/next_offset."
            ),
        ] = CHANGES_DEFAULT_LIMIT,
    ) -> Annotated[CallToolResult, ChangesResult]:
        """Lista skolenheter och/eller huvudmän (samt valfritt utbildningsanordnare och entreprenader) som ändrats
        sedan ett datum, via meta_modified_after. Ersätter v1:s diff-tjänster. Används för att hålla en lokal
        kopia uppdaterad eller bevaka förändringar i en kommun.

        Utan status efterfrågas skolenheter med alla fyra statusar explicit (AKTIV, VILANDE, UPPHORT, PLANERAD);
        att API:t faktiskt returnerar nedlagda och planerade enheter är inte verifierat. Svaret är storleksbegränsat:
        är truncated=true finns resten via respektive sökverktyg med offset=next_offset (se notes)."""
        day = iso_date(since, "since")
        if day is None:
            raise InvalidInputError("Ange since som ÅÅÅÅ-MM-DD")
        wanted = list(dict.fromkeys(entities or ["school_units", "organizers"]))
        result = ChangesResult(since=day)
        parts: list[tuple[str, ListResult, dict[str, Any]]] = []
        if "school_units" in wanted:
            result.school_units = await search_school_units(
                name=None,
                municipality=municipality_code,
                school_type=None,
                status=status,
                status_default=ALL_STATUSES,
                school_unit_type=None,
                organization=None,
                modified_since=day,
                limit=limit,
                offset=0,
            )
            extra = {k: result.school_units.filters.get(k) for k in ("status", "municipality_code")}
            parts.append(("skolverket_search_school_units", result.school_units, extra))
        if "organizers" in wanted:
            result.organizers = await search_organizers(
                name=None, organizer_type=None, modified_since=day, limit=limit, offset=0
            )
            parts.append(("skolverket_search_organizers", result.organizers, {}))
        if "education_providers" in wanted:
            result.education_providers = await search_education_providers(
                name=None, grading_rights=None, modified_since=day, limit=limit, offset=0
            )
            parts.append(("skolverket_search_education_providers", result.education_providers, {}))
        if "contracts" in wanted:
            result.contracts = await search_contracts(
                name=None, organizer=None, provider=None, status=None, modified_since=day, limit=limit, offset=0
            )
            parts.append(("skolverket_search_contracts", result.contracts, {}))
        share = CHANGES_MAX_BYTES // max(1, len(parts))
        for tool, part, extra in parts:
            if fit_to_budget(part, share):
                result.notes.append(
                    f"{tool}: svaret kortades till {len(part.items)} rader för att hålla storleken nere."
                )
            if part.truncated:
                result.truncated = True
                args = {"modified_since": day, **{k: v for k, v in extra.items() if v}, "offset": part.next_offset}
                result.notes.append(f"Fortsätt med {tool}({json.dumps(args, ensure_ascii=False)}).")
        return structured(result)

    @server.tool(
        name="skolverket_school_unit_api_info",
        title="Skolverket: Skolenhetsregistret API-info",
        annotations=READ_ONLY_OPEN,
    )
    @tool_errors
    async def skolverket_school_unit_api_info() -> dict[str, Any]:
        """Metadata om Skolenhetsregistrets API (utgivare, version, release, status, dokumentation). Fungerar
        också som hälsokontroll av API:t."""
        response = await services.http.get_json(SOURCE, f"{api.base}/api-info", use_cache=False)
        info = as_dict(response.data)
        return {
            "api_publisher": info.get("apiPublisher"),
            "api_name": info.get("apiName"),
            "api_version": info.get("apiVersion"),
            "api_released": info.get("apiReleased"),
            "api_status": info.get("apiStatus"),
            "api_documentation": info.get("apiDocumentation"),
            "base_url": api.base,
            "codes_resource": CODES_URI,
        }
