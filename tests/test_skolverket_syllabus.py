# SPDX-FileCopyrightText: 2026 Deniz Özer
#
# SPDX-License-Identifier: MIT

import json
import re

import httpx2
import pytest

from fuzzy_mcp.core import CUT_MARKER, TRUNCATION_KEY, client_size
from fuzzy_mcp.errors import InvalidInputError
from fuzzy_mcp.sources.skolverket.syllabus import (
    ALL_SUBJECT_SECTIONS,
    CODES_URI,
    FIT_NOTE,
    MAX_OUTPUT_CHARS,
    OMITTED_NOTE,
    OMITTED_NOTE_CHARS,
    html_to_text,
    normalize_code,
    normalize_school_type,
    parent_subject_candidates,
    payload_list,
    payload_object,
    truncate,
)

from .conftest import call, call_error, load_fixture

pytestmark = pytest.mark.anyio

BASE = "https://api.skolverket.se/syllabus/v1"
ENVELOPE = {
    "apiPublisher": "Skolverket",
    "apiName": "Syllabus API",
    "apiVersion": "1.16.2-SNAPSHOT",
    "apiReleased": "2026-05-08",
    "apiStatus": "active",
    "dataOrigin": "Syllabus API - Skolverket - 1.16.2-SNAPSHOT",
    "processingTime": "12 ms",
}
NOT_FOUND = {
    "errorCode": 404,
    "message": "Anrop av API ReadSubjectVersion ger tomt svar, code = MAT, version = 99, searchDate = 2026-10-06",
}


def params(index: int = -1, router=None) -> dict[str, str]:
    return dict(router.requests[index].url.params)


async def call_sized(client, tool: str, args: dict | None = None) -> tuple[dict, int]:
    """Structured result plus the length of the text content the host actually receives."""
    result = await client.call_tool(tool, args or {})
    text = "".join(getattr(c, "text", "") for c in result.content)
    assert not result.is_error, text[:500]
    return result.structured_content, len(text)


# Realistic GY11 sizes (review): central content ~2 500 characters, criteria E/C/A ~1 500 each.
CENTRAL_HTML = (
    "<h4>Undervisningen i kursen ska behandla följande centrala innehåll:</h4><ul>"
    + "<li>Centrala begrepp, metoder och arbetssätt inom kursens område och yrkeslivet.</li>" * 30
    + "</ul>"
)


def omitted_by_tool(notes: list[str]) -> list[str]:
    """Course codes get_subject's own budget left out, in order, from its notes; none of them is shortened."""
    found = [note for note in notes if note.startswith(OMITTED_NOTE)]
    assert all(len(note) <= OMITTED_NOTE_CHARS and not note.endswith(CUT_MARKER) for note in found), found
    return [code for note in found for code in note.removeprefix(OMITTED_NOTE).strip().split(", ")]


