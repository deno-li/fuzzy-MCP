# SPDX-FileCopyrightText: 2026 Deniz Özer
#
# SPDX-License-Identifier: MIT

import copy
import json
from urllib.parse import urlsplit

import httpx2
import pytest

from fuzzy_mcp.errors import InvalidInputError
from fuzzy_mcp.sources.skolverket.planned_educations import (
    MEDIA_TYPE,
    PERSONAL_DATA_NOTE,
    PERSONAL_DATA_OFF_NOTE,
    colon_sort,
    normalize_area_code,
    normalize_school_unit_code,
    normalize_semester_start,
    normalize_type_of_schooling,
    parse_number,
    spring_sort,
)

from .conftest import call, call_error, load_fixture

pytestmark = pytest.mark.anyio

BASE = "https://api.skolverket.se/planned-educations/v4"
# Output-size targets: hosts cap tool output at ~25k tokens.
DEFAULT_MAX_CHARS = 60_000
ABSOLUTE_MAX_CHARS = 100_000


def envelope(body, status="OK"):
    return {"status": status, "message": "Success", "body": body}


def assert_v4(request: httpx2.Request, path: str) -> None:
    assert request.headers["accept"] == MEDIA_TYPE
    assert request.url.path == f"/planned-educations/v4/{path}"


async def output_chars(client, tool: str, args: dict) -> int:
    result = await client.call_tool(tool, args)
    assert not result.is_error, "".join(getattr(c, "text", "") for c in result.content)[:300]
    return sum(len(getattr(c, "text", "")) for c in result.content)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("15,5", (15.5, False)),
        ("cirka 370", (370.0, True)),
        ("~100", (100.0, True)),
        ("44%", (44.0, False)),
        ("-24", (-24.0, False)),
        ("1 234", (1234.0, False)),
        (".", (None, False)),
        ("..", (None, False)),
        ("*", (None, False)),
        ("-", (None, False)),
        (None, (None, False)),
        (264, (264.0, False)),
        ("Svenska 3", (None, False)),
    ],
)
def test_parse_number(raw, expected):
    assert parse_number(raw) == expected


def test_type_of_schooling_aliases():
    assert normalize_type_of_schooling("GR") == "gr"
    assert normalize_type_of_schooling("Anpassad grundskola") == "gran"
    assert normalize_type_of_schooling("gymnasiesärskola") == "gyan"
    assert normalize_type_of_schooling("vuxgyan") == "vuxgyan"
    # vux, vuxgrs and vuxgys are listed by v4 /support/school-types (not v3-only codes).
    assert normalize_type_of_schooling("vux") == "vux"
    assert normalize_type_of_schooling("Komvux") == "vux"
    assert normalize_type_of_schooling("vuxgys") == "vuxgys"


@pytest.mark.parametrize(
    "raw,spring,colon",
    [
        ("name, ASC", "name,asc", "name:asc"),
        ("name", "name", "name"),
        # Spring multi-key form (as in the spec's own _links).
        (
            "schoolUnitName,studyPathName,asc",
            "schoolUnitName,studyPathName,asc",
            "schoolUnitName:asc,studyPathName:asc",
        ),
        # Colon form documented for /adult-education-events.
        ("titleSv:asc , typeOfSchool:desc", ["titleSv,asc", "typeOfSchool,desc"], "titleSv:asc,typeOfSchool:desc"),
        ("titleSv:asc, municipality:asc", "titleSv,municipality,asc", "titleSv:asc,municipality:asc"),
    ],
)
def test_sort_formats(raw, spring, colon):
    assert spring_sort(raw) == spring
    assert colon_sort(raw) == colon


@pytest.mark.parametrize("raw", ["asc", "name;drop table", "name,asc,desc", "na me"])
def test_sort_rejects_garbage(raw):
    with pytest.raises(InvalidInputError):
        spring_sort(raw)


@pytest.mark.parametrize(
    "raw,expected",
    [
        # Both examples from the spec's parameter description.
        ("2020-01-01TO2020-05-31,2020-08-01TO2020-12-31", "2020-01-01TO2020-05-31,2020-08-01TO2020-12-31"),
        ("2026-08-01 to 2026-12-31", "2026-08-01TO2026-12-31"),
        ("2026-08-01 To 2026-12-31; 2027-01-01 TO 2027-05-31", "2026-08-01TO2026-12-31,2027-01-01TO2027-05-31"),
        (["2026-08-01TO2026-12-31", "2027-01-01to2027-05-31"], "2026-08-01TO2026-12-31,2027-01-01TO2027-05-31"),
        ("2026-08-17", "2026-08-17"),
        ("  ", None),
    ],
)
def test_semester_start_format(raw, expected):
    assert normalize_semester_start(raw) == expected


@pytest.mark.parametrize(
    ("func", "value"),
    [
        (normalize_school_unit_code, "４４６７３０７４"),  # full-width digits
        (normalize_school_unit_code, "٤٤٦٧٣٠٧٤"),  # Arabic-Indic digits
        (normalize_area_code, "０１８０"),
        (normalize_area_code, "１８０"),
        (normalize_area_code, "٠١"),
    ],
)
def test_codes_accept_only_ascii_digits(func, value):
    with pytest.raises(InvalidInputError):
        func(value)


def test_codes_accept_ascii_digits():
    assert normalize_school_unit_code(" 4467 3074 ") == "44673074"
    assert normalize_area_code("180") == "0180"
    assert normalize_area_code("1") == "01"


@pytest.mark.parametrize("raw", ["hösten 2026", "2026-13-01", "2026-12-31TO2026-08-01", "2026-08-01 - 2026-12-31"])
def test_semester_start_rejects(raw):
    with pytest.raises(InvalidInputError, match="semester_start_from"):
        normalize_semester_start(raw)


async def test_search_school_units(router, make_client):
    router.add("GET", r"/v4/school-units\?", load_fixture("planned_educations_school_units.json"))
    async with make_client("skolverket") as client:
        result = await call(
            client,
            "skolverket_pe_search_school_units",
            {
                "name": "Abrahamsberg",
                "type_of_schooling": "grundskola",
                "principal_organizer_type": "kommunal",
                "school_orientation": "INTERNATIONELL",
                "resursskola": False,
                "geographical_area_code": "180",
                "school_years": "9",
                "latitude": 59.33,
                "longitude": 17.95,
                "radius_km": 5,
                "sort": "name, ASC",
                "size": 2,
            },
        )
    request = router.last()
    assert_v4(request, "school-units")
    params = request.url.params
    assert params["name"] == "Abrahamsberg"
    assert params["typeOfSchooling"] == "gr"
    assert params["principalOrganizerType"] == "Kommunal"
    assert params["schoolOrientation"] == "INTERNATIONELL"
    assert params["Resursskola"] == "false"
    assert params["geographicalAreaCode"] == "0180"
    assert params["schoolYears"] == "9"
    assert params["latitude"] == "59.330000" and params["longitude"] == "17.950000"
    assert params["distance"] == "5" and params["coordinateSystemType"] == "WGS84"
    assert params["sort"] == "name,asc"
    assert params["page"] == "0" and params["size"] == "2"
    assert result["total"] == 4749 and result["truncated"] is True and result["next_page"] == 1
    assert result["total_pages"] == 2375
    first, second = result["rows"]
    assert first["code"] == "44673074" and first["post_code_district"] == "Bromma"
    assert first["type_of_schooling"] == [{"code": "gr", "school_years": ["7", "8", "9"]}]
    assert second["type_of_schooling"][0]["school_years"][0] == "4"
    assert "_links" not in first


