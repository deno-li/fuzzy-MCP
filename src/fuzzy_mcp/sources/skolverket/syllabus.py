# SPDX-FileCopyrightText: 2026 Deniz Özer
#
# SPDX-License-Identifier: MIT

"""Skolverket – Läroplan/Syllabus API (ämnen, kurser, program, läroplaner).

Base: https://api.skolverket.se/syllabus, version v1 (``/v1/...``); v2 is alpha and
not used. Every endpoint is an anonymous ``GET`` returning ``application/json``
with a flat envelope (``apiVersion``, ``calledMethod``, ``totalElements`` …) and
ONE payload key (``subjects``/``subject``, ``courses``/``course``, ``programs``/
``program``, ``curriculums``/``curriculum``, ``codes`` …). Lists are not paged,
fields without values are omitted, texts are HTML fragments with soft hyphens,
and a 404 means "tomt svar" (no match) – also on list endpoints.

Only the subjects group has an official OpenAPI spec (1.16.1; it also defines the
nested Course/SubjectParent schemas). Courses, programs, curriculums, valuestore
and api-info have no published paths/schemas and are parsed defensively
(documented key first, otherwise the first array/object in the envelope).

Output size: hosts cap tool output at ~25k tokens, so every tool keeps its compact
JSON under ``MAX_OUTPUT_CHARS``: lists stop adding rows (``truncated`` + a note with
the next ``offset``), detail tools shorten texts step by step and finally cut
courses/lists with a note naming what was left out.

Gy25 (from 2025-07-01) uses the same school type ``GY`` as GY11. Gy25 subjects
are ``GRADE_SUBJECT_SYLLABUS`` with levels (``courses[]`` items typed
``LEVEL_IN_GRADE_SUBJECT_SYLLABUS``, codes like ``MATE1A00X``); the grading
criteria sit on the subject, not on the levels. GY11 subjects are
``SUBJECT_SYLLABUS`` with courses that carry their own criteria.
"""

import html as html_lib
import json
import re
import unicodedata
from collections.abc import Callable, Sequence
from datetime import date as Date
from html.parser import HTMLParser
from typing import Annotated, Any, Literal, TypeVar
from urllib.parse import quote

from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult
from pydantic import BaseModel, Field

from ...core import READ_ONLY_OPEN, Services, clamp, compact, dump, structured, tool_errors
from ...errors import InvalidInputError, UpstreamError
from ...http import ApiResponse

ModelT = TypeVar("ModelT", bound=BaseModel)

SOURCE = "skolverket"
BASE_KEY = "syllabus"
API_PREFIX = "/v1"
HEADERS = {"Accept": "application/json"}
CITATION = "Källa: Skolverket, Läroplan/Syllabus API (api.skolverket.se/syllabus)"
CODES_URI = "fuzzy://skolverket/syllabus/codes"

DEFAULT_LIMIT = 50
MAX_LIMIT = 500
# Output budget (characters of compact JSON); hosts cap tool output at ~25k tokens.
MAX_OUTPUT_CHARS = 60_000
# Per-field text budgets tried in turn when a detail result exceeds MAX_OUTPUT_CHARS.
SHRINK_STEPS: tuple[int, ...] = (1_200, 600, 300, 150)
MIN_MAX_CHARS = 50
DEFAULT_DETAIL_MAX_CHARS = 2_000


# --------------------------------------------------------------------------------------
# Code lists (spec §7). School-type names are interpretations of the abbreviations; the
# authoritative list is /v1/valuestore/schooltypes.
# --------------------------------------------------------------------------------------

SCHOOL_TYPES: dict[str, tuple[str, str]] = {
    "GR": ("Grundskolan", "active"),
    "GRAN": ("Anpassade grundskolan (tidigare grundsärskolan)", "active"),
    "SP": ("Specialskolan", "active"),
    "SAM": ("Sameskolan", "active"),
    "GY": ("Gymnasieskolan (både GY11 och Gy25)", "active"),
    "GYAN": ("Anpassade gymnasieskolan (tidigare gymnasiesärskolan)", "active"),
    "VUXGR": ("Kommunal vuxenutbildning (komvux) på grundläggande nivå", "active"),
    "VUXGRAN": ("Komvux som anpassad utbildning på grundläggande nivå", "active"),
    "VUXGY": ("Komvux på gymnasial nivå", "active"),
    "VUXGYAN": ("Komvux som anpassad utbildning på gymnasial nivå", "active"),
    "VUXSFI": ("Komvux i svenska för invandrare (sfi)", "active"),
    "GRS": ("Grundsärskolan (äldre benämning)", "expired"),
    "GYS": ("Gymnasiesärskolan (äldre benämning)", "expired"),
    "VUXSARGR": ("Särvux, grundläggande nivå (äldre)", "expired"),
    "VUXSARGY": ("Särvux, gymnasial nivå (äldre)", "expired"),
    "GR_GR2000": ("Grundskolan, kursplaner från 2000", "expired"),
    "GRS_GR2000": ("Grundsärskolan, kursplaner från 2000", "expired"),
    "SAM_GR2000": ("Sameskolan, kursplaner från 2000", "expired"),
    "SP_GR2000": ("Specialskolan, kursplaner från 2000", "expired"),
    "TR_GR2000": ("Träningsskolan, kursplaner från 2000", "expired"),
}

TYPE_OF_SYLLABUS_FILTER: dict[str, str] = {
    "ALL": "Alla ämnestyper (default)",
    "SUBJECT_SYLLABUS": "Ämnesplaner med kurser (GY11, Gyan13, komvux gymnasial nivå)",
    "GRADE_SUBJECT_SYLLABUS": "Ämnesplaner med nivåer (Gy25 och komvux på Gy25-ämnen)",
    "SUBJECT_AREA_SYLLABUS": "Kursplaner för ämnesområden och ämnesområden med kurser",
    "GRADE_SUBJECT_AREA_SYLLABUS": "Ämnesområdesplaner med nivåer (troligen Gyan25)",
    "GRVUX_COURSE": "Kursplaner för komvux på grundläggande nivå (även som anpassad utbildning)",
    "GRVUX_PARTIAL_COURSE": "Nationella delkurser för komvux på grundläggande nivå",
    "SFI_SUBJECT_SYLLABUS": "Kursplan för komvux i svenska för invandrare (sfi)",
    "COURSE_SYLLABUS": "Kursplaner för grundskola, anpassad grundskola, specialskola och sameskola",
    "OTHER_COURSE_SYLLABUS": "Övriga kursplaner för grundskolan (t.ex. dans, judiska studier)",
    "COURSE_SYLLABUS_SPAN": "Kursplaner för specialskolan baserade på anpassade grundskolans kursplaner",
}

TYPE_OF_SYLLABUS_ENTITY: dict[str, str] = {
    "COURSE_IN_SUBJECT_SYLLABUS": "Kurs i en ämnesplan (GY11), t.ex. MATMAT01a",
    "LEVEL_IN_GRADE_SUBJECT_SYLLABUS": "Nivå i en ämnesplan med nivåer (Gy25), t.ex. MATE1A00X",
    "COURSE_IN_SFI_SUBJECT_SYLLABUS": "Kurs i sfi-kursplanen, t.ex. SFIKUA9",
    "UNKNOWN": "Äldre koder (t.ex. ADM2000GY)",
}

TIMESPANS: dict[str, str] = {
    "LATEST": "Version som gäller på sökdatumet (default)",
    "FUTURE": "Versioner som börjar gälla efter sökdatumet",
    "CANCELED": "Upphävda med övergångsbestämmelser (canceledDate <= sökdatum); ämnen och program, sedan API 1.16",
    "EXPIRED": "Utgångna på sökdatumet (endDate < sökdatum)",
    "MODIFIED": "Ändrade från och med sökdatumet (modifiedDate >= sökdatum)",
}

STUDY_PATH_TYPES: dict[str, str] = {
    "PROGRAM": "Nationellt program GY11/Gyan13",
    "PROGRAM25": "Nationellt program Gy25/Gyan25 (koder med 25, t.ex. NA25)",
    "ORIENTATIONS": "Inriktning (GY11 och Gy25 blandat)",
    "PROFILE": "Profil (t.ex. VI/VI25, YX)",
    "PARTICULAR_STUDY_PATH": "Särskild studieväg GY11/Gyan13 (lärling, RIG, NIU, IM-varianter …)",
    "PARTICULAR_STUDY_PATH25": "Särskild studieväg Gy25/Gyan25 (t.ex. BABA00L lärling, IMA00J)",
    "INTRODUCTORY_PROGRAM": "Introduktionsprogram (IMIND, IMPRO, IMPRE, IMSPR, IMYRK)",
    "FOURTH_TECHNICAL_YEAR": "Fjärde tekniskt år (VI)",
    "FOURTH_TECHNICAL_YEAR25": "Fjärde tekniskt år Gy25 (VI25)",
    "INDIVIDUAL_PROGRAM": "Individuellt program (IAIND)",
    "COMPULSORY_PLAN": "Läroplan/plan för obligatorisk skolform (t.ex. GRGRLAR01)",
    "EDU_FOR_PUPILS_WITH_LEARNING_DISABILITIES_PLAN": "Plan för grundsärskolan (GRSARLAR01)",
    "ADULT_SECONDARY_EDUCATION_PLAN": "Plan för komvux gymnasial nivå (GYVULAR)",
    "ADULT_SECONDARY_EDUCATION_FOR_PUPILS_WITH_LEARNING_DISABILITIES_PLAN": "Plan för särvux (GYVUSARLAR)",
    "SWEDISH_FOR_IMMIGRANTS_PLAN": "Plan för sfi (GYVUSFILAR)",
}

PROGRAM_CATEGORIES: dict[str, str] = {
    "PRELIMINARY_PROGRAM_FOR_HIGHER_EDUCATION": "Högskoleförberedande program",
    "VOCATIONAL_PROGRAM": "Yrkesprogram",
    "NATIONAL_RECRUITMENT_FOR_LOCAL_SPECIALIZATION": "Riksrekryterande utbildning med lokal inriktning",
}

SUBJECT_CATEGORIES: dict[str, str] = {
    "COMMON2": "Kapitel 4-ämne (gymnasiegemensamma ämnen, nuvarande)",
    "COMMON": "Bilaga4-ämnen (äldre versioner)",
    "VOCATIONAL": "Yrkesämne",
    "OTHER": "Vissa ämne",
    "SUBJECT_AREA": "Ämnesområde",
    "NATURAL_SCIENCES": "Naturorienterande ämnen",
    "SOCIAL_SCIENCES": "Samhällsorienterande ämnen",
}

CENTRAL_CONTENT_TYPES: list[str] = [
    "WITHIN_STUDENT_CHOICE",
    "WITHIN_STUDENT_CHOICE_CHINESE",
    "WITHIN_LANGUAGE_CHOICE",
    "WITHIN_LANGUAGE_CHOICE_CHINESE",
    "WITHIN_SCHOOL_CHOICE",
    "FIRST_LANGUAGE",
    "SECOND_LANGUAGE",
    "SECOND_LANGUAGE_FOR_BEGINNERS",
    "FIN_LANGUAGE_FIRST",
    "FIN_LANGUAGE_SECOND",
    "MEANKIELI_LANGUAGE_FIRST",
    "MEANKIELI_LANGUAGE_SECOND",
    "ROMANI_LANGUAGE_FIRST",
    "ROMANI_LANGUAGE_SECOND",
    "JIDDISH_LANGUAGE_FIRST",
    "JIDDISH_LANGUAGE_SECOND",
]
REQUIREMENT_TYPES: list[str] = [
    *CENTRAL_CONTENT_TYPES,
    "BASIC_REQUIREMENTS",
    "ADVANCED_REQUIREMENTS",
    "SIGN_LANGUAGE_FOR_BEGINNERS",
]
SFI_ASPECTS: dict[str, str] = {
    "VERBAL_INTERACTION": "Muntlig interaktion",
    "WRITING_PROFICIENCY": "Skriftlig färdighet",
    "VERBAL_PRODUCTION": "Muntlig produktion",
    "READING_COMPREHENSION": "Läsförståelse",
    "LISTENING_COMPREHENSION": "Hörförståelse",
}
GRADE_STEPS: dict[str, str] = {
    "A": "Betyget A – högsta betyget",
    "B": "Betyget B – kunskaperna bedöms sammantaget vara mellan A och C",
    "C": "Betyget C",
    "D": "Betyget D – kunskaperna bedöms sammantaget vara mellan C och E",
    "E": "Betyget E – lägsta godkända betyget",
    "F": "Betyget F – icke godkänt (inga egna kriterier i API:t)",
}
REFORMS: dict[str, str] = {
    "GY11": "Gymnasieskolan 2011 – kurser med kursbetyg (upphävd 2025-07-01, övergångsperiod)",
    "GY25": "Gymnasieskolan 2025 – ämnen i nivåer med ämnesbetyg (från 2025-07-01)",
}
API_STATUS = ["alpha", "beta", "active", "deprecated", "retired", "decommissioned"]

