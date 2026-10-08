# SPDX-FileCopyrightText: 2026 Deniz Özer
#
# SPDX-License-Identifier: MIT

"""Skolverket – Planerad utbildning ("API för skolor, utbildningar och statistik"), API v4.

Base: https://api.skolverket.se/planned-educations (config key ``planned_educations``) + ``/v4/...``.
Every request sends ``Accept: application/vnd.skolverket.plannededucations.api.v4.hal+json``; the path
version and the Accept version must agree. All responses except ``/v4/api-info`` are wrapped in
``{"status": "OK", "message": "", "body": ...}``. Collections are Spring HATEOAS pages inside ``body``
(``_embedded.<rel>[]`` + ``page{size,totalElements,totalPages,number}``, 0-based pages; keep size ≤ 100,
size 500 has been observed to answer 404). Statistics are arrays of ``{value, valueType, timePeriod}``
with Swedish number formatting ("15,5", "cirka 370", "~100", ".." for suppressed values).

Statistics, survey and document endpoints take no query parameters. The success status is spelled "OK" in
the examples and "200 OK" in the schema enum; both are accepted. ``/school-units/{code}/education-events``
is not a HAL page but a map keyed by school form (``{"gy": [...]}``) and is flattened into rows. The
skolenhetskod (8 digits) is the same key as in Skolenhetsregistret, so the two APIs join on it.

Privacy: contactInfo.email can be a named person's address (the spec example is one), so it is only
returned with ``include_personal_data=true`` and contactInfo is removed from ``raw`` otherwise. With
``FUZZY_MCP_PERSONAL_DATA=off`` the opt-in is refused and the note says so instead of suggesting it.
"""

import datetime
import json
import math
import re
import unicodedata
from typing import Annotated, Any, Literal
from urllib.parse import quote

from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult
from pydantic import BaseModel, Field

from ...core import READ_ONLY_OPEN, Services, clamp, compact, personal_data, structured, tool_errors
from ...errors import InvalidInputError, UpstreamError
from ...http import ApiResponse

SOURCE = "skolverket"
BASE_URL_KEY = "planned_educations"
API_VERSION = "v4"
MEDIA_TYPE = "application/vnd.skolverket.plannededucations.api.v4.hal+json"
HEADERS = {"Accept": MEDIA_TYPE}
MAX_PAGE_SIZE = 100
CODES_URI = "fuzzy://skolverket/planned-educations/codes"
CITATION = "Källa: Skolverket, Planerad utbildning (API v4, api.skolverket.se/planned-educations)"
SALSA_MAX_ROWS = 100
SUPPORT_NESTED_LIST_CAP = 100
SUPPORT_MAX_ENTRIES = 500
# Hosts cap tool output at ~25k tokens; support lists stop adding entries beyond this many JSON characters.
SUPPORT_CHAR_BUDGET = 50_000
DOCUMENTS_MAX = 200
# A gy school with many programmes and include_history can exceed the host limit; see drop_program_history.
STATISTICS_CHAR_BUDGET = 60_000
PERSONAL_DATA_NOTE = (
    "E-postadress utelämnad: den kan innehålla en enskild persons namn (personuppgift). "
    "Ange include_personal_data=true för att ta med den."
)
# With FUZZY_MCP_PERSONAL_DATA=off include_personal_data=true is refused, so the note must not suggest it.
PERSONAL_DATA_OFF_NOTE = (
    "E-postadress utelämnad: den kan innehålla en enskild persons namn (personuppgift). "
    "Personuppgifter är avstängda i den här installationen (FUZZY_MCP_PERSONAL_DATA=off)."
)


def personal_data_note(allow_personal_data: bool) -> str:
    """The note for a withheld e-mail address: the opt-in hint, or that personal data is off in this installation."""
    return PERSONAL_DATA_NOTE if allow_personal_data else PERSONAL_DATA_OFF_NOTE


# --------------------------------------------------------------------------------------------------
# Code lists, taken from the examples of the official v4 spec (docs/referenser/…_v4-active.json).
# --------------------------------------------------------------------------------------------------

# Exactly the list in the example of GET /v4/support/school-types (official v4 spec), in that order.
TYPE_OF_SCHOOLING: dict[str, dict[str, Any]] = {
    "fs": {"name": "Förskolan"},
    "fsk": {"name": "Förskoleklassen", "statistics": True},
    "fth": {"name": "Fritidshemmet"},
    "gr": {"name": "Grundskolan", "statistics": True},
    "sp": {"name": "Specialskolan"},
    "sam": {"name": "Sameskolan"},
    "gy": {"name": "Gymnasieskolan", "statistics": True},
    "vuxgr": {"name": "Kommunal vuxenutbildning på grundläggande nivå"},
    "vux": {"name": "Kommunal vuxenutbildning"},
    "vuxgy": {"name": "Kommunal vuxenutbildning på gymnasial nivå"},
    "vuxgrs": {"name": "Särskild utbildning för vuxna på grundläggande nivå"},
    "vuxgys": {"name": "Särskild utbildning för vuxna på gymnasial nivå"},
    "sfi": {"name": "Kommunal vuxenutbildning i svenska för invandrare"},
    "gran": {"name": "Anpassad grundskola", "statistics": True},
    "gyan": {"name": "Anpassad gymnasieskola", "statistics": True},
    "vuxgran": {"name": "Kommunal vuxenutbildning som anpassad utbildning på grundläggande nivå"},
    "vuxgyan": {"name": "Kommunal vuxenutbildning som anpassad utbildning på gymnasial nivå"},
}
# Spec enum of /v4/school-units/{code}/documents/{typeOfSchooling} and the statistics paths.
STATISTICS_FORMS: tuple[str, ...] = ("fsk", "gr", "gran", "gy", "gyan")
_SCHOOLING_ALIASES = {
    "forskoleklass": "fsk",
    "forskoleklassen": "fsk",
    "preschool class": "fsk",
    "grundskola": "gr",
    "grundskolan": "gr",
    "compulsory school": "gr",
    "anpassad grundskola": "gran",
    "anpassade grundskolan": "gran",
    "grundsarskola": "gran",
    "grundsarskolan": "gran",
    "gymnasieskola": "gy",
    "gymnasieskolan": "gy",
    "gymnasium": "gy",
    "upper secondary": "gy",
    "upper secondary school": "gy",
    "anpassad gymnasieskola": "gyan",
    "anpassade gymnasieskolan": "gyan",
    "gymnasiesarskola": "gyan",
    "gymnasiesarskolan": "gyan",
    "specialskola": "sp",
    "specialskolan": "sp",
    "sameskola": "sam",
    "sameskolan": "sam",
    "forskola": "fs",
    "forskolan": "fs",
    "fritidshem": "fth",
    "fritidshemmet": "fth",
    "komvux": "vux",
    "kommunal vuxenutbildning": "vux",
}

# Exactly the example of GET /v4/support/principal-organizer-types. Unknown values are passed verbatim.
PRINCIPAL_ORGANIZER_TYPES = ["Kommunal", "Region", "Statlig", "Sameskolan", "Fristående", "Uppgift saknas"]
_ORGANIZER_ALIASES = {
    "kommunal": "Kommunal",
    "kommun": "Kommunal",
    "municipal": "Kommunal",
    "public": "Kommunal",
    "fristaende": "Fristående",
    "friskola": "Fristående",
    "independent": "Fristående",
    "private": "Fristående",
    "region": "Region",
    "statlig": "Statlig",
    "state": "Statlig",
    "sameskolan": "Sameskolan",
    "sameskola": "Sameskolan",
    "uppgiftsaknas": "Uppgift saknas",
}

# GET /v4/support/adultTypeOfSchooling (official example): id, display value and the retired code. The spec
# says "Använd inte de gamla koderna", so old codes given by a user are translated to the current id.
ADULT_TYPE_OF_SCHOOL: list[tuple[str, str, str | None]] = [
    ("aub", "Arbetsmarknadsutbildning", "arbmarknutb"),
    ("au", "annan undervisning", None),
    ("coursebasic", "Kurs eller kurspaket på grund eller grund avancerad nivå, högskola", None),
    ("courseadvanced", "Kurs eller kurspaket på avancerad nivå, högskola", None),
    ("fhsaub", "Folkhögskola (Arbetsmarknadsutbildning)", "fhskaub"),
    ("fhs", "Folkhögskola", "fhsk"),
    ("forutbildning", "Förutbildning för högskola (t.ex. tekniskt basår)", None),
    ("forberutb", "Förberedande utbildning", None),
    ("kku", "Övriga eftergymnasiala konst- och kulturutbildningar", "konstkultur"),
    ("ny", "Nationell yrkesutbildning", None),
    ("programbasic", "Program grundnivå", None),
    ("programadvanced", "Program avancerad nivå (ex. masterexamen)", None),
    ("testokartl", "Test och kartläggning inför AUB (arbetsmarknadsutbildning)", None),
    ("vuxgran", "Kommunal vuxenutbildning som anpassad utbildning på grundläggande nivå", "komvuxgrsar"),
    ("vuxgyan", "Kommunal vuxenutbildning som anpassad utbildning på gymnasial nivå", "komvuxgysar"),
    ("vuxgy", "Gymnasial vuxenutbildning", "komvuxgycourses"),
    ("vuxgr", "Grundläggande vuxenutbildning", "komvuxbasiccourses"),
    ("vuxsfi", "Svenska för invandrare", "komvuxsfi/sfi"),
    ("komvuxcoursepackage", "Yrkespaket eller kurspaket inom kommunal vuxenutbildning", None),
    ("yhkurskurspaket", "Yrkeshögskoleutbildning (kurs eller kurspaket)", None),
    ("yhprogram", "Yrkeshögskoleutbildning (program)", None),
    ("yh", "Yrkeshögskola (Allt inom YH)", None),
]
_ADULT_OLD_CODES: dict[str, str] = {
    old: new for new, _, olds in ADULT_TYPE_OF_SCHOOL if olds for old in olds.lower().split("/")
}
# executionCondition codes as documented on /v4/adult-education-events.
EXECUTION_CONDITIONS = {"0": "Ej fastställt", "1": "Datum satt", "2": "Löpande kursstart", "3": "Löpande kursstart"}

SURVEY_KEYS: dict[str, str] = {
    "custodiansfsk": "Vårdnadshavare, förskoleklass",
    "custodiansgr": "Vårdnadshavare, grundskola",
    "custodiansgran": "Vårdnadshavare, anpassad grundskola",
    "pupilsgr": "Elever, grundskola (åk 5 och åk 8)",
    "pupilsgy": "Elever, gymnasieskola (år 2)",
}
SURVEY_SCHOOL_YEARS = {"ak5": "Årskurs 5", "ak8": "Årskurs 8", "ar2": "Gymnasieskolan år 2"}

VALUE_TYPES: dict[str, str] = {
    "EXISTS": "Värde finns (kan vara avrundat: 'cirka 370', '~100').",
    "MISSING": "Uppgift saknas ('.', enkelprick) – value kan vara null.",
    "OMITTED_DUE_TO_BASED_ON_FEW_PUPILS": "Undertryckt eftersom värdet bygger på för få elever ('..', dubbelprick).",
    "ROUNDED_OFF_DUE_TO_FEW_PUPILS_NOT_ELIGIBLE": "Avrundat eftersom få elever inte var behöriga ('~100').",
    "TEACHERS_EXCLUDED_DUE_TO_NO_REQUIRED_LEGITIMATION": "Lärarlegitimation krävs inte för skolformen ('*').",
}
_NO_VALUE_TYPES = frozenset(
    {"MISSING", "OMITTED_DUE_TO_BASED_ON_FEW_PUPILS", "TEACHERS_EXCLUDED_DUE_TO_NO_REQUIRED_LEGITIMATION"}
)
_APPROXIMATE_VALUE_TYPES = frozenset({"ROUNDED_OFF_DUE_TO_FEW_PUPILS_NOT_ELIGIBLE"})
_MISSING_STRINGS = frozenset({"", ".", "..", "...", "*", "-", "–", "—", "na", "n/a", "null", "none"})

# GY25 study path codes as used by education events (studyPathCode "BF25", "EK25" in the spec examples).
# Statistics (programMetrics, national values) and /v4/support/programs use codes without "25" ("EK", "NA",
# "IMA"). Names are given only for the long-established national programmes.
GY25_PROGRAM_CODES: dict[str, str | None] = {
    "BA25": "Bygg- och anläggningsprogrammet",
    "BF25": "Barn- och fritidsprogrammet",
    "DS25": None,
    "EE25": "El- och energiprogrammet",
    "EK25": "Ekonomiprogrammet",
    "ES25": "Estetiska programmet",
    "FI25": None,
    "FL25": None,
    "FO25": None,
    "FR25": None,
    "FS25": None,
    "FT25": "Fordons- och transportprogrammet",
    "GU25": None,
    "HT25": "Hotell- och turismprogrammet",
    "HU25": "Humanistiska programmet",
    "HV25": "Hantverksprogrammet",
    "IN25": "Industritekniska programmet",
    "MA25": None,
    "NA25": "Naturvetenskapsprogrammet",
    "NB25": "Naturbruksprogrammet",
    "RL25": "Restaurang- och livsmedelsprogrammet",
    "SA25": "Samhällsvetenskapsprogrammet",
    "SJ25": None,
    "SM25": None,
    "TA25": None,
    "TE25": "Teknikprogrammet",
    "VF25": "VVS- och fastighetsprogrammet",
    "VI25": None,
    "VO25": "Vård- och omsorgsprogrammet",
    "YR25": None,
    "IMV": None,
    "IMY": None,
}