async def test_search_school_units_statlig_alias_and_multi_sort(router, make_client):
    router.add("GET", r"/v4/school-units\?", load_fixture("planned_educations_school_units.json"))
    async with make_client("skolverket") as client:
        await call(
            client,
            "skolverket_pe_search_school_units",
            {"principal_organizer_type": "statlig", "sort": "a:asc,b:desc"},
        )
    params = router.last().url.params
    assert params["principalOrganizerType"] == "Statlig"
    assert params.get_list("sort") == ["a,asc", "b,desc"]


async def test_search_school_units_compact_spec_shape(router, make_client):
    router.add("GET", r"/v4/compact-school-units", load_fixture("planned_educations_compact_school_units.json"))
    async with make_client("skolverket") as client:
        result = await call(
            client,
            "skolverket_pe_search_school_units",
            {"type_of_schooling": "gy", "compact": True, "resursskola": True, "size": 500},
        )
    request = router.last()
    assert_v4(request, "compact-school-units")
    assert request.url.params["coordinateSystemType"] == "WGS84"
    assert request.url.params["Resursskola"] == "true"
    assert request.url.params["size"] == "100"
    assert result["variant"] == "compact"
    first = result["rows"][0]
    assert first["code"] == "11317587" and first["name"] == "AG International School Gothenburg"
    # educationEventTypeOfSchooling "gy,gr" is kept as school forms.
    assert first["type_of_schooling"] == [{"code": "gy"}, {"code": "gr"}]
    assert first["resursskola"] is False and first["abroad_school"] is False
    assert first["latitude"] == pytest.approx(57.709076312329756)
    assert [f["code"] for f in result["rows"][1]["type_of_schooling"]] == ["gr", "fsk", "gy"]
    assert result["total"] == 6566 and result["truncated"] is True


async def test_search_school_units_validation(router, make_client):
    async with make_client("skolverket") as client:
        err = await call_error(client, "skolverket_pe_search_school_units", {"type_of_schooling": "xyz"})
        assert "Okänd skolform" in err
        err = await call_error(client, "skolverket_pe_search_school_units", {"latitude": 59.3})
        assert "både latitude och longitude" in err
        err = await call_error(client, "skolverket_pe_search_school_units", {"compact": True, "name": "x"})
        assert "compact=true" in err
        err = await call_error(
            client, "skolverket_pe_search_school_units", {"compact": True, "school_orientation": "ALLMAN"}
        )
        assert "school_orientation" in err
        err = await call_error(client, "skolverket_pe_search_school_units", {"geographical_area_code": "Göteborg"})
        assert "kommunkod" in err
        err = await call_error(client, "skolverket_pe_search_school_units", {"page": -1})
        assert "0-baserad" in err
        err = await call_error(client, "skolverket_pe_search_school_units", {"sort": "name;drop"})
        assert "sort" in err
    assert router.requests == []


async def test_get_school_unit_spec_example_hides_personal_email(router, make_client):
    router.add("GET", r"/v4/school-units/76026322$", load_fixture("planned_educations_school_unit.json"))
    router.add(
        "GET", r"/v4/school-units/76026322/distanceFrom\?", load_fixture("planned_educations_distance_from.json")
    )
    async with make_client("skolverket") as client:
        result = await client.call_tool(
            "skolverket_pe_get_school_unit",
            {
                "school_unit_code": "7602 6322",
                "distance_from_latitude": 59.3293,
                "distance_from_longitude": 18.0686,
                "include_raw": True,
            },
        )
        opted_in = await call(
            client,
            "skolverket_pe_get_school_unit",
            {"school_unit_code": "76026322", "include_personal_data": True, "include_raw": True},
        )
    text = "".join(getattr(c, "text", "") for c in result.content)
    detail = result.structured_content
    assert_v4(router.requests[0], "school-units/76026322")
    assert_v4(router.requests[1], "school-units/76026322/distanceFrom")
    assert router.requests[1].url.params["latitude"] == "59.329300"
    assert router.requests[1].url.params["coordinateSystemType"] == "WGS84"
    assert detail["code"] == "76026322" and detail["name"] == "Abisko skola"
    assert detail["organisation_number"] == "2120002783"
    assert detail["principal_organizer_type"] == "Kommunal"
    assert detail["resursskola"] is False and detail["start_date"] == "2013-10-01"
    assert detail["latitude"] == pytest.approx(68.348910876455) and detail["sweref99_n"] == pytest.approx(7586671.849)
    assert detail["addresses"] == [
        {"type": "VISITING_ADDRESS", "street": "Kalle Jons väg 18", "zip_code": "98107", "city": "Abisko"}
    ]
    assert detail["telephone"] == "0980-00 00 00"
    assert [f["code"] for f in detail["type_of_schooling"]] == ["fsk", "gr"]
    # kilometersToSchoolUnit "1003,795" → number.
    assert detail["distance_km"] == pytest.approx(1003.795)
    # Personal data: the named person's e-mail is neither in the fields nor in raw.
    assert "email" not in detail and "contactInfo" not in detail["raw"]
    assert "fornamn.efternamn" not in text.lower()
    assert any("personuppgift" in n for n in detail["notes"])
    assert detail["raw"]["organisationRegistryNumber"] == "2120002783"
    assert opted_in["email"] == "fornamn.efternamn@kommun.example"
    assert opted_in["raw"]["contactInfo"]["email"] == "fornamn.efternamn@kommun.example"
    assert "notes" not in opted_in


async def test_get_school_unit_not_found(router, make_client):
    router.add(
        "GET",
        r"/v4/school-units/99999999$",
        httpx2.Response(404, json={"status": "NOT_FOUND", "message": "School unit not found", "body": None}),
    )
    async with make_client("skolverket") as client:
        err = await call_error(client, "skolverket_pe_get_school_unit", {"school_unit_code": "99999999"})
        assert "HTTP 404" in err and "School unit not found" in err
        err = await call_error(client, "skolverket_pe_get_school_unit", {"school_unit_code": "123"})
        assert "8 siffror" in err


async def test_envelope_status_handling(router, make_client):
    router.add("GET", r"/v4/school-units/11111111$", {"status": "ERROR", "message": "Internt fel", "body": None})
    body = load_fixture("planned_educations_school_unit.json")["body"]
    # The schema's status enum spells success "200 OK" while the examples say "OK": accept both.
    router.add("GET", r"/v4/school-units/76026322$", envelope(body, status="200 OK"))
    async with make_client("skolverket") as client:
        err = await call_error(client, "skolverket_pe_get_school_unit", {"school_unit_code": "11111111"})
        ok = await call(client, "skolverket_pe_get_school_unit", {"school_unit_code": "76026322"})
    assert "status ERROR" in err and "Internt fel" in err
    assert ok["name"] == "Abisko skola"