VALUESTORE_LISTS: dict[str, tuple[str, str]] = {
    # list -> (path, documented payload key). Keys of the first three come from community
    # code only (uncertain); "codes" is confirmed for the last two.
    "schooltypes": ("/valuestore/schooltypes", "schoolTypes"),
    "schooltypes/expired": ("/valuestore/schooltypes/expired", "schoolTypes"),
    "typeofsyllabus": ("/valuestore/typeofsyllabus", "typesOfSyllabus"),
    "subjectandcoursecodes": ("/valuestore/subjectandcoursecodes", "codes"),
    "studypathcodes": ("/valuestore/studypathcodes", "codes"),
}

CODE_LISTS: dict[str, Any] = {
    "source": CITATION,
    "note": (
        "Kodlistor för Skolverkets Syllabus-API v1. Skoltypernas namn är tolkningar av förkortningarna – "
        "den auktoritativa listan hämtas med skolverket_syllabus_valuestore(list='schooltypes')."
    ),
    "school_types": [{"code": c, "name": n, "status": s} for c, (n, s) in SCHOOL_TYPES.items()],
    "type_of_syllabus_filter": [{"code": c, "description": d} for c, d in TYPE_OF_SYLLABUS_FILTER.items()],
    "type_of_syllabus_entity": [{"code": c, "description": d} for c, d in TYPE_OF_SYLLABUS_ENTITY.items()],
    "timespan": [{"code": c, "description": d} for c, d in TIMESPANS.items()],
    "study_path_types": [{"code": c, "description": d} for c, d in STUDY_PATH_TYPES.items()],
    "program_categories": [{"code": c, "name": n} for c, n in PROGRAM_CATEGORIES.items()],
    "subject_categories": [{"code": c, "name": n} for c, n in SUBJECT_CATEGORIES.items()],
    "central_content_types": CENTRAL_CONTENT_TYPES,
    "requirement_types": REQUIREMENT_TYPES,
    "sfi_aspect_types": [{"code": c, "name": n} for c, n in SFI_ASPECTS.items()],
    "grade_steps": [{"code": c, "label": n} for c, n in GRADE_STEPS.items()],
    "grade_scale": "A-F",
    "reforms": [{"code": c, "description": d} for c, d in REFORMS.items()],
    "api_status": API_STATUS,
    "gy25": (
        "Gy25 har ingen egen skoltyp (GY). Gy25-ämnen har typeOfSyllabus GRADE_SUBJECT_SYLLABUS och 4-teckenskoder "
        "(MATE, SVEN, ENGE); nivåerna ligger i ämnets courses[] med LEVEL_IN_GRADE_SUBJECT_SYLLABUS och koder som "
        "MATE1A00X. Betygskriterierna gäller ämnet (ämnesbetyg). Gy25-program har suffixet 25 (NA25) och "
        "studyPathType PROGRAM25. GY11-ämnen (3 tecken, t.ex. MAT) och GY11-program finns kvar under LATEST med "
        "canceledDate 2025-07-01 under övergångsperioden."
    ),
}

GY25_NOTE = (
    "Gy25: ämnet läses i nivåer (courses = nivåer, typeOfSyllabus LEVEL_IN_GRADE_SUBJECT_SYLLABUS). Centralt innehåll "
    "finns per nivå, men betygskriterierna gäller ämnet som helhet (ämnesbetyg) och bedöms på den aktuella nivån – "
    "de ska inte kopieras till enskilda nivåer."
)
GY11_NOTE = "GY11: betygskriterierna (tidigare kunskapskrav) finns per kurs och ger kursbetyg."

SubjectTimespan = Literal["LATEST", "FUTURE", "CANCELED", "EXPIRED", "MODIFIED"]
Timespan = Literal["LATEST", "FUTURE", "EXPIRED", "MODIFIED"]
TypeOfSyllabus = Literal[
    "ALL",
    "SUBJECT_SYLLABUS",
    "GRADE_SUBJECT_SYLLABUS",
    "SUBJECT_AREA_SYLLABUS",
    "GRADE_SUBJECT_AREA_SYLLABUS",
    "GRVUX_COURSE",
    "GRVUX_PARTIAL_COURSE",
    "SFI_SUBJECT_SYLLABUS",
    "COURSE_SYLLABUS",
    "OTHER_COURSE_SYLLABUS",
    "COURSE_SYLLABUS_SPAN",
]
SubjectSection = Literal["description", "purpose", "courses", "central_content", "grading_criteria"]
CourseSection = Literal["description", "purpose", "central_content", "grading_criteria"]
ValuestoreList = Literal[
    "schooltypes", "schooltypes/expired", "typeofsyllabus", "subjectandcoursecodes", "studypathcodes"
]

ALL_SUBJECT_SECTIONS: tuple[str, ...] = ("description", "purpose", "courses", "central_content", "grading_criteria")
DEFAULT_COURSE_SECTIONS: tuple[str, ...] = ("description", "central_content", "grading_criteria")

_ENVELOPE_KEYS = frozenset(
    {
        "apiPublisher",
        "apiName",
        "apiVersion",
        "apiReleased",
        "apiStatus",
        "apiDocumentation",
        "apiDeprecation",
        "apiLink",
        "apiSunset",
        "dataOrigin",
        "calledMethod",
        "totalElements",
        "processingTime",
        "startedCaching",
        "codeParam",
        "dateParam",
        "versionParam",
        "timeSpanParam",
        "schoolTypeParam",
        "typeOfSyllabusParam",
    }
)
# ASCII only (no known code has other letters; rejects Unicode look-alikes) and starting with a
# letter/digit, so "." / ".." (collapsed by the URL layer → escapes the resource path) are rejected.
# Codes may contain Swedish letters (subject codes VÅR, MÄT, ELÄ, JÄN were captured live), so letters are
# A–Z plus ÅÄÖ/åäö; a letter or digit first rules out "." and ".." (httpx collapses dot segments).
_CODE_RE = re.compile(r"[A-Za-z0-9ÅÄÖåäö][A-Za-z0-9ÅÄÖåäö_.\-]{0,39}")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# "Betyget E" (GY), "Betygskriterier för betyget E i slutet av årskurs 6" (GR)
_GRADE_LABEL_RE = re.compile(r"^(?:(?:betygskriterier|kunskapskrav)\s+för\s+)?betyget\s+[A-F]\b", re.IGNORECASE)


# --------------------------------------------------------------------------------------
# Output models
# --------------------------------------------------------------------------------------


class CodeName(BaseModel):
    code: str | None = None
    name: str | None = None


class SyllabusRow(BaseModel):
    code: str
    name: str | None = None
    english_name: str | None = None
    version: int | str | None = None
    type_of_syllabus: str | None = None
    reform: str | None = Field(default=None, description="GY11/GY25 när API:t anger det (fältet reform)")
    study_path_type: str | None = None
    category: str | None = None
    school_types: list[str] | None = None
    categories: list[str] | None = None
    points: int | str | None = None
    start_date: str | None = None
    end_date: str | None = None
    modified_date: str | None = None
    canceled_date: str | None = None
    canceled_skolfs: str | None = None
    skolfs_grund: str | None = None
    skolfs_andring: str | None = None
    version_info: str | None = None
    description: str | None = None
    course_codes: list[str] | None = Field(
        default=None, description="Kurs-/nivåkoder (ämneslistan innehåller dem bara när skoltyp anges)"
    )
    orientations: list[CodeName] | None = None


class SyllabusListResult(BaseModel):
    kind: str
    total: int = Field(description="Antal träffar efter klientfiltrering (före limit/offset)")
    returned: int
    offset: int = 0
    truncated: bool = False
    upstream_count: int | None = Field(default=None, description="Antal poster i API:ts svar före klientfiltrering")
    filters: dict[str, Any] = Field(default_factory=dict)
    items: list[SyllabusRow] = Field(default_factory=list)
    api_version: str | None = None
    request_url: str | None = None
    notes: list[str] | None = None
    citation: str = CITATION


class CentralContentItem(BaseModel):
    year: str | None = Field(default=None, description="Årskurser/stadium, t.ex. '1-3' (grundskolan)")
    type: str | None = Field(default=None, description="typeOfCentralContent")
    text: str | None = None


class GradingCriterion(BaseModel):
    grade_step: str | None = None
    label: str | None = Field(default=None, description="Rubrik, t.ex. 'Betyget E'")
    year: str | None = Field(default=None, description="Årskurs (grundskolan), t.ex. '6' eller '9'")
    type: str | None = Field(default=None, description="typeOfRequirement")
    aspect_type: str | None = None
    aspect_name: str | None = None
    aspect_description: str | None = None
    skolfs_id: str | None = None
    text: str | None = None


class CourseItem(BaseModel):
    code: str
    name: str | None = None
    display_name: str | None = Field(default=None, description="'{ämne} – {nivå}' för Gy25-nivåer")
    english_name: str | None = None
    type_of_syllabus: str | None = None
    points: int | str | None = None
    sort_order: int | None = None
    version_info: str | None = None
    gers: str | None = Field(default=None, description="GERS/CEFR-nivå (sfi)")
    study_path_description: str | None = None
    description: str | None = None
    central_content: list[CentralContentItem] | None = None
    grading_note: str | None = None
    grading_criteria: list[GradingCriterion] | None = None


class SubjectDetail(BaseModel):
    code: str
    name: str | None = None
    english_name: str | None = None
    type_of_syllabus: str | None = None
    reform: str | None = None
    version: int | str | None = None
    version_info: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    modified_date: str | None = None
    canceled_date: str | None = None
    canceled_skolfs: str | None = None
    skolfs_grund: str | None = None
    skolfs_andring: str | None = None
    grade_scale: str | None = None
    points: int | str | None = None
    designation: str | None = None
    school_types: list[str] | None = None
    categories: list[CodeName] | None = None
    appendix2: list[CodeName] | None = Field(default=None, description="Bilaga 2-varianter (språk, spetsvarianter)")
    description: str | None = None
    purpose: str | None = None
    other_texts: dict[str, str] | None = Field(default=None, description="Sfi-specifika texter m.m.")
    course_count: int | None = None
    courses: list[CourseItem] | None = None
    central_contents: list[CentralContentItem] | None = Field(
        default=None, description="Centralt innehåll på ämnesnivå (grundskolans kursplaner, per stadium)"
    )
    grading_note: str | None = None
    grading_criteria: list[GradingCriterion] | None = Field(
        default=None, description="Betygskriterier på ämnesnivå (Gy25 ämnesbetyg, grundskolan per årskurs)"
    )
    sections: list[str] = Field(default_factory=list)
    notes: list[str] | None = None
    api_version: str | None = None
    request_url: str | None = None
    citation: str = CITATION


class SubjectRef(BaseModel):
    code: str | None = None
    name: str | None = None
    version: int | str | None = None
    type_of_syllabus: str | None = None
    reform: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    modified_date: str | None = None
    canceled_date: str | None = None
    skolfs_grund: str | None = None
    skolfs_andring: str | None = None
    school_types: list[str] | None = None
    categories: list[CodeName] | None = None
    purpose: str | None = None


class CourseDetailResult(BaseModel):
    course: CourseItem
    subject: SubjectRef | None = None
    subject_grading_note: str | None = None
    subject_grading_criteria: list[GradingCriterion] | None = Field(
        default=None, description="Gy25: ämnets betygskriterier (gäller ämnet som helhet, inte bara nivån)"
    )
    resolved_via: Literal["courses", "subject"] = "courses"
    sections: list[str] = Field(default_factory=list)
    notes: list[str] | None = None
    api_version: str | None = None
    request_url: str | None = None
    citation: str = CITATION


class ProgramSubject(BaseModel):
    code: str | None = None
    name: str | None = None
    points: int | str | None = None
    type_of_syllabus: str | None = None
    course_codes: list[str] | None = None


class ProgramSubjectGroup(BaseModel):
    key: str = Field(description="Upstream-nyckel, t.ex. foundationSubjects (gymnasiegemensamma ämnen)")
    name: str | None = None
    points: int | str | None = None
    subject_count: int = 0
    subjects: list[ProgramSubject] = Field(default_factory=list)


class ProgramDetail(BaseModel):
    code: str
    name: str | None = None
    study_path_type: str | None = None
    category: str | None = None
    school_types: list[str] | None = None
    version: int | str | None = None
    version_info: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    modified_date: str | None = None
    canceled_date: str | None = None
    canceled_skolfs: str | None = None
    skolfs_grund: str | None = None
    skolfs_andring: str | None = None
    subject_groups: list[ProgramSubjectGroup] | None = None
    orientations: list[dict[str, Any]] | None = None
    profiles: list[dict[str, Any]] | None = None
    other: dict[str, Any] | None = Field(default=None, description="Övriga fält (HTML → text, förkortade)")
    raw: dict[str, Any] | None = None
    notes: list[str] | None = None
    api_version: str | None = None
    request_url: str | None = None
    citation: str = CITATION