# Meanings of statistics indicators (spec §5.2.2, §5.2.4). Units for specialTeacherPositions and
# specialEducatorsQuota conflict between sources (spec open question 5).
METRIC_LABELS: dict[str, str] = {
    "studentsPerTeacherQuota": "Antal elever per lärare (heltidstjänst)",
    "certifiedTeachersQuota": "Andel lärare (heltidstjänster) med legitimation och behörighet, %",
    "specialTeacherPositions": "Speciallärare (enhet oklar i källorna)",
    "specialEducatorsQuota": "Specialpedagoger (enhet oklar: andel % eller elever per specialpedagog)",
    "totalNumberOfPupils": "Antal elever, avrundat till tiotal (insamlas 15 oktober)",
    "ratioOfPupilsIn6thGradeWithAllSubjectsPassed": "Andel elever i åk 6 med godkända betyg i alla ämnen, %",
    "averageResultNationalTestsSubjectSVE6thGrade": "Genomsnittligt provbetyg nationellt prov svenska åk 6 (0–20)",
    "averageResultNationalTestsSubjectENG6thGrade": "Genomsnittligt provbetyg nationellt prov engelska åk 6 (0–20)",
    "averageResultNationalTestsSubjectMA6thGrade": "Genomsnittligt provbetyg nationellt prov matematik åk 6 (0–20)",
    "averageResultNationalTestsSubjectSVA6thGrade": "Genomsnittligt provbetyg nationellt prov svenska som andraspråk "
    "åk 6 (0–20)",
    "averageResultNationalTestsSubjectSVE9thGrade": "Genomsnittligt provbetyg nationellt prov svenska åk 9 (0–20)",
    "averageResultNationalTestsSubjectENG9thGrade": "Genomsnittligt provbetyg nationellt prov engelska åk 9 (0–20)",
    "averageResultNationalTestsSubjectMA9thGrade": "Genomsnittligt provbetyg nationellt prov matematik åk 9 (0–20)",
    "averageResultNationalTestsSubjectSVA9thGrade": "Genomsnittligt provbetyg nationellt prov svenska som andraspråk "
    "åk 9 (0–20)",
    "ratioOfPupilsIn9thGradeWithAllSubjectsPassed": "Andel elever i åk 9 med godkända betyg i alla ämnen, %",
    "averageGradesMeritRating9thGrade": "Genomsnittligt meritvärde åk 9 (0–340)",
    "ratioOfPupils9thGradeEligibleForNationalProgramYR": "Andel elever i åk 9 behöriga till yrkesprogram, %",
    "ratioOfPupils9thGradeEligibleForNationalProgramES": "Andel elever i åk 9 behöriga till estetiska programmet, %",
    "ratioOfPupils9thGradeEligibleForNationalProgramSAEKHU": "Andel elever i åk 9 behöriga till samhällsvetenskaps-, "
    "ekonomi- och humanistiska programmen, %",
    "ratioOfPupils9thGradeEligibleForNationalProgramNATE": "Andel elever i åk 9 behöriga till naturvetenskaps- och "
    "teknikprogrammen, %",
    "admissionPointsMin": "Lägsta antagningspoäng (meritpoäng 0–340), period = antagningsår",
    "admissionPointsAverage": "Genomsnittlig antagningspoäng (meritpoäng 0–340)",
    "admissionPointsSemester": "Termin för antagningen",
    "gradesPointsForStudents": "Genomsnittlig betygspoäng (GBP) för avgångselever (0–20)",
    "gradesPointsForStudentsWithExam": "Genomsnittlig betygspoäng (GBP) för elever med examen (0–20)",
    "ratioOfPupilsWithExamWithin3Years": "Andel elever med examen inom 3 år, %",
    "ratioOfStudentsEligibleForUndergraduateEducation": "Andel elever med grundläggande behörighet till högskola, %",
    "averageResultNationalTestsSubjectSVE": "Genomsnittligt provbetyg nationellt prov svenska (kurs i sveSubjectTest)",
    "averageResultNationalTestsSubjectSVA": "Genomsnittligt provbetyg nationellt prov svenska som andraspråk "
    "(kurs i svaSubjectTest)",
    "averageResultNationalTestsSubjectENG": "Genomsnittligt provbetyg nationellt prov engelska (kurs i engSubjectTest)",
    "averageResultNationalTestsSubjectMA1": "Genomsnittligt provbetyg första nationella provet i matematik "
    "(kurs i ma1SubjectTest)",
    "averageResultNationalTestsSubjectMA2": "Genomsnittligt provbetyg andra nationella provet i matematik "
    "(kurs i ma2SubjectTest)",
}
SALSA_LABELS: dict[str, str] = {
    "salsaAverageGradesIn9thGradeActual": "Faktiskt genomsnittligt meritvärde åk 9",
    "salsaAverageCalculated": "Modellberäknat (förväntat) meritvärde utifrån elevsammansättningen",
    "salsaAverageGradesIn9thGradeDeviation": "Avvikelse faktiskt – modellberäknat meritvärde (positivt = bättre än "
    "förväntat)",
    "salsaRequirementsReachedActual": "Faktisk andel elever som nått kunskapskraven i alla ämnen, %",
    "salsaRequirementsReachedCalculated": "Modellberäknad andel elever som nått kunskapskraven i alla ämnen, %",
    "salsaRequirementsReachedDeviation": "Avvikelse faktisk – modellberäknad andel (procentenheter)",
    "salsaNewlyImmigratedQuota": "Andel nyinvandrade elever, %",
    "salsaBoysQuota": "Andel pojkar, %",
    "salsaParentsEducation": "Föräldrarnas genomsnittliga utbildningsnivå (index ca 1–3)",
}

SalsaMetric = Literal[
    "salsaAverageGradesIn9thGradeActual",
    "salsaAverageCalculated",
    "salsaAverageGradesIn9thGradeDeviation",
    "salsaRequirementsReachedActual",
    "salsaRequirementsReachedCalculated",
    "salsaRequirementsReachedDeviation",
    "salsaNewlyImmigratedQuota",
    "salsaBoysQuota",
    "salsaParentsEducation",
]
StatisticsForm = Literal["fsk", "gr", "gran", "gy", "gyan"]
SecondaryForm = Literal["gy", "gyan"]
SupportList = Literal[
    "school-types",
    "geographical-areas",
    "municipality-schoolunit",
    "principal-organizer-types",
    "programs",
    "instruction-languages",
    "distance-studies",
    "adultTypeOfSchooling",
    "variants",
    "api-info",
]
SUPPORT_PATHS: dict[str, str] = {
    # Trailing slash as in the spec; requires typeOfSchooling=GY|GYAN.
    "variants": "support/variants/",
    "school-types": "support/school-types",
    "geographical-areas": "support/geographical-areas",
    "municipality-schoolunit": "support/municipality-schoolunit",
    "principal-organizer-types": "support/principal-organizer-types",
    "programs": "support/programs",
    "instruction-languages": "support/instruction-languages",
    "distance-studies": "support/distance-studies",
    # camelCase path segment per the official spec (not "adult-type-of-schooling").
    "adultTypeOfSchooling": "support/adultTypeOfSchooling",
    # Not wrapped in the {status,message,body} envelope.
    "api-info": "api-info",
}

CODES: dict[str, Any] = {
    "source": "Skolverket, Planerad utbildning – API v4",
    "base_url": "https://api.skolverket.se/planned-educations/v4",
    "accept_header": MEDIA_TYPE,
    "type_of_schooling": [{"code": code, **info} for code, info in TYPE_OF_SCHOOLING.items()],
    "statistics_school_forms": list(STATISTICS_FORMS),
    "principal_organizer_types": PRINCIPAL_ORGANIZER_TYPES,
    "survey_keys": [{"key": k, "description": d} for k, d in SURVEY_KEYS.items()],
    "survey_school_years": [{"code": c, "name": n} for c, n in SURVEY_SCHOOL_YEARS.items()],
    "survey_semester_format": "VTyy/HTyy, t.ex. VT26",
    "school_years_filter": "Årskurs som text, t.ex. '9' (filtret schoolYears i /v4/school-units; format ej "
    "specificerat)",
    "gy25_program_codes": [{"code": c, "name": n} for c, n in GY25_PROGRAM_CODES.items()],
    "gy_program_codes_note": "Utbildningstillfällen använder GY25-studievägskoder (BF25, EK25) och inriktningskoder "
    "(t.ex. FS25SKOLFORLAGD). Statistiken (programMetrics, riksvärden) och /support/programs använder koder utan "
    "'25' (EK, NA, IMA, IMV). Fullständig lista: skolverket_pe_support_list list='programs'.",
    "study_path_categories": ["VOCATIONAL_PROGRAM", "PRELIMINARY_PROGRAM_FOR_HIGHER_EDUCATION"],
    "program_variants_note": "Programvarianter (ProgramVariantList i /v4/education-events-new) hämtas med "
    "skolverket_pe_support_list list='variants', t.ex. 'Lärlingsutbildning, LU'.",
    "value_types": VALUE_TYPES,
    "value_parsing": "value är text med decimalkomma. 'cirka 370' och '~100' tolkas som ungefärliga tal "
    "(approximate=true); '.', '..', '*', '-' betyder saknat/undertryckt.",
    "time_period_formats": {
        "YYYY/YY": "Läsår, t.ex. 2024/25 (de flesta indikatorer, SALSA)",
        "YYYY": "Kalenderår, t.ex. 2025 (antagningspoäng gymnasiet)",
        "VTyy/HTyy": "Termin, t.ex. VT22 (nationella prov gymnasiet)",
    },
    "area_types": {
        "MUNICIPALITY": "Kommun – 4-siffrig kommunkod, t.ex. 0180 = Stockholm",
        "TOWN": "Ort – kod 'kommunkod-ortnamn', t.ex. '1489-Alingsås'",
        "COUNTY": "Län – 2-siffrig länskod (kommunkodens två första siffror)",
    },
    "coordinate_system_types": ["WGS84"],
    "distance_unit": "km (parametern distance och kilometersToSchoolUnit)",
    "address_types": ["VISITING_ADDRESS"],
    "instruction_languages_examples": ["swe", "eng"],
    "adult_type_of_school": [
        {"id": code, "name": name, **({"old_code": old} if old else {})} for code, name, old in ADULT_TYPE_OF_SCHOOL
    ],
    "adult_type_of_school_note": "Använd id (t.ex. 'vuxgy', 'vuxsfi', 'fhs', 'yh') i type_of_school; gamla koder "
    "(t.ex. 'komvuxgycourses', 'fhsk', 'sfi') översätts. Aktuell lista: skolverket_pe_support_list "
    "list='adultTypeOfSchooling'.",
    "adult_execution_conditions": EXECUTION_CONDITIONS,
    "adult_recommended_prior_knowledge": "'grundlaggande' (bara grundläggande behörighet krävs) eller tomt",
    "indicator_labels": METRIC_LABELS,
    "salsa_labels": SALSA_LABELS,
}

# --------------------------------------------------------------------------------------------------
# Output models
# --------------------------------------------------------------------------------------------------


class MetricValue(BaseModel):
    value: str | None = Field(default=None, description="Originalvärdet som text, t.ex. '15,5', 'cirka 370', '..'")
    number: float | None = Field(default=None, description="Tolkat tal; saknas när värdet är prickat/saknas")
    approximate: bool | None = Field(default=None, description="true när värdet är avrundat ('cirka', '~')")
    value_type: str | None = Field(default=None, description="valueType, t.ex. EXISTS, MISSING")
    time_period: str | None = Field(default=None, description="Läsår '2024/25', år '2025' eller termin 'VT22'")


class Indicator(MetricValue):
    """Latest value (newest period with a value) plus optional full history."""

    history: list[MetricValue] | None = Field(default=None, description="Alla värden, nyast först")


class PagedResult(BaseModel):
    total: int | None = Field(default=None, description="Totalt antal träffar (page.totalElements)")
    page: int = Field(default=0, description="Sidnummer (0-baserat)")
    page_size: int = 0
    total_pages: int | None = None
    truncated: bool = Field(default=False, description="Fler sidor finns – hämta nästa med page=next_page")
    next_page: int | None = None
    source_url: str | None = None


class SchoolFormEntry(BaseModel):
    code: str | None = None
    name: str | None = None
    school_years: list[str] | None = None


class SchoolUnitRow(BaseModel):
    code: str | None = Field(default=None, description="Skolenhetskod (8 siffror)")
    name: str | None = None
    principal_organizer_type: str | None = None
    school_orientation: str | None = None
    geographical_area_code: str | None = Field(default=None, description="Kommunkod")
    post_code_district: str | None = None
    abroad_school: bool | None = None
    resursskola: bool | None = None
    type_of_schooling: list[SchoolFormEntry] | None = None
    latitude: float | None = None
    longitude: float | None = None


class SchoolUnitSearchResult(PagedResult):
    variant: str = "full"
    rows: list[SchoolUnitRow] = Field(default_factory=list)


class Address(BaseModel):
    type: str | None = None
    street: str | None = None
    zip_code: str | None = None
    city: str | None = None


class SchoolUnitDetail(BaseModel):
    code: str
    name: str | None = None
    principal_organizer_type: str | None = None
    organisation_number: str | None = Field(default=None, description="organisationRegistryNumber (huvudmannens)")
    corporation_name: str | None = None
    company_form: str | None = None
    school_orientation: str | None = None
    geographical_area_code: str | None = Field(default=None, description="Kommunkod")
    abroad_school: bool | None = None
    resursskola: bool | None = None
    start_date: str | None = Field(default=None, description="schoolUnitStartDate")
    type_of_schooling: list[SchoolFormEntry] = Field(default_factory=list)
    email: str | None = Field(default=None, description="Endast med include_personal_data=true (personuppgift)")
    web: str | None = None
    telephone: str | None = None
    addresses: list[Address] = Field(default_factory=list)
    latitude: float | None = None
    longitude: float | None = None
    sweref99_n: float | None = None
    sweref99_e: float | None = None
    distance_km: float | None = Field(default=None, description="Avstånd i km från angiven punkt (/distanceFrom)")
    notes: list[str] | None = None
    source_url: str | None = None
    raw: dict[str, Any] | None = None


class ProgramStatistics(BaseModel):
    program_code: str | None = None
    indicators: dict[str, Indicator] = Field(default_factory=dict)
    attributes: dict[str, Any] | None = None


class StatisticsBlock(BaseModel):
    school_form: str
    school_form_name: str | None = None
    school_unit_code: str | None = None
    has_library: bool | None = None
    indicators: dict[str, Indicator] = Field(default_factory=dict)
    programs: list[ProgramStatistics] | None = None
    attributes: dict[str, Any] | None = None
    note: str | None = None
    source_url: str | None = None


class SchoolUnitStatisticsResult(BaseModel):
    school_unit_code: str
    available_school_forms: list[str] | None = None
    statistics: list[StatisticsBlock] = Field(default_factory=list)
    indicator_labels: dict[str, str] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)
    citation: str = CITATION


class NationalStatisticsResult(BaseModel):
    school_form: str
    program_code: str | None = None
    statistics: StatisticsBlock
    indicator_labels: dict[str, str] = Field(default_factory=dict)
    citation: str = CITATION


class SalsaRow(BaseModel):
    school_unit_code: str | None = None
    name: str | None = None
    geographical_area_code: str | None = Field(default=None, description="Kommunkod")
    metrics: dict[str, MetricValue] = Field(default_factory=dict)


class SalsaResult(BaseModel):
    time_period: str | None = None
    total: int = 0
    truncated: bool = False
    sorted_by: str | None = None
    rows: list[SalsaRow] = Field(default_factory=list)
    metric_labels: dict[str, str] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)
    source_url: str | None = None
    citation: str = CITATION


class SurveyQuestion(BaseModel):
    subject: str | None = Field(default=None, description="questionSubject, t.ex. 'Trygghet'")
    question: str | None = Field(default=None, description="questionDescription")
    average: str | None = Field(default=None, description="Medelvärde 0–10 som text, t.ex. '7,6'")
    average_number: float | None = None
    ratios: dict[str, str] = Field(default_factory=dict, description="ratio*-andelar som text ('44%', '-' = dolt)")


class SurveyGroup(BaseModel):
    school_unit_code: str | None = None
    school_year: str | None = Field(default=None, description="ak5, ak8 eller ar2 (elevenkäter)")
    semester: str | None = Field(default=None, description="VTyy/HTyy")
    answers: str | None = Field(default=None, description="noOfAnswers")
    group_size: str | None = Field(default=None, description="noInGroup")
    answer_rate: str | None = Field(default=None, description="answeringFrequency, t.ex. '90%'")
    questions: dict[str, SurveyQuestion] = Field(default_factory=dict)
    other: dict[str, Any] | None = None


class SurveyResult(BaseModel):
    survey: str
    description: str | None = None
    groups: list[SurveyGroup] | None = None
    rows: list[dict[str, Any]] | None = None
    note: str | None = None
    source_url: str | None = None


class SchoolUnitSurveysResult(BaseModel):
    school_unit_code: str
    format: str
    available_surveys: list[str] | None = None
    surveys: list[SurveyResult] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    citation: str = "Källa: Skolverket/Skolinspektionen, Skolenkäten via Planerad utbildning (API v4)"


class StudyPathRef(BaseModel):
    code: str | None = Field(default=None, description="studyPathCode för inriktningen, t.ex. 'FS25SKOLFORLAGD'")
    name: str | None = None