async def test_school_unit_statistics_discovers_forms(router, make_client):
    router.add("GET", r"/statistics$", load_fixture("planned_educations_statistics_index.json"))
    router.add("GET", r"/statistics/gr$", load_fixture("planned_educations_statistics_gr.json"))
    router.add("GET", r"/statistics/gy$", load_fixture("planned_educations_statistics_gy.json"))
    async with make_client("skolverket") as client:
        result = await call(client, "skolverket_pe_school_unit_statistics", {"school_unit_code": "85014247"})
    assert [r.url.path.rsplit("/", 1)[-1] for r in router.requests] == ["statistics", "gr", "gy"]
    for request in router.requests:
        assert request.headers["accept"] == MEDIA_TYPE
        assert not request.url.params
    assert result["available_school_forms"] == ["gr", "gy"]
    gr, gy = result["statistics"]
    assert gr["school_form"] == "gr" and gr["school_unit_code"] == "85014247" and "has_library" not in gr
    ind = gr["indicators"]
    assert ind["studentsPerTeacherQuota"] == {
        "value": "15,5",
        "number": 15.5,
        "value_type": "EXISTS",
        "time_period": "2024/25",
    }
    # Sorted by period although upstream listed 2024/25 last.
    assert ind["certifiedTeachersQuota"]["time_period"] == "2024/25"
    assert ind["certifiedTeachersQuota"]["number"] == 71.0
    assert ind["totalNumberOfPupils"]["number"] == 370 and ind["totalNumberOfPupils"]["approximate"] is True
    # Newest value is suppressed (..): latest falls back to the newest period with a value.
    assert ind["averageGradesMeritRating9thGrade"]["number"] == 264.0
    assert ind["averageGradesMeritRating9thGrade"]["time_period"] == "2023/24"
    assert ind["ratioOfPupils9thGradeEligibleForNationalProgramYR"]["approximate"] is True
    assert ind["specialTeacherPositions"] == {"value": ".", "value_type": "MISSING", "time_period": "2024/25"}
    assert "specialEducatorsQuota" not in ind and "history" not in ind["studentsPerTeacherQuota"]
    # gy as in the spec example: no top-level schoolUnit, programme codes without "25".
    assert gy["school_unit_code"] == "49862313"
    assert [p["program_code"] for p in gy["programs"]] == ["EK", "IMA", "IMS", "IMV", "NA", "SA", "TE"]
    ek = gy["programs"][0]
    assert ek["indicators"]["admissionPointsAverage"]["number"] == 259.3
    assert ek["indicators"]["admissionPointsAverage"]["time_period"] == "2024"
    assert ek["indicators"]["totalNumberOfPupils"]["number"] == 270
    assert ek["attributes"] == {
        "sveSubjectTest": "Svenska 3",
        "svaSubjectTest": "Svenska som andraspråk 3",
        "ma1SubjectTest": "Matematik 2B",
        "ma2SubjectTest": "Matematik 3B",
        "engSubjectTest": "Engelska 6",
    }
    for program in gy["programs"]:
        assert "schoolUnit" not in (program.get("attributes") or {})
        # School-level fields repeated in every programme are dropped there.
        assert "studentsPerTeacherQuota" not in program["indicators"]
    assert gy["indicators"]["studentsPerTeacherQuota"]["number"] == 18.3
    assert gy["programs"][1]["indicators"]["averageResultNationalTestsSubjectSVE"] == {"value_type": "MISSING"}
    assert result["indicator_labels"]["averageGradesMeritRating9thGrade"].startswith("Genomsnittligt meritvärde")


async def test_school_unit_statistics_program_filter_and_history(router, make_client):
    router.add("GET", r"/statistics/gy$", load_fixture("planned_educations_statistics_gy.json"))
    router.add("GET", r"/statistics/fsk$", httpx2.Response(404, json={"message": "Not Found"}))
    async with make_client("skolverket") as client:
        na = await call(
            client,
            "skolverket_pe_school_unit_statistics",
            {"school_unit_code": "49862313", "school_form": "gy", "program_code": "na25", "include_history": True},
        )
        im = await call(
            client,
            "skolverket_pe_school_unit_statistics",
            {"school_unit_code": "49862313", "school_form": "gy", "program_code": "IM"},
        )
        none = await call(
            client,
            "skolverket_pe_school_unit_statistics",
            {"school_unit_code": "49862313", "school_form": "gy", "program_code": "XX"},
        )
        missing = await call(
            client, "skolverket_pe_school_unit_statistics", {"school_unit_code": "49862313", "school_form": "fsk"}
        )
    assert_v4(router.requests[0], "school-units/49862313/statistics/gy")
    (gy,) = na["statistics"]
    # 'NA25' (GY25 code) falls back to the statistics code 'NA'.
    assert [p["program_code"] for p in gy["programs"]] == ["NA"]
    history = gy["programs"][0]["indicators"]["ratioOfPupilsWithExamWithin3Years"]["history"]
    assert [h["time_period"] for h in history] == ["2021/22", "2020/21", "2019/20", "2018/19", "2017/18"]
    assert "available_school_forms" not in na
    assert [p["program_code"] for p in im["statistics"][0]["programs"]] == ["IMA", "IMS", "IMV"]
    assert none["statistics"][0]["note"].startswith("Programkoden XX saknas") and "NA" in none["statistics"][0]["note"]
    assert missing["statistics"][0]["note"].startswith("Ingen statistik")


async def test_school_unit_statistics_history_stays_within_size_budget(router, make_client):
    big = load_fixture("planned_educations_statistics_gy.json")
    programs = big["body"]["programMetrics"]
    many = []
    for i in range(40):
        program = copy.deepcopy(programs[i % len(programs)])
        program["programCode"] = f"P{i:02d}"
        for key, value in program.items():
            if isinstance(value, list) and value and isinstance(value[0], dict):
                program[key] = [dict(value[0], timePeriod=f"{2024 - j}/{25 - j:02d}") for j in range(5)]
        many.append(program)
    big["body"]["programMetrics"] = many
    router.add("GET", r"/statistics/gy$", big)
    args = {"school_unit_code": "49862313", "school_form": "gy"}
    async with make_client("skolverket") as client:
        assert await output_chars(client, "skolverket_pe_school_unit_statistics", args) < DEFAULT_MAX_CHARS
        size = await output_chars(client, "skolverket_pe_school_unit_statistics", {**args, "include_history": True})
        result = await call(client, "skolverket_pe_school_unit_statistics", {**args, "include_history": True})
    assert size < ABSOLUTE_MAX_CHARS
    assert any("program_code" in n for n in result["notes"])
    assert "history" not in result["statistics"][0]["programs"][0]["indicators"]["totalNumberOfPupils"]
    assert "history" in result["statistics"][0]["indicators"]["studentsPerTeacherQuota"]