def criterion_html(step: str) -> str:
    sentence = f"Eleven redogör {'utförligt och nyanserat' if step == 'A' else 'översiktligt'} för begreppen. "
    return f"<h4>Betyget {step}</h4><p>" + sentence * (1 if step in "DB" else 1_500 // len(sentence)) + "</p>"


def big_subject(courses: int, code: str = "VAR") -> dict:
    return {
        "code": code,
        "name": "Vård och omsorg",
        "typeOfSyllabus": "SUBJECT_SYLLABUS",
        "schoolTypes": ["GY", "VUXGY"],
        "description": "<p>" + "Ämnet handlar om vård och omsorg. " * 20 + "</p>",
        "purpose": "<p>" + "Undervisningen ska syfta till att eleverna utvecklar kunskaper. " * 40 + "</p>",
        "courses": [
            {
                "code": f"{code}{code}{i:02d}",
                "name": f"Kurs {i}",
                "englishName": f"Course {i}",
                "points": "100",
                "typeOfSyllabus": "COURSE_IN_SUBJECT_SYLLABUS",
                "description": "<p>" + "Kursen omfattar punkterna 1–6 under rubriken Ämnets syfte. " * 3 + "</p>",
                "centralContent": {"text": CENTRAL_HTML},
                "knowledgeReqsHeading": "<h3>Betygskriterier</h3>",
                "knowledgeRequirements": [{"text": criterion_html(s), "gradeStep": s} for s in "EDCBA"],
            }
            for i in range(1, courses + 1)
        ],
    }


# ---------------------------------------------------------------- helpers


def test_html_to_text_lists_entities_and_soft_hyphens():
    html = (
        "<h3>Ämnets syfte</h3><p>Under­visningen &amp; mer</p> <ol> <li>Förmåga A.</li> <li>Förmåga B.</li> "
        "</ol><p><strong>Område</strong></p><ul><li>Punkt&nbsp;ett</li></ul><p></p>"
    )
    assert html_to_text(html) == "Ämnets syfte\nUndervisningen & mer\n1. Förmåga A.\n2. Förmåga B.\nOmråde\n- Punkt ett"
    assert html_to_text("<p></p>") == ""
    assert html_to_text("Ren text &amp; mer") == "Ren text & mer"
    short = truncate("ett två tre fyra fem sex", 12)
    assert short.startswith("ett två tre") and short.endswith("tecken totalt]")


def test_helpers_codes_and_payloads():
    assert normalize_school_type(" gy ") == "GY" and normalize_school_type("vuxsfi") == "VUXSFI"
    with pytest.raises(InvalidInputError, match="Okänd skoltyp"):
        normalize_school_type("GYSÄR")
    assert parent_subject_candidates("MATE1A00X") == ["MATE", "MAT"]
    assert parent_subject_candidates("MATMAT01c") == ["MAT", "MATM"]
    assert parent_subject_candidates("HUMT100PX") == ["HUMT", "HUM"]
    # Documented key first, otherwise the first array/object (community clients guessed wrong keys).
    assert payload_list({"apiVersion": "1", "codes": [1]}, "studyPaths") == [1]
    assert payload_object({"totalElements": 1, "program": {"code": "NA25"}}, "program") == {"code": "NA25"}
    assert payload_object({"apiVersion": "1", "code": "X", "name": "Y"}, "curriculum") == {"code": "X", "name": "Y"}


# ---------------------------------------------------------------- subjects


async def test_list_subjects_without_params(router, make_client):
    router.add("GET", r"/v1/subjects\?", load_fixture("syllabus_subjects.json"))
    async with make_client("skolverket") as client:
        result = await call(client, "skolverket_list_subjects")
    request = router.last()
    assert str(request.url).startswith(f"{BASE}/subjects?")
    assert dict(request.url.params) == {"timespan": "LATEST"}
    assert request.headers["accept"] == "application/json"
    assert result["total"] == 8 and result["returned"] == 8 and result["truncated"] is False
    assert result["api_version"] == "1.11.0" and result["citation"].startswith("Källa: Skolverket")
    rows = {r["code"]: r for r in result["items"]}
    assert rows["ADM"] == {
        "code": "ADM",
        "name": "Administration",
        "version": 3,
        "type_of_syllabus": "SUBJECT_SYLLABUS",
        "school_types": ["GY", "VUXGY"],
        "categories": ["VOCATIONAL"],
        "start_date": "2022-07-01",
        "modified_date": "2022-06-14",
    }
    assert rows["GRNENG2"]["points"] == 600 and rows["GRNENG2"]["type_of_syllabus"] == "GRVUX_COURSE"
    assert "course_codes" not in rows["MAT"]  # no school type → no embedded courses


async def test_list_subjects_client_side_search_and_paging(router, make_client):
    router.add("GET", r"/v1/subjects\?", load_fixture("syllabus_subjects.json"))
    async with make_client("skolverket") as client:
        found = await call(client, "skolverket_list_subjects", {"search": "MATEMATIK"})
        assert [r["code"] for r in found["items"]] == ["GRGRMAT01", "MAT"]
        assert found["upstream_count"] == 8 and found["filters"]["search"] == "MATEMATIK"
        english = await call(client, "skolverket_list_subjects", {"search": "eng"})
        assert [r["code"] for r in english["items"]] == ["ENG", "GRNENG2"]
        page = await call(client, "skolverket_list_subjects", {"limit": 3, "offset": 2})
        assert page["returned"] == 3 and page["offset"] == 2 and page["truncated"] is True
        assert [r["code"] for r in page["items"]] == ["ENG", "GRGRMAT01", "GRNENG2"]
        none = await call(client, "skolverket_list_subjects", {"search": "kemi"})
        assert none["total"] == 0 and "klientfiltreringen" in none["notes"][0]


async def test_list_subjects_gy_with_courses_and_levels(router, make_client):
    # The Gy25 filter gets a response with only GRADE_SUBJECT_SYLLABUS subjects (typeOfSyllabusParam echoed).
    router.add(
        "GET", r"/v1/subjects\?.*typeOfSyllabus=GRADE_SUBJECT_SYLLABUS", load_fixture("syllabus_subjects_gy25.json")
    )
    router.add("GET", r"/v1/subjects\?", load_fixture("syllabus_subjects_gy.json"))
    async with make_client("skolverket") as client:
        result = await call(
            client,
            "skolverket_list_subjects",
            {"schooltype": "gy", "type_of_syllabus": "GRADE_SUBJECT_SYLLABUS", "date": "2026-10-06"},
        )
        assert params(router=router) == {
            "schooltype": "GY",
            "timespan": "LATEST",
            "typeOfSyllabus": "GRADE_SUBJECT_SYLLABUS",
            "date": "2026-10-06",
        }
        assert (
            result["filters"]["schooltype"] == "GY" and result["filters"]["typeOfSyllabus"] == "GRADE_SUBJECT_SYLLABUS"
        )
        rows = {r["code"]: r for r in result["items"]}
        assert set(rows) == {"MATE", "MATT", "SVEN"}
        assert {r["type_of_syllabus"] for r in result["items"]} == {"GRADE_SUBJECT_SYLLABUS"}
        assert rows["MATE"]["course_codes"][:2] == ["MATE1A00X", "MATE1B00X"]
        assert rows["MATT"]["course_codes"] == ["MATT100PX"]
        # Without the filter (ALL) GY11 subjects such as MAT come back too.
        everything = await call(client, "skolverket_list_subjects", {"schooltype": "GY"})
        assert "typeOfSyllabus" not in params(router=router)
        all_rows = {r["code"]: r for r in everything["items"]}
        assert all_rows["MAT"]["course_codes"][0] == "MATMAT01a"
        assert all_rows["MAT"]["type_of_syllabus"] == "SUBJECT_SYLLABUS"
        assert all_rows["MAT"]["canceled_date"] == "2025-07-01" and all_rows["MAT"]["end_date"] == "2030-06-30"
        # Search also matches embedded level codes (only present because schooltype is given).
        by_level = await call(client, "skolverket_list_subjects", {"schooltype": "GY", "search": "mate2c00x"})
        assert [r["code"] for r in by_level["items"]] == ["MATE"]
        canceled = await call(client, "skolverket_list_subjects", {"schooltype": "GY", "timespan": "CANCELED"})
    assert canceled["filters"]["timespan"] == "CANCELED" and params(router=router)["timespan"] == "CANCELED"


async def test_list_subjects_validation_and_empty_result(router, make_client):
    router.add("GET", r"/v1/subjects\?", httpx2.Response(404, json={"errorCode": 404, "message": "Tomt svar."}))
    async with make_client("skolverket") as client:
        err = await call_error(client, "skolverket_list_subjects", {"schooltype": "GYSÄR"})
        assert "Okänd skoltyp" in err and "GYAN" in err
        err = await call_error(client, "skolverket_list_subjects", {"date": "2026-13-01"})
        assert "ÅÅÅÅ-MM-DD" in err
        await call_error(client, "skolverket_list_subjects", {"timespan": "NEWEST"})
        assert router.requests == []
        empty = await call(client, "skolverket_list_subjects", {"schooltype": "SP", "timespan": "FUTURE"})
    assert empty["total"] == 0 and empty["items"] == [] and "404" in empty["notes"][0]
    # The upstream message is passed on (not discarded).
    assert "API:ts meddelande: Tomt svar." in empty["notes"][0]


async def test_list_tools_stop_adding_rows_at_the_output_budget(router, make_client):
    """Regression: list_subjects(schooltype='GY', limit=500) gave ~184k characters (~50k tokens)."""
    subjects = [
        {
            "code": f"S{i:03d}",
            "name": f"Ämne {i} med ett ganska långt namn",
            "version": 1,
            "startDate": "2025-07-01",
            "typeOfSyllabus": "GRADE_SUBJECT_SYLLABUS",
            "schoolTypes": ["GY", "VUXGY"],
            "categories": [{"name": "Yrkesämne", "code": "VOCATIONAL"}],
            "courses": [{"code": f"S{i:03d}{n}00X", "name": f"Nivå {n}", "points": "100"} for n in range(1, 9)],
        }
        for i in range(600)
    ]
    router.add("GET", r"/v1/subjects\?", {**ENVELOPE, "subjects": subjects})
    async with make_client("skolverket") as client:
        first, size = await call_sized(client, "skolverket_list_subjects", {"schooltype": "GY", "limit": 500})
        assert size <= MAX_OUTPUT_CHARS
        kept = first["returned"]
        assert 0 < kept < 500 and len(first["items"]) == kept and first["truncated"] is True
        assert first["total"] == 600 and first["items"][-1]["code"] == f"S{kept - 1:03d}"
        assert any(f"offset={kept}" in n for n in first["notes"])
        second, size = await call_sized(
            client, "skolverket_list_subjects", {"schooltype": "GY", "limit": 500, "offset": kept}
        )
        assert size <= MAX_OUTPUT_CHARS and second["items"][0]["code"] == f"S{kept:03d}"
        small = await call(client, "skolverket_list_subjects", {"schooltype": "GY", "limit": 10})
        assert small["returned"] == 10 and "notes" not in small


async def test_valuestore_stops_adding_rows_at_the_output_budget(router, make_client):
    codes = [
        {
            "code": f"KOD{i:04d}",
            "name": f"Kurs {i}",
            "description": "<p>" + "Beskrivning av kursen. " * 30 + "</p>",
            "typeOfSyllabus": "COURSE_IN_SUBJECT_SYLLABUS",
        }
        for i in range(800)
    ]
    router.add("GET", r"/v1/valuestore/subjectandcoursecodes$", {**ENVELOPE, "codes": codes})
    async with make_client("skolverket") as client:
        result, size = await call_sized(
            client, "skolverket_syllabus_valuestore", {"list": "subjectandcoursecodes", "limit": 500}
        )
        assert size <= MAX_OUTPUT_CHARS
        kept = result["returned"]
        assert 0 < kept < 500 and result["truncated"] is True and result["offset"] == 0
        assert any(f"offset={kept}" in n for n in result["notes"])
        nxt = await call(
            client, "skolverket_syllabus_valuestore", {"list": "subjectandcoursecodes", "limit": 5, "offset": kept}
        )
    assert nxt["items"][0]["code"] == f"KOD{kept:04d}" and nxt["offset"] == kept


async def test_get_subject_gy11_courses_and_criteria(router, make_client):
    router.add("GET", r"/v1/subjects/MAT$", load_fixture("syllabus_subject_mat.json"))
    async with make_client("skolverket") as client:
        subject = await call(client, "skolverket_get_subject", {"code": "MAT"})
    assert str(router.last().url) == f"{BASE}/subjects/MAT"
    assert router.last().headers["accept"] == "application/json"
    assert (
        subject["reform"] == "GY11" and subject["version"] == 11 and subject["type_of_syllabus"] == "SUBJECT_SYLLABUS"
    )
    assert subject["canceled_date"] == "2025-07-01" and subject["canceled_skolfs"] == "2023:130"
    assert any("Upphävd från 2025-07-01 (SKOLFS 2023:130)" in n for n in subject["notes"])
    assert any(n.startswith("GY11") for n in subject["notes"])
    assert "<" not in subject["purpose"] and "\n1. Förmåga att använda och beskriva" in subject["purpose"]
    assert subject["appendix2"] == [{"code": "SQI", "name": "Albanska – nybörjare"}]
    assert subject["course_count"] == 2 and "grading_criteria" not in subject
    first, second = subject["courses"]
    assert first["code"] == "MATMAT01a" and first["english_name"] == "Mathematics 1a" and first["points"] == 100
    assert "display_name" not in first
    assert first["description"].startswith("Kursen matematik 1a omfattar punkterna 1–6")
    assert first["central_content"][0]["text"].startswith("Undervisningen i kursen ska behandla")
    assert "\n- " in first["central_content"][0]["text"]
    assert [c["grade_step"] for c in first["grading_criteria"]] == ["E", "D", "C", "B", "A"]
    criterion = first["grading_criteria"][0]
    assert criterion["label"] == "Betyget E" and criterion["text"].startswith("Eleven ")
    assert second["code"] == "MATMAT01c"


async def test_get_subject_gy25_levels_and_subject_criteria(router, make_client):
    router.add("GET", r"/v1/subjects/MATE$", load_fixture("syllabus_subject_mate.json"))
    async with make_client("skolverket") as client:
        subject = await call(client, "skolverket_get_subject", {"code": "MATE"})
    assert subject["reform"] == "GY25" and subject["type_of_syllabus"] == "GRADE_SUBJECT_SYLLABUS"
    assert any(n.startswith("Gy25") for n in subject["notes"])
    levels = subject["courses"]
    assert [lv["code"] for lv in levels] == ["MATE1A00X", "MATE1B00X", "MATE2C00X"]
    level = levels[0]
    assert level["display_name"] == "Matematik – Nivå 1a" and level["points"] == 100
    assert level["type_of_syllabus"] == "LEVEL_IN_GRADE_SUBJECT_SYLLABUS" and "description" not in level
    assert level["central_content"][0]["text"].startswith("Undervisningen i ämnet matematik på nivå 1a")
    assert all("grading_criteria" not in lv for lv in levels)  # criteria belong to the subject
    assert [c["grade_step"] for c in subject["grading_criteria"]] == ["E", "D", "C", "B", "A"]
    assert subject["grading_criteria"][1]["text"] == "Elevens kunskaper bedöms sammantaget vara mellan C och E."
    assert subject["grading_note"].startswith("Av 15 kap. 24 §")


async def test_get_subject_sections_course_filter_and_max_chars(router, make_client):
    router.add("GET", r"/v1/subjects/MAT$", load_fixture("syllabus_subject_mat.json"))
    async with make_client("skolverket") as client:
        purpose_only = await call(client, "skolverket_get_subject", {"code": "MAT", "sections": ["purpose"]})
        assert purpose_only["sections"] == ["purpose"] and purpose_only["course_count"] == 2
        assert "courses" not in purpose_only and "description" not in purpose_only
        one = await call(
            client,
            "skolverket_get_subject",
            {"code": "MAT", "course_codes": ["matmat01c", "MATMAT09"], "sections": ["central_content"]},
        )
        assert [c["code"] for c in one["courses"]] == ["MATMAT01c"]
        course = one["courses"][0]
        assert "central_content" in course and "grading_criteria" not in course and "description" not in course
        assert any("matmat09" in n and "MATMAT01a" in n for n in one["notes"])
        short = await call(client, "skolverket_get_subject", {"code": "MAT", "max_chars": 200})
    assert len(short["purpose"]) < 260 and short["purpose"].endswith("tecken totalt]")


async def test_get_subject_gr_central_contents_per_stage(router, make_client):
    router.add("GET", r"/v1/subjects/GRGRMAT01\?date=2026-08-01$", load_fixture("syllabus_subject_grgrmat01.json"))
    async with make_client("skolverket") as client:
        subject = await call(client, "skolverket_get_subject", {"code": "GRGRMAT01", "date": "2026-08-01"})
    assert [c["year"] for c in subject["central_contents"]] == ["1-3", "7-9"]
    assert subject["central_contents"][0]["text"].startswith("Taluppfattning och tals användning\n- Naturliga tal")
    criteria = subject["grading_criteria"]
    assert [(c["year"], c["grade_step"]) for c in criteria] == [("6", "E"), ("6", "D"), ("9", "E"), ("9", "D")]
    assert criteria[0]["label"] == "Betygskriterier för betyget E i slutet av årskurs 6"
    assert criteria[0]["text"].startswith("Eleven visar")
    assert "courses" not in subject and "notes" not in subject


async def test_get_subject_version_encoding_and_errors(router, make_client):
    router.add("GET", r"/v1/subjects/MAT/versions/11$", load_fixture("syllabus_subject_mat.json"))
    router.add("GET", r"/v1/subjects/MAT/versions/99$", httpx2.Response(404, json=NOT_FOUND))
    router.add("GET", r"/v1/subjects/BOOM$", httpx2.Response(500, json={"errorCode": 500, "message": "Internt fel"}))
    async with make_client("skolverket") as client:
        subject = await call(client, "skolverket_get_subject", {"code": "MAT", "version": 11})
        assert subject["version"] == 11 and str(router.last().url) == f"{BASE}/subjects/MAT/versions/11"
        err = await call_error(client, "skolverket_get_subject", {"code": "MAT", "version": 99})
        assert "Ämnet 'MAT' version 99 hittades inte" in err and "ReadSubjectVersion ger tomt svar" in err
        assert "HTTP 404" in err and "skolverket_list_subject_versions(code='MAT')" in err
        err = await call_error(client, "skolverket_get_subject", {"code": "BOOM"})
        assert "HTTP 500" in err and "Internt fel" in err
        err = await call_error(client, "skolverket_get_subject", {"code": "MAT", "version": 11, "date": "2026-01-01"})
        assert "antingen version eller date" in err
        err = await call_error(client, "skolverket_get_subject", {"code": "MAT/../x"})
        assert "Ogiltig ämneskod" in err


def test_normalize_code_rejects_dot_segments_and_lookalikes():
    assert normalize_code(" MATE1A00X ") == "MATE1A00X" and normalize_code("ADM2000GY") == "ADM2000GY"
    assert normalize_code("SFI_KURS-A.1") == "SFI_KURS-A.1"
    # Real subject codes contain Swedish letters (VÅR, MÄT, ELÄ, JÄN).
    assert [normalize_code(c) for c in ("VÅR", "MÄT", "ELÄ", "JÄN")] == ["VÅR", "MÄT", "ELÄ", "JÄN"]
    # "." / ".." would be collapsed by the URL layer (GET /syllabus/v1/versions/3, /syllabus/v1/programs).
    for bad in (".", "..", "...", ".MAT", "-MAT", "MAТ", "MA T", "MAT/x", "M" * 41):  # "MAТ" has a Cyrillic Т
        with pytest.raises(InvalidInputError, match="Ogiltig"):
            normalize_code(bad)


async def test_dot_only_codes_never_reach_the_api(router, make_client):
    async with make_client("skolverket") as client:
        for tool, args in [
            ("skolverket_get_subject", {"code": "..", "version": 3}),
            ("skolverket_get_subject", {"code": "."}),
            ("skolverket_list_subject_versions", {"code": ".."}),
            ("skolverket_get_course", {"code": ".."}),
            ("skolverket_get_course", {"code": "MATE1A00X", "subject_code": ".."}),
            ("skolverket_list_course_versions", {"code": "."}),
            ("skolverket_get_program", {"code": "."}),
            ("skolverket_list_program_versions", {"code": ".."}),
            ("skolverket_get_curriculum", {"code": ".."}),
            ("skolverket_list_curriculum_versions", {"code": "."}),
            ("skolverket_get_subject", {"code": "MAТ"}),  # Cyrillic Т
        ]:
            err = await call_error(client, tool, args)
            assert "Ogiltig" in err, (tool, args, err)
    assert router.requests == []


async def test_not_found_hint_mentions_date_and_versions(router, make_client):
    message = "Anrop av API ReadSubject ger tomt svar, code = MATE, searchDate = 2024-01-01"
    router.add("GET", r"/v1/subjects/MATE\?date=2024-01-01$", httpx2.Response(404, json={"message": message}))
    router.add("GET", r"/v1/programs/NA25\?date=2024-01-01$", httpx2.Response(404, json={"message": "Tomt svar."}))
    router.add("GET", r"/v1/curriculums/LGR22$", httpx2.Response(404, json={"message": "Tomt svar."}))
    router.add("GET", r"/v1/courses/MATE1A00X\?date=2024-01-01$", httpx2.Response(404, json={"message": "Tomt svar."}))
    router.add("GET", r"/v1/subjects/MATE?\?date=2024-01-01$", httpx2.Response(404, json={"message": "Tomt svar."}))
    async with make_client("skolverket") as client:
        err = await call_error(client, "skolverket_get_subject", {"code": "MATE", "date": "2024-01-01"})
        # Gy25 subjects start 2025-07-01: a date issue, not (only) a wrong code.
        assert "Ämnet 'MATE' hittades inte" in err and "startDate <= sökdatumet 2024-01-01" in err
        assert "skolverket_list_subject_versions(code='MATE')" in err and message in err
        err = await call_error(client, "skolverket_get_program", {"code": "NA25", "date": "2024-01-01"})
        assert "startDate <= sökdatumet 2024-01-01" in err and "skolverket_list_program_versions" in err
        err = await call_error(client, "skolverket_get_curriculum", {"code": "LGR22"})
        assert "dagens datum" in err and "skolverket_list_curriculum_versions" in err
        err = await call_error(client, "skolverket_get_course", {"code": "MATE1A00X", "date": "2024-01-01"})
        assert "sökdatumet 2024-01-01" in err and "startDate <= date" in err


async def test_get_subject_shrinks_oversized_output(router, make_client):
    long_html = "<p>" + "Lång text om centralt innehåll. " * 120 + "</p>"
    subject = {
        "code": "VAR",
        "name": "Vård och omsorg",
        "typeOfSyllabus": "SUBJECT_SYLLABUS",
        "schoolTypes": ["GY"],
        "courses": [
            {
                "code": f"VARVAR0{i}",
                "name": f"Kurs {i}",
                "points": "100",
                "centralContent": {"text": long_html},
                "knowledgeRequirements": [{"text": f"<h4>Betyget {s}</h4>{long_html}", "gradeStep": s} for s in "EC"],
            }
            for i in range(12)
        ],
    }
    router.add("GET", r"/v1/subjects/VAR$", {**ENVELOPE, "subject": subject})
    async with make_client("skolverket") as client:
        result = await call(client, "skolverket_get_subject", {"code": "VAR"})
    assert len(json.dumps(result, ensure_ascii=False)) < 60_000
    assert any("kortats" in n for n in result["notes"])
    assert result["courses"][0]["central_content"][0]["text"].endswith("tecken totalt]")


@pytest.mark.parametrize("courses", [8, 12, 20, 30, 60, 150])
async def test_get_subject_output_budget_for_any_arguments(router, make_client, courses):
    """Regression: the safety net used to shrink once (to 1 200 chars) – 20 realistic GY11 courses gave ~119k
    characters, max_chars <= 1 200 disabled it and max_chars <= 0 disabled truncation altogether."""
    router.add("GET", r"/v1/subjects/VAR$", {**ENVELOPE, "subject": big_subject(courses)})
    async with make_client("skolverket") as client:
        for max_chars in (None, 50, 150, 1_200, 5_000, 100_000):
            args = {"code": "VAR"} if max_chars is None else {"code": "VAR", "max_chars": max_chars}
            result, size = await call_sized(client, "skolverket_get_subject", args)
            assert size <= MAX_OUTPUT_CHARS, (courses, max_chars, size)
            assert result["course_count"] == courses
            if courses >= 12 and max_chars in (None, 5_000, 100_000):
                assert any("kortats" in n for n in result["notes"])
        # One course always fits in full.
        single, size = await call_sized(client, "skolverket_get_subject", {"code": "VAR", "course_codes": ["VARVAR01"]})
        assert size <= MAX_OUTPUT_CHARS and "kortats" not in json.dumps(single.get("notes"), ensure_ascii=False)
        assert single["courses"][0]["central_content"][0]["text"].count("Centrala begrepp") == 30
        err = await call_error(client, "skolverket_get_subject", {"code": "VAR", "max_chars": 0})
        assert "max_chars" in err
        err = await call_error(client, "skolverket_get_subject", {"code": "VAR", "max_chars": -5})
        assert "max_chars" in err


async def test_get_subject_drops_trailing_courses_and_names_them(router, make_client):
    router.add("GET", r"/v1/subjects/VAR$", {**ENVELOPE, "subject": big_subject(150)})
    async with make_client("skolverket") as client:
        result, size = await call_sized(client, "skolverket_get_subject", {"code": "VAR", "max_chars": 50})
    assert size <= MAX_OUTPUT_CHARS
    kept = [c["code"] for c in result["courses"]]
    assert 0 < len(kept) < 150 and kept[0] == "VARVAR01" and result["course_count"] == 150
    note = next(n for n in result["notes"] if "utelämnades" in n)
    omitted = [f"VARVAR{i:02d}" for i in range(len(kept) + 1, 151)]
    assert f"{len(omitted)} av 150 kurser/nivåer" in note and "course_codes" in note
    assert omitted_by_tool(result["notes"]) == omitted


async def test_get_subject_caps_huge_subject_level_lists(router, make_client):
    criteria = [
        {"text": f"<h4>Betyget {s}</h4><p>{'Eleven visar kunskaper. ' * 20}</p>", "gradeStep": s, "year": str(y)}
        for y in range(1, 400)
        for s in "ECA"
    ]
    subject = {
        "code": "GRGRXXX01",
        "name": "Ämne",
        "typeOfSyllabus": "COURSE_SYLLABUS",
        "knowledgeRequirements": criteria,
    }
    router.add("GET", r"/v1/subjects/GRGRXXX01$", {**ENVELOPE, "subject": subject})
    async with make_client("skolverket") as client:
        result, size = await call_sized(client, "skolverket_get_subject", {"code": "GRGRXXX01"})
    assert size <= MAX_OUTPUT_CHARS
    assert 0 < len(result["grading_criteria"]) < len(criteria)
    assert any("kapats" in n for n in result["notes"])


async def test_list_subject_versions(router, make_client):
    router.add("GET", r"/v1/subjects/MAT/versions$", load_fixture("syllabus_subject_versions_mat.json"))
    async with make_client("skolverket") as client:
        result = await call(client, "skolverket_list_subject_versions", {"code": "MAT"})
    assert str(router.last().url) == f"{BASE}/subjects/MAT/versions"
    assert [r["version"] for r in result["items"]] == [11, 10]
    assert result["items"][0]["skolfs_andring"] == "2022:12" and result["items"][1]["categories"] == ["COMMON"]
    assert result["filters"] == {"code": "MAT"} and result["kind"] == "subject_versions"


# ---------------------------------------------------------------- courses


async def test_list_courses_and_versions(router, make_client):
    courses = {
        **ENVELOPE,
        "calledMethod": "ListCourses",
        "courses": [
            {"code": "MATMAT01a", "name": "Matematik 1a", "points": "100", "version": 11, "schoolTypes": ["GY"]},
            {"code": "MATMAT01c", "name": "Matematik 1c", "points": "100", "version": 11, "schoolTypes": ["GY"]},
            {"code": "ENGENG05", "name": "Engelska 5", "points": "100", "version": 6, "schoolTypes": ["GY"]},
        ],
    }
    versions = {**ENVELOPE, "courses": [{"code": "MATMAT01c", "version": 5}, {"code": "MATMAT01c", "version": 11}]}
    router.add("GET", r"/v1/courses\?", courses)
    router.add("GET", r"/v1/courses/MATMAT01c/versions$", versions)
    async with make_client("skolverket") as client:
        result = await call(client, "skolverket_list_courses", {"schooltype": "GY", "search": "matematik 1"})
        assert params(router=router) == {"schooltype": "GY", "timespan": "LATEST"}
        assert [r["code"] for r in result["items"]] == ["MATMAT01a", "MATMAT01c"]
        assert result["items"][0]["points"] == 100
        await call_error(client, "skolverket_list_courses", {"timespan": "CANCELED"})
        history = await call(client, "skolverket_list_course_versions", {"code": "MATMAT01c"})
    assert [r["version"] for r in history["items"]] == [11, 5]


async def test_get_course_standalone_with_subject_parent(router, make_client):
    router.add("GET", r"/v1/courses/MATMAT01c$", load_fixture("syllabus_course_matmat01c.json"))
    async with make_client("skolverket") as client:
        result = await call(client, "skolverket_get_course", {"code": "MATMAT01c"})
        assert len(router.requests) == 1 and str(router.last().url) == f"{BASE}/courses/MATMAT01c"
        course = result["course"]
        assert result["resolved_via"] == "courses" and course["name"] == "Matematik 1c" and course["points"] == 100
        assert course["central_content"][0]["text"].startswith("Undervisningen i kursen ska behandla")
        assert [c["grade_step"] for c in course["grading_criteria"]] == ["E", "D", "C", "B", "A"]
        assert result["subject"]["code"] == "MAT" and result["subject"]["version"] == 11
        assert "purpose" not in result["subject"]  # not in the default sections
        with_purpose = await call(
            client, "skolverket_get_course", {"code": "MATMAT01c", "sections": ["purpose", "central_content"]}
        )
    assert with_purpose["subject"]["purpose"].startswith("Undervisningen i ämnet matematik ska syfta")
    assert "grading_criteria" not in with_purpose["course"] and "description" not in with_purpose["course"]


async def test_get_course_gy25_level_falls_back_to_subject(router, make_client):
    router.add("GET", r"/v1/courses/MATE1B00X$", httpx2.Response(404, json={"errorCode": 404, "message": "Tomt svar."}))
    router.add("GET", r"/v1/subjects/MATE$", load_fixture("syllabus_subject_mate.json"))
    async with make_client("skolverket") as client:
        result = await call(client, "skolverket_get_course", {"code": "MATE1B00X"})
    assert [str(r.url) for r in router.requests] == [f"{BASE}/courses/MATE1B00X", f"{BASE}/subjects/MATE"]
    assert result["resolved_via"] == "subject"
    assert result["course"]["display_name"] == "Matematik – Nivå 1b" and "grading_criteria" not in result["course"]
    assert result["subject"]["code"] == "MATE" and result["subject"]["reform"] == "GY25"
    assert [c["grade_step"] for c in result["subject_grading_criteria"]] == ["E", "D", "C", "B", "A"]
    assert result["subject_grading_note"].startswith("Av 15 kap.")
    assert any("Hämtad via ämnet MATE" in n for n in result["notes"])


async def test_get_course_explicit_subject_and_not_found(router, make_client):
    router.add("GET", r"/v1/subjects/MAT\?date=2026-08-01$", load_fixture("syllabus_subject_mat.json"))
    async with make_client("skolverket") as client:
        result = await call(
            client, "skolverket_get_course", {"code": "MATMAT01a", "subject_code": "MAT", "date": "2026-08-01"}
        )
        assert [str(r.url) for r in router.requests] == [f"{BASE}/subjects/MAT?date=2026-08-01"]
        assert result["course"]["code"] == "MATMAT01a" and result["resolved_via"] == "subject"
        assert "subject_grading_criteria" not in result  # GY11: criteria sit on the course
        router.requests.clear()
        err = await call_error(client, "skolverket_get_course", {"code": "XYZABC01"})
    assert "Kursen/nivån 'XYZABC01' hittades inte" in err and "XYZ, XYZA" in err
    assert [r.url.path for r in router.requests] == [
        "/syllabus/v1/courses/XYZABC01",
        "/syllabus/v1/subjects/XYZ",
        "/syllabus/v1/subjects/XYZA",
    ]


async def test_get_course_gy25_level_from_courses_endpoint_adds_subject_criteria(router, make_client):
    """Regression: when /v1/courses answered 200 for a Gy25 level, subject_grading_criteria (promised by the
    docstring) was missing – it was only filled in the fallback path."""
    router.add("GET", r"/v1/courses/MATE1A00X\?date=2026-08-01$", load_fixture("syllabus_course_mate1a00x.json"))
    router.add("GET", r"/v1/courses/MATE1A00X/versions/1$", load_fixture("syllabus_course_mate1a00x.json"))
    router.add("GET", r"/v1/subjects/MATE\?date=2026-08-01$", load_fixture("syllabus_subject_mate.json"))
    router.add("GET", r"/v1/subjects/MATE/versions/1$", load_fixture("syllabus_subject_mate.json"))
    async with make_client("skolverket") as client:
        result = await call(client, "skolverket_get_course", {"code": "MATE1A00X", "date": "2026-08-01"})
        assert [str(r.url) for r in router.requests] == [
            f"{BASE}/courses/MATE1A00X?date=2026-08-01",
            f"{BASE}/subjects/MATE?date=2026-08-01",
        ]
        assert (
            result["resolved_via"] == "courses" and result["request_url"] == f"{BASE}/courses/MATE1A00X?date=2026-08-01"
        )
        assert result["course"]["display_name"] == "Matematik – Nivå 1a" and "grading_criteria" not in result["course"]
        assert [c["grade_step"] for c in result["subject_grading_criteria"]] == ["E", "D", "C", "B", "A"]
        assert result["subject_grading_note"].startswith("Av 15 kap.")
        assert result["subject"]["code"] == "MATE" and result["subject"]["reform"] == "GY25"
        assert any(n.startswith("Gy25") for n in result["notes"])
        assert any(f"hämtades från {BASE}/subjects/MATE?date=2026-08-01" in n for n in result["notes"])
        # A level requested by version reads the subject version named in subjectParent.
        router.requests.clear()
        by_version = await call(client, "skolverket_get_course", {"code": "MATE1A00X", "version": 1})
        assert [r.url.path for r in router.requests] == [
            "/syllabus/v1/courses/MATE1A00X/versions/1",
            "/syllabus/v1/subjects/MATE/versions/1",
        ]
        assert len(by_version["subject_grading_criteria"]) == 5
        # Without grading_criteria no extra request is made.
        router.requests.clear()
        content = await call(
            client,
            "skolverket_get_course",
            {"code": "MATE1A00X", "date": "2026-08-01", "sections": ["central_content"]},
        )
        assert len(router.requests) == 1 and "subject_grading_criteria" not in content


async def test_get_course_gy25_level_subject_failure_becomes_a_note(router, make_client):
    router.add("GET", r"/v1/courses/MATE1A00X$", load_fixture("syllabus_course_mate1a00x.json"))
    router.add("GET", r"/v1/subjects/MATE$", httpx2.Response(500, json={"errorCode": 500, "message": "Internt fel"}))
    async with make_client("skolverket") as client:
        result = await call(client, "skolverket_get_course", {"code": "MATE1A00X"})
    assert result["course"]["code"] == "MATE1A00X" and "subject_grading_criteria" not in result
    assert result["subject"]["code"] == "MATE"  # from subjectParent
    note = next(n for n in result["notes"] if "kunde inte hämtas" in n)
    assert "HTTP 500" in note and "skolverket_get_subject" in note


async def test_get_course_output_budget(router, make_client):
    course = big_subject(1)["courses"][0]
    course["centralContent"] = {"text": CENTRAL_HTML * 40}
    course["knowledgeRequirements"] = [{"text": criterion_html(s) * 20, "gradeStep": s} for s in "EDCBA"]
    router.add("GET", r"/v1/courses/VARVAR01$", {**ENVELOPE, "course": course})
    async with make_client("skolverket") as client:
        for max_chars in (None, 50, 100_000):
            args = {"code": "VARVAR01"} if max_chars is None else {"code": "VARVAR01", "max_chars": max_chars}
            result, size = await call_sized(client, "skolverket_get_course", args)
            assert size <= MAX_OUTPUT_CHARS and result["course"]["code"] == "VARVAR01"
        err = await call_error(client, "skolverket_get_course", {"code": "VARVAR01", "max_chars": 10})
    assert "max_chars" in err


# ---------------------------------------------------------------- programs


async def test_list_programs_filters(router, make_client):
    router.add("GET", r"/v1/programs\?", load_fixture("syllabus_programs.json"))
    async with make_client("skolverket") as client:
        gy25 = await call(client, "skolverket_list_programs", {"study_path_type": "program25"})
        assert params(router=router) == {"schooltype": "GY", "timespan": "LATEST"}
        assert [r["code"] for r in gy25["items"]] == ["EK25", "NA25"]
        assert gy25["filters"]["study_path_type"] == "PROGRAM25" and gy25["upstream_count"] == 4
        na = await call(client, "skolverket_list_programs", {"search": "naturvetenskap", "timespan": "CANCELED"})
        assert params(router=router)["timespan"] == "CANCELED"
        rows = {r["code"]: r for r in na["items"]}
        assert set(rows) == {"NA", "NA25"}
        assert rows["NA"]["canceled_date"] == "2025-07-01" and rows["NA"]["study_path_type"] == "PROGRAM"
        assert rows["NA25"]["orientations"] == [
            {"code": "NANAP", "name": "Naturvetenskap"},
            {"code": "NANAA", "name": "Naturvetenskap och samhälle"},
        ]
        assert rows["NA25"]["category"] == "PRELIMINARY_PROGRAM_FOR_HIGHER_EDUCATION"
        err = await call_error(client, "skolverket_list_programs", {"study_path_type": "GY25"})
    assert "Okänd studievägstyp" in err


async def test_get_program_subject_groups(router, make_client):
    router.add("GET", r"/v1/programs/NA25$", load_fixture("syllabus_program_na25.json"))
    router.add("GET", r"/v1/programs/NA25/versions/2$", load_fixture("syllabus_program_na25.json"))
    router.add("GET", r"/v1/programs/XX25$", httpx2.Response(404, json={"errorCode": 404, "message": "Tomt svar."}))
    async with make_client("skolverket") as client:
        program = await call(client, "skolverket_get_program", {"code": "NA25"})
        groups = {g["key"]: g for g in program["subject_groups"]}
        assert [s["code"] for s in groups["foundationSubjects"]["subjects"]][:4] == ["ENGE", "HIST", "IDRO", "MATE"]
        assert groups["programmeSpecificSubjects"]["subject_count"] == 4
        assert groups["specialization"]["name"] == "Fördjupningsämnen"
        assert program["orientations"][0] == {"code": "NANAP", "name": "Naturvetenskap"}
        assert program["other"] == {"description": "Naturvetenskapsprogrammet är ett högskoleförberedande program."}
        assert program["study_path_type"] == "PROGRAM25" and any("Gy25" in n for n in program["notes"])
        assert "raw" not in program
        raw = await call(client, "skolverket_get_program", {"code": "NA25", "version": 2, "include_raw": True})
        assert str(router.last().url) == f"{BASE}/programs/NA25/versions/2"
        assert raw["raw"]["foundationSubjects"]["subjects"][0]["code"] == "ENGE"
        err = await call_error(client, "skolverket_get_program", {"code": "XX25"})
    assert "Programmet 'XX25' hittades inte" in err


async def test_get_program_include_raw_stays_within_budget(router, make_client):
    """Regression: include_raw bypassed the output budget."""
    program = load_fixture("syllabus_program_na25.json")
    long_text = "<p>" + "Programmet ger en bred grund för fortsatta studier. " * 3_000 + "</p>"
    program["program"]["description"] = long_text
    router.add("GET", r"/v1/programs/NA25$", program)
    huge = load_fixture("syllabus_program_na25.json")
    huge["program"]["appendix"] = [f"<p>Rad {i} {'x' * 140}</p>" for i in range(3_000)]
    router.add("GET", r"/v1/programs/NA25/versions/2$", huge)
    async with make_client("skolverket") as client:
        trimmed, size = await call_sized(client, "skolverket_get_program", {"code": "NA25", "include_raw": True})
        assert size <= MAX_OUTPUT_CHARS
        assert trimmed["raw"]["description"].endswith("tecken totalt]") and trimmed["raw"]["code"] == "NA25"
        assert trimmed["raw"]["foundationSubjects"]["subjects"][0]["code"] == "ENGE"
        assert any("kortats" in n for n in trimmed["notes"])
        for max_chars in (50, 2_000, 1_000_000):
            dropped, size = await call_sized(
                client,
                "skolverket_get_program",
                {"code": "NA25", "version": 2, "include_raw": True, "max_chars": max_chars},
            )
            assert size <= MAX_OUTPUT_CHARS and "raw" not in dropped
            assert any("råobjektet utelämnades" in n for n in dropped["notes"])
            assert dropped["subject_groups"][0]["subjects"][0]["code"] == "ENGE"
        err = await call_error(client, "skolverket_get_program", {"code": "NA25", "max_chars": 0})
    assert "max_chars" in err


async def test_list_program_versions(router, make_client):
    router.add("GET", r"/v1/programs/NA/versions$", {**ENVELOPE, "programs": [{"code": "NA", "version": 3}]})
    async with make_client("skolverket") as client:
        result = await call(client, "skolverket_list_program_versions", {"code": "NA"})
    assert result["items"] == [{"code": "NA", "version": 3}] and result["kind"] == "program_versions"


# ---------------------------------------------------------------- curriculums


async def test_curriculums_parsed_defensively(router, make_client):
    listing = {
        **ENVELOPE,
        "curriculums": [
            {"code": "LGR22", "name": "Läroplan för grundskolan", "schoolType": "GR", "validFrom": "2022-07-01"},
            {"code": "LGYAN2013", "name": "Läroplan för anpassade gymnasieskolan", "schoolTypes": ["GYAN"]},
        ],
    }
    detail = {
        **ENVELOPE,
        "curriculum": {
            "code": "LGR22",
            "name": "Läroplan för grundskolan",
            "schoolTypes": ["GR"],
            "startDate": "2022-07-01",
            "version": "3",
            "sections": [
                {
                    "heading": "<h2>1. Skolans värdegrund och uppdrag</h2>",
                    "sectionHeading": "<h2>dubblett</h2>",
                    "content": "<p>Skolväsendet vilar på demokratins grund.</p>" + "<p>Mer text.</p>" * 400,
                }
            ],
        },
    }
    router.add("GET", r"/v1/curriculums\?", listing)
    router.add("GET", r"/v1/curriculums/LGR22(\?|$)", detail)
    router.add("GET", r"/v1/curriculums/LGR22/versions$", {**ENVELOPE, "curriculums": [{"code": "LGR22"}]})
    async with make_client("skolverket") as client:
        result = await call(client, "skolverket_list_curriculums", {"schooltype": "GR"})
        assert params(router=router) == {"schooltype": "GR", "timespan": "LATEST"}
        first = result["items"][0]
        assert first["school_types"] == ["GR"] and first["start_date"] == "2022-07-01"
        assert [r["code"] for r in result["items"]] == ["LGR22", "LGYAN2013"]
        curriculum = await call(client, "skolverket_get_curriculum", {"code": "LGR22", "max_chars": 100})
        section = curriculum["content"]["sections"][0]
        assert section["heading"] == "1. Skolans värdegrund och uppdrag" and "sectionHeading" not in section
        assert section["content"].startswith("Skolväsendet vilar") and section["content"].endswith("tecken totalt]")
        assert curriculum["version"] == 3 and curriculum["school_types"] == ["GR"]
        versions = await call(client, "skolverket_list_curriculum_versions", {"code": "LGR22"})
    assert versions["items"] == [{"code": "LGR22"}]


async def test_get_curriculum_output_budget_with_raw(router, make_client):
    sections = [{"heading": f"<h2>Avsnitt {i}</h2>", "content": "<p>" + "Text. " * 2_000 + "</p>"} for i in range(60)]
    detail = {**ENVELOPE, "curriculum": {"code": "LGR22", "name": "Läroplan för grundskolan", "sections": sections}}
    router.add("GET", r"/v1/curriculums/LGR22(\?|$)", detail)
    async with make_client("skolverket") as client:
        for args in ({}, {"include_raw": True}, {"include_raw": True, "max_chars": 1_000_000}, {"max_chars": 50}):
            result, size = await call_sized(client, "skolverket_get_curriculum", {"code": "LGR22", **args})
            assert size <= MAX_OUTPUT_CHARS, (args, size)
            assert result["code"] == "LGR22"
            if args.get("include_raw"):
                assert any("kortats" in n or "råobjektet" in n for n in result["notes"])


# ---------------------------------------------------------------- valuestore, info, resource


async def test_valuestore_studypathcodes(router, make_client):
    router.add("GET", r"/v1/valuestore/studypathcodes", load_fixture("syllabus_studypathcodes.json"))
    async with make_client("skolverket") as client:
        result = await call(
            client,
            "skolverket_syllabus_valuestore",
            {
                "list": "studypathcodes",
                "schooltype": "gy",
                "type_of_study_path": "orientations",
                "type_of_program": "program25",
                "search": "natur",
            },
        )
        assert router.last().url.path == "/syllabus/v1/valuestore/studypathcodes"
        assert params(router=router) == {
            "schooltype": "GY",
            "typeOfStudyPath": "ORIENTATIONS",
            "typeOfProgram": "PROGRAM25",
        }
        # The mock ignores the filters; the client-side safety net keeps only orientations.
        assert [r["code"] for r in result["items"]] == ["NANAA", "NANAP", "NANAT"]
        assert result["items"][0] == {
            "code": "NANAA",
            "name": "Naturvetenskap och samhälle",
            "type": "ORIENTATIONS",
            "start_date": "2025-07-01",
            "modified_date": "2026-02-13",
            "school_types": ["GY"],
        }
        assert result["code_list"] == "studypathcodes" and result["upstream_count"] == 13
        everything = await call(client, "skolverket_syllabus_valuestore", {"list": "studypathcodes", "limit": 5})
    assert dict(router.last().url.params) == {}
    assert everything["total"] == 13 and everything["returned"] == 5 and everything["truncated"] is True


async def test_valuestore_subject_and_course_codes(router, make_client):
    router.add("GET", r"/v1/valuestore/subjectandcoursecodes$", load_fixture("syllabus_subjectandcoursecodes.json"))
    async with make_client("skolverket") as client:
        levels = await call(
            client,
            "skolverket_syllabus_valuestore",
            {"list": "subjectandcoursecodes", "type_of_syllabus": "level_in_grade_subject_syllabus"},
        )
        assert [r["code"] for r in levels["items"]] == ["ADMI1000X", "MATE1A00X", "MATE1B00X", "MATT100PX"]
        found = await call(
            client, "skolverket_syllabus_valuestore", {"list": "subjectandcoursecodes", "search": "matematik 1a"}
        )
        assert [r["code"] for r in found["items"]] == ["MATMAT01a"]
        assert found["items"][0]["type_of_syllabus"] == "COURSE_IN_SUBJECT_SYLLABUS"
        err = await call_error(
            client, "skolverket_syllabus_valuestore", {"list": "subjectandcoursecodes", "schooltype": "GY"}
        )
        assert "bara list='studypathcodes'" in err
        err = await call_error(
            client, "skolverket_syllabus_valuestore", {"list": "studypathcodes", "type_of_syllabus": "X"}
        )
        assert "subjectandcoursecodes" in err
        await call_error(client, "skolverket_syllabus_valuestore", {"list": "programs"})


async def test_valuestore_schooltypes_and_unknown_payload_key(router, make_client):
    router.add(
        "GET",
        r"/v1/valuestore/schooltypes$",
        {**ENVELOPE, "schoolTypes": [{"code": "GR", "name": "Grundskolan"}, {"code": "GY", "name": "Gymnasieskolan"}]},
    )
    # Undocumented key → first array in the envelope is used.
    router.add(
        "GET", r"/v1/valuestore/typeofsyllabus$", {**ENVELOPE, "values": ["SUBJECT_SYLLABUS", "COURSE_SYLLABUS"]}
    )
    router.add("GET", r"/v1/valuestore/schooltypes/expired$", httpx2.Response(404, json={"message": "Tomt svar."}))
    async with make_client("skolverket") as client:
        school_types = await call(client, "skolverket_syllabus_valuestore", {"list": "schooltypes", "search": "gymn"})
        assert school_types["items"] == [{"code": "GY", "name": "Gymnasieskolan"}]
        types = await call(client, "skolverket_syllabus_valuestore", {"list": "typeofsyllabus"})
        assert [r["code"] for r in types["items"]] == ["SUBJECT_SYLLABUS", "COURSE_SYLLABUS"]
        expired = await call(client, "skolverket_syllabus_valuestore", {"list": "schooltypes/expired"})
    assert expired["total"] == 0 and "404" in expired["notes"][0]


async def test_api_info(router, make_client):
    router.add(
        "GET",
        r"/v1/api-info$",
        {**ENVELOPE, "apiDocumentation": "https://www.skolverket.se/om-oss/oppna-data", "apiSunset": "2027-12-31"},
    )
    async with make_client("skolverket") as client:
        info = await call(client, "skolverket_syllabus_api_info")
    assert str(router.last().url) == f"{BASE}/api-info"
    assert info["api"]["apiVersion"] == "1.16.2-SNAPSHOT" and info["base_url"] == BASE
    assert info["openapi"] == "https://api.skolverket.se/syllabus/v3/api-docs"
    assert info["notes"] == ["Avveckling (sunset): 2027-12-31"]


async def test_api_info_output_budget(router, make_client):
    router.add("GET", r"/v1/api-info$", {**ENVELOPE, "extra": [{"a": ["x" * 600] * 300}] * 300})
    async with make_client("skolverket") as client:
        info, size = await call_sized(client, "skolverket_syllabus_api_info")
    assert size <= MAX_OUTPUT_CHARS and "extra" not in info["api"] and info["api"]["apiName"] == "Syllabus API"
    assert any("utelämnades" in n for n in info["notes"])


async def test_codes_resource(make_client):
    async with make_client("skolverket") as client:
        resource = await client.read_resource(CODES_URI)
    data = json.loads(resource.contents[0].text)
    school_types = {s["code"]: s for s in data["school_types"]}
    assert school_types["GY"]["status"] == "active" and "GRAN" in school_types and "GRSÄR" not in school_types
    assert {"PROGRAM25", "PARTICULAR_STUDY_PATH25", "ORIENTATIONS"} <= {s["code"] for s in data["study_path_types"]}
    assert [g["code"] for g in data["grade_steps"]] == ["A", "B", "C", "D", "E", "F"]
    assert "LEVEL_IN_GRADE_SUBJECT_SYLLABUS" in {t["code"] for t in data["type_of_syllabus_entity"]}
    assert "CANCELED" in {t["code"] for t in data["timespan"]}
    assert "BASIC_REQUIREMENTS" in data["requirement_types"] and "GY25" in {r["code"] for r in data["reforms"]}


# ---------------------------------------------------------------- server output cap (FUZZY_MCP_MAX_OUTPUT_CHARS)

# Tool, upstream URL, payload key and fixed arguments of every list tool that pages with offset.
PAGED_LIST_TOOLS = [
    ("skolverket_list_subjects", r"/v1/subjects\?", "subjects", {}),
    ("skolverket_list_courses", r"/v1/courses\?", "courses", {}),
    ("skolverket_list_programs", r"/v1/programs\?", "programs", {}),
    ("skolverket_list_curriculums", r"/v1/curriculums\?", "curriculums", {}),
    (
        "skolverket_syllabus_valuestore",
        r"/v1/valuestore/subjectandcoursecodes$",
        "codes",
        {"list": "subjectandcoursecodes"},
    ),
]


def list_rows(count: int) -> list[dict]:
    """Rows large enough that limit=500 passes the tool's own budget (fit_items) and the cap shortens most pages."""
    return [
        {
            "code": f"K{i:03d}",
            "name": f"Post {i} med ett ganska långt namn",
            "version": 1,
            "startDate": "2025-07-01",
            "typeOfSyllabus": "COURSE_IN_SUBJECT_SYLLABUS",
            "schoolTypes": ["GY", "VUXGY"],
            "description": "Beskrivning av posten. " * 8,
        }
        for i in range(count)
    ]


async def walk_offsets(client, tool: str, args: dict, cap: int) -> tuple[list[str], int]:
    """Page through a list tool with offset = offset + returned (the documented pointer); returns the codes seen and
    the number of pages the cap shortened. Every answer must fit the cap, never claim to be complete when it is not,
    agree with _kortat.fortsatt and keep no stale offset in its notes."""
    seen: list[str] = []
    offset = cut = 0
    for _ in range(200):
        result = await client.call_tool(tool, {**args, "limit": 500, "offset": offset})
        text = result.content[0].text
        assert not result.is_error and client_size(text) <= cap, text[:300]
        page = result.structured_content
        returned, total = page["returned"], page["total"]
        assert page["offset"] == offset and returned == len(page["items"]) > 0
        assert page["truncated"] is (offset + returned < total)
        if TRUNCATION_KEY in page:
            cut += 1
            assert page["truncated"] is True
            assert page[TRUNCATION_KEY]["fortsatt"] == [{"falt": "items", "anrop": {"offset": offset + returned}}]
        assert "Avveckling (sunset): 2027-12-31" in page["notes"]  # only the stale fit_items note is removed
        for note in page["notes"]:
            assert all(int(n) == offset + returned for n in re.findall(r"offset=(\d+)", note)), note
        seen += [row["code"] for row in page["items"]]
        offset += returned
        if not page["truncated"]:
            return seen, cut
    raise AssertionError("paging did not end")


@pytest.mark.parametrize("tool,pattern,key,args", PAGED_LIST_TOOLS, ids=[t[0] for t in PAGED_LIST_TOOLS])
async def test_lists_walk_exactly_once_under_the_output_cap(router, make_client, tool, pattern, key, args):
    """Regression (#20): the cap shortened items but left returned, truncated and fit_items' 'offset=' note as they
    were, so offset + returned skipped the rows the cap removed (and a cut last page claimed to be complete)."""
    rows = list_rows(600)
    router.add("GET", pattern, {**ENVELOPE, "apiSunset": "2027-12-31", key: rows})
    async with make_client("skolverket") as client:
        uncapped = await call(client, tool, {**args, "limit": 500})
    assert any(n.startswith(FIT_NOTE) for n in uncapped["notes"])  # the tool's own budget cut the first page
    for cap in (32_768, 8_000):
        async with make_client("skolverket", max_output_chars=cap) as client:
            seen, cut = await walk_offsets(client, tool, args, cap)
        assert cut > 0 and seen == [row["code"] for row in rows], cap  # every row exactly once, in order


VERSION_TOOLS = [
    ("skolverket_list_subject_versions", "MAT", "subjects", "skolverket_get_subject"),
    ("skolverket_list_course_versions", "MATMAT01c", "courses", "skolverket_get_course"),
    ("skolverket_list_program_versions", "NA", "programs", "skolverket_get_program"),
    ("skolverket_list_curriculum_versions", "LGR22", "curriculums", "skolverket_get_curriculum"),
]


@pytest.mark.parametrize("tool,code,key,get_tool", VERSION_TOOLS, ids=[t[0] for t in VERSION_TOOLS])
async def test_version_lists_name_omitted_versions(router, make_client, tool, code, key, get_tool):
    """Regression (#20): a cut version list kept truncated=false and returned=60. The tools have no offset and the
    version numbers are not consecutive, so the cap names the versions it left out instead."""
    versions = [
        {"code": code, "version": 3 * i + 1, "startDate": "2011-07-01", "versionInfo": "Ändrad genom SKOLFS 2020:1."}
        for i in range(60)
    ]
    newest_first = [3 * i + 1 for i in reversed(range(60))]
    router.add("GET", rf"/v1/{key}/{code}/versions$", {**ENVELOPE, key: versions})
    async with make_client("skolverket", max_output_chars=6_000) as client:
        result = await client.call_tool(tool, {"code": code, "limit": 500})
    assert not result.is_error and client_size(result.content[0].text) <= 6_000
    page = result.structured_content
    kept = [row["version"] for row in page["items"]]
    assert 0 < len(kept) < 60 and page["returned"] == len(kept) and page["total"] == 60
    assert page["truncated"] is True
    note = page[TRUNCATION_KEY]
    assert note["utelamnade"] == [{"falt": "items", "koder": newest_first[len(kept) :]}]
    assert kept + note["utelamnade"][0]["koder"] == newest_first
    assert "fortsatt" not in note  # offset is not an argument: a pointer would repeat the same page
    assert f"{get_tool}(code, version=...)" in note["meddelande"]


async def test_version_list_cut_removes_the_stale_fit_note(router, make_client):
    """The tool's own budget (fit_items) cuts first and says how many rows it kept; the cap cuts further, so that
    count no longer holds. The versions the cap removed are named, in order, right after the rows shown."""
    versions = [{"code": "MAT", "version": v, "versionInfo": "Ändrad genom SKOLFS 2020:1. " * 15} for v in range(300)]
    router.add("GET", r"/v1/subjects/MAT/versions$", {**ENVELOPE, "subjects": versions})
    async with make_client("skolverket") as client:
        own = await call(client, "skolverket_list_subject_versions", {"code": "MAT", "limit": 500})
    assert own["returned"] < 300 and any(n.startswith(FIT_NOTE) for n in own["notes"])
    async with make_client("skolverket", max_output_chars=6_000) as client:
        result = await client.call_tool("skolverket_list_subject_versions", {"code": "MAT", "limit": 500})
    assert not result.is_error and client_size(result.content[0].text) <= 6_000
    page = result.structured_content
    assert not any(n.startswith(FIT_NOTE) for n in page.get("notes") or [])
    kept = [row["version"] for row in page["items"]]
    assert page["returned"] == len(kept) < own["returned"] and page["truncated"] is True and page["total"] == 300
    assert kept + page[TRUNCATION_KEY]["utelamnade"][0]["koder"] == [row["version"] for row in own["items"]]


async def test_get_subject_names_every_omitted_course(router, make_client):
    """Regression (#20): the tool named the courses it left out (VARVAR64–150), then the cap cut courses 63 → ~16
    and VARVAR17–63 were missing with no note. Shown + fortsatt + the tool's note now cover every course once."""
    router.add("GET", r"/v1/subjects/VAR$", {**ENVELOPE, "subject": big_subject(150)})
    every = [f"VARVAR{i:02d}" for i in range(1, 151)]
    async with make_client("skolverket", max_output_chars=20_000) as client:
        result = await client.call_tool("skolverket_get_subject", {"code": "VAR", "max_chars": 50})
        assert not result.is_error and client_size(result.content[0].text) <= 20_000
        first = result.structured_content
        shown = [course["code"] for course in first["courses"]]
        note = first[TRUNCATION_KEY]
        [resume] = note["fortsatt"]
        assert resume["falt"] == "courses" and list(resume["anrop"]) == ["course_codes"]
        named = omitted_by_tool(first["notes"])
        assert shown + resume["anrop"]["course_codes"] + named == every and first["course_count"] == 150
        assert first["sections"] == list(ALL_SUBJECT_SECTIONS)  # echoes and notes are never shortened
        assert not {"sections", "notes"} & {entry["falt"] for entry in note["kortade"]}
        assert note["meddelande"].endswith(" Kurser som verktyget redan utelämnat står i notes.")
        # Following fortsatt (other arguments unchanged) returns exactly those courses, in the subject's order.
        fetched: list[str] = []
        call_args = {"code": "VAR", "max_chars": 50, **resume["anrop"]}
        for _ in range(20):
            result = await client.call_tool("skolverket_get_subject", call_args)
            assert not result.is_error and client_size(result.content[0].text) <= 20_000
            answer = result.structured_content
            fetched += [course["code"] for course in answer["courses"]]
            more = answer.get(TRUNCATION_KEY, {}).get("fortsatt")
            if not more:
                break
            call_args = {"code": "VAR", "max_chars": 50, **more[0]["anrop"]}
    assert fetched == resume["anrop"]["course_codes"]


# (courses, cap, must_fit). At 150/4000, 120/4000 and 80/3600 the cap used to halve the tool's note on the courses it
# had left out, losing 53, 39 and 20 codes; one course plus every code does not fit there, so an explicit error is a
# valid answer. The other caps leave room for the whole note beside the cap's own fortsatt.
OMITTED_COURSE_CASES = [
    (150, 4_000, False),
    (120, 4_000, False),
    (80, 3_600, False),
    (150, 6_000, True),
    (80, 4_000, True),
]


@pytest.mark.parametrize("courses,cap,must_fit", OMITTED_COURSE_CASES)
async def test_get_subject_cap_never_halves_the_omitted_course_codes(router, make_client, courses, cap, must_fit):
    """Regression (#20): the note in which the tool names the courses its own budget left out was the longest string
    left once the cap had cut courses to one, so the cap halved it: the codes in its second half were in neither
    courses, notes nor _kortat.fortsatt. The codes now stand in notes too short to be shortened, so an answer names
    every course (shown + fortsatt + notes) or is an explicit error."""
    router.add("GET", r"/v1/subjects/VAR$", {**ENVELOPE, "subject": big_subject(courses)})
    async with make_client("skolverket", max_output_chars=cap) as client:
        result = await client.call_tool("skolverket_get_subject", {"code": "VAR"})
    text = result.content[0].text
    if result.is_error:
        assert not must_fit and "Svaret ryms inte ens efter kortning" in text, text[:300]
        return
    assert client_size(text) <= cap
    page = result.structured_content
    note = page[TRUNCATION_KEY]
    shown = [course["code"] for course in page["courses"]]
    [resume] = note["fortsatt"]
    named = omitted_by_tool(page["notes"])
    assert named and page["course_count"] == courses
    assert shown + resume["anrop"]["course_codes"] + named == [f"VARVAR{i:02d}" for i in range(1, courses + 1)]
    assert not any(entry["falt"].startswith("notes") for entry in note["kortade"])
    assert note["meddelande"].endswith(" Kurser som verktyget redan utelämnat står i notes.")