class EducationEventRow(BaseModel):
    id: str | None = Field(default=None, description="Utbildningstillfällets id, t.ex. 'edu-event-30530571-BF25'")
    school_unit_code: str | None = Field(default=None, description="Skolenhetskod (8 siffror)")
    school_unit_name: str | None = None
    study_path_code: str | None = Field(default=None, description="Studievägskod, t.ex. 'BF25'")
    study_path_name: str | None = None
    type_of_schooling: str | None = Field(default=None, description="gy eller gyan")
    study_path_category: str | None = Field(
        default=None, description="VOCATIONAL_PROGRAM eller PRELIMINARY_PROGRAM_FOR_HIGHER_EDUCATION"
    )
    school_orientation: str | None = None
    principal_organizer_type: str | None = None
    geographical_area_code: str | None = Field(default=None, description="Kommunkod")
    city: str | None = Field(default=None, description="visitingAddressCity")
    street: str | None = Field(default=None, description="visitingAddressStreet")
    resursskola: bool | None = None
    start_date: str | None = None
    end_date: str | None = None
    admission_points_min: float | str | None = Field(default=None, description="Lägsta antagningspoäng")
    admission_points_average: float | str | None = Field(default=None, description="Genomsnittlig antagningspoäng")
    admission_points_year: str | None = Field(default=None, description="admissionPointsSemester, t.ex. '2025'")
    study_paths: list[StudyPathRef] | None = Field(default=None, description="Inriktningar (studyPaths)")
    program_variants: Any = Field(default=None, description="programVariantList/programVarianter")
    study_path_variants: Any = None
    latitude: float | None = None
    longitude: float | None = None


class EducationEventsResult(PagedResult):
    school_unit_code: str | None = None
    study_path_code: str | None = None
    variant: str = "full"
    count_source: str | None = None
    rows: list[EducationEventRow] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class AdultEventRow(BaseModel):
    education_event_id: str | None = None
    title: str | None = None
    provider_name: str | None = None
    type_of_school: str | None = None
    town: str | None = None
    municipality: str | None = None
    county: str | None = None
    geographical_area_code: str | None = None
    semester_start_from: str | None = None
    last_application_date: str | None = None
    pace_of_study: str | None = None
    extent: str | None = None
    distance_learning: bool | None = None
    credits: str | None = None
    credits_system: str | None = None
    location: str | None = None
    contractor: str | None = None
    execution_condition: int | None = Field(
        default=None, description="0 = ej fastställt, 1 = datum satt, 2/3 = löpande kursstart"
    )
    recommended_prior_knowledge: str | None = None


class AdultEventSearchResult(PagedResult):
    count_only: bool = False
    rows: list[AdultEventRow] = Field(default_factory=list)


class AdultEventContact(BaseModel):
    email: str | None = None
    web: str | None = None
    telephone: str | None = None
    addresses: list[Address] = Field(default_factory=list)


class AdultEventDetail(BaseModel):
    education_event_id: str
    title: str | None = None
    study_path_name: str | None = None
    provider_name: str | None = None
    organizer_name: str | None = None
    type_of_school: str | None = None
    municipality: str | None = None
    town: str | None = None
    geographical_area_code: str | None = None
    distance_learning: bool | None = None
    semester_start_from: str | None = None
    last_application_date: str | None = None
    pace_of_study: str | None = None
    extent: str | None = None
    time_of_study: str | None = None
    credits: str | None = None
    credits_system: str | None = None
    instruction_languages: str | None = None
    areas_of_interest: str | None = None
    description: str | None = None
    provider_description: str | None = None
    requirements: str | None = None
    fee: str | None = None
    eligible_for_student_aid: bool | None = None
    contractor: str | None = None
    execution_condition: int | None = Field(
        default=None, description="0 = ej fastställt, 1 = datum satt, 2/3 = löpande kursstart"
    )
    contact: AdultEventContact | None = None
    other: dict[str, Any] | None = None
    notes: list[str] | None = None
    source_url: str | None = None


class Direction(BaseModel):
    direction_id: int | None = None
    name: str | None = None
    name_en: str | None = None


class Area(BaseModel):
    area_id: int | None = None
    name: str | None = None
    name_en: str | None = None
    directions: list[Direction] = Field(default_factory=list)


class AreasResult(BaseModel):
    total: int = 0
    areas: list[Area] = Field(default_factory=list)
    hint: str = "Använd direction_id i direction_ids till skolverket_pe_search_adult_education_events"
    source_url: str | None = None


class SupportListResult(BaseModel):
    name: str
    total: int = 0
    truncated: bool = False
    entries: list[Any] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    source_url: str | None = None


class SecondaryProgramRef(BaseModel):
    school_unit_code: str = Field(description="Skolenhetskod (8 siffror)")
    study_path_code: str = Field(description="Studievägs-/programkod, t.ex. 'EK' eller 'NA25'")
    type_of_schooling: SecondaryForm = Field(default="gy", description="gy eller gyan")


class CompareSecondaryResult(BaseModel):
    requested: list[dict[str, str]] = Field(default_factory=list)
    result: Any = None
    note: str = (
        "Svar enligt specifikationen: per par schoolUnitCode, studyPathCode, admissionPointsMin/-Average, "
        "studentsInProgram, studentsInSemester, studyPaths och typeOfSchooling. Eventuella värdeobjekt "
        "({value,valueType,timePeriod}) normaliseras."
    )
    source_url: str | None = None
    citation: str = CITATION


class DocumentGroup(BaseModel):
    type_of_schooling: str | None = None
    documents: list[Any] = Field(default_factory=list)


class DocumentsResult(BaseModel):
    school_unit_code: str
    type_of_schooling: str | None = None
    total: int = 0
    truncated: bool = False
    groups: list[DocumentGroup] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    source_url: str | None = None


# --------------------------------------------------------------------------------------------------
# Input normalisation
# --------------------------------------------------------------------------------------------------


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return re.sub(r"[\s_\-]+", " ", stripped.lower()).strip()


def normalize_school_unit_code(value: str) -> str:
    text = re.sub(r"\s+", "", str(value))
    if not re.fullmatch(r"\d{8}", text):
        raise InvalidInputError(f"Skolenhetskoden måste vara 8 siffror (t.ex. '44673074'), fick {value!r}")
    return text


def normalize_area_code(value: str) -> str:
    text = re.sub(r"\s+", "", str(value))
    if text.isdigit() and len(text) in (1, 3):
        text = text.zfill(len(text) + 1)
    if not re.fullmatch(r"\d{2}|\d{4}", text):
        raise InvalidInputError(
            f"Ogiltig geografisk kod {value!r}: ange kommunkod (4 siffror, t.ex. '0180') eller länskod (2 siffror)"
        )
    return text


def normalize_type_of_schooling(value: str, allowed: tuple[str, ...] | None = None) -> str:
    folded = _fold(value)
    code = folded.replace(" ", "") if folded.replace(" ", "") in TYPE_OF_SCHOOLING else _SCHOOLING_ALIASES.get(folded)
    valid = allowed or tuple(TYPE_OF_SCHOOLING)
    if code is None or code not in valid:
        raise InvalidInputError(f"Okänd skolform {value!r}. Giltiga v4-koder här: {', '.join(valid)} (se {CODES_URI})")
    return code


def normalize_adult_type_of_school(value: str) -> str:
    """Translate retired codes (``komvuxgycourses``, ``fhsk``, ``sfi`` …) to the current v4 id; other values are
    passed verbatim because the list is maintained upstream (/v4/support/adultTypeOfSchooling)."""
    text = value.strip()
    if not text:
        raise InvalidInputError("type_of_school får inte vara tom")
    return _ADULT_OLD_CODES.get(text.lower(), text)


def normalize_execution_condition(value: str | int | None) -> str | None:
    if value is None:
        return None
    text = re.sub(r"\s+", "", str(value))
    if not text:
        return None
    if not re.fullmatch(r"[0-3](?:,[0-3])*", text):
        raise InvalidInputError(
            "execution_condition ska vara en eller flera koder 0–3: 0 = ej fastställt, 1 = datum satt, "
            f"2/3 = löpande kursstart (fick {value!r})"
        )
    return text


def normalize_adult_event_id(value: str) -> str:
    # Spec: minLength 5. Only one safe path segment: a leading alphanumeric excludes "." and ".." (which
    # httpx would resolve as dot segments onto another endpoint).
    text = value.strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{4,}", text):
        raise InvalidInputError(
            f"Ogiltigt educationEventId {value!r}: minst 5 tecken (bokstäver, siffror, '.', '_', '-'), "
            "t.ex. 'e.myh.27538'"
        )
    return text


def normalize_principal_organizer_type(value: str) -> str:
    text = value.strip()
    if not text:
        raise InvalidInputError("principal_organizer_type får inte vara tom")
    return _ORGANIZER_ALIASES.get(_fold(text).replace(" ", ""), text)


def normalize_program_code(value: str, label: str = "Programkoden") -> str:
    text = value.strip().upper()
    # Inriktningskoder can be long, e.g. "FS25SKOLFORLAGD" in the spec examples.
    if not re.fullmatch(r"[A-Z0-9]{2,25}", text):
        raise InvalidInputError(f"{label} {value!r} ser inte ut som en kod (t.ex. 'NA25', 'EK', 'IMV')")
    return text


def normalize_survey_key(value: str) -> str:
    key = re.sub(r"[\s_\-]+", "", value.strip().lower())
    if key not in SURVEY_KEYS:
        raise InvalidInputError(f"Okänd enkät {value!r}. Giltiga: {', '.join(SURVEY_KEYS)}")
    return key


_SORT_PROPERTY = r"[A-Za-z][A-Za-z0-9_.]*"


def parse_sort(value: str | None) -> list[tuple[str, str | None]]:
    """Parse a sort expression into ``[(property, direction|None)]``. Accepts the Spring style used by most
    endpoints ('name,asc', multi-key 'schoolUnitName,studyPathName,asc' where the direction applies to the
    preceding keys) and the colon style of /adult-education-events ('titleSv:asc , typeOfSchool:desc')."""
    if value is None or not value.strip():
        return []
    keys: list[tuple[str, str | None]] = []
    pending: list[str] = []
    for token in (t.strip() for t in value.split(",")):
        if not token:
            continue
        if re.fullmatch(r"(?i)asc|desc", token):
            if not pending:
                raise InvalidInputError(f"sort: riktningen {token!r} saknar egenskap före sig ({value!r})")
            keys.extend((p, token.lower()) for p in pending)
            pending = []
        elif match := re.fullmatch(rf"({_SORT_PROPERTY})\s*:\s*(asc|desc)", token, flags=re.IGNORECASE):
            keys.extend((p, None) for p in pending)
            pending = []
            keys.append((match.group(1), match.group(2).lower()))
        elif re.fullmatch(_SORT_PROPERTY, token):
            pending.append(token)
        else:
            raise InvalidInputError(
                "sort ska ha formen 'egenskap', 'egenskap,asc|desc', 'a,b,asc' eller 'a:asc,b:desc' "
                f"(t.ex. 'name,asc'), fick {value!r}"
            )
    keys.extend((p, None) for p in pending)
    return keys


def spring_sort(value: str | None) -> str | list[str] | None:
    """Spring Data ``sort``: one 'a,b,dir' string when all keys share a direction, otherwise one repeated
    ``sort`` parameter per key."""
    keys = parse_sort(value)
    if not keys:
        return None
    directions = {d for _, d in keys}
    if len(directions) == 1:
        (direction,) = directions
        return ",".join([p for p, _ in keys] + ([direction] if direction else []))
    return [f"{p},{d}" if d else p for p, d in keys]


def colon_sort(value: str | None) -> str | None:
    """Sort for /adult-education-events, spec format 'titleSv:asc,typeOfSchool:desc'."""
    keys = parse_sort(value)
    return ",".join(f"{p}:{d}" if d else p for p, d in keys) or None


def check_page(page: int, size: int) -> tuple[int, int]:
    if page < 0:
        raise InvalidInputError("page är 0-baserad och får inte vara negativ")
    return page, clamp(size, 1, MAX_PAGE_SIZE)


def coordinate_params(
    latitude: float | None, longitude: float | None, radius_km: float | None, *, with_system: bool
) -> dict[str, Any]:
    if (latitude is None) != (longitude is None):
        raise InvalidInputError("Ange både latitude och longitude (WGS84), eller ingen av dem")
    if latitude is None or longitude is None:
        if radius_km is not None:
            raise InvalidInputError("radius_km kräver latitude och longitude")
        return {}
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        raise InvalidInputError("latitude/longitude ligger utanför giltigt WGS84-intervall")
    if radius_km is not None and radius_km <= 0:
        raise InvalidInputError("radius_km måste vara större än 0")
    # The API wants decimal degrees with at least 4 decimals (community finding, spec §5.1.1).
    params: dict[str, Any] = {"latitude": f"{latitude:.6f}", "longitude": f"{longitude:.6f}"}
    if radius_km is not None:
        params["distance"] = f"{radius_km:g}"
    if with_system:
        params["coordinateSystemType"] = "WGS84"
    return params


def _string_list(value: str | list[str] | None) -> list[str]:
    if value is None:
        return []
    items = value if isinstance(value, list) else re.split(r"[,;]", value)
    return [str(v).strip() for v in items if str(v).strip()]


def normalize_semester_start(value: str | list[str] | None) -> str | None:
    """semesterStartFrom per the spec: 'ÅÅÅÅ-MM-DDTOÅÅÅÅ-MM-DD', several ranges comma-separated
    ('2020-01-01TO2020-05-31,2020-08-01TO2020-12-31'). Spaces around TO and lower-case 'to' are accepted;
    the value is sent upper-case without spaces. A single date is passed on as is."""
    parts = _string_list(value)
    if not parts:
        return None
    out: list[str] = []
    for part in parts:
        match = re.fullmatch(r"(\d{4}-\d{2}-\d{2})(?:\s*TO\s*(\d{4}-\d{2}-\d{2}))?", part, flags=re.IGNORECASE)
        dates: list[datetime.date] = []
        if match:
            for text in filter(None, match.groups()):
                try:
                    dates.append(datetime.date.fromisoformat(text))
                except ValueError:
                    dates = []
                    break
        if not match or not dates or (len(dates) == 2 and dates[0] > dates[1]):
            raise InvalidInputError(
                "semester_start_from ska vara 'ÅÅÅÅ-MM-DD' eller intervall 'ÅÅÅÅ-MM-DDTOÅÅÅÅ-MM-DD', flera "
                f"kommaseparerade (t.ex. '2026-08-01TO2026-12-31'); fick {part!r}"
            )
        out.append("TO".join(d.isoformat() for d in dates))
    return ",".join(out)


# --------------------------------------------------------------------------------------------------
# Response helpers
# --------------------------------------------------------------------------------------------------


def unwrap(response: ApiResponse) -> Any:
    """Return ``body`` from the ``{status, message, body}`` envelope (or the bare payload)."""
    data = response.data
    if isinstance(data, str):
        raise UpstreamError(
            SOURCE,
            "Oväntat svar (inte JSON) från Planerad utbildning",
            status=response.status,
            url=response.url,
            detail=data.strip()[:200] or None,
        )
    if isinstance(data, dict) and "body" in data and ("status" in data or "message" in data):
        status = data.get("status")
        # The examples say "OK" while the schema enum says "200 OK"; any 2xx status is success.
        text = str(status).strip().upper() if status is not None else ""
        if text not in ("OK", "") and not re.match(r"2\d\d\b", text):
            raise UpstreamError(
                SOURCE,
                f"API:t svarade med status {status}",
                status=response.status,
                url=response.url,
                detail=data.get("message") or None,
            )
        return data.get("body")
    return data