async def test_national_statistics(router, make_client):
    body = {
        "studentsPerTeacherQuota": [{"value": "12,0", "valueType": "EXISTS", "timePeriod": "2023/24"}],
        "certifiedTeachersQuota": [{"value": "71,5", "valueType": "EXISTS", "timePeriod": "2023/24"}],
        "totalNumberOfPupils": None,
    }
    router.add("GET", r"/national-values/gy/EK25$", httpx2.Response(404, json={"message": "Not Found"}))
    router.add("GET", r"/national-values/gy/EK$", load_fixture("planned_educations_national_gy.json"))
    router.add("GET", r"/national-values/gr$", envelope(body))
    async with make_client("skolverket") as client:
        gy = await call(client, "skolverket_pe_national_statistics", {"school_form": "gy", "program_code": "ek25"})
        direct = await call(client, "skolverket_pe_national_statistics", {"school_form": "gy", "program_code": "EK"})
        gr = await call(client, "skolverket_pe_national_statistics", {"school_form": "gr"})
        err = await call_error(client, "skolverket_pe_national_statistics", {"school_form": "gy"})
    assert_v4(router.requests[0], "statistics/national-values/gy/EK25")
    assert_v4(router.requests[1], "statistics/national-values/gy/EK")
    assert_v4(router.requests[2], "statistics/national-values/gy/EK")
    assert_v4(router.requests[3], "statistics/national-values/gr")
    assert gy["program_code"] == "EK" and "EK25" in gy["statistics"]["note"]
    assert gy["statistics"]["indicators"]["certifiedTeachersQuota"]["number"] == 84.2
    assert gy["statistics"]["attributes"]["sveSubjectTest"] == "Svenska 3"
    assert direct["program_code"] == "EK" and "note" not in direct["statistics"]
    assert gr["statistics"]["indicators"]["studentsPerTeacherQuota"]["number"] == 12.0
    assert "program_code" in err


async def test_salsa_all_sorted_single_and_null(router, make_client):
    salsa = load_fixture("planned_educations_salsa.json")
    suppressed = salsa["body"]["compulsorySchoolUnitSalsaMetricList"][2]
    suppressed["salsaAverageGradesIn9thGradeActual"] = {
        "value": "..",
        "valueType": "OMITTED_DUE_TO_BASED_ON_FEW_PUPILS",
    }
    router.add("GET", r"/all-schools/salsa$", salsa)
    router.add("GET", r"/all-schools/salsa/99648792$", load_fixture("planned_educations_salsa_unit.json"))
    # Verified live for salsa/87313898: 200 with a list holding only null.
    router.add(
        "GET",
        r"/all-schools/salsa/87313898$",
        envelope({"compulsorySchoolUnitSalsaMetricList": [None], "timePeriod": "2024/25"}),
    )
    async with make_client("skolverket") as client:
        ranked = await call(
            client,
            "skolverket_pe_salsa",
            {"sort_by": "salsaAverageGradesIn9thGradeDeviation", "descending": False, "limit": 2},
        )
        one = await call(client, "skolverket_pe_salsa", {"school_unit_code": "99648792"})
        empty = await call(client, "skolverket_pe_salsa", {"school_unit_code": "87313898"})
        filtered = await call(client, "skolverket_pe_salsa", {"school_unit_codes": ["68613823", "36374693"]})
    assert_v4(router.requests[0], "statistics/all-schools/salsa")
    assert_v4(router.requests[1], "statistics/all-schools/salsa/99648792")
    assert ranked["time_period"] == "2022/23"
    assert ranked["total"] == 3 and ranked["truncated"] is True
    assert [r["school_unit_code"] for r in ranked["rows"]] == ["68653982", "68613823"]
    first = ranked["rows"][0]
    # Name and kommunkod come from the SALSA item itself.
    assert first["name"] == "Ankarskolan 4-9" and first["geographical_area_code"] == "1383"
    assert first["metrics"]["salsaAverageGradesIn9thGradeDeviation"]["number"] == -23
    assert ranked["metric_labels"]["salsaParentsEducation"].startswith("Föräldrarnas")
    (row,) = one["rows"]
    assert row["name"] == "Adolf Fredriks musikklasser" and row["metrics"]["salsaParentsEducation"]["number"] == 2.8
    assert empty["rows"] == [] and empty["total"] == 0
    assert any("Ingen SALSA för skolenheten" in n for n in empty["notes"])
    assert [r["school_unit_code"] for r in filtered["rows"]] == ["68613823", "36374693"]
    assert filtered["rows"][1]["metrics"]["salsaAverageGradesIn9thGradeActual"] == {
        "value": "..",
        "value_type": "OMITTED_DUE_TO_BASED_ON_FEW_PUPILS",
    }


async def test_salsa_area_filter_is_local(router, make_client):
    router.add("GET", r"/all-schools/salsa$", load_fixture("planned_educations_salsa.json"))
    async with make_client("skolverket") as client:
        municipality = await call(client, "skolverket_pe_salsa", {"geographical_area_code": "1383"})
        county = await call(
            client,
            "skolverket_pe_salsa",
            {"geographical_area_code": "03", "metrics": ["salsaAverageGradesIn9thGradeDeviation"]},
        )
    # No /school-units look-ups (previously up to 10 calls, silently capped at 1 000 units).
    assert [r.url.path for r in router.requests] == ["/planned-educations/v4/statistics/all-schools/salsa"] * 2
    assert [r["name"] for r in municipality["rows"]] == ["Väröbackaskolan 4-9", "Ankarskolan 4-9"]
    assert any("kommun 1383" in n for n in municipality["notes"])
    (heby,) = county["rows"]
    assert heby["school_unit_code"] == "36374693" and heby["geographical_area_code"] == "0331"
    assert list(heby["metrics"]) == ["salsaAverageGradesIn9thGradeDeviation"]
    assert list(county["metric_labels"]) == ["salsaAverageGradesIn9thGradeDeviation"]


async def test_salsa_output_size(router, make_client):
    salsa = load_fixture("planned_educations_salsa.json")
    template = salsa["body"]["compulsorySchoolUnitSalsaMetricList"]
    salsa["body"]["compulsorySchoolUnitSalsaMetricList"] = [
        dict(copy.deepcopy(template[i % 3]), schoolUnitCode=f"{10000000 + i}", name=f"Skolan nummer {i} 4-9")
        for i in range(1500)
    ]
    router.add("GET", r"/all-schools/salsa$", salsa)
    async with make_client("skolverket") as client:
        assert await output_chars(client, "skolverket_pe_salsa", {}) < DEFAULT_MAX_CHARS
        assert await output_chars(client, "skolverket_pe_salsa", {"limit": 500}) < ABSOLUTE_MAX_CHARS
        capped = await call(client, "skolverket_pe_salsa", {"limit": 500})
    assert len(capped["rows"]) == 100 and capped["total"] == 1500 and capped["truncated"] is True