class CurriculumDetail(BaseModel):
    code: str
    name: str | None = None
    school_types: list[str] | None = None
    type_of_syllabus: str | None = None
    version: int | str | None = None
    start_date: str | None = None
    end_date: str | None = None
    modified_date: str | None = None
    content: dict[str, Any] | None = Field(default=None, description="Övrigt innehåll (HTML → text, förkortat)")
    raw: dict[str, Any] | None = None
    notes: list[str] | None = None
    api_version: str | None = None
    request_url: str | None = None
    citation: str = CITATION


class ValueRow(BaseModel):
    code: str | None = None
    name: str | None = None
    description: str | None = None
    type: str | None = Field(default=None, description="Studievägstyp (studypathcodes)")
    type_of_syllabus: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    modified_date: str | None = None
    school_types: list[str] | None = None


class ValuestoreResult(BaseModel):
    code_list: str
    total: int
    returned: int
    offset: int = 0
    truncated: bool = False
    upstream_count: int | None = None
    filters: dict[str, Any] = Field(default_factory=dict)
    items: list[ValueRow] = Field(default_factory=list)
    api_version: str | None = None
    request_url: str | None = None
    notes: list[str] | None = None
    citation: str = CITATION


# --------------------------------------------------------------------------------------
# Text helpers
# --------------------------------------------------------------------------------------

_BLOCK_TAGS = frozenset(
    {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "table", "tr", "section", "article", "blockquote", "dl", "dt"}
)


class _TextExtractor(HTMLParser):
    """HTML fragment → plain text: block elements become line breaks, list items
    become '- ' bullets, ordered list items '1. ' (course descriptions refer to the
    numbered points in the subject's purpose)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.lists: list[list[int] | None] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("ul", "ol"):
            self.lists.append([0] if tag == "ol" else None)
            self.parts.append("\n")
        elif tag == "li":
            counter = self.lists[-1] if self.lists else None
            if counter is not None:
                counter[0] += 1
                self.parts.append(f"\n{counter[0]}. ")
            else:
                self.parts.append("\n- ")
        elif tag == "br" or tag in _BLOCK_TAGS:
            self.parts.append("\n")
        elif tag in ("td", "th", "dd"):
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("ul", "ol"):
            if self.lists:
                self.lists.pop()
            self.parts.append("\n")
        elif tag == "li" or tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def html_to_text(value: Any) -> str:
    """Strip HTML to compact text (one line per block, bullets for list items) and
    remove soft hyphens (U+00AD)."""
    if value is None:
        return ""
    text = str(value)
    if "<" in text and ">" in text:
        parser = _TextExtractor()
        parser.feed(text)
        parser.close()
        text = "".join(parser.parts)
    else:
        text = html_lib.unescape(text)
    text = text.replace("­", "").replace("\xa0", " ")
    lines = (" ".join(line.split()) for line in text.splitlines())
    return "\n".join(line for line in lines if line)


def truncate(text: str, max_chars: int | None) -> str:
    if not max_chars or max_chars <= 0 or len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    space = cut.rfind(" ")
    if space > max_chars * 0.7:
        cut = cut[:space]
    return f"{cut.rstrip()} …[förkortad, {len(text)} tecken totalt]"


def _text(value: Any, max_chars: int | None = None) -> str | None:
    text = html_to_text(value)
    return truncate(text, max_chars) if text else None


def _norm(value: Any) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).replace("­", "").casefold()


def _str(value: Any) -> str | None:
    if value is None or isinstance(value, (dict, list)):
        return None
    text = str(value).strip()
    return text or None


def _name(value: Any) -> str | None:
    if value is None:
        return None
    return _text(value) if isinstance(value, str) else _str(value)


def _version(value: Any) -> int | str | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if re.fullmatch(r"\d+", text):
        return int(text)
    return text or None


def _points(value: Any) -> int | str | None:
    """Points are strings in the API ("100"); return an int when possible."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    text = str(value).strip()
    if not text or text == "-":
        return None
    if re.fullmatch(r"\d+", text):
        return int(text)
    return text


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _school_types(value: Any) -> list[str] | None:
    """Spec 1.16.1 types schoolTypes as an array of strings; a single string, nested
    lists or ``{"code": ...}`` objects are accepted defensively."""
    out: list[str] = []

    def walk(item: Any) -> None:
        if isinstance(item, list):
            for sub in item:
                walk(sub)
        elif isinstance(item, dict):
            code = item.get("code")
            if code:
                out.append(str(code))
        elif item is not None and str(item).strip():
            out.append(str(item).strip())

    walk(value)
    return out or None


def _code_names(value: Any) -> list[CodeName] | None:
    if not isinstance(value, list):
        return None
    out = []
    for item in value:
        if isinstance(item, dict):
            code, name = _str(item.get("code")), _name(item.get("name"))
            if code or name:
                out.append(CodeName(code=code, name=name))
        elif isinstance(item, str) and item.strip():
            out.append(CodeName(code=item.strip()))
    return out or None


def _course_codes(value: Any) -> list[str] | None:
    if not isinstance(value, list):
        return None
    codes = [str(c.get("code")) for c in value if isinstance(c, dict) and c.get("code")]
    return codes or None


# --------------------------------------------------------------------------------------
# Input validation
# --------------------------------------------------------------------------------------