def embedded_items(body: Any) -> list[Any]:
    """Items of a HAL page. The ``_embedded`` key name varies per resource (and is unverified for
    education events), so the first list under ``_embedded`` is used. Spring omits ``_embedded``
    entirely for empty pages."""
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        embedded = body.get("_embedded")
        if isinstance(embedded, dict):
            for value in embedded.values():
                if isinstance(value, list):
                    return value
    return []


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def page_fields(body: Any, page: int, size: int, count: int) -> dict[str, Any]:
    meta = body.get("page") if isinstance(body, dict) else None
    links = body.get("_links") if isinstance(body, dict) else None
    total = total_pages = None
    number, page_size = page, size
    if isinstance(meta, dict):
        total = _int(meta.get("totalElements"))
        total_pages = _int(meta.get("totalPages"))
        number = _int(meta.get("number")) if _int(meta.get("number")) is not None else page
        page_size = _int(meta.get("size")) or size
    if isinstance(links, dict) and "next" in links:
        has_next = True
    elif total_pages is not None:
        has_next = number + 1 < total_pages
    elif total is not None:
        has_next = (number + 1) * page_size < total
    else:
        has_next = False
    if total is None and not isinstance(meta, dict):
        total = count if not has_next else None
    return {
        "total": total,
        "page": number,
        "page_size": page_size,
        "total_pages": total_pages,
        "truncated": has_next,
        "next_page": number + 1 if has_next else None,
    }


def strip_links(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: strip_links(v) for k, v in value.items() if k not in ("_links", "links")}
    if isinstance(value, list):
        return [strip_links(v) for v in value]
    return value


def clean(value: Any) -> Any:
    return compact(strip_links(value))


def _str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    text = str(value)
    return text if text.strip() else None


_CDATA = re.compile(r"^\s*<!\[CDATA\[(.*)\]\]>\s*$", re.DOTALL)


def _text(value: Any) -> str | None:
    """Like ``_str`` but cleans adult-education text: unwraps '<![CDATA[…]]>' and treats the string
    'null' (seen as paceOfStudy in the spec examples) as missing."""
    text = _str(value)
    if text is None:
        return None
    if match := _CDATA.match(text):
        text = match.group(1).strip()
    return None if not text or text.lower() == "null" else text


def _bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    return None


def _float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(str(value).strip().replace(",", "."))
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def _coordinate(value: Any) -> float | None:
    number = _float(value)
    # 0/"0" means "unknown" in the compact list (spec §5.1.2).
    return None if number is None or number == 0 else number