async def test_surveys_nested_all_available(router, make_client):
    router.add("GET", r"/nestedsurveys$", load_fixture("planned_educations_surveys_index.json"))
    router.add("GET", r"/nestedsurveys/pupilsgr$", load_fixture("planned_educations_nested_pupilsgr.json"))
    router.add("GET", r"/nestedsurveys/custodiansgr$", load_fixture("planned_educations_nested_custodiansgr.json"))
    async with make_client("skolverket") as client:
        result = await call(client, "skolverket_pe_school_unit_surveys", {"school_unit_code": "44673074"})
    paths = [r.url.path for r in router.requests]
    assert paths == [
        "/planned-educations/v4/school-units/44673074/nestedsurveys",
        "/planned-educations/v4/school-units/44673074/nestedsurveys/custodiansgr",
        "/planned-educations/v4/school-units/44673074/nestedsurveys/pupilsgr",
    ]
    assert all(r.headers["accept"] == MEDIA_TYPE for r in router.requests)
    assert result["available_surveys"] == ["custodiansgr", "pupilsgr"]
    custodians, pupils = result["surveys"]
    (group,) = custodians["groups"]
    assert group["answers"] == "292" and group["semester"] == "VT26"
    assert group["questions"]["security"]["average_number"] == 8.8
    ak5, ak8 = pupils["groups"]
    assert ak5["school_year"] == "ak5" and ak5["answer_rate"] == "90%" and ak5["group_size"] == "50"
    satisfaction = ak5["questions"]["satisfaction"]
    assert satisfaction["average"] == "7,6" and satisfaction["average_number"] == 7.6
    assert satisfaction["subject"] == "Övergripande nöjdhet"
    assert satisfaction["ratios"]["ratioCorrespondsFully"] == "44%"
    assert satisfaction["ratios"]["ratioCorrespondsNotAtAll"] == "-"
    assert "ratioCorrespondsFully" not in ak5["questions"]["workingEnvironment"]["ratios"]
    assert ak5["questions"]["workingEnvironment"]["ratios"]["ratioOften"] == "44%"
    assert "recommend" not in ak8["questions"]  # all-null metric object dropped
    assert ak8["questions"]["inspiration"]["average_number"] == 6.3


async def test_surveys_flat_single_and_missing(router, make_client):
    router.add("GET", r"/surveys/pupilsgr$", load_fixture("planned_educations_flat_pupilsgr.json"))
    router.add("GET", r"/surveys/pupilsgy$", httpx2.Response(404, json={"message": "Not Found"}))
    async with make_client("skolverket") as client:
        flat = await call(
            client,
            "skolverket_pe_school_unit_surveys",
            {"school_unit_code": "44673074", "survey": "pupils_GR", "format": "flat"},
        )
        missing = await call(
            client, "skolverket_pe_school_unit_surveys", {"school_unit_code": "44673074", "survey": "pupilsgy"}
        )
        err = await call_error(
            client, "skolverket_pe_school_unit_surveys", {"school_unit_code": "44673074", "survey": "teachersgr"}
        )
    assert_v4(router.requests[0], "school-units/44673074/surveys/pupilsgr")
    assert_v4(router.requests[1], "school-units/44673074/nestedsurveys/pupilsgy")
    (row,) = flat["surveys"][0]["rows"]
    assert row["satisfactionAverage"] == "7,6" and "recommendAverage" not in row
    assert missing["surveys"][0]["note"].startswith("Inga resultat")
    assert "Okänd enkät" in err and "pupilsgr" in err


async def test_school_unit_education_events_map_by_school_form(router, make_client):
    router.add(
        "GET", r"/30530571/education-events$", load_fixture("planned_educations_school_unit_education_events.json")
    )
    async with make_client("skolverket") as client:
        listing = await call(client, "skolverket_pe_school_unit_education_events", {"school_unit_code": "30530571"})
        gyan = await call(
            client,
            "skolverket_pe_school_unit_education_events",
            {"school_unit_code": "30530571", "type_of_schooling": "gyan"},
        )
    request = router.requests[0]
    assert_v4(request, "school-units/30530571/education-events")
    # The spec lists no query parameters for this path.
    assert not request.url.params
    # {"gy": [8 programmes]} is flattened into 8 rows (previously 1 row holding the whole map).
    assert listing["total"] == 8 and listing["truncated"] is False and len(listing["rows"]) == 8
    assert [r["study_path_code"] for r in listing["rows"]] == [
        "BF25",
        "EK25",
        "FS25",
        "HT25",
        "IN25",
        "RL25",
        "SA25",
        "VO25",
    ]
    assert all(r["type_of_schooling"] == "gy" for r in listing["rows"])
    first = listing["rows"][0]
    assert first == {
        "id": "edu-event-30530571-BF25",
        "school_unit_code": "30530571",
        "school_unit_name": "af Chapmangymnasiet",
        "study_path_code": "BF25",
        "study_path_name": "Barn- och fritidsprogrammet",
        "type_of_schooling": "gy",
        "study_path_category": "VOCATIONAL_PROGRAM",
        "school_orientation": "Ej relevant",
        "principal_organizer_type": "Kommun",
        "geographical_area_code": "1080",
        "city": "Karlskrona",
        "resursskola": False,
        "start_date": "2026-07-31 00:00:00",
        "end_date": "2027-07-30 22:00:00",
        "admission_points_min": 105.0,
        "admission_points_average": 165.8,
        "admission_points_year": "2025",
    }
    fs = listing["rows"][2]
    assert fs["study_paths"] == [{"code": "FS25SKOLFORLAGD", "name": "Försäljnings- och serviceprogrammet"}]
    assert "admission_points_min" not in fs
    assert gyan["rows"] == [] and gyan["total"] == 0


async def test_school_unit_education_events_single_and_compact(router, make_client):
    router.add("GET", r"/30530571/education-events/EK25$", load_fixture("planned_educations_education_event.json"))
    router.add(
        "GET", r"/77364720/compact-education-events\?", load_fixture("planned_educations_compact_education_events.json")
    )
    async with make_client("skolverket") as client:
        one = await call(
            client,
            "skolverket_pe_school_unit_education_events",
            {"school_unit_code": "30530571", "study_path_code": "ek25"},
        )
        compact = await call(
            client,
            "skolverket_pe_school_unit_education_events",
            {"school_unit_code": "77364720", "compact": True, "type_of_schooling": "gy", "sort": "studyPathName"},
        )
        err = await call_error(
            client, "skolverket_pe_school_unit_education_events", {"school_unit_code": "30530571", "sort": "name"}
        )
        err2 = await call_error(
            client,
            "skolverket_pe_school_unit_education_events",
            {"school_unit_code": "30530571", "study_path_code": "EK25", "compact": True},
        )
    assert_v4(router.requests[0], "school-units/30530571/education-events/EK25")
    assert not router.requests[0].url.params
    (row,) = one["rows"]
    assert one["total"] == 1 and one["study_path_code"] == "EK25"
    assert row["study_path_code"] == "EK25" and row["street"] == "Drottninggatan 49"
    assert row["admission_points_min"] == 215.0 and "id" not in row
    assert_v4(router.requests[1], "school-units/77364720/compact-education-events")
    params = router.requests[1].url.params
    assert params["typeOfSchooling"] == "gy" and params["sort"] == "studyPathName" and params["size"] == "50"
    assert compact["variant"] == "compact" and compact["total"] == 3
    assert compact["rows"][0] == {
        "school_unit_code": "77364720",
        "school_unit_name": "ABF Stockholms gymnasium",
        "study_path_name": "Ekonomiprogrammet",
        "type_of_schooling": "gy",
    }
    assert "compact=true" in err and "compact=true" in err2
    assert len(router.requests) == 2