def normalize_school_type(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    code = value.strip().upper()
    if code not in SCHOOL_TYPES:
        raise InvalidInputError(
            f"Okänd skoltyp {value!r}. Giltiga koder: {', '.join(SCHOOL_TYPES)}. "
            f"Se resursen {CODES_URI} eller skolverket_syllabus_valuestore(list='schooltypes')."
        )
    return code


def normalize_date(value: str | None, field: str = "date") -> str | None:
    if value is None or not value.strip():
        return None
    text = value.strip()
    try:
        if not _DATE_RE.match(text):
            raise ValueError
        Date.fromisoformat(text)
    except ValueError as exc:
        raise InvalidInputError(f"{field} måste vara ett datum på formen ÅÅÅÅ-MM-DD, fick {value!r}") from exc
    return text


def normalize_code(value: str, what: str = "kod") -> str:
    text = (value or "").strip()
    if not text:
        raise InvalidInputError(f"Ange en {what}")
    if not _CODE_RE.fullmatch(text):
        raise InvalidInputError(
            f"Ogiltig {what} {value!r}: endast bokstäver A–Ö, siffror, '_', '-' och '.' (börjar med bokstav eller "
            "siffra, max 40 tecken)"
        )
    return text


def normalize_version(value: int | None) -> int | None:
    if value is None:
        return None
    if value < 1:
        raise InvalidInputError("version måste vara ett positivt heltal")
    return value


def normalize_study_path_type(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    code = value.strip().upper()
    if code not in STUDY_PATH_TYPES:
        raise InvalidInputError(f"Okänd studievägstyp {value!r}. Giltiga: {', '.join(STUDY_PATH_TYPES)}")
    return code


def normalize_type_of_program(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    code = value.strip().upper()
    if not re.fullmatch(r"[A-Z0-9_]{2,60}", code):
        raise InvalidInputError(f"Ogiltig programtyp {value!r} (t.ex. 'PROGRAM25')")
    return code


# --------------------------------------------------------------------------------------
# Response parsing
# --------------------------------------------------------------------------------------


def payload_list(data: Any, key: str) -> list[Any]:
    """The documented list key, else the first top-level array (defensive: only the
    subjects group has an official spec)."""
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []
    value = data.get(key)
    if isinstance(value, list):
        return value
    for name, candidate in data.items():
        if name not in _ENVELOPE_KEYS and isinstance(candidate, list):
            return candidate
    return []


def payload_object(data: Any, key: str) -> dict[str, Any] | None:
    """The documented object key, else the first top-level object, else the body
    itself when it looks like the entity (has a ``code``)."""
    if not isinstance(data, dict):
        return None
    value = data.get(key)
    if isinstance(value, dict):
        return value
    for name, candidate in data.items():
        if name not in _ENVELOPE_KEYS and isinstance(candidate, dict):
            return candidate
    if "code" in data:
        return {k: v for k, v in data.items() if k not in _ENVELOPE_KEYS}
    return None


def envelope_notes(data: Any) -> list[str]:
    if not isinstance(data, dict):
        return []
    notes = []
    status = data.get("apiStatus")
    if isinstance(status, str) and status.lower() not in ("active", ""):
        notes.append(f"API-status: {status}")
    for key, label in (("apiDeprecation", "Utfasning (deprecation)"), ("apiSunset", "Avveckling (sunset)")):
        if data.get(key):
            notes.append(f"{label}: {data[key]}")
    if data.get("apiLink") and notes:
        notes.append(f"Nyare version: {data['apiLink']}")
    return notes


def to_row(item: Any) -> SyllabusRow | None:
    if isinstance(item, str):
        return SyllabusRow(code=item)
    if not isinstance(item, dict):
        return None
    code = _str(item.get("code"))
    if not code:
        return None
    categories = [
        str(c.get("code") or c.get("name")) for c in item.get("categories") or [] if isinstance(c, dict)
    ] or None
    description = item.get("description")
    return SyllabusRow(
        code=code,
        name=_name(item.get("name")),
        english_name=_name(item.get("englishName")),
        version=_version(item.get("version")),
        type_of_syllabus=_str(item.get("typeOfSyllabus")),
        reform=_str(item.get("reform")),
        study_path_type=_str(item.get("studyPathType") or item.get("typeOfStudyPath")),
        category=_str(item.get("category")),
        school_types=_school_types(item.get("schoolTypes") if "schoolTypes" in item else item.get("schoolType")),
        categories=categories,
        points=_points(item.get("points") if item.get("points") is not None else item.get("point")),
        # Curriculums: community code guesses validFrom/validTo; subjects use startDate/endDate.
        start_date=_str(item.get("startDate") or item.get("validFrom")),
        end_date=_str(item.get("endDate") or item.get("validTo")),
        modified_date=_str(item.get("modifiedDate")),
        canceled_date=_str(item.get("canceledDate")),
        canceled_skolfs=_str(item.get("canceledSkolfs")),
        skolfs_grund=_str(item.get("skolfsGrund")),
        skolfs_andring=_str(item.get("skolfsAndring")),
        version_info=_str(item.get("versionInfo")),
        description=_text(description, 300) if isinstance(description, str) else None,
        course_codes=_course_codes(item.get("courses")),
        orientations=_code_names(item.get("orientations")),
    )


def row_matches(item: Any, tokens: list[str]) -> bool:
    """Client-side search: every token must occur in code, name, English name or
    one of the embedded course/level/orientation codes or names. The subject list
    only embeds courses when a school type is requested (spec: listSubjects)."""
    if not tokens:
        return True
    if not isinstance(item, dict):
        haystack = _norm(item)
    else:
        parts = [item.get("code"), item.get("name"), item.get("englishName")]
        for key in ("courses", "orientations"):
            for sub in item.get(key) or []:
                if isinstance(sub, dict):
                    parts.extend([sub.get("code"), sub.get("name")])
        haystack = " ".join(_norm(p) for p in parts if p)
    return all(token in haystack for token in tokens)


def search_tokens(search: str | None) -> list[str]:
    return [t for t in _norm(search).split() if t] if search else []


def to_central_content(value: Any, max_chars: int | None) -> list[CentralContentItem] | None:
    # Course.centralContent is a single object, Subject.centralContents an array; accept both.
    items = value if isinstance(value, list) else [value] if isinstance(value, dict) else []
    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        text = _text(item.get("text"), max_chars)
        if not text:
            continue
        out.append(
            CentralContentItem(year=_str(item.get("year")), type=_str(item.get("typeOfCentralContent")), text=text)
        )
    return out or None


def to_criteria(value: Any, max_chars: int | None) -> list[GradingCriterion] | None:
    if not isinstance(value, list):
        return None
    out = []
    for item in value:
        if not isinstance(item, dict):
            continue
        step = _str(item.get("gradeStep"))
        full = html_to_text(item.get("text"))
        label = None
        if full:
            first, _, rest = full.partition("\n")
            if _GRADE_LABEL_RE.match(first):
                label, full = first, rest
        year = _str(item.get("year"))
        if label is None and step:
            label = f"Betyget {step}" + (f" (årskurs {year})" if year else "")
        aspect_desc = item.get("aspectDesc")
        out.append(
            GradingCriterion(
                grade_step=step,
                label=label,
                year=year,
                type=_str(item.get("typeOfRequirement")),
                aspect_type=_str(item.get("aspectType")),
                aspect_name=_str(item.get("aspectName")),
                aspect_description=_text(aspect_desc, max_chars) if aspect_desc else None,
                skolfs_id=_str(item.get("skolfsId")),
                text=truncate(full, max_chars) if full else None,
            )
        )
    return out or None


def grading_note(heading: Any, max_chars: int | None = None) -> str | None:
    """knowledgeReqsHeading minus its 'Betygskriterier' title; for Gy25 the rest explains
    the subject-grade rule."""
    text = html_to_text(heading)
    if not text:
        return None
    first, _, rest = text.partition("\n")
    if first.strip().casefold() in ("betygskriterier", "kunskapskrav"):
        text = rest
    text = text.strip()
    return truncate(text, max_chars) if text else None


def is_level(course: dict[str, Any]) -> bool:
    kind = str(course.get("typeOfSyllabus") or "")
    return kind.startswith("LEVEL_") or str(course.get("name") or "").startswith("Nivå")


def to_course(
    course: dict[str, Any], sections: set[str], max_chars: int | None, subject_name: str | None = None
) -> CourseItem:
    name = _name(course.get("name"))
    display = f"{subject_name} – {name}" if subject_name and name and is_level(course) else None
    include_description = "courses" in sections or "description" in sections
    return CourseItem(
        code=str(course.get("code") or ""),
        name=name,
        display_name=display,
        english_name=_name(course.get("englishName")),
        type_of_syllabus=_str(course.get("typeOfSyllabus")),
        points=_points(course.get("points") if course.get("points") is not None else course.get("point")),
        sort_order=_int(course.get("sortOrder")),
        version_info=_str(course.get("versionInfo")),
        gers=_str(course.get("gers")),
        study_path_description=_str(course.get("studyPathDescription")),
        description=_text(course.get("description"), max_chars) if include_description else None,
        central_content=(
            to_central_content(course.get("centralContent"), max_chars) if "central_content" in sections else None
        ),
        grading_note=(
            grading_note(course.get("knowledgeReqsHeading"), max_chars) if "grading_criteria" in sections else None
        ),
        grading_criteria=(
            to_criteria(course.get("knowledgeRequirements"), max_chars) if "grading_criteria" in sections else None
        ),
    )


def subject_notes(subject: dict[str, Any], today: str | None = None) -> list[str]:
    notes = []
    kind = str(subject.get("typeOfSyllabus") or "")
    reform = str(subject.get("reform") or "").upper()
    school_types = _school_types(subject.get("schoolTypes")) or []
    if reform == "GY25" or kind in ("GRADE_SUBJECT_SYLLABUS", "GRADE_SUBJECT_AREA_SYLLABUS"):
        notes.append(GY25_NOTE)
    elif reform == "GY11" or (kind == "SUBJECT_SYLLABUS" and "GY" in school_types):
        notes.append(GY11_NOTE)
    canceled = _str(subject.get("canceledDate"))
    if canceled:
        today = today or Date.today().isoformat()
        verb = "Upphävd" if canceled <= today else "Upphävs"
        text = f"{verb} från {canceled}"
        if subject.get("canceledSkolfs"):
            text += f" (SKOLFS {subject['canceledSkolfs']})"
        if subject.get("endDate"):
            text += f"; gäller under övergångsperioden till och med {subject['endDate']}"
        notes.append(text + ".")
    return notes


def build_subject_detail(
    subject: dict[str, Any],
    sections: set[str],
    course_filter: set[str] | None,
    max_chars: int | None,
) -> SubjectDetail:
    name = _name(subject.get("name"))
    courses_raw = [c for c in subject.get("courses") or [] if isinstance(c, dict)]
    notes = subject_notes(subject)
    courses = None
    if sections & {"courses", "central_content", "grading_criteria"} and courses_raw:
        selected = courses_raw
        if course_filter:
            selected = [c for c in courses_raw if _norm(c.get("code")) in course_filter]
            missing = course_filter - {_norm(c.get("code")) for c in courses_raw}
            if missing:
                notes.append(
                    "Kurs-/nivåkoder som saknas i ämnet: "
                    + ", ".join(sorted(missing))
                    + ". Tillgängliga: "
                    + ", ".join(str(c.get("code")) for c in courses_raw)
                )
        courses = [to_course(c, sections, max_chars, name) for c in selected] or None
    other_texts = None
    if "description" in sections:
        other = {
            "educational_objectives": subject.get("educationalObjectives"),
            "educational_structure": subject.get("educationalStructure"),
            "read_and_write_learning": subject.get("readAndWriteLearning"),
            "assessment": subject.get("assessment"),
        }
        other_texts = {k: t for k, v in other.items() if v and (t := _text(v, max_chars))} or None
    return SubjectDetail(
        code=str(subject.get("code") or ""),
        name=name,
        english_name=_name(subject.get("englishName")),
        type_of_syllabus=_str(subject.get("typeOfSyllabus")),
        reform=_str(subject.get("reform")),
        version=_version(subject.get("version")),
        version_info=_str(subject.get("versionInfo")),
        start_date=_str(subject.get("startDate")),
        end_date=_str(subject.get("endDate")),
        modified_date=_str(subject.get("modifiedDate")),
        canceled_date=_str(subject.get("canceledDate")),
        canceled_skolfs=_str(subject.get("canceledSkolfs")),
        skolfs_grund=_str(subject.get("skolfsGrund")),
        skolfs_andring=_str(subject.get("skolfsAndring")),
        grade_scale=_str(subject.get("gradeScale")),
        points=_points(subject.get("points")),
        designation=_str(subject.get("designation")),
        school_types=_school_types(subject.get("schoolTypes")),
        categories=_code_names(subject.get("categories")),
        appendix2=_code_names(subject.get("appendix2List")),
        description=_text(subject.get("description"), max_chars) if "description" in sections else None,
        purpose=_text(subject.get("purpose"), max_chars) if "purpose" in sections else None,
        other_texts=other_texts,
        course_count=len(courses_raw) or None,
        courses=courses,
        central_contents=(
            to_central_content(subject.get("centralContents"), max_chars) if "central_content" in sections else None
        ),
        grading_note=(
            grading_note(subject.get("knowledgeReqsHeading"), max_chars) if "grading_criteria" in sections else None
        ),
        grading_criteria=(
            to_criteria(subject.get("knowledgeRequirements"), max_chars) if "grading_criteria" in sections else None
        ),
        sections=[s for s in ALL_SUBJECT_SECTIONS if s in sections],
        notes=notes or None,
    )


def subject_ref(subject: dict[str, Any], include_purpose: bool, max_chars: int | None) -> SubjectRef:
    """From a full Subject or from Course.subjectParent (subjectCode/subjectName/subjectPurpose)."""
    purpose = subject.get("purpose") if "purpose" in subject else subject.get("subjectPurpose")
    return SubjectRef(
        code=_str(subject.get("code") or subject.get("subjectCode")),
        name=_name(subject.get("name") or subject.get("subjectName")),
        version=_version(subject.get("version")),
        type_of_syllabus=_str(subject.get("typeOfSyllabus")),
        reform=_str(subject.get("reform")),
        start_date=_str(subject.get("startDate")),
        end_date=_str(subject.get("endDate")),
        modified_date=_str(subject.get("modifiedDate")),
        canceled_date=_str(subject.get("canceledDate")),
        skolfs_grund=_str(subject.get("skolfsGrund")),
        skolfs_andring=_str(subject.get("skolfsAndring")),
        school_types=_school_types(subject.get("schoolTypes")),
        categories=_code_names(subject.get("categories")),
        purpose=_text(purpose, max_chars) if include_purpose and purpose else None,
    )


def parent_subject_candidates(course_code: str) -> list[str]:
    """Guess the parent subject of a course/level code: Gy25 levels end in 'X' and use
    the 4-letter subject (MATE1A00X → MATE); GY11 courses start with the 3-letter
    subject (MATMAT01c → MAT). Inferred from the data, not documented."""
    letters = re.match(r"^[^\W\d_]+", course_code)
    prefix = letters.group(0) if letters else ""
    candidates: list[str] = []
    if course_code.upper().endswith("X") and len(prefix) >= 4:
        candidates.append(course_code[:4])
    if len(prefix) >= 3:
        candidates.append(course_code[:3])
    if len(prefix) >= 4:
        candidates.append(course_code[:4])
    return list(dict.fromkeys(c.upper() for c in candidates))


def simplify(value: Any, max_chars: int | None, depth: int = 0) -> Any:
    """Generic trimming of unknown upstream structures: HTML → text, drop *Heading
    duplicates and empty values, cap long lists."""
    if isinstance(value, str):
        text = html_to_text(value) if ("<" in value and ">" in value) or "­" in value else value.strip()
        return truncate(text, max_chars) if text else None
    if isinstance(value, dict):
        if depth > 8:
            return None
        out = {}
        for key, item in value.items():
            if isinstance(key, str) and key.endswith("Heading"):
                continue
            simplified = simplify(item, max_chars, depth + 1)
            if simplified not in (None, {}, []):
                out[key] = simplified
        return out
    if isinstance(value, list):
        if depth > 8:
            return None
        items = [simplify(v, max_chars, depth + 1) for v in value[:200]]
        items = [v for v in items if v not in (None, {}, [])]
        if len(value) > 200:
            items.append(f"…[{len(value) - 200} poster till]")
        return items
    return value


_PROGRAM_META_KEYS = frozenset(
    {
        "code",
        "name",
        "studyPathType",
        "category",
        "schoolTypes",
        "schoolType",
        "version",
        "versionInfo",
        "startDate",
        "endDate",
        "modifiedDate",
        "canceledDate",
        "canceledSkolfs",
        "skolfsGrund",
        "skolfsAndring",
        "orientations",
        "profiles",
    }
)


def subject_groups(program: dict[str, Any]) -> tuple[list[ProgramSubjectGroup], set[str]]:
    """Every top-level object with a ``subjects`` array is a subject group
    (confirmed: foundationSubjects = gymnasiegemensamma, programmeSpecificSubjects =
    programgemensamma; other groups, e.g. specialization, have unconfirmed keys)."""
    groups: list[ProgramSubjectGroup] = []
    used: set[str] = set()
    for key, value in program.items():
        if not isinstance(value, dict) or not isinstance(value.get("subjects"), list):
            continue
        used.add(key)
        subjects = []
        for item in value["subjects"]:
            if not isinstance(item, dict):
                continue
            subjects.append(
                ProgramSubject(
                    code=_str(item.get("code")),
                    name=_name(item.get("name")),
                    points=_points(item.get("points") if item.get("points") is not None else item.get("point")),
                    type_of_syllabus=_str(item.get("typeOfSyllabus")),
                    course_codes=_course_codes(item.get("courses")),
                )
            )
        groups.append(
            ProgramSubjectGroup(
                key=key,
                name=_name(value.get("name") or value.get("heading") or value.get("title") or value.get("nameHeading")),
                points=_points(value.get("points") if value.get("points") is not None else value.get("totalPoints")),
                subject_count=len(subjects),
                subjects=subjects,
            )
        )
    return groups, used


def _dict_list(value: Any) -> list[dict[str, Any]] | None:
    if not isinstance(value, list):
        return None
    return [v for v in value if isinstance(v, dict)] or None


def build_program_detail(program: dict[str, Any], max_chars: int | None, include_raw: bool) -> ProgramDetail:
    groups, used = subject_groups(program)
    orientations = simplify(program.get("orientations"), max_chars) if program.get("orientations") else None
    profiles = simplify(program.get("profiles"), max_chars) if program.get("profiles") else None
    rest = {k: v for k, v in program.items() if k not in _PROGRAM_META_KEYS and k not in used}
    other = simplify(rest, max_chars) if rest else None
    category = program.get("category")
    return ProgramDetail(
        code=str(program.get("code") or ""),
        name=_name(program.get("name")),
        study_path_type=_str(program.get("studyPathType")),
        category=_str(category) if not isinstance(category, dict) else _str(category.get("code")),
        school_types=_school_types(program.get("schoolTypes") or program.get("schoolType")),
        version=_version(program.get("version")),
        version_info=_str(program.get("versionInfo")),
        start_date=_str(program.get("startDate")),
        end_date=_str(program.get("endDate")),
        modified_date=_str(program.get("modifiedDate")),
        canceled_date=_str(program.get("canceledDate")),
        canceled_skolfs=_str(program.get("canceledSkolfs")),
        skolfs_grund=_str(program.get("skolfsGrund")),
        skolfs_andring=_str(program.get("skolfsAndring")),
        subject_groups=groups or None,
        orientations=_dict_list(orientations),
        profiles=_dict_list(profiles),
        other=other or None,
        raw=program if include_raw else None,
    )


_CURRICULUM_META_KEYS = frozenset(
    {
        "code",
        "name",
        "schoolTypes",
        "schoolType",
        "typeOfSyllabus",
        "version",
        "startDate",
        "endDate",
        "validFrom",
        "validTo",
        "modifiedDate",
    }
)


def build_curriculum_detail(curriculum: dict[str, Any], max_chars: int | None, include_raw: bool) -> CurriculumDetail:
    rest = {k: v for k, v in curriculum.items() if k not in _CURRICULUM_META_KEYS}
    content = simplify(rest, max_chars) if rest else None
    return CurriculumDetail(
        code=str(curriculum.get("code") or ""),
        name=_name(curriculum.get("name")),
        school_types=_school_types(curriculum.get("schoolTypes") or curriculum.get("schoolType")),
        type_of_syllabus=_str(curriculum.get("typeOfSyllabus")),
        version=_version(curriculum.get("version")),
        start_date=_str(curriculum.get("startDate") or curriculum.get("validFrom")),
        end_date=_str(curriculum.get("endDate") or curriculum.get("validTo")),
        modified_date=_str(curriculum.get("modifiedDate")),
        content=content or None,
        raw=curriculum if include_raw else None,
    )


def to_value_row(item: Any) -> ValueRow | None:
    if isinstance(item, str):
        return ValueRow(code=item.strip()) if item.strip() else None
    if not isinstance(item, dict):
        return None
    description = item.get("description")
    return ValueRow(
        code=_str(item.get("code")),
        name=_name(item.get("name")),
        description=_text(description, 500) if isinstance(description, str) else None,
        type=_str(item.get("type") or item.get("studyPathType")),
        type_of_syllabus=_str(item.get("typeOfSyllabus")),
        start_date=_str(item.get("startDate")),
        end_date=_str(item.get("endDate")),
        modified_date=_str(item.get("modifiedDate")),
        school_types=_school_types(item.get("schoolTypes") or item.get("schoolType")),
    )


def output_size(model: BaseModel) -> int:
    """Length of the compact JSON text that ``structured`` sends for ``model``."""
    return len(json.dumps(dump(model), ensure_ascii=False, separators=(",", ":")))


def add_note(model: BaseModel, note: str) -> None:
    model.notes = [*(getattr(model, "notes", None) or []), note]  # type: ignore[attr-defined]


def trim_strings(value: Any, max_chars: int | None) -> Any:
    """Copy of an upstream structure with every string shortened to ``max_chars``
    (keeps ``include_raw`` output inside the budget)."""
    if isinstance(value, str):
        return truncate(value, max_chars)
    if isinstance(value, dict):
        return {k: trim_strings(v, max_chars) for k, v in value.items()}
    if isinstance(value, list):
        return [trim_strings(v, max_chars) for v in value]
    return value


_UNCAPPED_FIELDS = frozenset({"notes", "sections"})


def cap_containers(value: Any, size: int) -> Any:
    """Keep at most ``size`` entries of every list/dict (models are changed in place).
    Last resort of :func:`fit_output`; ``notes`` and ``sections`` are left alone."""
    if isinstance(value, BaseModel):
        for name in type(value).model_fields:
            item = getattr(value, name)
            if name not in _UNCAPPED_FIELDS and isinstance(item, (list, dict, BaseModel)):
                setattr(value, name, cap_containers(item, size))
        return value
    if isinstance(value, list):
        return [cap_containers(v, size) for v in value[:size]]
    if isinstance(value, dict):
        return {k: cap_containers(v, size) for k, v in list(value.items())[:size]}
    return value


def fit_output(
    build: Callable[[int | None], ModelT],
    max_chars: int | None,
    reducers: Sequence[Callable[[ModelT], None]] = (),
) -> ModelT:
    """Build the result and keep its compact JSON under ``MAX_OUTPUT_CHARS``:

    1. rebuild with every text field shortened to the ``SHRINK_STEPS`` budgets (only
       those below ``max_chars``) until it fits;
    2. apply the structural ``reducers`` in order (each removes something and says
       what in ``notes``);
    3. last resort: cap every list/dict to fewer and fewer entries.
    """
    model = build(max_chars)
    if output_size(model) <= MAX_OUTPUT_CHARS:
        return model
    shortened: int | None = None
    for chars in SHRINK_STEPS:
        if max_chars is not None and chars >= max_chars:
            continue
        model = build(chars)
        shortened = chars
        if output_size(model) <= MAX_OUTPUT_CHARS:
            break
    if shortened is not None:
        add_note(
            model,
            f"Svaret blev för stort (gräns ca {MAX_OUTPUT_CHARS} tecken) och texterna har kortats till {shortened} "
            "tecken per fält. Begränsa urvalet (t.ex. sections/course_codes) för att få längre texter.",
        )
    for reduce in reducers:
        if output_size(model) <= MAX_OUTPUT_CHARS:
            break
        reduce(model)
    if output_size(model) > MAX_OUTPUT_CHARS:
        add_note(
            model,
            f"Svaret var fortfarande för stort: listor och objekt har kapats (de första posterna behålls) för att "
            f"hålla det under ca {MAX_OUTPUT_CHARS} tecken. Begränsa urvalet.",
        )
        size = 32
        while size >= 1 and output_size(model) > MAX_OUTPUT_CHARS:
            cap_containers(model, size)
            size //= 2
    return model


def drop_courses(detail: SubjectDetail) -> None:
    """Reducer for subjects: leave out courses/levels from the end until the result
    fits and name the omitted codes so they can be fetched separately."""
    courses = list(detail.courses or [])
    if not courses:
        return
    total = len(courses)
    notes = list(detail.notes or [])
    omitted: list[str] = []

    def apply() -> None:
        detail.courses = courses or None
        detail.notes = [
            *notes,
            f"Svaret blev för stort: {len(omitted)} av {total} kurser/nivåer utelämnades ({', '.join(omitted)}). "
            "Hämta dem med course_codes=[...] eller skolverket_get_course.",
        ]

    # Estimate from the per-course sizes, then verify exactly.
    sizes = [output_size(course) + 1 for course in courses]
    excess = output_size(detail) - MAX_OUTPUT_CHARS + 300
    removed = 0
    while courses and removed < excess + sum(len(code) + 2 for code in omitted):
        removed += sizes[len(courses) - 1]
        omitted.insert(0, courses.pop().code)
    apply()
    while courses and output_size(detail) > MAX_OUTPUT_CHARS:
        omitted.insert(0, courses.pop().code)
        apply()


def drop_raw(detail: ProgramDetail | CurriculumDetail) -> None:
    """Reducer for programs/curriculums: leave out the include_raw object."""
    if detail.raw is not None:
        detail.raw = None
        add_note(
            detail,
            "include_raw: råobjektet utelämnades eftersom svaret annars blev för stort; de bearbetade fälten "
            "bygger på samma data.",
        )


ListT = TypeVar("ListT", SyllabusListResult, ValuestoreResult)


def fit_items(result: ListT, pageable: bool = True) -> ListT:
    """Stop adding rows once the compact JSON passes ``MAX_OUTPUT_CHARS`` (a large
    ``limit`` alone could exceed the hosts' ~25k-token cap)."""
    if output_size(result) <= MAX_OUTPUT_CHARS:
        return result
    rows = list(result.items)
    budget = MAX_OUTPUT_CHARS - output_size(result.model_copy(update={"items": []})) - 400  # room for the note
    used = kept = 0
    for row in rows:
        size = output_size(row) + 1
        if kept and used + size > budget:
            break
        used += size
        kept += 1
    result.items = rows[:kept]
    result.returned = kept
    result.truncated = True
    hint = (
        f"Hämta nästa sida med offset={result.offset + kept} eller smalna av urvalet (search/filter)."
        if pageable
        else "Resten utelämnades."
    )
    add_note(
        result, f"Svaret begränsades till {kept} rader för att hålla det under ca {MAX_OUTPUT_CHARS} tecken. {hint}"
    )
    return result


def paginate(rows: list[Any], offset: int, limit: int) -> tuple[list[Any], int, int]:
    start = max(0, offset)
    size = clamp(limit, 1, MAX_LIMIT)
    return rows[start : start + size], start, size


# --------------------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------------------


def register(server: MCPServer[Any], services: Services) -> None:
    def url(path: str) -> str:
        return f"{services.settings.base_url(BASE_KEY)}{API_PREFIX}{path}"

    async def get(path: str, params: dict[str, Any] | None = None) -> ApiResponse:
        return await services.http.get_json(SOURCE, url(path), params=params, headers=HEADERS)

    async def get_list(
        path: str, params: dict[str, Any], key: str
    ) -> tuple[list[Any], ApiResponse | None, str | None, str | None]:
        """404 on a list endpoint means "tomt svar" (no match) → empty list plus a note
        that quotes the upstream message (it echoes the parameters the API used)."""
        try:
            response = await get(path, params)
        except UpstreamError as exc:
            if exc.status == 404:
                note = "Inga träffar: API:t svarade 404 ('tomt svar') för urvalet."
                detail = _str(exc.detail)
                if detail:
                    note += f" API:ts meddelande: {truncate(detail, 300)}"
                return [], None, exc.url, note
            raise
        return payload_list(response.data, key), response, response.url, None

    def missing_hint(version: int | None, when: str | None, versions_tool: str, code: str) -> str:
        """Per spec a read returns max(version) with startDate <= date, else 404 – so a
        miss is a wrong code, a wrong version or a date before the first version."""
        if version is not None:
            return f"Versionen finns inte – se {versions_tool}(code={code!r}) för befintliga versioner."
        day = f"sökdatumet {when}" if when else "dagens datum (default för date)"
        return (
            f"Antingen är koden fel (skiftlägeskänslig) eller så har ingen version startDate <= {day} – API:t "
            f"returnerar den senaste versionen som börjat gälla på sökdatumet. Se giltighetsdatumen med "
            f"{versions_tool}(code={code!r}) och ange ett senare date för planer som börjar gälla senare."
        )

    def not_found(exc: UpstreamError, what: str, hint: str) -> UpstreamError:
        return UpstreamError(
            SOURCE,
            f"{what} hittades inte (API:t svarade 'tomt svar'). {hint}",
            status=404,
            url=exc.url,
            detail=exc.detail,
        )

    def api_version(response: ApiResponse | None) -> str | None:
        if response is None or not isinstance(response.data, dict):
            return None
        return _str(response.data.get("apiVersion"))

    async def list_entities(
        *,
        kind: str,
        path: str,
        key: str,
        params: dict[str, Any],
        search: str | None,
        limit: int,
        offset: int,
        extra_filter: Callable[[Any], bool] | None = None,
        filters_echo: dict[str, Any] | None = None,
        sort_versions: bool = False,
        pageable: bool = True,
    ) -> SyllabusListResult:
        items, response, request_url, empty_note = await get_list(path, params, key)
        tokens = search_tokens(search)
        matched = [it for it in items if row_matches(it, tokens) and (extra_filter is None or extra_filter(it))]
        rows = [r for r in (to_row(it) for it in matched) if r is not None]
        if sort_versions:
            rows.sort(key=lambda r: r.version if isinstance(r.version, int) else -1, reverse=True)
        page, start, _ = paginate(rows, offset, limit)
        notes = envelope_notes(response.data) if response else []
        if empty_note:
            notes.append(empty_note)
        elif items and not rows:
            notes.append("Inga poster matchade klientfiltreringen (search m.m.).")
        echo = {k: v for k, v in (filters_echo or params).items() if v is not None}
        if search:
            echo["search"] = search
        result = SyllabusListResult(
            kind=kind,
            total=len(rows),
            returned=len(page),
            offset=start,
            truncated=start + len(page) < len(rows),
            upstream_count=len(items),
            filters=echo,
            items=page,
            api_version=api_version(response),
            request_url=request_url,
            notes=notes or None,
        )
        return fit_items(result, pageable)

    async def find_in_subjects(
        course_code: str, candidates: list[str], when: str | None
    ) -> tuple[dict[str, Any], dict[str, Any], ApiResponse] | None:
        """Read candidate parent subjects and return the first whose courses[] holds the code."""
        wanted_code = _norm(course_code)
        for candidate in candidates:
            try:
                response = await get(f"/subjects/{quote(candidate, safe='')}", {"date": when})
            except UpstreamError as exc:
                if exc.status == 404:
                    continue
                raise
            subject = payload_object(response.data, "subject")
            for course in (subject or {}).get("courses") or []:
                if isinstance(course, dict) and _norm(course.get("code")) == wanted_code:
                    return subject or {}, course, response
        return None

    async def subject_for_level(
        course_code: str, parent: dict[str, Any] | None, by_version: bool, when: str | None
    ) -> tuple[dict[str, Any] | None, str | None, str | None]:
        """Gy25 levels carry no criteria of their own (they belong to the subject): read
        the parent subject – subjectParent.subjectCode at the parent's version when the
        level was requested by version, otherwise at the same date. Returns
        (subject, request_url, problem); failures become a note, not an error."""
        code = _str(parent.get("subjectCode")) if parent else None
        try:
            if code is None:
                found = await find_in_subjects(course_code, parent_subject_candidates(course_code), when)
                if found is None:
                    return None, None, "Ämnet kunde inte bestämmas (subjectParent saknas i svaret)."
                return found[0], found[2].url, None
            if not _CODE_RE.fullmatch(code):
                return None, None, f"Oväntad ämneskod i subjectParent: {code!r}."
            parent_version = _int(parent.get("version")) if parent else None
            if by_version and parent_version is not None:
                path, params = f"/subjects/{quote(code, safe='')}/versions/{parent_version}", None
            else:
                path, params = f"/subjects/{quote(code, safe='')}", {"date": when}
            response = await get(path, params)
        except UpstreamError as exc:
            status = f"HTTP {exc.status}" if exc.status else exc.message
            return None, exc.url, f"Ämnet {code or '?'} kunde inte hämtas för betygskriterierna ({status})."
        subject = payload_object(response.data, "subject")
        if subject is None:
            return None, response.url, f"Oväntat svar för ämnet {code}: objektet 'subject' saknas."
        return subject, response.url, None

    LimitArg = Annotated[int, Field(description=f"Max antal rader (1–{MAX_LIMIT})")]
    OffsetArg = Annotated[int, Field(description="Hoppa över så många träffar (för bläddring)")]
    SearchArg = Annotated[
        str | None,
        Field(
            description="Fritextfilter (klientsida, API:t saknar textsökning): alla ord måste finnas i kod, namn "
            "eller engelskt namn, skiftlägesokänsligt. Inbäddade kurs-/nivåkoder och inriktningar genomsöks när "
            "API:t tar med dem – ämneslistan innehåller kurser bara när schooltype anges"
        ),
    ]
    SchoolTypeArg = Annotated[
        str | None,
        Field(
            description="Skoltyp (school type), t.ex. 'GR' (grundskolan), 'GY' (gymnasieskolan, både GY11 och Gy25), "
            f"'GYAN', 'VUXGY', 'VUXSFI'. Skiftlägesokänslig. Alla koder: resursen {CODES_URI}"
        ),
    ]
    DateArg = Annotated[str | None, Field(description="Sökdatum ÅÅÅÅ-MM-DD (API:ts default är dagens datum)")]

    # ---------------------------------------------------------------- subjects

    @server.tool(name="skolverket_list_subjects", title="Skolverket: lista ämnen", annotations=READ_ONLY_OPEN)
    @tool_errors
    async def skolverket_list_subjects(
        schooltype: SchoolTypeArg = None,
        timespan: Annotated[
            SubjectTimespan,
            Field(
                description="LATEST = gällande på sökdatumet (default), FUTURE = kommande, CANCELED = upphävda med "
                "övergångsbestämmelser, EXPIRED = utgångna, MODIFIED = ändrade från och med sökdatumet"
            ),
        ] = "LATEST",
        date: DateArg = None,
        type_of_syllabus: Annotated[
            TypeOfSyllabus | None,
            Field(
                description="Ämnestyp: GRADE_SUBJECT_SYLLABUS = Gy25-ämnen med nivåer, SUBJECT_SYLLABUS = GY11-ämnen "
                "med kurser, COURSE_SYLLABUS = grundskolans kursplaner, SFI_SUBJECT_SYLLABUS = sfi m.fl. Default ALL."
            ),
        ] = None,
        search: SearchArg = None,
        limit: LimitArg = DEFAULT_LIMIT,
        offset: OffsetArg = 0,
    ) -> Annotated[CallToolResult, SyllabusListResult]:
        """Lista ämnen/ämnesplaner/kursplaner (subjects) i Skolverkets Syllabus-API med kod, namn, skoltyper,
        ämnestyp, reform (GY11/GY25 när API:t anger det), version och giltighetsdatum. När skoltyp anges innehåller
        varje rad även kurs-/nivåkoderna (course_codes). Gy25: schooltype='GY' + type_of_syllabus=
        'GRADE_SUBJECT_SYLLABUS'; GY11: 'SUBJECT_SYLLABUS'. Fritextsökning görs på klientsidan.
        Använd sedan skolverket_get_subject för innehåll (syfte, centralt innehåll, betygskriterier)."""
        params = {
            "schooltype": normalize_school_type(schooltype),
            "timespan": timespan,
            "typeOfSyllabus": type_of_syllabus,
            "date": normalize_date(date),
        }
        result = await list_entities(
            kind="subjects", path="/subjects", key="subjects", params=params, search=search, limit=limit, offset=offset
        )
        return structured(result)

    @server.tool(name="skolverket_get_subject", title="Skolverket: hämta ämne", annotations=READ_ONLY_OPEN)
    @tool_errors
    async def skolverket_get_subject(
        code: Annotated[
            str,
            Field(
                description="Ämneskod (subject code), skiftlägeskänslig: t.ex. 'MAT' (GY11), 'MATE' (Gy25), "
                "'GRGRMAT01' (grundskolans kursplan), 'SFI'"
            ),
        ],
        version: Annotated[
            int | None,
            Field(description="Specifik version (se skolverket_list_subject_versions). Utelämna för gällande version."),
        ] = None,
        date: DateArg = None,
        sections: Annotated[
            list[SubjectSection] | None,
            Field(
                description="Delar att ta med (default alla): description (ämnets beskrivning), purpose (ämnets "
                "syfte), courses (kurser/nivåer), central_content (centralt innehåll), grading_criteria "
                "(betygskriterier/kunskapskrav)"
            ),
        ] = None,
        course_codes: Annotated[
            list[str] | None,
            Field(description="Ta bara med dessa kurs-/nivåkoder, t.ex. ['MATMAT01c'] eller ['MATE1A00X']"),
        ] = None,
        max_chars: Annotated[
            int | None,
            Field(
                ge=MIN_MAX_CHARS,
                description=f"Korta varje textfält till högst så många tecken (minst {MIN_MAX_CHARS}, t.ex. 1500). "
                f"Utelämna = hela texter. Oavsett värde hålls hela svaret under ca {MAX_OUTPUT_CHARS} tecken "
                "(texter kortas och kurser utelämnas vid behov, se notes).",
            ),
        ] = None,
    ) -> Annotated[CallToolResult, SubjectDetail]:
        """Hämta ett ämne (ämnesplan/kursplan) med ämnets beskrivning och syfte (HTML omgjort till text), kurser
        eller Gy25-nivåer med kod/namn/poäng, centralt innehåll per kurs/nivå (och per stadium för grundskolans
        kursplaner) samt betygskriterier med betygssteg (E–A). GY11: kriterierna ligger per kurs. Gy25: nivåerna
        har eget centralt innehåll men kriterierna gäller ämnet (ämnesbetyg). Begränsa storleken med sections,
        course_codes och max_chars; blir svaret ändå för stort kortas texterna och sista kurserna utelämnas
        (koderna anges i notes)."""
        subject_code = normalize_code(code, "ämneskod")
        version = normalize_version(version)
        when = normalize_date(date)
        if version is not None and when is not None:
            raise InvalidInputError("Ange antingen version eller date, inte båda")
        if version is not None:
            path, params = f"/subjects/{quote(subject_code, safe='')}/versions/{version}", None
        else:
            path, params = f"/subjects/{quote(subject_code, safe='')}", {"date": when}
        try:
            response = await get(path, params)
        except UpstreamError as exc:
            if exc.status == 404:
                what = f"Ämnet {subject_code!r}" + (f" version {version}" if version is not None else "")
                hint = missing_hint(version, when, "skolverket_list_subject_versions", subject_code)
                if version is None:
                    hint += (
                        " Giltiga koder: skolverket_list_subjects eller "
                        "skolverket_syllabus_valuestore(list='subjectandcoursecodes')."
                    )
                raise not_found(exc, what, hint) from exc
            raise
        subject = payload_object(response.data, "subject")
        if subject is None:
            raise UpstreamError(SOURCE, "Oväntat svar: objektet 'subject' saknas", url=response.url)
        wanted = set(sections) if sections else set(ALL_SUBJECT_SECTIONS)
        course_filter = {_norm(c.strip()) for c in course_codes if c.strip()} if course_codes else None

        def build(chars: int | None) -> SubjectDetail:
            detail = build_subject_detail(subject, wanted, course_filter, chars)
            extra = envelope_notes(response.data)
            if extra:
                detail.notes = [*(detail.notes or []), *extra]
            detail.api_version = api_version(response)
            detail.request_url = response.url
            return detail

        return structured(fit_output(build, max_chars, [drop_courses]))

    @server.tool(
        name="skolverket_list_subject_versions", title="Skolverket: ämnesversioner", annotations=READ_ONLY_OPEN
    )
    @tool_errors
    async def skolverket_list_subject_versions(
        code: Annotated[str, Field(description="Ämneskod, t.ex. 'MAT' eller 'MATE'")],
        limit: LimitArg = DEFAULT_LIMIT,
    ) -> Annotated[CallToolResult, SyllabusListResult]:
        """Lista alla versioner av ett ämne (nyast först) med giltighetsdatum, ändringsdatum och SKOLFS-nummer
        (skolfsGrund/skolfsAndring visas bara här). Versionsnumren är inte löpande. Hämta en viss version med
        skolverket_get_subject(code, version=...)."""
        subject_code = normalize_code(code, "ämneskod")
        result = await list_entities(
            kind="subject_versions",
            path=f"/subjects/{quote(subject_code, safe='')}/versions",
            key="subjects",
            params={},
            search=None,
            limit=limit,
            offset=0,
            filters_echo={"code": subject_code},
            sort_versions=True,
            pageable=False,
        )
        return structured(result)

    # ---------------------------------------------------------------- courses

    @server.tool(name="skolverket_list_courses", title="Skolverket: lista kurser", annotations=READ_ONLY_OPEN)
    @tool_errors
    async def skolverket_list_courses(
        schooltype: SchoolTypeArg = None,
        timespan: Annotated[
            Timespan, Field(description="LATEST (default), FUTURE, EXPIRED eller MODIFIED (se ämneslistan)")
        ] = "LATEST",
        date: DateArg = None,
        search: SearchArg = None,
        limit: LimitArg = DEFAULT_LIMIT,
        offset: OffsetArg = 0,
    ) -> Annotated[CallToolResult, SyllabusListResult]:
        """Lista kurser (courses) i Syllabus-API:t med kod, namn, poäng och giltighet. Gy25 har nivåer i stället
        för kurser – de hör till ett ämne (t.ex. MATE1A00X i ämnet MATE) och listas enklast via
        skolverket_list_subjects(schooltype='GY') eller skolverket_get_subject. Fritextsökning görs på klientsidan.
        Fältuppsättningen för kurslistan är inte officiellt dokumenterad och tolkas defensivt."""
        params = {"schooltype": normalize_school_type(schooltype), "timespan": timespan, "date": normalize_date(date)}
        result = await list_entities(
            kind="courses", path="/courses", key="courses", params=params, search=search, limit=limit, offset=offset
        )
        return structured(result)

    @server.tool(name="skolverket_get_course", title="Skolverket: hämta kurs/nivå", annotations=READ_ONLY_OPEN)
    @tool_errors
    async def skolverket_get_course(
        code: Annotated[
            str,
            Field(
                description="Kurskod (GY11, t.ex. 'MATMAT01c') eller nivåkod (Gy25, t.ex. 'MATE1A00X'); "
                "skiftlägeskänslig"
            ),
        ],
        version: Annotated[
            int | None, Field(description="Specifik version (se skolverket_list_course_versions)")
        ] = None,
        date: DateArg = None,
        subject_code: Annotated[
            str | None,
            Field(
                description="Ämneskoden om kursen/nivån ska hämtas via sitt ämne (annars gissas den: MATE1A00X → "
                "MATE, MATMAT01c → MAT)"
            ),
        ] = None,
        sections: Annotated[
            list[CourseSection] | None,
            Field(
                description="Delar att ta med: description, purpose (ämnets syfte), central_content, "
                "grading_criteria. Default allt utom purpose."
            ),
        ] = None,
        max_chars: Annotated[
            int | None,
            Field(
                ge=MIN_MAX_CHARS,
                description=f"Korta varje textfält till högst så många tecken (minst {MIN_MAX_CHARS}). Svaret hålls "
                f"alltid under ca {MAX_OUTPUT_CHARS} tecken.",
            ),
        ] = None,
    ) -> Annotated[CallToolResult, CourseDetailResult]:
        """Hämta en kurs (GY11) eller nivå (Gy25) med namn, poäng, centralt innehåll och betygskriterier samt
        ämnet den hör till. Läser /v1/courses/{code}; om API:t inte hittar koden (t.ex. Gy25-nivåer) hämtas
        ämnet och kursen/nivån plockas ur dess kurslista. För Gy25-nivåer redovisas ämnets betygskriterier separat
        (subject_grading_criteria, med grading_criteria i sections) eftersom betyget sätts på ämnet; svarar
        /v1/courses för nivån hämtas ämnet med ett extra anrop för kriteriernas skull."""
        course_code = normalize_code(code, "kurskod")
        version = normalize_version(version)
        when = normalize_date(date)
        if version is not None and when is not None:
            raise InvalidInputError("Ange antingen version eller date, inte båda")
        explicit_subject = normalize_code(subject_code, "ämneskod") if subject_code else None
        wanted = set(sections) if sections else set(DEFAULT_COURSE_SECTIONS)
        include_purpose = "purpose" in wanted
        if version is not None:
            path, params = f"/courses/{quote(course_code, safe='')}/versions/{version}", None
        else:
            path, params = f"/courses/{quote(course_code, safe='')}", {"date": when}

        first_error: UpstreamError | None = None
        response: ApiResponse | None = None
        if explicit_subject is None or version is not None:
            try:
                response = await get(path, params)
            except UpstreamError as exc:
                if exc.status != 404:
                    raise
                first_error = exc
                if version is not None:
                    raise not_found(
                        exc,
                        f"Kursen {course_code!r} version {version}",
                        missing_hint(version, when, "skolverket_list_course_versions", course_code),
                    ) from exc

        if response is not None:
            course = payload_object(response.data, "course")
            if course is None:
                raise UpstreamError(SOURCE, "Oväntat svar: objektet 'course' saknas", url=response.url)
            parent = course.get("subjectParent") if isinstance(course.get("subjectParent"), dict) else None
            current = response
            level_subject: dict[str, Any] | None = None
            level_url: str | None = None
            level_problem: str | None = None
            if is_level(course) and "grading_criteria" in wanted and not course.get("knowledgeRequirements"):
                level_subject, level_url, level_problem = await subject_for_level(
                    course_code, parent, version is not None, when
                )

            def build_standalone(chars: int | None) -> CourseDetailResult:
                subject_name = _name(parent.get("subjectName")) if parent else None
                if subject_name is None and level_subject is not None:
                    subject_name = _name(level_subject.get("name"))
                item = to_course(course, wanted, chars, subject_name)
                notes = envelope_notes(current.data)
                level_criteria: list[GradingCriterion] | None = None
                level_note: str | None = None
                if level_subject is not None and not item.grading_criteria:
                    level_criteria = to_criteria(level_subject.get("knowledgeRequirements"), chars)
                    level_note = grading_note(level_subject.get("knowledgeReqsHeading"), chars)
                if is_level(course):
                    notes.append(GY25_NOTE)
                    if level_criteria:
                        notes.append(f"Ämnets betygskriterier (subject_grading_criteria) hämtades från {level_url}.")
                    elif "grading_criteria" in wanted and not item.grading_criteria:
                        notes.append(
                            (f"{level_problem} " if level_problem else "")
                            + "Ämnets betygskriterier hämtas med skolverket_get_subject(code=<ämneskod>, "
                            "sections=['grading_criteria'])."
                        )
                if level_subject is not None:
                    subject = subject_ref(level_subject, include_purpose, chars)
                else:
                    subject = subject_ref(parent, include_purpose, chars) if parent else None
                return CourseDetailResult(
                    course=item,
                    subject=subject,
                    subject_grading_note=level_note,
                    subject_grading_criteria=level_criteria,
                    resolved_via="courses",
                    sections=sorted(wanted),
                    notes=notes or None,
                    api_version=api_version(current),
                    request_url=current.url,
                )

            return structured(fit_output(build_standalone, max_chars))

        # Fallback: read the parent subject and pick the course/level from courses[].
        candidates = [explicit_subject] if explicit_subject else parent_subject_candidates(course_code)
        found = await find_in_subjects(course_code, candidates, when)
        if found is not None:
            matched_subject, matched_course, subject_response = found

            def build_from_subject(chars: int | None) -> CourseDetailResult:
                item = to_course(matched_course, wanted, chars, _name(matched_subject.get("name")))
                notes = [*subject_notes(matched_subject), *envelope_notes(subject_response.data)]
                reason = "enligt subject_code" if explicit_subject else "eftersom /v1/courses inte gav något svar"
                notes.append(f"Hämtad via ämnet {matched_subject.get('code')} (/v1/subjects) {reason}.")
                level_criteria = None
                level_note = None
                if is_level(matched_course) and "grading_criteria" in wanted and not item.grading_criteria:
                    level_criteria = to_criteria(matched_subject.get("knowledgeRequirements"), chars)
                    level_note = grading_note(matched_subject.get("knowledgeReqsHeading"), chars)
                return CourseDetailResult(
                    course=item,
                    subject=subject_ref(matched_subject, include_purpose, chars),
                    subject_grading_note=level_note,
                    subject_grading_criteria=level_criteria,
                    resolved_via="subject",
                    sections=sorted(wanted),
                    notes=notes or None,
                    api_version=api_version(subject_response),
                    request_url=subject_response.url,
                )

            return structured(fit_output(build_from_subject, max_chars))

        tried = ", ".join(candidates) or "–"
        where = f"ämnet {tried}" if explicit_subject else f"/v1/courses eller i ämnena {tried}"
        day = f"sökdatumet {when}" if when else "dagens datum (default för date)"
        error = first_error or UpstreamError(SOURCE, "Hittades inte", status=404)
        raise not_found(
            error,
            f"Kursen/nivån {course_code!r}",
            f"Den fanns inte i {where} på {day}. Antingen är koden fel (skiftlägeskänslig) eller så har ingen "
            "version börjat gälla på sökdatumet (startDate <= date) – ange då ett senare date. Ange subject_code, "
            "eller sök koden med skolverket_syllabus_valuestore(list='subjectandcoursecodes', search=...). "
            "Grundskolans kursplaner (t.ex. GRGRMAT01) är ämnen – använd skolverket_get_subject.",
        )

    @server.tool(name="skolverket_list_course_versions", title="Skolverket: kursversioner", annotations=READ_ONLY_OPEN)
    @tool_errors
    async def skolverket_list_course_versions(
        code: Annotated[str, Field(description="Kurskod, t.ex. 'MATMAT01c'")],
        limit: LimitArg = DEFAULT_LIMIT,
    ) -> Annotated[CallToolResult, SyllabusListResult]:
        """Lista versioner av en kurs (nyast först). Svarsformatet är inte officiellt dokumenterat och tolkas
        defensivt; för GY11-kurser ger skolverket_list_subject_versions för ämnet samma versionshistorik."""
        course_code = normalize_code(code, "kurskod")
        result = await list_entities(
            kind="course_versions",
            path=f"/courses/{quote(course_code, safe='')}/versions",
            key="courses",
            params={},
            search=None,
            limit=limit,
            offset=0,
            filters_echo={"code": course_code},
            sort_versions=True,
            pageable=False,
        )
        return structured(result)

    # ---------------------------------------------------------------- programs

    @server.tool(name="skolverket_list_programs", title="Skolverket: lista program", annotations=READ_ONLY_OPEN)
    @tool_errors
    async def skolverket_list_programs(
        schooltype: Annotated[
            str | None, Field(description="Skoltyp, normalt 'GY' (gymnasieskolan) eller 'GYAN'")
        ] = "GY",
        timespan: Annotated[
            SubjectTimespan, Field(description="LATEST (default), FUTURE, CANCELED, EXPIRED eller MODIFIED")
        ] = "LATEST",
        date: DateArg = None,
        study_path_type: Annotated[
            str | None,
            Field(
                description="Filtrera (klientsida) på studievägstyp: PROGRAM25 = Gy25-program (NA25 …), PROGRAM = "
                "GY11-program, FOURTH_TECHNICAL_YEAR25 = fjärde tekniskt år Gy25"
            ),
        ] = None,
        search: SearchArg = None,
        limit: LimitArg = DEFAULT_LIMIT,
        offset: OffsetArg = 0,
    ) -> Annotated[CallToolResult, SyllabusListResult]:
        """Lista gymnasieprogram (programs) med kod, namn, studievägstyp (PROGRAM = GY11, PROGRAM25 = Gy25),
        kategori (högskoleförberedande/yrkesprogram/riksrekryterande), inriktningar (orientations) och
        canceledDate för upphävda GY11-program. Gy25-program har suffixet 25 (NA25). Använd
        skolverket_get_program för programmets ämnen."""
        study_type = normalize_study_path_type(study_path_type)
        params = {"schooltype": normalize_school_type(schooltype), "timespan": timespan, "date": normalize_date(date)}

        def type_filter(item: Any) -> bool:
            if study_type is None or not isinstance(item, dict):
                return True
            return str(item.get("studyPathType") or "").upper() == study_type

        echo = dict(params, study_path_type=study_type)
        result = await list_entities(
            kind="programs",
            path="/programs",
            key="programs",
            params=params,
            search=search,
            limit=limit,
            offset=offset,
            extra_filter=type_filter,
            filters_echo=echo,
        )
        return structured(result)

    @server.tool(name="skolverket_get_program", title="Skolverket: hämta program", annotations=READ_ONLY_OPEN)
    @tool_errors
    async def skolverket_get_program(
        code: Annotated[str, Field(description="Programkod, t.ex. 'NA25' (Gy25), 'EK' (GY11), 'VI25'")],
        version: Annotated[
            int | None, Field(description="Specifik version (se skolverket_list_program_versions)")
        ] = None,
        date: DateArg = None,
        max_chars: Annotated[
            int | None,
            Field(
                ge=MIN_MAX_CHARS,
                description=f"Max tecken per textfält i övriga fält (default {DEFAULT_DETAIL_MAX_CHARS}, minst "
                f"{MIN_MAX_CHARS}). Svaret hålls alltid under ca {MAX_OUTPUT_CHARS} tecken.",
            ),
        ] = DEFAULT_DETAIL_MAX_CHARS,
        include_raw: Annotated[
            bool,
            Field(
                description="Ta med API:ts hela programobjekt (stort). Blir svaret för stort kortas råobjektets "
                "texter och till sist utelämnas det (se notes)."
            ),
        ] = False,
    ) -> Annotated[CallToolResult, ProgramDetail]:
        """Hämta ett gymnasieprogram med metadata, ämnesgrupper (foundationSubjects = gymnasiegemensamma ämnen,
        programmeSpecificSubjects = programgemensamma ämnen och övriga grupper som API:t returnerar) med
        ämneskoder, inriktningar/profiler och övriga fält (HTML omgjort till text). Programstrukturen är inte
        officiellt dokumenterad; okända fält redovisas under 'other' (eller råa med include_raw)."""
        program_code = normalize_code(code, "programkod")
        version = normalize_version(version)
        when = normalize_date(date)
        if version is not None and when is not None:
            raise InvalidInputError("Ange antingen version eller date, inte båda")
        if version is not None:
            path, params = f"/programs/{quote(program_code, safe='')}/versions/{version}", None
        else:
            path, params = f"/programs/{quote(program_code, safe='')}", {"date": when}
        try:
            response = await get(path, params)
        except UpstreamError as exc:
            if exc.status == 404:
                hint = missing_hint(version, when, "skolverket_list_program_versions", program_code)
                if version is None:
                    hint += " Giltiga koder: skolverket_list_programs (Gy25-program har suffixet 25, t.ex. NA25)."
                raise not_found(
                    exc,
                    f"Programmet {program_code!r}" + (f" version {version}" if version is not None else ""),
                    hint,
                ) from exc
            raise
        program = payload_object(response.data, "program")
        if program is None:
            raise UpstreamError(SOURCE, "Oväntat svar: objektet 'program' saknas", url=response.url)

        def build(chars: int | None) -> ProgramDetail:
            detail = build_program_detail(program, chars, include_raw)
            if include_raw and chars != max_chars:
                # fit_output is shrinking (every later build uses a smaller budget): shorten raw texts too.
                detail.raw = trim_strings(program, chars)
            notes = envelope_notes(response.data)
            if str(detail.study_path_type or "").endswith("25"):
                notes.append("Gy25-program: ämnena läses i nivåer och ger ämnesbetyg.")
            if detail.canceled_date:
                notes.append(f"Programmet är upphävt från {detail.canceled_date} (övergångsbestämmelser kan gälla).")
            detail.notes = notes or None
            detail.api_version = api_version(response)
            detail.request_url = response.url
            return detail

        return structured(fit_output(build, max_chars, [drop_raw]))

    @server.tool(
        name="skolverket_list_program_versions", title="Skolverket: programversioner", annotations=READ_ONLY_OPEN
    )
    @tool_errors
    async def skolverket_list_program_versions(
        code: Annotated[str, Field(description="Programkod, t.ex. 'NA25'")],
        limit: LimitArg = DEFAULT_LIMIT,
    ) -> Annotated[CallToolResult, SyllabusListResult]:
        """Lista versioner av ett program (nyast först). Svarsformatet är inte officiellt dokumenterat och tolkas
        defensivt."""
        program_code = normalize_code(code, "programkod")
        result = await list_entities(
            kind="program_versions",
            path=f"/programs/{quote(program_code, safe='')}/versions",
            key="programs",
            params={},
            search=None,
            limit=limit,
            offset=0,
            filters_echo={"code": program_code},
            sort_versions=True,
            pageable=False,
        )
        return structured(result)

    # ---------------------------------------------------------------- curriculums

    @server.tool(name="skolverket_list_curriculums", title="Skolverket: lista läroplaner", annotations=READ_ONLY_OPEN)
    @tool_errors
    async def skolverket_list_curriculums(
        schooltype: SchoolTypeArg = None,
        timespan: Annotated[Timespan, Field(description="LATEST (default), FUTURE, EXPIRED eller MODIFIED")] = "LATEST",
        date: DateArg = None,
        search: SearchArg = None,
        limit: LimitArg = DEFAULT_LIMIT,
        offset: OffsetArg = 0,
    ) -> Annotated[CallToolResult, SyllabusListResult]:
        """Lista läroplaner (curriculums), t.ex. LGR22 (grundskolan). Koderna ska hämtas härifrån – de är inte
        dokumenterade. Fälten tolkas defensivt (API-gruppen saknar officiell specifikation)."""
        params = {"schooltype": normalize_school_type(schooltype), "timespan": timespan, "date": normalize_date(date)}
        result = await list_entities(
            kind="curriculums",
            path="/curriculums",
            key="curriculums",
            params=params,
            search=search,
            limit=limit,
            offset=offset,
        )
        return structured(result)

    @server.tool(name="skolverket_get_curriculum", title="Skolverket: hämta läroplan", annotations=READ_ONLY_OPEN)
    @tool_errors
    async def skolverket_get_curriculum(
        code: Annotated[str, Field(description="Läroplanskod från skolverket_list_curriculums, t.ex. 'LGR22'")],
        version: Annotated[int | None, Field(description="Specifik version")] = None,
        date: DateArg = None,
        max_chars: Annotated[
            int | None,
            Field(
                ge=MIN_MAX_CHARS,
                description=f"Max tecken per textfält (default {DEFAULT_DETAIL_MAX_CHARS}, minst {MIN_MAX_CHARS}). "
                f"Svaret hålls alltid under ca {MAX_OUTPUT_CHARS} tecken.",
            ),
        ] = DEFAULT_DETAIL_MAX_CHARS,
        include_raw: Annotated[
            bool,
            Field(
                description="Ta med API:ts hela läroplansobjekt (kan vara mycket stort). Blir svaret för stort "
                "kortas råobjektets texter och till sist utelämnas det (se notes)."
            ),
        ] = False,
    ) -> Annotated[CallToolResult, CurriculumDetail]:
        """Hämta en läroplan (curriculum) med metadata och innehåll (HTML omgjort till text, långa texter
        förkortade). Strukturen är inte officiellt dokumenterad och återges generiskt under 'content'."""
        curriculum_code = normalize_code(code, "läroplanskod")
        version = normalize_version(version)
        when = normalize_date(date)
        if version is not None and when is not None:
            raise InvalidInputError("Ange antingen version eller date, inte båda")
        if version is not None:
            path, params = f"/curriculums/{quote(curriculum_code, safe='')}/versions/{version}", None
        else:
            path, params = f"/curriculums/{quote(curriculum_code, safe='')}", {"date": when}
        try:
            response = await get(path, params)
        except UpstreamError as exc:
            if exc.status == 404:
                hint = missing_hint(version, when, "skolverket_list_curriculum_versions", curriculum_code)
                if version is None:
                    hint += " Giltiga koder: skolverket_list_curriculums."
                raise not_found(
                    exc,
                    f"Läroplanen {curriculum_code!r}" + (f" version {version}" if version is not None else ""),
                    hint,
                ) from exc
            raise
        curriculum = payload_object(response.data, "curriculum")
        if curriculum is None:
            raise UpstreamError(SOURCE, "Oväntat svar: objektet 'curriculum' saknas", url=response.url)

        def build(chars: int | None) -> CurriculumDetail:
            detail = build_curriculum_detail(curriculum, chars, include_raw)
            if include_raw and chars != max_chars:
                # fit_output is shrinking (every later build uses a smaller budget): shorten raw texts too.
                detail.raw = trim_strings(curriculum, chars)
            detail.notes = envelope_notes(response.data) or None
            detail.api_version = api_version(response)
            detail.request_url = response.url
            return detail

        return structured(fit_output(build, max_chars, [drop_raw]))

    @server.tool(
        name="skolverket_list_curriculum_versions", title="Skolverket: läroplansversioner", annotations=READ_ONLY_OPEN
    )
    @tool_errors
    async def skolverket_list_curriculum_versions(
        code: Annotated[str, Field(description="Läroplanskod, t.ex. 'LGR22'")],
        limit: LimitArg = DEFAULT_LIMIT,
    ) -> Annotated[CallToolResult, SyllabusListResult]:
        """Lista versioner av en läroplan (nyast först). Svarsformatet är inte officiellt dokumenterat och tolkas
        defensivt."""
        curriculum_code = normalize_code(code, "läroplanskod")
        result = await list_entities(
            kind="curriculum_versions",
            path=f"/curriculums/{quote(curriculum_code, safe='')}/versions",
            key="curriculums",
            params={},
            search=None,
            limit=limit,
            offset=0,
            filters_echo={"code": curriculum_code},
            sort_versions=True,
            pageable=False,
        )
        return structured(result)

    # ---------------------------------------------------------------- valuestore & info

    @server.tool(
        name="skolverket_syllabus_valuestore", title="Skolverket: värdeförråd och kodlistor", annotations=READ_ONLY_OPEN
    )
    @tool_errors
    async def skolverket_syllabus_valuestore(
        list: Annotated[
            ValuestoreList,
            Field(
                description="Kodlista: schooltypes (skoltyper), schooltypes/expired (upphörda skoltyper), "
                "typeofsyllabus (ämnestyper), subjectandcoursecodes (alla giltiga ämnes-, kurs- och nivåkoder), "
                "studypathcodes (studievägskoder: program, inriktningar, profiler, särskilda studievägar)"
            ),
        ],
        search: Annotated[
            str | None, Field(description="Fritextfilter (klientsida) på kod och namn, skiftlägesokänsligt")
        ] = None,
        type_of_syllabus: Annotated[
            str | None,
            Field(
                description="Endast subjectandcoursecodes: filtrera (klientsida) på typeOfSyllabus, t.ex. "
                "LEVEL_IN_GRADE_SUBJECT_SYLLABUS (Gy25-nivåer) eller COURSE_IN_SUBJECT_SYLLABUS (GY11-kurser)"
            ),
        ] = None,
        schooltype: Annotated[
            str | None, Field(description="Endast studypathcodes: skoltyp, t.ex. 'GY' eller 'GYAN'")
        ] = (None),
        type_of_study_path: Annotated[
            str | None,
            Field(
                description="Endast studypathcodes: typeOfStudyPath, t.ex. PROGRAM25, ORIENTATIONS, "
                "PARTICULAR_STUDY_PATH25 (se resursen för alla)"
            ),
        ] = None,
        type_of_program: Annotated[
            str | None, Field(description="Endast studypathcodes: typeOfProgram, t.ex. PROGRAM25 (Gy25)")
        ] = None,
        timespan: Annotated[Timespan | None, Field(description="Endast studypathcodes: tidsintervall")] = None,
        date: Annotated[str | None, Field(description="Endast studypathcodes: sökdatum ÅÅÅÅ-MM-DD")] = None,
        limit: LimitArg = DEFAULT_LIMIT,
        offset: OffsetArg = 0,
    ) -> Annotated[CallToolResult, ValuestoreResult]:
        """Hämta Syllabus-API:ts värdeförråd/kodlistor. Bra för att slå upp eller validera koder: vilket ämne
        en kurs-/nivåkod hör till, alla Gy25-nivåer, programkoder och inriktningar. Gy25-studievägar:
        studypathcodes med type_of_study_path='ORIENTATIONS' + type_of_program='PROGRAM25' (program med
        inriktningar), 'PROGRAM25' (program utan inriktning) och 'PARTICULAR_STUDY_PATH25' (lärling, RIG, NIU, IM …).
        Utan filter ger studypathcodes även historiska studievägar."""
        path, key = VALUESTORE_LISTS[list]
        study_type = normalize_study_path_type(type_of_study_path)
        program_type = normalize_type_of_program(type_of_program)
        school = normalize_school_type(schooltype)
        when = normalize_date(date)
        params: dict[str, Any] = {}
        if list == "studypathcodes":
            params = {
                "schooltype": school,
                "typeOfStudyPath": study_type,
                "typeOfProgram": program_type,
                "timespan": timespan,
                "date": when,
            }
        elif any(v is not None for v in (study_type, program_type, school, timespan, when)):
            raise InvalidInputError(
                "Filtren schooltype, type_of_study_path, type_of_program, timespan och date gäller bara "
                "list='studypathcodes'"
            )
        syllabus_type = type_of_syllabus.strip().upper() if type_of_syllabus and type_of_syllabus.strip() else None
        if syllabus_type and list != "subjectandcoursecodes":
            raise InvalidInputError("type_of_syllabus gäller bara list='subjectandcoursecodes'")

        items, response, request_url, empty_note = await get_list(path, params, key)
        tokens = search_tokens(search)

        def keep(item: Any) -> bool:
            if not row_matches(item, tokens):
                return False
            if not isinstance(item, dict):
                return syllabus_type is None and study_type is None
            # Safety net if the server ignores a filter: only applied when the field is present.
            if syllabus_type and str(item.get("typeOfSyllabus") or "").upper() != syllabus_type:
                return False
            if study_type and "type" in item and str(item.get("type") or "").upper() != study_type:
                return False
            return not (school and "schoolTypes" in item and school not in (_school_types(item["schoolTypes"]) or []))

        rows = [r for r in (to_value_row(it) for it in items if keep(it)) if r is not None]
        page, start, _ = paginate(rows, offset, limit)
        notes = envelope_notes(response.data) if response else []
        if empty_note:
            notes.append(empty_note)
        elif items and not rows:
            notes.append("Inga poster matchade klientfiltreringen (search/type_of_syllabus m.m.).")
        echo = {k: v for k, v in params.items() if v is not None}
        if search:
            echo["search"] = search
        if syllabus_type:
            echo["type_of_syllabus"] = syllabus_type
        result = ValuestoreResult(
            code_list=list,
            total=len(rows),
            returned=len(page),
            offset=start,
            truncated=start + len(page) < len(rows),
            upstream_count=len(items),
            filters=echo,
            items=page,
            api_version=api_version(response),
            request_url=request_url,
            notes=notes or None,
        )
        return structured(fit_items(result))

    @server.tool(name="skolverket_syllabus_api_info", title="Skolverket: Syllabus-API-info", annotations=READ_ONLY_OPEN)
    @tool_errors
    async def skolverket_syllabus_api_info() -> dict[str, Any]:
        """Visa information om Skolverkets Syllabus-API (version, releasedatum, status, dokumentation och ev.
        utfasning/länk till nyare version) från /v1/api-info. Bra som hälsokontroll."""
        response = await get("/api-info")
        data = response.data
        info: dict[str, Any] = {}
        if isinstance(data, dict):
            for key, value in data.items():
                if isinstance(value, (str, int, float, bool)) or value is None:
                    info[key] = truncate(value, 500) if isinstance(value, str) else value
                else:
                    info[key] = simplify(value, 500)
        else:
            info["response"] = truncate(str(data), 1000)
        notes = envelope_notes(data)
        # The dict result is rendered with indentation (≈ twice the compact size): stay well under the budget.
        if len(json.dumps(info, ensure_ascii=False, separators=(",", ":"))) > MAX_OUTPUT_CHARS // 3:
            info = {k: v for k, v in info.items() if not isinstance(v, (dict, list))}
            if len(json.dumps(info, ensure_ascii=False, separators=(",", ":"))) > MAX_OUTPUT_CHARS // 3:
                info = {k: v for k, v in info.items() if k in _ENVELOPE_KEYS}
            notes.append("Svaret var för stort; nästlade/okända fält i api-info utelämnades.")
        base = services.settings.base_url(BASE_KEY)
        return compact(
            {
                "api": info,
                "notes": notes or None,
                "base_url": f"{base}{API_PREFIX}",
                "swagger_ui": f"{base}/swagger-ui/index.html",
                "openapi": f"{base}/v3/api-docs",
                "request_url": response.url,
                "citation": CITATION,
            }
        )

    @server.resource(
        CODES_URI,
        name="skolverket-syllabus-codes",
        title="Skolverket Syllabus: kodlistor",
        description=(
            "Kodlistor för Skolverkets Syllabus-API: skoltyper, typeOfSyllabus, tidsintervall, studievägstyper, "
            "programkategorier, ämneskategorier, typer av centralt innehåll/betygskriterier, sfi-aspekter och "
            "betygssteg."
        ),
        mime_type="application/json",
    )
    def syllabus_codes() -> dict[str, Any]:
        return CODE_LISTS