def _first(item: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = item.get(key)
        if value is not None and value != "":
            return value
    return None


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    return [] if value is None else [value]


def _link_targets(body: Any) -> list[tuple[str, str]]:
    """``(rel, last path segment of href)`` for every link in ``body._links``."""
    links = body.get("_links") if isinstance(body, dict) else None
    out: list[tuple[str, str]] = []
    if not isinstance(links, dict):
        return out
    for rel, link in links.items():
        for entry in _as_list(link):
            href = entry.get("href") if isinstance(entry, dict) else None
            segment = str(href).rstrip("/").rsplit("/", 1)[-1].split("?")[0] if href else ""
            out.append((rel, segment))
    return out


# --- statistics values ------------------------------------------------------------------------------

_APPROX_PREFIX = re.compile(r"^(?:(?:cirka|ca\.?|ungefär)\s+|[~≈]\s*)", re.IGNORECASE)


def parse_number(raw: Any) -> tuple[float | None, bool]:
    """Parse Swedish formatted statistics values. Returns ``(number, approximate)``.

    "15,5" → 15.5; "cirka 370" → (370, True); "~100" → (100, True); "44%" → 44; ".", "..", "*", "-" → None.
    """
    if raw is None or isinstance(raw, bool):
        return None, False
    if isinstance(raw, int | float):
        number = float(raw)
        return (number, False) if math.isfinite(number) else (None, False)
    text = str(raw).strip()
    if text.lower() in _MISSING_STRINGS:
        return None, False
    approximate = False
    while match := _APPROX_PREFIX.match(text):
        approximate = True
        text = text[match.end() :]
    text = text.replace(" ", "").replace(" ", "").replace(" ", "").replace("−", "-")
    text = text.rstrip("%").replace(",", ".")
    try:
        number = float(text)
    except ValueError:
        return None, False
    if not math.isfinite(number):
        return None, False
    return number, approximate


def metric_value(obj: dict[str, Any]) -> MetricValue:
    raw = obj.get("value")
    value_type = _str(obj.get("valueType"))
    number, approximate = (None, False) if value_type in _NO_VALUE_TYPES else parse_number(raw)
    if number is not None and value_type in _APPROXIMATE_VALUE_TYPES:
        approximate = True
    return MetricValue(
        value=_str(raw),
        number=number,
        approximate=True if approximate and number is not None else None,
        value_type=value_type,
        time_period=_str(obj.get("timePeriod")),
    )


def _is_metric_obj(value: Any) -> bool:
    return isinstance(value, dict) and "value" in value and ("valueType" in value or "timePeriod" in value)


def _is_metric(value: Any) -> bool:
    if _is_metric_obj(value):
        return True
    return (
        isinstance(value, list)
        and any(_is_metric_obj(v) for v in value)
        and all(v is None or _is_metric_obj(v) for v in value)
    )


def _period_key(period: str | None) -> float | None:
    if not period:
        return None
    text = period.strip().upper()
    if match := re.fullmatch(r"(\d{4})/(\d{2}|\d{4})", text):
        return int(match.group(1)) + 0.5
    if match := re.fullmatch(r"(\d{4})", text):
        return float(match.group(1))
    if match := re.fullmatch(r"(VT|HT)(\d{2}|\d{4})", text):
        year = int(match.group(2))
        year = year + 2000 if year < 100 else year
        return year + (0.0 if match.group(1) == "VT" else 0.5)
    return None


def _has_value(entry: MetricValue) -> bool:
    if entry.number is not None:
        return True
    return (
        entry.value is not None
        and entry.value.strip().lower() not in _MISSING_STRINGS
        and entry.value_type not in _NO_VALUE_TYPES
    )


def ordered_values(raw: Any) -> list[MetricValue]:
    """All values of an indicator, newest first. Upstream already sends newest first; sort when every
    period can be parsed, otherwise keep upstream order (v3 sent single objects, v4 arrays)."""
    entries = [metric_value(e) for e in _as_list(raw) if isinstance(e, dict)]
    keys = [_period_key(e.time_period) for e in entries]
    if entries and all(k is not None for k in keys):
        pairs = sorted(zip(keys, entries, strict=True), key=lambda pair: pair[0] or 0.0, reverse=True)
        entries = [entry for _, entry in pairs]
    return entries


def build_indicator(raw: Any, include_history: bool) -> Indicator | None:
    entries = ordered_values(raw)
    if not entries:
        return None
    latest = next((e for e in entries if _has_value(e)), entries[0])
    indicator = Indicator(**latest.model_dump())
    if include_history:
        indicator.history = entries
    return indicator


def latest_value(raw: Any) -> MetricValue | None:
    entries = ordered_values(raw)
    if not entries:
        return None
    return next((e for e in entries if _has_value(e)), entries[0])


def flatten_metrics(
    obj: dict[str, Any], include_history: bool, skip: frozenset[str] = frozenset()
) -> tuple[dict[str, Indicator], dict[str, Any]]:
    indicators: dict[str, Indicator] = {}
    attributes: dict[str, Any] = {}
    for key, value in obj.items():
        if key in skip or key in ("_links", "links", "programMetrics"):
            continue
        if _is_metric(value):
            indicator = build_indicator(value, include_history)
            if indicator is not None:
                indicators[key] = indicator
            continue
        cleaned = clean(value)
        if cleaned is None or cleaned in ([], {}, ""):
            continue
        attributes[key] = cleaned
    return indicators, attributes


# School-level fields that v3 repeated inside every programMetrics element.
_PROGRAM_DUPLICATE_KEYS = frozenset(
    {
        "schoolUnit",
        "specialTeacherPositions",
        "studentsPerTeacherQuota",
        "certifiedTeachersQuota",
        "specialEducatorsQuota",
        "docLinks",
        "hasLibrary",
    }
)


def program_code_candidates(code: str) -> list[str]:
    """Statistics use programme codes without the GY25 suffix ('NA'), education events use 'NA25'."""
    candidates = [code]
    if len(code) > 2 and code.endswith("25"):
        candidates.append(code[:-2])
    return candidates


def select_programs(programs: list[ProgramStatistics], program_code: str) -> tuple[list[ProgramStatistics], str | None]:
    candidates = program_code_candidates(program_code)
    codes = [(p, (p.program_code or "").upper()) for p in programs]
    chosen = [p for p, c in codes if c in candidates]
    if not chosen:
        chosen = [p for p, c in codes if any(c.startswith(candidate) for candidate in candidates)]
    if not chosen:
        available = ", ".join(c or "?" for _, c in codes) or "inga"
        return [], f"Programkoden {program_code} saknas i statistiken. Tillgängliga: {available}"
    return chosen, None


def statistics_block(
    body: Any,
    school_form: str,
    *,
    include_history: bool,
    program_code: str | None,
    source_url: str | None,
) -> StatisticsBlock:
    data = body if isinstance(body, dict) else {}
    indicators, attributes = flatten_metrics(data, include_history)
    school_unit = attributes.pop("schoolUnit", None)
    # Not in the v4 spec examples; kept for tolerance of older payloads.
    has_library = attributes.pop("hasLibrary", None)
    raw_programs = data.get("programMetrics")
    program_items = [p for p in raw_programs if isinstance(p, dict)] if isinstance(raw_programs, list) else []
    if school_unit is None:
        # gy/gyan (spec example): no top-level schoolUnit, it is repeated inside every programMetrics element.
        school_unit = next((p.get("schoolUnit") for p in program_items if p.get("schoolUnit")), None)
    block = StatisticsBlock(
        school_form=school_form,
        school_form_name=TYPE_OF_SCHOOLING.get(school_form, {}).get("name"),
        school_unit_code=_str(school_unit),
        has_library=_bool(has_library),
        indicators=indicators,
        attributes=attributes or None,
        source_url=source_url,
    )
    if isinstance(raw_programs, list):
        skip = frozenset(
            (_PROGRAM_DUPLICATE_KEYS & set(data)) | {"programCode", "studyPathCode", "schoolUnit", "docLinks"}
        )
        programs: list[ProgramStatistics] = []
        for item in program_items:
            p_indicators, p_attributes = flatten_metrics(item, include_history, skip)
            programs.append(
                ProgramStatistics(
                    program_code=_str(_first(item, "programCode", "studyPathCode")),
                    indicators=p_indicators,
                    attributes=p_attributes or None,
                )
            )
        if program_code:
            programs, note = select_programs(programs, program_code)
            block.note = note
        block.programs = programs
    elif program_code:
        block.note = "program_code ignoreras: svaret innehåller ingen programMetrics-lista"
    return block


def drop_program_history(blocks: list[StatisticsBlock]) -> bool:
    """Remove per-programme time series (the bulk of a large gy/gyan answer); school-level history stays."""
    dropped = False
    for block in blocks:
        for program in block.programs or []:
            for indicator in program.indicators.values():
                if indicator.history is not None:
                    indicator.history = None
                    dropped = True
    return dropped


def labels_for(blocks: list[StatisticsBlock]) -> dict[str, str]:
    keys: list[str] = []
    for block in blocks:
        keys.extend(block.indicators)
        for program in block.programs or []:
            keys.extend(program.indicators)
    return {k: METRIC_LABELS[k] for k in dict.fromkeys(keys) if k in METRIC_LABELS}


def normalize_metrics_deep(value: Any, include_history: bool) -> Any:
    """Replace every ``{value, valueType, timePeriod}`` object/array in an unknown structure by a
    normalised indicator."""
    if _is_metric(value):
        indicator = build_indicator(value, include_history)
        return indicator.model_dump(exclude_none=True) if indicator else None
    if isinstance(value, dict):
        return {k: normalize_metrics_deep(v, include_history) for k, v in value.items()}
    if isinstance(value, list):
        return [normalize_metrics_deep(v, include_history) for v in value]
    return value


# --- rows -------------------------------------------------------------------------------------------


def school_forms(value: Any) -> list[SchoolFormEntry]:
    out: list[SchoolFormEntry] = []
    for entry in _as_list(value):
        if isinstance(entry, dict):
            years = entry.get("schoolYears")
            out.append(
                SchoolFormEntry(
                    code=_str(_first(entry, "code", "typeOfSchoolingCode")),
                    name=_str(entry.get("displayName")),
                    school_years=[str(y) for y in years] if isinstance(years, list) else None,
                )
            )
        elif isinstance(entry, str):
            out.append(SchoolFormEntry(code=entry))
    return out


def school_unit_row(item: dict[str, Any]) -> SchoolUnitRow:
    forms = school_forms(item.get("typeOfSchooling"))
    if not forms:
        # /compact-school-units: comma-separated codes, e.g. "gy,gr".
        forms = [SchoolFormEntry(code=c) for c in _string_list(_str(item.get("educationEventTypeOfSchooling")))]
    return SchoolUnitRow(
        code=_str(_first(item, "code", "schoolUnitCode")),
        name=_str(_first(item, "name", "schoolUnitName")),
        principal_organizer_type=_str(item.get("principalOrganizerType")),
        school_orientation=_str(item.get("schoolOrientation")),
        geographical_area_code=_str(item.get("geographicalAreaCode")),
        post_code_district=_str(item.get("postCodeDistrict")),
        abroad_school=_bool(item.get("abroadSchool")),
        resursskola=_bool(item.get("resursSkola")),
        type_of_schooling=forms or None,
        latitude=_coordinate(_first(item, "wgs84Latitude", "wgs84_Lat", "wgs84Lat")),
        longitude=_coordinate(_first(item, "wgs84Longitude", "wgs84_Long", "wgs84Long")),
    )


def addresses(value: Any) -> list[Address]:
    return [
        Address(
            type=_str(a.get("type")),
            street=_str(a.get("street")),
            zip_code=_str(_first(a, "zipCode", "postalCode", "postCode")),
            city=_str(a.get("city")),
        )
        for a in _as_list(value)
        if isinstance(a, dict)
    ]


def school_unit_detail(
    code: str, body: dict[str, Any], *, include_personal_data: bool, allow_personal_data: bool = True
) -> SchoolUnitDetail:
    contact = body.get("contactInfo") if isinstance(body.get("contactInfo"), dict) else {}
    email = _str(contact.get("email"))
    detail = SchoolUnitDetail(
        code=_str(body.get("code")) or code,
        name=_str(body.get("name")),
        principal_organizer_type=_str(body.get("principalOrganizerType")),
        organisation_number=_str(body.get("organisationRegistryNumber")),
        corporation_name=_str(body.get("corporationName")),
        company_form=_str(body.get("companyForm")),
        school_orientation=_str(body.get("schoolOrientation")),
        geographical_area_code=_str(body.get("geographicalAreaCode")),
        abroad_school=_bool(body.get("abroadSchool")),
        resursskola=_bool(body.get("resursSkola")),
        start_date=_str(body.get("schoolUnitStartDate")),
        type_of_schooling=school_forms(body.get("typeOfSchooling")),
        # The spec example is a named person's address (contactInfo.email), so it is personal data.
        email=email if include_personal_data else None,
        web=_str(contact.get("web")),
        telephone=_str(contact.get("telephone")),
        addresses=addresses(contact.get("addresses")),
        latitude=_coordinate(_first(body, "wgs84_Lat", "wgs84Lat", "wgs84Latitude")),
        longitude=_coordinate(_first(body, "wgs84_Long", "wgs84Long", "wgs84Longitude")),
        sweref99_n=_coordinate(_first(body, "sweRef_N", "sweRefN", "sweref99N")),
        sweref99_e=_coordinate(_first(body, "sweRef_E", "sweRefE", "sweref99E")),
    )
    if email and not include_personal_data:
        detail.notes = [personal_data_note(allow_personal_data)]
    return detail


def redacted_raw(body: dict[str, Any], *, include_personal_data: bool) -> dict[str, Any]:
    """Original body without links; contactInfo (holds personal e-mail addresses) only when opted in."""
    raw = strip_links(body)
    if not include_personal_data:
        raw.pop("contactInfo", None)
    return raw


def parse_distance_km(body: Any) -> float | None:
    """/distanceFrom answers ``{schoolUnitCode, schoolUnitName, requestLatitude, requestLongitude,
    kilometersToSchoolUnit: "1003,795"}``; a bare number is tolerated."""
    if isinstance(body, dict):
        body = _first(body, "kilometersToSchoolUnit", "distance", "km")
    number, _ = parse_number(body)
    return number


def _points(value: Any) -> float | str | None:
    number, _ = parse_number(value)
    return number if number is not None else _str(value)


_EDUCATION_EVENT_KEYS = frozenset({"id", "schoolUnitCode", "studyPathCode", "studyPathName"})


def education_event_row(item: dict[str, Any], school_form: str | None = None) -> EducationEventRow:
    tos = item.get("typeOfSchooling")
    tos_code = tos.get("code") if isinstance(tos, dict) else tos
    study_paths = [
        StudyPathRef(
            code=_str(_first(sp, "studyPathCode", "code")),
            name=_str(_first(sp, "studyPathName", "name")),
        )
        for sp in _as_list(item.get("studyPaths"))
        if isinstance(sp, dict)
    ]
    variants = clean(item.get("programVariantList")) or _str(item.get("programVarianter"))
    return EducationEventRow(
        id=_str(item.get("id")),
        school_unit_code=_str(item.get("schoolUnitCode")),
        school_unit_name=_str(item.get("schoolUnitName")),
        study_path_code=_str(item.get("studyPathCode")),
        study_path_name=_str(item.get("studyPathName")),
        type_of_schooling=_str(tos_code) or _str(item.get("educationEventTypeOfSchooling")) or school_form,
        study_path_category=_str(item.get("studyPathCategory")),
        school_orientation=_str(item.get("schoolOrientation")),
        principal_organizer_type=_str(item.get("principalOrganizerType")),
        geographical_area_code=_str(item.get("geographicalAreaCode")),
        city=_str(item.get("visitingAddressCity")),
        street=_str(item.get("visitingAddressStreet")),
        resursskola=_bool(item.get("resursSkola")),
        start_date=_str(item.get("startDate")),
        end_date=_str(item.get("endDate")),
        admission_points_min=_points(item.get("admissionPointsMin")),
        admission_points_average=_points(item.get("admissionPointsAverage")),
        admission_points_year=_str(item.get("admissionPointsSemester")),
        study_paths=study_paths or None,
        program_variants=variants or None,
        study_path_variants=clean(item.get("studyPathsVariants")) or None,
        latitude=_coordinate(item.get("wgs84Latitude")),
        longitude=_coordinate(item.get("wgs84Longitude")),
    )


def education_event_rows(body: Any) -> list[EducationEventRow]:
    """Education events in the three shapes of the spec: a HAL page (``_embedded.educationEvents`` /
    ``compactEducationEvents``), the per-school map keyed by school form (``{"gy": [...]}`` from
    /school-units/{code}/education-events) and a single object (…/education-events/{studyPathCode})."""
    if isinstance(body, list):
        return [education_event_row(i) for i in body if isinstance(i, dict)]
    if not isinstance(body, dict) or not body:
        return []
    if "_embedded" in body or "page" in body:
        return [education_event_row(i) for i in embedded_items(body) if isinstance(i, dict)]
    if _EDUCATION_EVENT_KEYS & body.keys():
        return [education_event_row(body)]
    rows: list[EducationEventRow] = []
    for form, items in body.items():
        if form in ("_links", "links") or not isinstance(items, list):
            continue
        rows.extend(education_event_row(i, form) for i in items if isinstance(i, dict))
    return rows


def parse_count(body: Any) -> int | None:
    if isinstance(body, bool):
        return None
    if isinstance(body, int):
        return body
    if isinstance(body, str) and body.strip().isdigit():
        return int(body.strip())
    if isinstance(body, dict):
        for key in ("count", "totalElements", "total", "value"):
            if (number := _int(body.get(key))) is not None:
                return number
        page = body.get("page")
        if isinstance(page, dict):
            return _int(page.get("totalElements"))
    return None


def adult_row(item: dict[str, Any]) -> AdultEventRow:
    return AdultEventRow(
        education_event_id=_str(item.get("educationEventId")),
        title=_text(_first(item, "titleSv", "title", "studyPathName")),
        provider_name=_text(item.get("providerName")),
        type_of_school=_text(item.get("typeOfSchool")),
        town=_text(_first(item, "town", "contactInfoAddressCity")),
        municipality=_text(item.get("municipality")),
        county=_text(item.get("county")),
        geographical_area_code=_text(item.get("geographicalAreaCode")),
        semester_start_from=_text(item.get("semesterStartFrom")),
        last_application_date=_text(item.get("lastApplicationDate")),
        pace_of_study=_text(item.get("paceOfStudy")),
        extent=_text(item.get("extent")),
        distance_learning=_bool(item.get("distance")),
        credits=_text(item.get("credits")),
        credits_system=_text(item.get("creditsSystem")),
        location=_text(item.get("location")),
        contractor=_text(item.get("contractor")),
        execution_condition=_int(item.get("executionCondition")),
        recommended_prior_knowledge=_text(item.get("recommendedPriorKnowledge")),
    )


_ADULT_DETAIL_KEYS = frozenset(
    {
        "educationEventId",
        "titleSv",
        "title",
        "studyPathName",
        "providerName",
        "organizerName",
        "typeOfSchool",
        "municipality",
        "town",
        "geographicalAreaCode",
        "distance",
        "semesterStartFrom",
        "lastApplicationDate",
        "paceOfStudy",
        "extent",
        "timeOfStudy",
        "credits",
        "creditsSystem",
        "instructionLanguages",
        "areasOfInterest",
        "educationEventDescription",
        "providerDescription",
        "requirements",
        "fee",
        "eligibleForStudentAid",
        "contractor",
        "executionCondition",
        "contactInfo",
        "_links",
        "links",
    }
)


def adult_detail(
    event_id: str, body: dict[str, Any], *, include_personal_data: bool, allow_personal_data: bool = True
) -> AdultEventDetail:
    contact = body.get("contactInfo")
    other = {k: clean(v) for k, v in body.items() if k not in _ADULT_DETAIL_KEYS}
    other = {k: v for k, v in other.items() if v not in (None, "", [], {})}
    email = _str(contact.get("email")) if isinstance(contact, dict) else None
    return AdultEventDetail(
        education_event_id=_str(body.get("educationEventId")) or event_id,
        title=_text(_first(body, "titleSv", "title", "studyPathName")),
        study_path_name=_text(body.get("studyPathName")),
        provider_name=_text(body.get("providerName")),
        organizer_name=_text(body.get("organizerName")),
        type_of_school=_text(body.get("typeOfSchool")),
        municipality=_text(body.get("municipality")),
        town=_text(body.get("town")),
        geographical_area_code=_text(body.get("geographicalAreaCode")),
        distance_learning=_bool(body.get("distance")),
        semester_start_from=_text(body.get("semesterStartFrom")),
        last_application_date=_text(body.get("lastApplicationDate")),
        pace_of_study=_text(body.get("paceOfStudy")),
        extent=_text(body.get("extent")),
        time_of_study=_text(body.get("timeOfStudy")),
        credits=_text(body.get("credits")),
        credits_system=_text(body.get("creditsSystem")),
        instruction_languages=_text(body.get("instructionLanguages")),
        areas_of_interest=_text(body.get("areasOfInterest")),
        description=_text(body.get("educationEventDescription")),
        provider_description=_text(body.get("providerDescription")),
        requirements=_text(body.get("requirements")),
        fee=_text(body.get("fee")),
        eligible_for_student_aid=_bool(body.get("eligibleForStudentAid")),
        contractor=_text(body.get("contractor")),
        execution_condition=_int(body.get("executionCondition")),
        contact=AdultEventContact(
            email=email if include_personal_data else None,
            web=_str(contact.get("web")),
            telephone=_str(contact.get("telephone")),
            addresses=addresses(contact.get("addresses")),
        )
        if isinstance(contact, dict)
        else None,
        other=other or None,
        notes=[personal_data_note(allow_personal_data)] if email and not include_personal_data else None,
    )


# --- surveys ----------------------------------------------------------------------------------------

_SURVEY_GROUP_FIELDS = frozenset(
    {"schoolUnitCode", "schoolUnit", "schoolYear", "semester", "noOfAnswers", "noInGroup", "answeringFrequency"}
)


def survey_question(metric: dict[str, Any]) -> SurveyQuestion | None:
    if all(v is None for v in metric.values()):
        return None
    average = _str(metric.get("average"))
    number, _ = parse_number(average)
    ratios = {k: _str(v) or "" for k, v in metric.items() if k.startswith("ratio") and v is not None}
    return SurveyQuestion(
        subject=_str(metric.get("questionSubject")),
        question=_str(metric.get("questionDescription")),
        average=average,
        average_number=number,
        ratios=ratios,
    )


def survey_group(raw: dict[str, Any]) -> SurveyGroup:
    questions: dict[str, SurveyQuestion] = {}
    other: dict[str, Any] = {}
    for key, value in raw.items():
        if key in _SURVEY_GROUP_FIELDS or key in ("_links", "links"):
            continue
        if isinstance(value, dict) and key.endswith("Metrics"):
            question = survey_question(value)
            if question is not None:
                questions[key[: -len("Metrics")]] = question
            continue
        cleaned = clean(value)
        if cleaned not in (None, "", [], {}):
            other[key] = cleaned
    return SurveyGroup(
        school_unit_code=_str(_first(raw, "schoolUnitCode", "schoolUnit")),
        school_year=_str(raw.get("schoolYear")),
        semester=_str(raw.get("semester")),
        answers=_str(raw.get("noOfAnswers")),
        group_size=_str(raw.get("noInGroup")),
        answer_rate=_str(raw.get("answeringFrequency")),
        questions=questions,
        other=other or None,
    )


def _survey_raw_groups(body: Any) -> list[dict[str, Any]]:
    # Pupils: body.schoolYearMetrics[]; custodians (nested): metric objects directly in body (spec §5.3).
    if isinstance(body, dict) and isinstance(body.get("schoolYearMetrics"), list):
        return [g for g in body["schoolYearMetrics"] if isinstance(g, dict)]
    if isinstance(body, list):
        return [g for g in body if isinstance(g, dict)]
    if isinstance(body, dict) and strip_links(body):
        return [body]
    return []


def survey_keys_from_links(body: Any) -> list[str]:
    found: set[str] = set()
    for rel, segment in _link_targets(body):
        for candidate in (segment.lower(), rel.lower()):
            if candidate in SURVEY_KEYS:
                found.add(candidate)
    return [k for k in SURVEY_KEYS if k in found]


def statistics_forms_from_links(body: Any) -> tuple[list[str], list[str]]:
    known: set[str] = set()
    unknown: list[str] = []
    for rel, segment in _link_targets(body):
        match = re.fullmatch(r"([a-z]+)-statistics", rel.lower())
        form = match.group(1) if match else (segment.lower() if rel.lower() != "self" else "")
        if form in STATISTICS_FORMS:
            known.add(form)
        elif match:
            unknown.append(form)
    return [f for f in STATISTICS_FORMS if f in known], unknown


# --- support ----------------------------------------------------------------------------------------


def support_entries(body: Any) -> list[Any]:
    if body is None:
        return []
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        items = embedded_items(body)
        if items:
            return items
        lists = {k: v for k, v in body.items() if isinstance(v, list) and k not in ("links", "_links")}
        if lists:
            # /support/programs keyed by school form ({"gy": [...]}) and /support/municipality-schoolunit
            # ({"schoolUnits": [...], "geographicalAreas": [...]}): keep the key as "group".
            out: list[Any] = []
            for group, values in lists.items():
                for value in values:
                    out.append(
                        {"group": group, **value} if isinstance(value, dict) else {"group": group, "value": value}
                    )
            return out
        return [body]
    return [body]


def _scalars(value: Any) -> list[str]:
    if isinstance(value, dict):
        return [s for v in value.values() for s in _scalars(v)]
    if isinstance(value, list):
        return [s for v in value for s in _scalars(v)]
    return [] if value is None else [str(value)]


def cap_nested(value: Any, cap: int, depth: int = 0) -> tuple[Any, bool]:
    """Truncate nested lists (not the top level) to ``cap`` items."""
    if isinstance(value, dict):
        truncated = False
        out: dict[str, Any] = {}
        for key, item in value.items():
            out[key], flag = cap_nested(item, cap, depth + 1)
            truncated = truncated or flag
        return out, truncated
    if isinstance(value, list):
        truncated = depth > 0 and len(value) > cap
        items = value[:cap] if depth > 0 else value
        out_list = []
        for item in items:
            capped, flag = cap_nested(item, cap, depth + 1)
            out_list.append(capped)
            truncated = truncated or flag
        return out_list, truncated
    return value, False


def json_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def take_within_budget(entries: list[Any], budget: int) -> tuple[list[Any], bool]:
    """Leading entries whose compact JSON fits in ``budget`` characters (always at least one)."""
    shown: list[Any] = []
    used = 0
    for entry in entries:
        size = json_size(entry) + 1
        if shown and used + size > budget:
            return shown, True
        shown.append(entry)
        used += size
    return shown, False


def variant_entry(value: Any) -> Any:
    """/support/variants answers strings like 'Lärlingsutbildning, LU' → ``{code, name, value}``."""
    if isinstance(value, str) and (match := re.fullmatch(r"\s*(.+?),\s*([A-ZÅÄÖ0-9]{1,10})\s*", value)):
        return {"code": match.group(2), "name": match.group(1), "value": value}
    return value


# --------------------------------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------------------------------

SchoolUnitCode = Annotated[str, Field(description="Skolenhetskod (8 siffror, school unit code), t.ex. '44673074'")]
PageParam = Annotated[int, Field(description="Sidnummer, 0-baserat (page)")]
Latitude = Annotated[float | None, Field(description="Latitud WGS84 (decimalgrader) för närhetssökning")]
Longitude = Annotated[float | None, Field(description="Longitud WGS84 (decimalgrader) för närhetssökning")]
RadiusKm = Annotated[
    float | None,
    Field(description="Avstånd i km från latitude/longitude (API-parametern 'distance', 'Avstånd från punkt i km')"),
]
SortParam = Annotated[
    str | None,
    Field(
        description="Sortering 'egenskap,asc|desc', t.ex. 'name,asc', eller flera nycklar "
        "'schoolUnitName,studyPathName,asc' ('a:asc,b:desc' går också)"
    ),
]
Resursskola = Annotated[
    bool | None,
    Field(description="Resursskola: true = bara resursskolor, false = inga resursskolor, utelämna = alla"),
]
IncludePersonalData = Annotated[
    bool,
    Field(
        description="Ta med kontaktens e-postadress. Den kan innehålla en enskild persons namn (personuppgift); "
        "utelämnas som standard"
    ),
]


def register(server: MCPServer[Any], services: Services) -> None:
    def api_url(path: str) -> str:
        return f"{services.settings.base_url(BASE_URL_KEY)}/{API_VERSION}/{path.lstrip('/')}"

    async def api_get(path: str, params: dict[str, Any] | None = None) -> tuple[Any, ApiResponse]:
        response = await services.http.get_json(SOURCE, api_url(path), params=params, headers=HEADERS)
        return unwrap(response), response

    async def api_get_optional(path: str, params: dict[str, Any] | None = None) -> tuple[Any, ApiResponse | None]:
        """Like ``api_get`` but 404 (the API's usual "no data" answer) gives ``(None, None)``."""
        try:
            return await api_get(path, params)
        except UpstreamError as exc:
            if exc.status == 404:
                return None, None
            raise

    @server.tool(
        name="skolverket_pe_search_school_units",
        title="Planerad utbildning: sök skolenheter",
        annotations=READ_ONLY_OPEN,
    )
    @tool_errors
    async def skolverket_pe_search_school_units(
        name: Annotated[str | None, Field(description="Skolenhetens namn eller del av namn (school name)")] = None,
        type_of_schooling: Annotated[
            str | None,
            Field(
                description="Skolform (type of schooling), v4-kod: fsk, gr, gran, gy, gyan, sp, sam, vuxgr, vuxgy, "
                "vuxgran, vuxgyan, vux, vuxgrs, vuxgys, sfi, fs, fth. Namn som 'grundskola' eller 'gymnasieskola' "
                "översätts."
            ),
        ] = None,
        principal_organizer_type: Annotated[
            str | None,
            Field(description="Huvudmannatyp: Kommunal, Region, Statlig, Sameskolan, Fristående, Uppgift saknas"),
        ] = None,
        school_orientation: Annotated[
            str | None, Field(description="Inriktning (schoolOrientation), t.ex. 'INTERNATIONELL' eller 'Ej relevant'")
        ] = None,
        resursskola: Resursskola = None,
        geographical_area_code: Annotated[
            str | None, Field(description="Områdeskod: kommunkod (4 siffror, t.ex. '0180') eller länskod (2 siffror)")
        ] = None,
        school_years: Annotated[str | None, Field(description="Årskurs som erbjuds, t.ex. '9'")] = None,
        latitude: Latitude = None,
        longitude: Longitude = None,
        radius_km: RadiusKm = None,
        compact: Annotated[
            bool,
            Field(
                description="Använd /compact-school-units: kod, namn, skolformer och WGS84-koordinater. Stöder "
                "endast type_of_schooling, resursskola, latitude/longitude/radius_km och sort."
            ),
        ] = False,
        sort: SortParam = None,
        page: PageParam = 0,
        size: Annotated[int, Field(description="Antal per sida (1–100)")] = 50,
    ) -> Annotated[CallToolResult, SchoolUnitSearchResult]:
        """Sök skolenheter i Skolverkets Planerad utbildning (v4) med serverfilter: namn, skolform, huvudmannatyp,
        inriktning, resursskola, kommun-/länskod, årskurs och närhet (lat/long + radie i km). Returnerar kompakta
        rader (skolenhetskod, namn, huvudmannatyp, kommunkod, postort, skolformer med årskurser) och
        sidinformation (0-baserade sidor). Med compact=true fås skolenhetskod + koordinater för kartor. Gå vidare
        med skolverket_pe_get_school_unit eller skolverket_pe_school_unit_statistics."""
        page, size = check_page(page, size)
        tos = normalize_type_of_schooling(type_of_schooling) if type_of_schooling else None
        if compact:
            unsupported = [
                label
                for label, value in (
                    ("name", name),
                    ("principal_organizer_type", principal_organizer_type),
                    ("school_orientation", school_orientation),
                    ("geographical_area_code", geographical_area_code),
                    ("school_years", school_years),
                )
                if value
            ]
            if unsupported:
                raise InvalidInputError(
                    "compact=true stöder bara type_of_schooling, resursskola, latitude/longitude/radius_km och sort; "
                    f"ta bort {', '.join(unsupported)} eller använd compact=false"
                )
            params: dict[str, Any] = {
                "typeOfSchooling": tos,
                **coordinate_params(latitude, longitude, radius_km, with_system=False),
                "coordinateSystemType": "WGS84",
                "sort": spring_sort(sort),
                "Resursskola": resursskola,
                "page": page,
                "size": size,
            }
            path = "compact-school-units"
        else:
            params = {
                "name": name.strip() if name and name.strip() else None,
                "typeOfSchooling": tos,
                "principalOrganizerType": normalize_principal_organizer_type(principal_organizer_type)
                if principal_organizer_type
                else None,
                "schoolOrientation": school_orientation.strip()
                if school_orientation and school_orientation.strip()
                else None,
                # Upper-case R as in the spec.
                "Resursskola": resursskola,
                "geographicalAreaCode": normalize_area_code(geographical_area_code) if geographical_area_code else None,
                "schoolYears": school_years.strip() if school_years and school_years.strip() else None,
                **coordinate_params(latitude, longitude, radius_km, with_system=True),
                "sort": spring_sort(sort),
                "page": page,
                "size": size,
            }
            path = "school-units"
        body, response = await api_get(path, params)
        items = [i for i in embedded_items(body) if isinstance(i, dict)]
        return structured(
            SchoolUnitSearchResult(
                **page_fields(body, page, size, len(items)),
                variant="compact" if compact else "full",
                rows=[school_unit_row(i) for i in items],
                source_url=response.url,
            )
        )

    @server.tool(
        name="skolverket_pe_get_school_unit",
        title="Planerad utbildning: skolenhet",
        annotations=READ_ONLY_OPEN,
    )
    @tool_errors
    async def skolverket_pe_get_school_unit(
        school_unit_code: SchoolUnitCode,
        distance_from_latitude: Annotated[
            float | None, Field(description="Valfritt: beräkna avstånd i km från denna punkt (WGS84) via /distanceFrom")
        ] = None,
        distance_from_longitude: Annotated[float | None, Field(description="Longitud för avståndsberäkningen")] = None,
        include_personal_data: IncludePersonalData = False,
        include_raw: Annotated[
            bool, Field(description="Ta med originalsvaret (body); contactInfo bara med include_personal_data=true")
        ] = False,
    ) -> Annotated[CallToolResult, SchoolUnitDetail]:
        """Hämta en skolenhet ur Planerad utbildning: namn, huvudman (organisationsnummer, bolagsnamn,
        bolagsform), huvudmannatyp, inriktning, resursskola, startdatum, kommunkod, webb, telefon och adresser,
        koordinater (WGS84 och SWEREF 99) samt skolformer med årskurser. Valfritt avstånd i km från en punkt.
        Kontaktens e-post (ofta en namngiven person) tas bara med på begäran. Okända eller nedlagda
        skolenhetskoder ger 404."""
        include_personal_data = personal_data(services, include_personal_data)
        code = normalize_school_unit_code(school_unit_code)
        body, response = await api_get(f"school-units/{code}")
        if not isinstance(body, dict) or not body:
            raise UpstreamError(SOURCE, "Tomt svar för skolenheten", status=response.status, url=response.url)
        detail = school_unit_detail(
            code,
            body,
            include_personal_data=include_personal_data,
            allow_personal_data=services.settings.allow_personal_data,
        )
        detail.source_url = response.url
        if distance_from_latitude is not None or distance_from_longitude is not None:
            params = coordinate_params(distance_from_latitude, distance_from_longitude, None, with_system=True)
            distance, _ = await api_get_optional(f"school-units/{code}/distanceFrom", params)
            detail.distance_km = parse_distance_km(distance)
            if detail.distance_km is None:
                detail.notes = [*(detail.notes or []), "Avståndet kunde inte beräknas (inget svar från /distanceFrom)"]
        if include_raw:
            detail.raw = redacted_raw(body, include_personal_data=include_personal_data)
        return structured(detail)

    @server.tool(
        name="skolverket_pe_school_unit_statistics",
        title="Planerad utbildning: statistik för skolenhet",
        annotations=READ_ONLY_OPEN,
    )
    @tool_errors
    async def skolverket_pe_school_unit_statistics(
        school_unit_code: SchoolUnitCode,
        school_form: Annotated[
            StatisticsForm | None,
            Field(
                description="Skolform: fsk, gr, gran, gy, gyan. Utelämna för att hämta alla skolformer som "
                "skolenheten har statistik för."
            ),
        ] = None,
        program_code: Annotated[
            str | None,
            Field(
                description="Endast gy/gyan: filtrera programMetrics på programkod, t.ex. 'NA' eller 'IM' (prefix). "
                "Statistiken använder koder utan '25'; 'NA25' matchar 'NA'."
            ),
        ] = None,
        include_history: Annotated[
            bool, Field(description="Ta med alla tidsperioder per indikator (tidsserie), inte bara senaste")
        ] = False,
    ) -> Annotated[CallToolResult, SchoolUnitStatisticsResult]:
        """Statistik för en skolenhet per skolform (fsk|gr|gran|gy|gyan): elever per lärare, andel legitimerade
        lärare, elevantal, betyg och meritvärde, nationella prov, behörighet, och för gymnasiet per program
        (antagningspoäng, GBP, examen inom 3 år). Varje indikator ges som senaste värde med originaltext
        ('cirka 370'), tolkat tal (number, approximate), value_type och time_period; include_history ger hela
        tidsserien. Indikatornamn är API:ts fältnamn; betydelser i indicator_labels."""
        code = normalize_school_unit_code(school_unit_code)
        program = normalize_program_code(program_code) if program_code else None
        notes: list[str] = []
        available: list[str] | None = None
        if school_form:
            forms = [school_form]
        else:
            index, _ = await api_get_optional(f"school-units/{code}/statistics")
            forms, unknown = statistics_forms_from_links(index)
            available = forms
            if unknown:
                notes.append(f"Okända statistiklänkar ignorerades: {', '.join(unknown)}")
            if not forms:
                notes.append(
                    "Ingen statistik hittades för skolenheten (okänd/nedlagd kod eller ingen publicerad statistik)."
                )
        if program and not any(f in ("gy", "gyan") for f in forms):
            notes.append("program_code används bara för gymnasieskolan (gy/gyan)")
        blocks: list[StatisticsBlock] = []
        for form in forms:
            body, response = await api_get_optional(f"school-units/{code}/statistics/{form}")
            if response is None:
                blocks.append(
                    StatisticsBlock(
                        school_form=form,
                        school_form_name=TYPE_OF_SCHOOLING[form]["name"],
                        note="Ingen statistik för skolformen (HTTP 404)",
                    )
                )
                continue
            blocks.append(
                statistics_block(
                    body,
                    form,
                    include_history=include_history,
                    program_code=program if form in ("gy", "gyan") else None,
                    source_url=response.url,
                )
            )
        result = SchoolUnitStatisticsResult(
            school_unit_code=code,
            available_school_forms=available,
            statistics=blocks,
            indicator_labels=labels_for(blocks),
            notes=notes,
        )
        too_large = include_history and json_size(result.model_dump(exclude_none=True)) > STATISTICS_CHAR_BUDGET
        if too_large and drop_program_history(result.statistics):
            result.notes.append(
                "Tidsserierna per program utelämnades eftersom svaret blev för stort; ange program_code för "
                "ett programs tidsserie."
            )
        return structured(result)

    @server.tool(
        name="skolverket_pe_national_statistics",
        title="Planerad utbildning: riksvärden",
        annotations=READ_ONLY_OPEN,
    )
    @tool_errors
    async def skolverket_pe_national_statistics(
        school_form: Annotated[StatisticsForm, Field(description="Skolform: fsk, gr, gran, gy, gyan")],
        program_code: Annotated[
            str | None,
            Field(
                description="Krävs för gy: programkod, t.ex. 'EK' eller 'NA' (statistikens koder, utan '25'); "
                "'NA25' prövas också som 'NA'. Riksvärden för gy finns bara per program."
            ),
        ] = None,
        include_history: Annotated[bool, Field(description="Ta med hela tidsserien per indikator")] = False,
    ) -> Annotated[CallToolResult, NationalStatisticsResult]:
        """Nationella jämförelsevärden (riket) för en skolform, i samma format som skolenhetsstatistiken:
        senaste värde per indikator (text + tolkat tal) och valfri tidsserie. För gymnasieskolan (gy) per
        program, t.ex. EK. Använd för att jämföra en skolas värden med riket."""
        program: str | None = None
        note: str | None = None
        if school_form == "gy":
            if not program_code:
                raise InvalidInputError(
                    "För gy krävs program_code, t.ex. 'EK' eller 'NA' (riksvärden finns per program)"
                )
            requested = normalize_program_code(program_code)
            candidates = program_code_candidates(requested)
            for index, candidate in enumerate(candidates):
                try:
                    body, response = await api_get(f"statistics/national-values/gy/{candidate}")
                except UpstreamError as exc:
                    if exc.status == 404 and index + 1 < len(candidates):
                        continue
                    raise
                program = candidate
                break
            if program != requested:
                note = f"Inga riksvärden för {requested}; visar programkoden {program} (statistikens kod utan '25')"
        else:
            body, response = await api_get(f"statistics/national-values/{school_form}")
        block = statistics_block(
            body, school_form, include_history=include_history, program_code=None, source_url=response.url
        )
        if program_code and school_form != "gy":
            note = "program_code används bara för gy och ignorerades"
        block.note = note
        return structured(
            NationalStatisticsResult(
                school_form=school_form,
                program_code=program,
                statistics=block,
                indicator_labels=labels_for([block]),
            )
        )

    @server.tool(name="skolverket_pe_salsa", title="Planerad utbildning: SALSA", annotations=READ_ONLY_OPEN)
    @tool_errors
    async def skolverket_pe_salsa(
        school_unit_code: Annotated[
            str | None, Field(description="En skolenhet (8 siffror) – hämtar bara den skolans SALSA-värden")
        ] = None,
        school_unit_codes: Annotated[
            list[str] | None, Field(description="Filtrera alla-skolor-listan på flera skolenhetskoder")
        ] = None,
        geographical_area_code: Annotated[
            str | None,
            Field(
                description="Filtrera på kommunkod (4 siffror, t.ex. '0180') eller länskod (2 siffror = kommunkodens "
                "två första siffror) – via geographicalAreaCode i SALSA-svaret"
            ),
        ] = None,
        metrics: Annotated[
            list[SalsaMetric] | None,
            Field(description="Ta bara med dessa SALSA-mått (standard: alla nio); minskar svaret"),
        ] = None,
        sort_by: Annotated[
            SalsaMetric | None,
            Field(description="Sortera på ett SALSA-mått, t.ex. 'salsaAverageGradesIn9thGradeDeviation'"),
        ] = None,
        descending: Annotated[bool, Field(description="Störst först vid sortering")] = True,
        limit: Annotated[int, Field(description=f"Max antal skolor (1–{SALSA_MAX_ROWS})")] = 25,
    ) -> Annotated[CallToolResult, SalsaResult]:
        """SALSA (Skolverkets värdeadderingsmodell för grundskolan, åk 9): faktiskt och modellberäknat
        meritvärde och andel som nått kunskapskraven, avvikelsen (positivt = bättre än förväntat utifrån
        elevsammansättningen) samt bakgrundsvariabler (andel nyinvandrade, andel pojkar, föräldrarnas
        utbildning). Varje rad har skolenhetskod, skolnamn och kommunkod. Ange en skolenhet, eller filtrera hela
        listan (~1 500 skolor, filtreras här på servern) på skolenhetskoder, kommun eller län och sortera, t.ex.
        för att hitta skolor med störst positiv avvikelse."""
        notes: list[str] = []
        if school_unit_code:
            code = normalize_school_unit_code(school_unit_code)
            body, response = await api_get(f"statistics/all-schools/salsa/{code}")
        else:
            body, response = await api_get("statistics/all-schools/salsa")
        period: str | None = None
        items: list[Any] = []
        if isinstance(body, dict):
            period = _str(body.get("timePeriod"))
            raw_items = body.get("compulsorySchoolUnitSalsaMetricList")
            if isinstance(raw_items, list):
                items = raw_items
            elif "schoolUnitCode" in body:
                items = [body]
            else:
                items = embedded_items(body)
        elif isinstance(body, list):
            items = body
        rows: list[SalsaRow] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            values = {k: v for k, raw in item.items() if _is_metric(raw) and (v := latest_value(raw)) is not None}
            rows.append(
                SalsaRow(
                    school_unit_code=_str(item.get("schoolUnitCode")),
                    name=_str(item.get("name")),
                    geographical_area_code=_str(item.get("geographicalAreaCode")),
                    metrics=values,
                )
            )
        if not rows:
            # Verified live: salsa/{code} can answer 200 with compulsorySchoolUnitSalsaMetricList [null].
            notes.append(
                "Ingen SALSA för skolenheten (API:t svarade med en tom lista); SALSA avser grundskolans åk 9."
                if school_unit_code
                else "SALSA-listan var tom."
            )
        if school_unit_codes:
            wanted = {normalize_school_unit_code(c) for c in school_unit_codes}
            rows = [r for r in rows if r.school_unit_code in wanted]
        if geographical_area_code:
            area = normalize_area_code(geographical_area_code)
            if rows and not any(r.geographical_area_code for r in rows):
                notes.append("SALSA-svaret saknar geographicalAreaCode; områdesfiltret gav därför inga träffar")
            rows = [r for r in rows if (r.geographical_area_code or "").startswith(area)]
            notes.append(f"Filtrerat på {'län' if len(area) == 2 else 'kommun'} {area} (geographicalAreaCode)")
        if sort_by:

            def sort_key(row: SalsaRow) -> tuple[bool, float]:
                number = row.metrics[sort_by].number if sort_by in row.metrics else None
                if number is None:
                    return True, 0.0
                return False, -number if descending else number

            rows.sort(key=sort_key)
        cap = clamp(limit, 1, SALSA_MAX_ROWS)
        shown = rows[:cap]
        if metrics:
            keep = set(metrics) | ({sort_by} if sort_by else set())
            for row in shown:
                row.metrics = {k: v for k, v in row.metrics.items() if k in keep}
        present = {k for r in shown for k in r.metrics}
        return structured(
            SalsaResult(
                time_period=period,
                total=len(rows),
                truncated=len(rows) > cap,
                sorted_by=sort_by,
                rows=shown,
                metric_labels={k: v for k, v in SALSA_LABELS.items() if k in present},
                notes=notes,
                source_url=response.url,
            )
        )

    @server.tool(
        name="skolverket_pe_school_unit_surveys",
        title="Planerad utbildning: Skolenkäten",
        annotations=READ_ONLY_OPEN,
    )
    @tool_errors
    async def skolverket_pe_school_unit_surveys(
        school_unit_code: SchoolUnitCode,
        survey: Annotated[
            str | None,
            Field(
                description="Enkät: custodiansfsk, custodiansgr, custodiansgran (vårdnadshavare), pupilsgr, "
                "pupilsgy (elever). Utelämna för alla enkäter som finns för skolenheten."
            ),
        ] = None,
        format: Annotated[
            Literal["nested", "flat"],
            Field(
                description="nested = frågeobjekt (rekommenderas: medelvärde 0–10 + andelar per fråga); "
                "flat = API:ts platta fält (satisfactionAverage …)"
            ),
        ] = "nested",
    ) -> Annotated[CallToolResult, SchoolUnitSurveysResult]:
        """Skolenkätens resultat (Skolinspektionen) för en skolenhet: elevers (åk 5, åk 8, gymnasiet år 2) och
        vårdnadshavares svar om nöjdhet, trygghet, studiero, stöd och stimulans – medelvärde (0–10, text +
        tal) och svarsandelar ('44%', '-' = dolt pga få svar), antal svar och svarsfrekvens. Enkäten görs
        vartannat år per skola, så många skolor saknar data."""
        code = normalize_school_unit_code(school_unit_code)
        prefix = "nestedsurveys" if format == "nested" else "surveys"
        notes: list[str] = []
        available: list[str] | None = None
        if survey:
            keys = [normalize_survey_key(survey)]
        else:
            index, _ = await api_get_optional(f"school-units/{code}/{prefix}")
            keys = survey_keys_from_links(index)
            available = keys
            if not keys:
                notes.append("Inga enkätresultat hittades för skolenheten.")
        results: list[SurveyResult] = []
        for key in keys:
            body, response = await api_get_optional(f"school-units/{code}/{prefix}/{key}")
            result = SurveyResult(
                survey=key, description=SURVEY_KEYS[key], source_url=response.url if response else None
            )
            raw_groups = _survey_raw_groups(body)
            if not raw_groups:
                result.note = "Inga resultat (enkäten genomförs vartannat år per skola)"
            elif format == "nested":
                result.groups = [survey_group(g) for g in raw_groups]
            else:
                result.rows = [clean(g) for g in raw_groups]
            results.append(result)
        return structured(
            SchoolUnitSurveysResult(
                school_unit_code=code, format=format, available_surveys=available, surveys=results, notes=notes
            )
        )

    @server.tool(
        name="skolverket_pe_school_unit_education_events",
        title="Planerad utbildning: gymnasieutbud för skolenhet",
        annotations=READ_ONLY_OPEN,
    )
    @tool_errors
    async def skolverket_pe_school_unit_education_events(
        school_unit_code: SchoolUnitCode,
        study_path_code: Annotated[
            str | None,
            Field(
                description="Studievägskod (program/inriktning), t.ex. 'EK25' eller 'FS25SKOLFORLAGD' – hämtar bara den"
            ),
        ] = None,
        type_of_schooling: Annotated[
            SecondaryForm | None,
            Field(description="gy eller gyan (med compact=true filtrerar API:t, annars filtreras svaret här)"),
        ] = None,
        compact: Annotated[
            bool,
            Field(description="Använd /compact-education-events (sidindelad kompakt lista med sort/page/size)"),
        ] = False,
        sort: SortParam = None,
        page: Annotated[int, Field(description="Sidnummer, 0-baserat – endast med compact=true")] = 0,
        size: Annotated[int, Field(description="Antal per sida (1–100) – endast med compact=true")] = 50,
    ) -> Annotated[CallToolResult, EducationEventsResult]:
        """Gymnasieutbildningar (program/inriktningar, 'education events') som en skolenhet erbjuder enligt
        Planerad utbildning, valfritt en enskild studieväg. Varje rad: studievägskod och namn, skolform (gy/gyan),
        kategori (yrkes-/högskoleförberedande), huvudmannaform, ort, start/slut, antagningspoäng (min/medel, år),
        inriktningar (studyPaths) och programvarianter. Hela listan är inte sidindelad (API:t grupperar per
        skolform); compact=true ger den sidindelade kompakta listan."""
        code = normalize_school_unit_code(school_unit_code)
        page, size = check_page(page, size)
        if study_path_code and compact:
            raise InvalidInputError("study_path_code kan inte kombineras med compact=true")
        if not compact and (sort or page):
            raise InvalidInputError("sort och page används bara med compact=true (övriga svar är inte sidindelade)")
        notes: list[str] = []
        spc = normalize_program_code(study_path_code, "Studievägskoden") if study_path_code else None
        params: dict[str, Any] | None = None
        if spc:
            path = f"school-units/{code}/education-events/{quote(spc, safe='')}"
        elif compact:
            path = f"school-units/{code}/compact-education-events"
            params = {"typeOfSchooling": type_of_schooling, "sort": spring_sort(sort), "page": page, "size": size}
        else:
            # The spec lists no query parameters here; the body is a map keyed by school form.
            path = f"school-units/{code}/education-events"
        body, response = await api_get_optional(path, params)
        if response is None:
            notes.append("Inga utbildningar hittades (HTTP 404)")
        rows = education_event_rows(body)
        if compact:
            paging = page_fields(body, page, size, len(rows))
        else:
            if type_of_schooling:
                rows = [r for r in rows if r.type_of_schooling == type_of_schooling]
            paging = {"total": len(rows), "page": 0, "page_size": len(rows)}
        return structured(
            EducationEventsResult(
                **paging,
                school_unit_code=code,
                study_path_code=spc,
                variant="compact" if compact else "full",
                rows=rows,
                notes=notes,
                source_url=response.url if response else None,
            )
        )

    @server.tool(
        name="skolverket_pe_search_education_events",
        title="Planerad utbildning: sök gymnasieutbildningar",
        annotations=READ_ONLY_OPEN,
    )
    @tool_errors
    async def skolverket_pe_search_education_events(
        name: Annotated[
            str | None,
            Field(description="Skolenhetens namn: hela ord (första, andra … ordet), inte delar av ett ord"),
        ] = None,
        study_path_code: Annotated[
            str | None, Field(description="Studievägskod (study path code), t.ex. 'NA25' eller 'EK25'")
        ] = None,
        program_variants: Annotated[
            list[str] | str | None,
            Field(
                description="Programvarianter (ProgramVariantList, CSV), t.ex. ['LU', 'NIU'] – se "
                "skolverket_pe_support_list list='variants'. Använder /v4/education-events-new."
            ),
        ] = None,
        principal_organizer_type: Annotated[
            str | None,
            Field(
                description="Huvudmannaform, skickas oförändrad (exemplen i specifikationen visar 'Kommun' och "
                "'ENSKILD')"
            ),
        ] = None,
        school_orientation: Annotated[
            str | None, Field(description="Inriktning (schoolOrientation), t.ex. 'ALLMAN'")
        ] = None,
        geographical_area_code: Annotated[
            str | None, Field(description="Områdeskod: kommunkod (4 siffror) eller länskod (2 siffror)")
        ] = None,
        type_of_schooling: Annotated[SecondaryForm | None, Field(description="gy eller gyan")] = None,
        resursskola: Resursskola = None,
        latitude: Latitude = None,
        longitude: Longitude = None,
        radius_km: RadiusKm = None,
        compact: Annotated[
            bool,
            Field(description="Använd /compact-education-events (stöder bara type_of_schooling, koordinater och sort)"),
        ] = False,
        new_endpoint: Annotated[
            bool,
            Field(
                description="Använd /v4/education-events-new (nya Utbildningsguiden); väljs automatiskt med "
                "program_variants"
            ),
        ] = False,
        count_only: Annotated[
            bool,
            Field(
                description="Returnera bara antalet träffar (page.totalElements; med new_endpoint "
                "/v4/education-events-new-count)"
            ),
        ] = False,
        sort: SortParam = None,
        page: PageParam = 0,
        size: Annotated[int, Field(description="Antal per sida (1–100)")] = 25,
    ) -> Annotated[CallToolResult, EducationEventsResult]:
        """Sök gymnasieutbildningar (program/inriktningar per skola) i hela landet med filter för skolnamn,
        studievägskod (t.ex. NA25), programvarianter, huvudmannaform, inriktning, kommun/län, skolform (gy/gyan),
        resursskola och närhet (km). Rader: skolenhetskod och namn, studieväg, kategori, ort, antagningspoäng,
        inriktningar. count_only ger bara antalet."""
        page, size = check_page(page, size)
        variants = _string_list(program_variants)
        use_new = new_endpoint or bool(variants)
        if compact and (count_only or use_new):
            raise InvalidInputError("compact kan inte kombineras med count_only, new_endpoint eller program_variants")
        filters: dict[str, Any] = {
            "name": name.strip() if name and name.strip() else None,
            "studyPathCode": normalize_program_code(study_path_code, "Studievägskoden") if study_path_code else None,
            "ProgramVariantList": ",".join(variants) if variants else None,
            "principalOrganizerType": principal_organizer_type.strip()
            if principal_organizer_type and principal_organizer_type.strip()
            else None,
            "schoolOrientation": school_orientation.strip()
            if school_orientation and school_orientation.strip()
            else None,
            "geographicalAreaCode": normalize_area_code(geographical_area_code) if geographical_area_code else None,
            "typeOfSchooling": type_of_schooling,
            "resursskolaFilter": resursskola,
        }
        if compact:
            unsupported = [k for k, v in filters.items() if v is not None and k != "typeOfSchooling"]
            if unsupported:
                raise InvalidInputError(
                    "compact=true stöder bara type_of_schooling, latitude/longitude/radius_km och sort "
                    f"(ej {', '.join(unsupported)})"
                )
            params = {
                "typeOfSchooling": type_of_schooling,
                **coordinate_params(latitude, longitude, radius_km, with_system=True),
                "sort": spring_sort(sort),
                "page": page,
                "size": size,
            }
            body, response = await api_get("compact-education-events", params)
            rows = education_event_rows(body)
            return structured(
                EducationEventsResult(
                    **page_fields(body, page, size, len(rows)), variant="compact", rows=rows, source_url=response.url
                )
            )
        # coordinateSystemType is not a parameter of /education-events(-new) in the spec, so it is not sent.
        filters.update(coordinate_params(latitude, longitude, radius_km, with_system=False))
        path = "education-events-new" if use_new else "education-events"
        if count_only:
            if use_new:
                body, response = await api_get("education-events-new-count", filters)
                count, source = parse_count(body), "education-events-new-count"
            else:
                # The spec has no count endpoint for /education-events: read page.totalElements of a 1-row page.
                body, response = await api_get(path, {**filters, "page": 0, "size": 1})
                count, source = parse_count(body), "page.totalElements"
                if count is None:
                    count = len(embedded_items(body))
            if count is None:
                raise UpstreamError(
                    SOURCE, "Kunde inte tolka antalet i svaret", url=response.url, detail=str(body)[:200]
                )
            return structured(
                EducationEventsResult(
                    total=count, count_source=source, variant="new" if use_new else "full", source_url=response.url
                )
            )
        params = {**filters, "sort": spring_sort(sort), "page": page, "size": size}
        body, response = await api_get(path, params)
        rows = education_event_rows(body)
        return structured(
            EducationEventsResult(
                **page_fields(body, page, size, len(rows)),
                variant="new" if use_new else "full",
                rows=rows,
                source_url=response.url,
            )
        )

    @server.tool(
        name="skolverket_pe_search_adult_education_events",
        title="Planerad utbildning: sök vuxenutbildningar",
        annotations=READ_ONLY_OPEN,
    )
    @tool_errors
    async def skolverket_pe_search_adult_education_events(
        search_term: Annotated[
            str | None, Field(description="Fritext (searchTerm), t.ex. 'svenska' eller 'sjuksköterska'")
        ] = None,
        town: Annotated[
            str | list[str] | None, Field(description="Studieort(er), t.ex. 'Göteborg' eller ['Solna', 'Göteborg']")
        ] = None,
        municipality: Annotated[str | list[str] | None, Field(description="Kommun(er), namn")] = None,
        county: Annotated[str | None, Field(description="Län, namn")] = None,
        geographical_area_code: Annotated[
            str | list[str] | None, Field(description="Kommunkod(er) (4 siffror) eller länskod (2 siffror)")
        ] = None,
        type_of_school: Annotated[
            str | None,
            Field(
                description="Vuxenutbildningsform (typeOfSchool), id från /support/adultTypeOfSchooling, t.ex. "
                "'vuxgy' (gymnasial vuxenutbildning), 'vuxgr', 'vuxsfi', 'fhs' (folkhögskola), 'yh', 'coursebasic'. "
                "Gamla koder (komvuxgycourses, fhsk, sfi …) översätts."
            ),
        ] = None,
        direction_ids: Annotated[
            list[int] | None,
            Field(description="Inriktnings-id (directionId) från skolverket_pe_adult_education_areas"),
        ] = None,
        instruction_languages: Annotated[
            str | list[str] | None,
            Field(description="Undervisningsspråk, ISO 639-2: 'swe', 'eng'"),
        ] = None,
        pace_of_study: Annotated[
            str | None,
            Field(description="Studietakt i procent: '100', '25,50,100' eller intervall '0-25', '25-75'"),
        ] = None,
        semester_start_from: Annotated[
            str | list[str] | None,
            Field(
                description="Terminsstart: intervall 'ÅÅÅÅ-MM-DDTOÅÅÅÅ-MM-DD', flera kommaseparerade, t.ex. "
                "'2026-08-01TO2026-12-31' (mellanslag runt TO tillåts)"
            ),
        ] = None,
        distance_learning: Annotated[
            bool | None,
            Field(description="Distansutbildning true/false (API-parametern 'distance' är här en flagga, ingen radie)"),
        ] = None,
        execution_condition: Annotated[
            str | int | None,
            Field(description="Kursstart: 0 = ej fastställt, 1 = datum satt, 2 eller 3 = löpande kursstart"),
        ] = None,
        recommended_prior_knowledge: Annotated[
            str | None,
            Field(description="'grundlaggande' = bara utbildningar som kräver grundläggande behörighet"),
        ] = None,
        count_only: Annotated[
            bool, Field(description="Returnera bara antalet träffar (/adult-education-events-count)")
        ] = False,
        sort: Annotated[
            str | None,
            Field(
                description="Sortering, t.ex. 'titleSv:asc' eller 'titleSv:asc,typeOfSchool:desc,municipality:desc' "
                "('titleSv,asc' översätts)"
            ),
        ] = None,
        page: PageParam = 0,
        size: Annotated[int, Field(description="Antal per sida (1–100)")] = 25,
    ) -> Annotated[CallToolResult, AdultEventSearchResult]:
        """Sök vuxenutbildningar (komvux, sfi, yrkeshögskola, folkhögskola, högskolekurser m.m.) med fritext,
        ort, kommun/län, kommunkod, utbildningsform, inriktning, språk, studietakt, terminsstart, kursstart och
        distans. Returnerar kompakta rader med educationEventId (detaljer via
        skolverket_pe_get_adult_education_event) och sidinformation; count_only ger bara antalet."""
        page, size = check_page(page, size)
        languages = [lang.lower() for lang in _string_list(instruction_languages)]
        for lang in languages:
            if not re.fullmatch(r"[a-z]{3}", lang):
                raise InvalidInputError(f"Språkkod {lang!r} ska vara ISO 639-2 med tre bokstäver, t.ex. 'swe'")
        area_codes = [normalize_area_code(c) for c in _string_list(geographical_area_code)]

        def text(value: str | None) -> str | None:
            return value.strip() if value and value.strip() else None

        def csv(value: str | list[str] | None) -> str | None:
            return ",".join(_string_list(value)) or None

        filters: dict[str, Any] = {
            "searchTerm": text(search_term),
            "town": csv(town),
            "municipality": csv(municipality),
            "county": text(county),
            # Several codes are sent as repeated keys (Feign EXPANDED style).
            "geographicalAreaCode": area_codes or None,
            "typeOfSchool": normalize_adult_type_of_school(type_of_school) if text(type_of_school) else None,
            "directionIds": ",".join(str(d) for d in direction_ids) if direction_ids else None,
            "instructionLanguages": ",".join(languages) if languages else None,
            "paceOfStudy": text(pace_of_study),
            "semesterStartFrom": normalize_semester_start(semester_start_from),
            "distance": distance_learning,
            "executionCondition": normalize_execution_condition(execution_condition),
            "recommendedPriorKnowledge": text(recommended_prior_knowledge),
        }
        if count_only:
            # Note the hyphen: /adult-education-events-count, not a /count sub-path.
            body, response = await api_get("adult-education-events-count", filters)
            count = parse_count(body)
            if count is None:
                raise UpstreamError(
                    SOURCE, "Kunde inte tolka antalet i svaret", url=response.url, detail=str(body)[:200]
                )
            return structured(AdultEventSearchResult(total=count, count_only=True, source_url=response.url))
        params = {**filters, "sort": colon_sort(sort), "page": page, "size": size}
        body, response = await api_get("adult-education-events", params)
        items = [i for i in embedded_items(body) if isinstance(i, dict)]
        return structured(
            AdultEventSearchResult(
                **page_fields(body, page, size, len(items)),
                rows=[adult_row(i) for i in items],
                source_url=response.url,
            )
        )

    @server.tool(
        name="skolverket_pe_get_adult_education_event",
        title="Planerad utbildning: vuxenutbildning",
        annotations=READ_ONLY_OPEN,
    )
    @tool_errors
    async def skolverket_pe_get_adult_education_event(
        education_event_id: Annotated[
            str,
            Field(description="educationEventId från skolverket_pe_search_adult_education_events, t.ex. 'e.myh.27538'"),
        ],
        include_personal_data: IncludePersonalData = False,
    ) -> Annotated[CallToolResult, AdultEventDetail]:
        """Detaljer för ett vuxenutbildningstillfälle: utbildningsnamn, anordnare, ort, start, studietakt,
        omfattning, poäng, språk, beskrivning, behörighetskrav, avgift, CSN-berättigande och kontaktuppgifter
        (webb, telefon, adress; e-post bara på begäran)."""
        include_personal_data = personal_data(services, include_personal_data)
        event_id = normalize_adult_event_id(education_event_id)
        body, response = await api_get(f"adult-education-events/{quote(event_id, safe='')}")
        if not isinstance(body, dict) or not body:
            raise UpstreamError(SOURCE, "Tomt svar för utbildningstillfället", url=response.url)
        detail = adult_detail(
            event_id,
            body,
            include_personal_data=include_personal_data,
            allow_personal_data=services.settings.allow_personal_data,
        )
        detail.source_url = response.url
        return structured(detail)

    @server.tool(
        name="skolverket_pe_adult_education_areas",
        title="Planerad utbildning: utbildningsområden (vuxen)",
        annotations=READ_ONLY_OPEN,
    )
    @tool_errors
    async def skolverket_pe_adult_education_areas(
        search: Annotated[
            str | None, Field(description="Filtrera områden/inriktningar på namn (svenska eller engelska)")
        ] = None,
    ) -> Annotated[CallToolResult, AreasResult]:
        """Utbildningsområden och inriktningar för vuxenutbildning (/adult-education-events/areas) med areaId
        och directionId. directionId används i direction_ids vid sökning av vuxenutbildningar."""
        body, response = await api_get("adult-education-events/areas")
        raw_areas = body.get("areas") if isinstance(body, dict) else body
        needle = _fold(search) if search and search.strip() else None
        areas: list[Area] = []
        for item in _as_list(raw_areas):
            if not isinstance(item, dict):
                continue
            directions = [
                Direction(
                    direction_id=_int(d.get("directionId")), name=_str(d.get("name")), name_en=_str(d.get("nameEn"))
                )
                for d in _as_list(item.get("directions"))
                if isinstance(d, dict)
            ]
            area = Area(
                area_id=_int(item.get("areaId")),
                name=_str(item.get("name")),
                name_en=_str(item.get("nameEn")),
                directions=directions,
            )
            if needle:
                area_hit = any(needle in _fold(n or "") for n in (area.name, area.name_en))
                if not area_hit:
                    area.directions = [
                        d for d in directions if any(needle in _fold(n or "") for n in (d.name, d.name_en))
                    ]
                    if not area.directions:
                        continue
            areas.append(area)
        return structured(AreasResult(total=len(areas), areas=areas, source_url=response.url))

    @server.tool(
        name="skolverket_pe_support_list",
        title="Planerad utbildning: stödlistor",
        annotations=READ_ONLY_OPEN,
    )
    @tool_errors
    async def skolverket_pe_support_list(
        list: Annotated[
            SupportList,
            Field(
                description="Stödlista: school-types (skolformer), geographical-areas (kommuner/orter/län med kod), "
                "municipality-schoolunit (kommuner och skolenheter), principal-organizer-types (huvudmannatyper), "
                "programs (gymnasieprogram och inriktningar), instruction-languages (språk), distance-studies "
                "(distansformer), adultTypeOfSchooling (vuxenutbildningsformer), variants (programvarianter för "
                "gy/gyan), api-info (API-version/status)"
            ),
        ],
        search: Annotated[
            str | None, Field(description="Filtrera poster som innehåller texten (kod eller namn), t.ex. 'Göteborg'")
        ] = None,
        type_of_schooling: Annotated[
            SecondaryForm, Field(description="Endast list='variants': gy eller gyan (krävs av API:t)")
        ] = "gy",
        limit: Annotated[int, Field(description=f"Max antal poster (1–{SUPPORT_MAX_ENTRIES})")] = 100,
    ) -> Annotated[CallToolResult, SupportListResult]:
        """Hämta Planerad utbildnings stöd-/kodlistor (giltiga filtervärden): skolformer, geografiska områden
        (code, name, areaType MUNICIPALITY/TOWN), kommuner och skolenheter, huvudmannatyper, gymnasieprogram
        med inriktningar, undervisningsspråk, distansformer, vuxenutbildningsformer (id + gammal kod) och
        programvarianter. Filtrering med search sker här (klientsidan); långa listor kortas (limit och
        teckenbudget). Statiska kodlistor finns i resursen fuzzy://skolverket/planned-educations/codes."""
        params = {"typeOfSchooling": type_of_schooling.upper()} if list == "variants" else None
        body, response = await api_get(SUPPORT_PATHS[list], params)
        entries = [clean(e) for e in support_entries(body)]
        if list == "variants":
            entries = [variant_entry(e) for e in entries]
        notes: list[str] = []
        if search and search.strip():
            needle = _fold(search)
            entries = [e for e in entries if any(needle in _fold(s) for s in _scalars(e))]
        cap = clamp(limit, 1, SUPPORT_MAX_ENTRIES)
        capped, nested_truncated = cap_nested(entries[:cap], SUPPORT_NESTED_LIST_CAP)
        shown, over_budget = take_within_budget(capped, SUPPORT_CHAR_BUDGET)
        if nested_truncated:
            notes.append(
                f"Inre listor har kortats till {SUPPORT_NESTED_LIST_CAP} poster; använd search för att smalna av"
            )
        if over_budget:
            notes.append(f"Svaret kortades till {len(shown)} poster för att hålla storleken nere; använd search")
        return structured(
            SupportListResult(
                name=list,
                total=len(entries),
                truncated=len(entries) > len(shown),
                entries=shown,
                notes=notes,
                source_url=response.url,
            )
        )

    @server.tool(
        name="skolverket_pe_compare_secondary",
        title="Planerad utbildning: jämför gymnasieutbildningar",
        annotations=READ_ONLY_OPEN,
    )
    @tool_errors
    async def skolverket_pe_compare_secondary(
        items: Annotated[
            list[SecondaryProgramRef],
            Field(
                description="Lista (1–20) av {school_unit_code, study_path_code, type_of_schooling}, t.ex. "
                "[{'school_unit_code': '77364720', 'study_path_code': 'EK'}]"
            ),
        ],
        include_history: Annotated[bool, Field(description="Ta med hela tidsserien för värdeobjekt")] = False,
    ) -> Annotated[CallToolResult, CompareSecondaryResult]:
        """Hämta inriktningar, antagningspoäng (min/medel) och antal elever på program och skolenhet för flera
        (skolenhet, studieväg)-par i ett anrop via POST /v4/school-unit-secondary – underlaget till
        Utbildningsguidens jämförelsevy. Endast läsning."""
        if not items:
            raise InvalidInputError("Ange minst en skolenhet + studieväg")
        if len(items) > 20:
            raise InvalidInputError("Högst 20 par per anrop")
        payload = [
            {
                "schoolUnitCode": normalize_school_unit_code(item.school_unit_code),
                "studyPathCode": normalize_program_code(item.study_path_code, "Studievägskoden"),
                "typeOfSchooling": item.type_of_schooling,
            }
            for item in items
        ]
        response = await services.http.post_json(SOURCE, api_url("school-unit-secondary"), payload, headers=HEADERS)
        body = unwrap(response)
        return structured(
            CompareSecondaryResult(
                requested=payload,
                result=clean(normalize_metrics_deep(body, include_history)),
                source_url=response.url,
            )
        )

    @server.tool(
        name="skolverket_pe_school_unit_documents",
        title="Planerad utbildning: dokument för skolenhet",
        annotations=READ_ONLY_OPEN,
    )
    @tool_errors
    async def skolverket_pe_school_unit_documents(
        school_unit_code: SchoolUnitCode,
        type_of_schooling: Annotated[
            str | None, Field(description="Begränsa till en skolform: fsk, gr, gran, gy eller gyan")
        ] = None,
        limit: Annotated[int, Field(description=f"Max antal dokument totalt (1–{DOCUMENTS_MAX})")] = 50,
    ) -> Annotated[CallToolResult, DocumentsResult]:
        """Dokument kopplade till en skolenhet (t.ex. Skolenkätens skolenhetsrapporter och Skolinspektionens
        beslut) grupperade per skolform: typ, titel, filnamn, filtyp, storlek och URL."""
        code = normalize_school_unit_code(school_unit_code)
        tos = normalize_type_of_schooling(type_of_schooling, STATISTICS_FORMS) if type_of_schooling else None
        path = f"school-units/{code}/documents" + (f"/{tos}" if tos else "")
        body, response = await api_get_optional(path)
        notes: list[str] = []
        if response is None:
            notes.append("Inga dokument hittades (HTTP 404)")
        raw_groups = embedded_items(body) or (
            [body] if isinstance(body, dict) and strip_links(body) and "page" not in body else []
        )
        groups: list[DocumentGroup] = []
        loose: list[Any] = []
        for group in raw_groups:
            if isinstance(group, dict) and "documents" in group:
                groups.append(
                    DocumentGroup(
                        type_of_schooling=_str(_first(group, "typeOfSchoolingCode", "typeOfSchooling")),
                        documents=[clean(d) for d in _as_list(group.get("documents"))],
                    )
                )
            elif group is not None:
                loose.append(clean(group))
        if loose:
            groups.append(DocumentGroup(type_of_schooling=tos, documents=loose))
        total = sum(len(g.documents) for g in groups)
        remaining = clamp(limit, 1, DOCUMENTS_MAX)
        for group in groups:
            group.documents = group.documents[:remaining]
            remaining -= len(group.documents)
        return structured(
            DocumentsResult(
                school_unit_code=code,
                type_of_schooling=tos,
                total=total,
                truncated=total > sum(len(g.documents) for g in groups),
                groups=groups,
                notes=notes,
                source_url=response.url if response else None,
            )
        )

    @server.resource(
        CODES_URI,
        name="skolverket-planned-educations-codes",
        title="Planerad utbildning: kodlistor",
        description="Kodlistor för Skolverkets Planerad utbildning (v4): skolformer (typeOfSchooling), "
        "huvudmannatyper, enkätnycklar och årskurser, GY25-programkoder, valueType-betydelser, areaType och "
        "indikatorbeskrivningar.",
        mime_type="application/json",
    )
    def planned_educations_codes() -> dict[str, Any]:
        return CODES