async def test_search_education_events_and_count(router, make_client):
    router.add("GET", r"/v4/education-events\?", load_fixture("planned_educations_education_events.json"))
    async with make_client("skolverket") as client:
        result = await call(
            client,
            "skolverket_pe_search_education_events",
            {
                "study_path_code": "VI25",
                "geographical_area_code": "0180",
                "principal_organizer_type": "ENSKILD",
                "school_orientation": "ALLMAN",
                "type_of_schooling": "gy",
                "resursskola": False,
                "latitude": 59.3,
                "longitude": 18.0,
                "radius_km": 10,
                "sort": "relevance,desc",
            },
        )
        count = await call(
            client, "skolverket_pe_search_education_events", {"study_path_code": "VI25", "count_only": True}
        )
    params = router.requests[0].url.params
    assert_v4(router.requests[0], "education-events")
    assert params["studyPathCode"] == "VI25" and params["geographicalAreaCode"] == "0180"
    assert params["principalOrganizerType"] == "ENSKILD" and params["schoolOrientation"] == "ALLMAN"
    assert params["typeOfSchooling"] == "gy" and params["distance"] == "10"
    assert params["resursskolaFilter"] == "false" and params["sort"] == "relevance,desc"
    assert "coordinateSystemType" not in params
    assert result["total"] == 7116 and result["truncated"] is True
    first = result["rows"][0]
    assert first["school_unit_code"] == "79033948" and first["school_unit_name"] == "NTI Gymnasiet Helsingborg"
    assert first["study_path_code"] == "VI25" and first["geographical_area_code"] == "1283"
    assert first["study_paths"] == [{"code": "VIINO", "name": "Informationsteknik"}]
    # count_only reads page.totalElements of a 1-row page: no undocumented /education-events/count call.
    assert len(router.requests) == 2
    assert_v4(router.requests[1], "education-events")
    assert router.requests[1].url.params["size"] == "1" and router.requests[1].url.params["page"] == "0"
    assert count["total"] == 7116 and count["count_source"] == "page.totalElements" and count["rows"] == []


async def test_search_education_events_new_endpoint(router, make_client):
    router.add("GET", r"/v4/education-events-new\?", load_fixture("planned_educations_education_events.json"))
    router.add("GET", r"/v4/education-events-new-count\?", envelope(7122))
    async with make_client("skolverket") as client:
        result = await call(
            client, "skolverket_pe_search_education_events", {"program_variants": ["LU", "NIU"], "size": 2}
        )
        count = await call(
            client, "skolverket_pe_search_education_events", {"program_variants": "LU", "count_only": True}
        )
        err = await call_error(client, "skolverket_pe_search_education_events", {"compact": True, "count_only": True})
    assert_v4(router.requests[0], "education-events-new")
    assert router.requests[0].url.params["ProgramVariantList"] == "LU,NIU"
    assert result["variant"] == "new" and len(result["rows"]) == 2
    assert_v4(router.requests[1], "education-events-new-count")
    assert "page" not in router.requests[1].url.params
    assert router.requests[1].url.params["ProgramVariantList"] == "LU"
    assert count["total"] == 7122 and count["count_source"] == "education-events-new-count"
    assert "compact" in err


async def test_search_education_events_output_size(router, make_client):
    page = load_fixture("planned_educations_education_events.json")
    item = page["body"]["_embedded"]["educationEvents"][0]

    def respond(request: httpx2.Request) -> httpx2.Response:
        size = int(request.url.params["size"])
        body = copy.deepcopy(page)
        body["body"]["_embedded"]["educationEvents"] = [copy.deepcopy(item) for _ in range(size)]
        return httpx2.Response(200, json=body)

    router.add("GET", r"/v4/education-events\?", respond)
    async with make_client("skolverket") as client:
        assert await output_chars(client, "skolverket_pe_search_education_events", {}) < DEFAULT_MAX_CHARS
        assert await output_chars(client, "skolverket_pe_search_education_events", {"size": 1000}) < ABSOLUTE_MAX_CHARS
    assert router.last().url.params["size"] == "100"


async def test_search_adult_education_events(router, make_client):
    router.add("GET", r"/v4/adult-education-events\?", load_fixture("planned_educations_adult_events.json"))
    router.add("GET", r"/v4/adult-education-events-count", envelope(42))
    args = {
        "search_term": "svenska",
        "town": ["Solna", "Göteborg"],
        "municipality": "Göteborg",
        "geographical_area_code": ["1480", "1402"],
        "type_of_school": "komvuxgycourses",
        "direction_ids": [11, 12],
        "instruction_languages": "swe, ENG",
        "semester_start_from": "2026-08-01 to 2026-12-31",
        "distance_learning": True,
        "pace_of_study": "50-100",
        "execution_condition": 2,
        "recommended_prior_knowledge": "grundlaggande",
        "sort": "titleSv:asc , typeOfSchool:desc",
    }
    async with make_client("skolverket") as client:
        result = await call(client, "skolverket_pe_search_adult_education_events", args)
        count = await call(client, "skolverket_pe_search_adult_education_events", {**args, "count_only": True})
        err = await call_error(
            client, "skolverket_pe_search_adult_education_events", {"semester_start_from": "hösten 2026"}
        )
        err2 = await call_error(client, "skolverket_pe_search_adult_education_events", {"execution_condition": "5"})
    request = router.requests[0]
    assert_v4(request, "adult-education-events")
    params = request.url.params
    assert params["searchTerm"] == "svenska" and params["town"] == "Solna,Göteborg"
    assert params["municipality"] == "Göteborg"
    assert params.get_list("geographicalAreaCode") == ["1480", "1402"]
    assert params["directionIds"] == "11,12" and params["instructionLanguages"] == "swe,eng"
    # Spec format: no spaces, upper-case TO.
    assert params["semesterStartFrom"] == "2026-08-01TO2026-12-31"
    # Retired code translated to the current id ("Använd inte de gamla koderna").
    assert params["typeOfSchool"] == "vuxgy"
    assert params["distance"] == "true" and params["paceOfStudy"] == "50-100"
    assert params["executionCondition"] == "2" and params["recommendedPriorKnowledge"] == "grundlaggande"
    assert params["sort"] == "titleSv:asc,typeOfSchool:desc"
    assert result["total"] == 337133 and result["truncated"] is True and len(result["rows"]) == 20
    first = result["rows"][0]
    assert first["education_event_id"] == "e.myh.27538" and first["title"] == ".NET Cloud Developer"
    assert first["distance_learning"] is False and first["execution_condition"] == 1
    assert first["pace_of_study"] == "100.0" and first["credits_system"] == "yh"
    rows = {r["education_event_id"]: r for r in result["rows"]}
    # CDATA wrapper and the string "null" are cleaned.
    assert rows["e.IST.33006RLXACNVUXY251BAG"]["title"] == "Bagare och konditor"
    flex = rows["e.IST.518107VUXFLEXENG05"]
    assert flex["title"] == "Engelska 5" and "pace_of_study" not in flex and flex["execution_condition"] == 2
    assert rows["e.uoh.hj.lgrg14.12887.20252"]["recommended_prior_knowledge"] == "grundlaggande"
    count_request = router.requests[1]
    assert_v4(count_request, "adult-education-events-count")
    assert "page" not in count_request.url.params and "sort" not in count_request.url.params
    assert count_request.url.params["searchTerm"] == "svenska"
    assert count == {
        "total": 42,
        "count_only": True,
        "page": 0,
        "page_size": 0,
        "truncated": False,
        "rows": [],
        "source_url": str(count_request.url),
    }
    assert "semester_start_from" in err and "execution_condition" in err2
    assert len(router.requests) == 2


async def test_adult_event_detail_and_areas(router, make_client):
    router.add("GET", r"/adult-education-events/e\.myh\.27538$", load_fixture("planned_educations_adult_event.json"))
    router.add("GET", r"/adult-education-events/areas$", load_fixture("planned_educations_areas.json"))
    async with make_client("skolverket") as client:
        detail = await call(client, "skolverket_pe_get_adult_education_event", {"education_event_id": "e.myh.27538"})
        opted_in = await call(
            client,
            "skolverket_pe_get_adult_education_event",
            {"education_event_id": "e.myh.27538", "include_personal_data": True},
        )
        areas = await call(client, "skolverket_pe_adult_education_areas", {"search": "sköterska"})
        everything = await call(client, "skolverket_pe_adult_education_areas", {})
    assert_v4(router.requests[0], "adult-education-events/e.myh.27538")
    assert_v4(router.requests[2], "adult-education-events/areas")
    assert detail["title"] == ".NET Cloud Developer" and detail["study_path_name"] == ".NET Cloud Developer"
    assert detail["organizer_name"] == "IT-Högskolan Sverige AB (Enskild)"
    assert detail["eligible_for_student_aid"] is True and detail["execution_condition"] == 0
    assert detail["credits"] == "400" and detail["credits_system"] == "yh"
    assert detail["contact"]["addresses"][0]["zip_code"] == "41265"
    assert urlsplit(detail["contact"]["web"]).hostname == "www.iths.se"
    assert "email" not in detail["contact"] and any("personuppgift" in n for n in detail["notes"])
    assert detail["description"].startswith("Cloud development")
    assert "other" not in detail
    assert opted_in["contact"]["email"] == "info@iths.se" and "notes" not in opted_in
    assert areas["total"] == 1
    assert areas["areas"][0]["name"] == "Medicin och vård"
    assert areas["areas"][0]["directions"] == [{"direction_id": 103, "name": "Sjuksköterska"}]
    assert everything["total"] == 21


async def test_personal_data_off_notes_do_not_suggest_opt_in(router, make_client):
    router.add("GET", r"/v4/school-units/76026322$", load_fixture("planned_educations_school_unit.json"))
    router.add("GET", r"/adult-education-events/e\.myh\.27538$", load_fixture("planned_educations_adult_event.json"))
    async with make_client("skolverket", allow_personal_data=False) as client:
        unit = await call(
            client, "skolverket_pe_get_school_unit", {"school_unit_code": "76026322", "include_raw": True}
        )
        event = await call(client, "skolverket_pe_get_adult_education_event", {"education_event_id": "e.myh.27538"})
        refused = await call_error(
            client, "skolverket_pe_get_school_unit", {"school_unit_code": "76026322", "include_personal_data": True}
        )
    assert "FUZZY_MCP_PERSONAL_DATA=off" in refused
    for detail in (unit, event):
        assert detail["notes"] == [PERSONAL_DATA_OFF_NOTE]
        assert "include_personal_data" not in json.dumps(detail, ensure_ascii=False)
    assert "email" not in unit and "contactInfo" not in unit["raw"] and "efternamn" not in json.dumps(unit)
    assert "email" not in event["contact"] and urlsplit(event["contact"]["web"]).hostname == "www.iths.se"
    # Default installation: the note keeps pointing to the opt-in.
    async with make_client("skolverket") as client:
        default = await call(client, "skolverket_pe_get_adult_education_event", {"education_event_id": "e.myh.27538"})
    assert default["notes"] == [PERSONAL_DATA_NOTE] and "include_personal_data=true" in PERSONAL_DATA_NOTE


@pytest.mark.parametrize("event_id", [".", "..", "abc", "../x", "a/b/c/d", ". . ."])
async def test_adult_event_id_validation(router, make_client, event_id):
    async with make_client("skolverket") as client:
        err = await call_error(client, "skolverket_pe_get_adult_education_event", {"education_event_id": event_id})
    assert "educationEventId" in err
    assert router.requests == []


async def test_support_lists(router, make_client):
    router.add("GET", r"/support/geographical-areas$", load_fixture("planned_educations_geographical_areas.json"))
    router.add("GET", r"/support/programs$", load_fixture("planned_educations_programs.json"))
    router.add(
        "GET", r"/support/adultTypeOfSchooling$", load_fixture("planned_educations_adult_type_of_schooling.json")
    )
    router.add("GET", r"/support/variants/\?", load_fixture("planned_educations_variants.json"))
    router.add(
        "GET",
        r"/support/principal-organizer-types$",
        load_fixture("planned_educations_principal_organizer_types.json"),
    )
    router.add("GET", r"/v4/api-info$", {"apiName": "planned-educations", "apiVersion": "4", "apiStatus": "active"})
    async with make_client("skolverket") as client:
        areas = await call(client, "skolverket_pe_support_list", {"list": "geographical-areas", "search": "alingsas"})
        programs = await call(client, "skolverket_pe_support_list", {"list": "programs"})
        adult = await call(client, "skolverket_pe_support_list", {"list": "adultTypeOfSchooling", "search": "fhsk"})
        variants = await call(client, "skolverket_pe_support_list", {"list": "variants", "type_of_schooling": "gyan"})
        organizers = await call(client, "skolverket_pe_support_list", {"list": "principal-organizer-types"})
        info = await call(client, "skolverket_pe_support_list", {"list": "api-info"})
    assert_v4(router.requests[0], "support/geographical-areas")
    assert_v4(router.requests[2], "support/adultTypeOfSchooling")
    assert_v4(router.requests[3], "support/variants/")
    assert router.requests[3].url.params["typeOfSchooling"] == "GYAN"
    assert_v4(router.requests[5], "api-info")
    assert areas["entries"] == [
        {"code": "1489", "name": "Alingsås", "areaType": "MUNICIPALITY"},
        {"code": "1489-Alingsås", "name": "Alingsås", "areaType": "TOWN"},
    ]
    assert areas["total"] == 2
    bf = programs["entries"][0]
    assert (
        bf["group"] == "gy"
        and bf["code"] == "BF"
        and bf["studyPaths"][0] == {"code": "BFFRH", "name": "Fritid och hälsa"}
    )
    assert adult["entries"] == [
        {"id": "fhsaub", "value": "Folkhögskola (Arbetsmarknadsutbildning)", "old code": "fhskaub"},
        {"id": "fhs", "value": "Folkhögskola", "old code": "fhsk"},
    ]
    assert variants["entries"][1] == {"code": "LU", "name": "Lärlingsutbildning", "value": "Lärlingsutbildning, LU"}
    assert variants["total"] == 7
    assert [e["name"] for e in organizers["entries"]] == [
        "Kommunal",
        "Region",
        "Statlig",
        "Sameskolan",
        "Fristående",
        "Uppgift saknas",
    ]
    assert info["entries"][0]["apiVersion"] == "4"


async def test_support_list_output_size(router, make_client):
    shape = load_fixture("planned_educations_municipality_schoolunit.json")
    unit = shape["body"]["schoolUnits"][0]
    area = shape["body"]["geographicalAreas"][0]
    shape["body"]["schoolUnits"] = [
        dict(unit, code=f"{20000000 + i}", name=f"Exempelskolan nummer {i} i en kommun") for i in range(6500)
    ]
    shape["body"]["geographicalAreas"] = [dict(area, code=f"{i:04d}") for i in range(300)]
    router.add("GET", r"/support/municipality-schoolunit$", shape)
    async with make_client("skolverket") as client:
        default = await output_chars(client, "skolverket_pe_support_list", {"list": "municipality-schoolunit"})
        maximum = await output_chars(
            client, "skolverket_pe_support_list", {"list": "municipality-schoolunit", "limit": 100_000}
        )
        result = await call(client, "skolverket_pe_support_list", {"list": "municipality-schoolunit", "limit": 2000})
    assert default < DEFAULT_MAX_CHARS and maximum < ABSOLUTE_MAX_CHARS
    assert result["total"] == 6800 and result["truncated"] is True
    assert result["entries"][0] == {
        "group": "schoolUnits",
        "code": "20000000",
        "name": "Exempelskolan nummer 0 i en kommun",
        "typeOfSchooling": "UPPER_SECONDARY_EDUCATION",
    }
    assert len(json.dumps(result["entries"], ensure_ascii=False)) < ABSOLUTE_MAX_CHARS


async def test_compare_secondary_posts_program_pairs(router, make_client):
    router.add("POST", r"/v4/school-unit-secondary$", load_fixture("planned_educations_school_unit_secondary.json"))
    async with make_client("skolverket") as client:
        result = await call(
            client,
            "skolverket_pe_compare_secondary",
            {"items": [{"school_unit_code": "77364720", "study_path_code": "ek"}]},
        )
        err = await call_error(client, "skolverket_pe_compare_secondary", {"items": []})
    request = router.requests[0]
    assert request.method == "POST"
    assert_v4(request, "school-unit-secondary")
    assert router.json_body(0) == [{"schoolUnitCode": "77364720", "studyPathCode": "EK", "typeOfSchooling": "gy"}]
    assert result["result"] == [{"schoolUnitCode": "77364720", "studyPathCode": "EK", "typeOfSchooling": "gy"}]
    assert "minst en" in err


async def test_school_unit_documents(router, make_client):
    router.add("GET", r"/76026322/documents$", load_fixture("planned_educations_documents.json"))
    router.add("GET", r"/76026322/documents/gr$", load_fixture("planned_educations_document_group.json"))
    router.add("GET", r"/11111111/documents$", httpx2.Response(404, json={"message": "Not Found"}))
    async with make_client("skolverket") as client:
        docs = await call(client, "skolverket_pe_school_unit_documents", {"school_unit_code": "76026322"})
        gr = await call(
            client,
            "skolverket_pe_school_unit_documents",
            {"school_unit_code": "76026322", "type_of_schooling": "Grundskola"},
        )
        limited = await call(
            client, "skolverket_pe_school_unit_documents", {"school_unit_code": "76026322", "limit": 3}
        )
        none = await call(client, "skolverket_pe_school_unit_documents", {"school_unit_code": "11111111"})
        err = await call_error(
            client, "skolverket_pe_school_unit_documents", {"school_unit_code": "76026322", "type_of_schooling": "sfi"}
        )
    assert_v4(router.requests[0], "school-units/76026322/documents")
    assert_v4(router.requests[1], "school-units/76026322/documents/gr")
    assert docs["total"] == 4 and docs["truncated"] is False
    assert [g["type_of_schooling"] for g in docs["groups"]] == ["fsk", "gr"]
    first = docs["groups"][0]["documents"][0]
    assert first["typeId"] == "SCHOOL_SURVEY" and first["url"].endswith("docID=671068")
    assert "_links" not in docs["groups"][0]
    (group,) = gr["groups"]
    assert group["type_of_schooling"] == "gr" and len(group["documents"]) == 2
    assert limited["total"] == 4 and limited["truncated"] is True
    assert [len(g["documents"]) for g in limited["groups"]] == [2, 1]
    assert none["total"] == 0 and none["notes"]
    # 'sfi' is outside the spec enum (gy, gr, gyan, gran, fsk): rejected without a request.
    assert "Okänd skolform" in err
    assert len(router.requests) == 4


async def test_codes_resource(make_client):
    async with make_client("skolverket") as client:
        resources = (await client.list_resources()).resources
        assert any(str(r.uri) == "fuzzy://skolverket/planned-educations/codes" for r in resources)
        res = await client.read_resource("fuzzy://skolverket/planned-educations/codes")
    text = res.contents[0].text
    codes = json.loads(text)
    assert '"vuxgyan"' in text and '"custodiansfsk"' in text and '"NA25"' in text
    assert "OMITTED_DUE_TO_BASED_ON_FEW_PUPILS" in text and "MUNICIPALITY" in text
    assert codes["principal_organizer_types"] == [
        "Kommunal",
        "Region",
        "Statlig",
        "Sameskolan",
        "Fristående",
        "Uppgift saknas",
    ]
    adult_ids = {a["id"] for a in codes["adult_type_of_school"]}
    assert {"vuxgy", "vuxgr", "vuxsfi", "fhs", "yh"} <= adult_ids and "fhsk" not in adult_ids
    assert codes["adult_execution_conditions"]["0"] == "Ej fastställt"
    assert {"vux", "vuxgrs", "vuxgys"} <= {t["code"] for t in codes["type_of_schooling"]}
