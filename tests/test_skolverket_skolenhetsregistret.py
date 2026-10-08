# SPDX-FileCopyrightText: 2026 Deniz Özer
#
# SPDX-License-Identifier: MIT

import json

import httpx2
import pytest

from fuzzy_mcp.errors import InvalidInputError
from fuzzy_mcp.sources.skolverket.skolenhetsregistret import (
    CHANGES_MAX_BYTES,
    CODE_LISTS,
    MAX_LIMIT_CONTRACTS,
    MAX_LIMIT_EDUCATION_PROVIDERS,
    MAX_LIMIT_ORGANIZERS,
    MAX_LIMIT_SCHOOL_UNITS,
    fold,
    iso_date,
    normalize_codes,
    normalize_municipality_code,
    normalize_organization_number,
    normalize_school_unit_code,
    postal_code,
    redact_personal,
    resolve_status,
    to_float,
)

from .conftest import call, call_error, load_fixture

pytestmark = pytest.mark.anyio

BASE = "https://api.skolverket.se/skolenhetsregistret/v2"
ALL_STATUSES = ["AKTIV", "VILANDE", "UPPHORT", "PLANERAD"]
LIST_UNITS = r"/v2/school-units(\?|$)"
PROBLEM_404 = {"type": "about:blank", "title": "Not Found", "status": "404", "detail": "Angiven resurs hittades inte"}


def detail_router(router, details: dict[str, str]):
    """Route /v2/school-units/{code} to a fixture per code (others → 404 problem JSON)."""

    def respond(request: httpx2.Request) -> httpx2.Response:
        code = request.url.path.rsplit("/", 1)[-1]
        if code in details:
            payload = load_fixture(details[code])
            payload["data"]["schoolUnitCode"] = code
            return httpx2.Response(200, json=payload)
        return httpx2.Response(404, json=PROBLEM_404)

    router.add("GET", r"/v2/school-units/\d{8}", respond)


# --- pure helpers ---------------------------------------------------------------------------------


def test_helpers_normalise_inputs():
    assert normalize_organization_number("212000-0126") == "2120000126"
    assert normalize_organization_number("16 212000-0126") == "2120000126"
    assert normalize_municipality_code("180") == "0180"
    assert iso_date("20240301", "x") == "2024-03-01"
    assert iso_date("2024-03-01T12:00:00", "x") == "2024-03-01"
    assert to_float("6562397,097") == 6562397.097
    assert to_float("59.1628") == 59.1628
    assert to_float("") is None and to_float(None) is None and to_float("n/a") is None
    assert postal_code("17998") == "179 98" and postal_code("234 67") == "234 67"
    assert fold("Färentuna Skola") == "farentuna skola"
    assert normalize_codes(["gr", "Grundskola", "komvux", "Anpassad gymnasieskola"], "school_type", "x") == [
        "GR",
        "VUX",
        "GYAN",
    ]
    assert normalize_codes("aktiv, upphörd", "school_unit_status", "status") == ["AKTIV", "UPPHORT"]


@pytest.mark.parametrize(
    ("func", "value"),
    [
        (normalize_school_unit_code, "４３０３８６６２"),  # full-width digits
        (normalize_school_unit_code, "٤٣٠٣٨٦٦٢"),  # Arabic-Indic digits
        (normalize_organization_number, "٢١٢٠٠٠٠١٢٦"),
        (normalize_municipality_code, "٠١٨٠"),
        (normalize_municipality_code, "１８０"),
        (lambda v: iso_date(v, "x"), "٢٠٢٤-٠٣-٠١"),
    ],
)
def test_validation_accepts_only_ascii_digits(func, value):
    with pytest.raises(InvalidInputError):
        func(value)


def test_resolve_status_all_is_explicit():
    assert resolve_status(None, ("AKTIV",)) == ["AKTIV"]
    assert resolve_status("ALLA", ("AKTIV",)) == ALL_STATUSES
    assert resolve_status([], ("AKTIV",)) == ALL_STATUSES
    assert resolve_status(["aktiv", "*"], ("AKTIV",)) == ALL_STATUSES
    assert resolve_status("vilande", ()) == ["VILANDE"]


def test_redact_personal_is_deep_and_does_not_mutate():
    payload = {
        "data": {
            "attributes": {
                "headMaster": "Förnamn Efternamn",
                "addresses": [{"careOfAddress": "CO-EXEMPEL", "streetAddress": "Agatan 1"}],
            }
        }
    }
    redacted = redact_personal(payload)
    assert redacted == {"data": {"attributes": {"addresses": [{"streetAddress": "Agatan 1"}]}}}
    assert payload["data"]["attributes"]["headMaster"] == "Förnamn Efternamn"
    assert payload["data"]["attributes"]["addresses"][0]["careOfAddress"] == "CO-EXEMPEL"


def test_code_lists_cover_spec_enums():
    codes = {key: [c["code"] for c in spec["codes"]] for key, spec in CODE_LISTS.items()}
    assert codes["school_unit_status"] == ["AKTIV", "VILANDE", "UPPHORT", "PLANERAD"]
    assert codes["school_type"] == ["FKLASS", "FTH", "OPPFTH", "GR", "GRAN", "SP", "SAM", "GY", "GYAN", "VUX"]
    assert codes["school_type_part_vux"] == ["VUXGR", "VUXGY", "VUXGRAN", "VUXGYAN", "VUXSFI"]
    assert codes["school_unit_type"] == ["SKOLENHET", "CENTRAL", "UTLAND"]
    assert codes["organizer_type"] == ["KOMMUN", "REGION", "STAT", "SAME", "ENSKILD", "SPECIAL", "KOMMFORB", "HMANUTL"]
    assert {"orientation_type", "legal_entity_status", "company_status", "report_type", "address_type"} <= set(codes)


# --- school units -----------------------------------------------------------------------------------


async def test_search_school_units_server_filters_and_name_match(router, make_client):
    router.add("GET", LIST_UNITS, load_fixture("skolenhetsregistret_school_units.json"))
    async with make_client("skolverket") as client:
        result = await call(
            client,
            "skolverket_search_school_units",
            {
                "name": "FARENTUNA",
                "municipality_code": ["125"],
                "school_type": ["grundskola", "fklass"],
                "school_unit_type": "SKOLENHET",
                "organization_number": "212000-0126",
                "modified_since": "2025-01-01",
            },
        )
    request = router.last()
    assert str(request.url).startswith(f"{BASE}/school-units?")
    assert request.headers["accept"] == "application/json"
    params = request.url.params
    assert params.get_list("status") == ["AKTIV"]  # default
    assert params.get_list("municipality_code") == ["0125"]
    assert params.get_list("school_type") == ["GR", "FKLASS"]
    assert params.get_list("school_unit_type") == ["SKOLENHET"]
    assert params.get_list("organization_number") == ["2120000126"]
    assert params["meta_modified_after"] == "2025-01-01"
    assert result["total"] == 1 and result["truncated"] is False
    assert result["extract_date"] == "2025-05-08T00:06:10.19+02:00"
    row = result["items"][0]
    # Single-valued server-side filters are known for every returned row.
    assert row == {
        "school_unit_code": "43038662",
        "name": "Färentuna skola",
        "status": "AKTIV",
        "municipality_code": "0125",
        "organizer_organization_number": "2120000126",
    }
    assert result["filters"]["name"] == "FARENTUNA"


async def test_search_school_units_repeated_status_ranking_and_paging(router, make_client):
    router.add("GET", LIST_UNITS, load_fixture("skolenhetsregistret_school_units.json"))
    async with make_client("skolverket") as client:
        ranked = await call(client, "skolverket_search_school_units", {"name": "ska", "status": ["aktiv", "Vilande"]})
        assert router.last().url.params.get_list("status") == ["AKTIV", "VILANDE"]
        # "Skå skola" starts with the query (å folded), Kunskapsskolan only contains it.
        assert [i["name"] for i in ranked["items"]] == ["Skå skola", "Kunskapsskolan Tumba"]

        page = await call(client, "skolverket_search_school_units", {"status": "ALLA", "limit": 2, "offset": 1})
        # "All" is sent explicitly: an unfiltered list has only been seen to hold AKTIV and VILANDE units.
        assert router.last().url.params.get_list("status") == ALL_STATUSES
        assert page["filters"]["status"] == ALL_STATUSES
        assert page["total"] == 6 and page["truncated"] is True and page["next_offset"] == 3
        assert [i["school_unit_code"] for i in page["items"]] == ["12345678", "84411355"]

        everything = await call(client, "skolverket_search_school_units", {"limit": 9999})
        assert everything["limit"] == MAX_LIMIT_SCHOOL_UNITS == 300
        assert everything["truncated"] is False and "next_offset" not in everything


async def test_search_school_units_with_details(router, make_client):
    router.add("GET", LIST_UNITS, load_fixture("skolenhetsregistret_school_units.json"))
    detail_router(
        router,
        {
            "43038662": "skolenhetsregistret_school_unit_minimal.json",
            "12345678": "skolenhetsregistret_school_unit.json",
        },
    )
    async with make_client("skolverket") as client:
        result = await call(client, "skolverket_search_school_units", {"limit": 3, "with_details": True})
    first, second, third = result["items"]
    assert first["school_types"] == ["GR", "FKLASS"] and first["municipality_code"] == "0125"
    assert first["city"] == "Färentuna" and first["latitude"] == 59.393258 and first["longitude"] == 17.662399
    assert first["organizer_name"] == "EKERÖ KOMMUN" and "organizer_organization_number" not in first
    assert second["organizer_organization_number"] == "1234567890" and second["city"] == "Borås"
    assert "latitude" not in third  # detail call failed (404) → row kept, note added
    assert result["notes"] == ["Detaljer kunde inte hämtas för: 84411355"]
    detail_paths = sorted(r.url.path for r in router.requests if r.url.path.count("/") == 4)
    assert detail_paths == [
        "/skolenhetsregistret/v2/school-units/12345678",
        "/skolenhetsregistret/v2/school-units/43038662",
        "/skolenhetsregistret/v2/school-units/84411355",
    ]


async def test_search_school_units_validation(router, make_client):
    async with make_client("skolverket") as client:
        err = await call_error(client, "skolverket_search_school_units", {"school_type": ["förskola"]})
        assert "Okänt värde 'förskola' för school_type" in err and "FKLASS (Förskoleklass)" in err
        err = await call_error(client, "skolverket_search_school_units", {"status": "STÄNGD"})
        assert "Giltiga koder: AKTIV" in err
        err = await call_error(client, "skolverket_search_school_units", {"municipality_code": "12"})
        assert "Ogiltig kommunkod" in err
        err = await call_error(client, "skolverket_search_school_units", {"organization_number": "123"})
        assert "Ogiltigt organisationsnummer" in err
        err = await call_error(client, "skolverket_search_school_units", {"modified_since": "2025-13-01"})
        assert "Ogiltigt datum" in err
    assert router.requests == []


async def test_search_school_units_upstream_400(router, make_client):
    problem = {"type": "about:blank", "title": "Bad Request", "status": 400, "detail": "Ogiltigt värde för status"}
    router.add("GET", LIST_UNITS, httpx2.Response(400, json=problem))
    async with make_client("skolverket") as client:
        err = await call_error(client, "skolverket_search_school_units", {})
    assert "[skolverket]" in err and "HTTP 400" in err and "Ogiltigt värde för status" in err


async def test_get_school_unit_flattens_detail(router, make_client):
    router.add("GET", r"/v2/school-units/12345678", load_fixture("skolenhetsregistret_school_unit.json"))
    async with make_client("skolverket") as client:
        unit = await call(
            client, "skolverket_get_school_unit", {"school_unit_code": " 12345678 ", "as_of_date": "20240101"}
        )
    request = router.last()
    assert request.url.path == "/skolenhetsregistret/v2/school-units/12345678"
    assert request.url.params["search_date"] == "2024-01-01"
    assert unit["school_unit_code"] == "12345678" and unit["name"] == "Storskolan"
    assert unit["status"] == "AKTIV" and unit["status_label"] == "Aktiv"
    assert unit["organizer"] == {
        "organization_number": "1234567890",
        "name": "Enskilda gymnasiet",
        "organizer_type": "ENSKILD",
        "organizer_type_label": "Enskild",
    }
    visit = unit["visit_address"]
    assert visit["type"] == "BESOKSADRESS" and visit["street"] == "Agatan 1" and visit["postal_code"] == "234 67"
    assert visit["latitude"] == 59.16286545184274 and visit["longitude"] == 18.134313905557242
    assert visit["sweref99tm_n"] == 6562397.097 and visit["sweref99tm_e"] == 679180.062
    assert unit["latitude"] == 59.16286545184274 and unit["longitude"] == 18.134313905557242
    assert unit["other_addresses"] == [
        {
            "type": "POSTADRESS",
            "street": "Box 123",
            "postal_code": "501 10",
            "locality": "Borås",
            "continent": "Europa",
        }
    ]
    types = {t["code"]: t for t in unit["school_types"]}
    assert list(types) == ["GR", "FKLASS", "FTH", "GY", "VUX"]
    assert types["GR"]["grades"] == [str(i) for i in range(1, 10)] and types["GR"]["label"] == "Grundskola"
    assert types["GY"]["programmes"] == ["BA", "NA"] and types["GY"]["csn_code"] == "21"
    assert types["VUX"]["school_type_parts"] == ["VUXGR", "VUXSFI"]
    assert types["FKLASS"] == {"code": "FKLASS", "label": "Förskoleklass"}
    # Personal data (principal's name, c/o names) is not returned by default.
    assert "head_master" not in unit and "Förnamn Efternamn" not in json.dumps(unit, ensure_ascii=False)
    assert "Rektorsexpeditionen" not in json.dumps(unit, ensure_ascii=False)
    assert unit["email"] == "storskolan@skolverket.se"
    assert unit["phone"] == "12345678" and unit["website"] == "https://www.skolverket.se"
    assert unit["start_date"] == "2022-11-08" and "end_date" not in unit
    assert unit["reports_personnel"] == "NEJ" and unit["special_support_school"] is False
    assert unit["municipal_adult_education_provided_internally"] is False
    assert unit["meta"] == {
        "extract_date": "2025-05-08T00:06:10.19+02:00",
        "created": "2015-12-12",
        "modified": "2024-08-30",
    }
    assert unit["as_of_date"] == "2024-01-01" and "raw" not in unit
    assert unit["self_url"] == f"{BASE}/school-units/12345678"


async def test_get_school_unit_real_world_shape_and_raw(router, make_client):
    router.add("GET", r"/v2/school-units/43038662", load_fixture("skolenhetsregistret_school_unit_minimal.json"))
    async with make_client("skolverket") as client:
        unit = await call(client, "skolverket_get_school_unit", {"school_unit_code": "43038662", "include_raw": True})
    assert "search_date" not in router.last().url.params
    assert unit["organizer"] == {"name": "EKERÖ KOMMUN", "organizer_type": "KOMMUN", "organizer_type_label": "Kommunal"}
    assert unit["visit_address"]["postal_code"] == "179 98" and unit["latitude"] == 59.393258
    assert unit["raw"]["included"]["type"] == "organizer"


async def test_get_school_unit_personal_data_opt_in_and_raw_redaction(router, make_client):
    router.add("GET", r"/v2/school-units/12345678", load_fixture("skolenhetsregistret_school_unit.json"))
    async with make_client("skolverket") as client:
        redacted = await call(
            client, "skolverket_get_school_unit", {"school_unit_code": "12345678", "include_raw": True}
        )
        opted_in = await call(
            client,
            "skolverket_get_school_unit",
            {"school_unit_code": "12345678", "include_raw": True, "include_personal_data": True},
        )
        tools = {t.name: t for t in (await client.list_tools()).tools}
    # Default: neither the flattened output nor raw carries the principal's name or c/o names.
    text = json.dumps(redacted, ensure_ascii=False)
    assert "Förnamn Efternamn" not in text and "Rektorsexpeditionen" not in text
    assert "headMaster" not in redacted["raw"]["data"]["attributes"]
    assert all("careOfAddress" not in a for a in redacted["raw"]["data"]["attributes"]["addresses"])
    assert redacted["raw"]["data"]["attributes"]["email"] == "storskolan@skolverket.se"
    # Opt-in: returned (non-mutation of the shared payload is covered by test_redact_personal_is_deep...).
    assert opted_in["head_master"] == "Förnamn Efternamn"
    assert opted_in["other_addresses"][0]["care_of"] == "Rektorsexpeditionen"
    assert opted_in["raw"]["data"]["attributes"]["headMaster"] == "Förnamn Efternamn"
    assert opted_in["raw"]["data"]["attributes"]["addresses"][0]["careOfAddress"] == "Rektorsexpeditionen"
    schema = tools["skolverket_get_school_unit"].input_schema["properties"]["include_personal_data"]
    assert schema["default"] is False and "personuppgift" in schema["description"].lower()


async def test_get_school_unit_municipal_adult_education_flag(router, make_client):
    payload = load_fixture("skolenhetsregistret_school_unit.json")
    payload["data"]["attributes"]["municipalAdultEducationProvidedInternally"] = "true"  # spec example is a string
    router.add("GET", r"/v2/school-units/12345678", payload)
    async with make_client("skolverket") as client:
        unit = await call(client, "skolverket_get_school_unit", {"school_unit_code": "12345678"})
    assert unit["municipal_adult_education_provided_internally"] is True


async def test_get_school_unit_not_found_and_invalid_code(router, make_client):
    router.add("GET", r"/v2/school-units/99999999", httpx2.Response(404, json=PROBLEM_404))
    async with make_client("skolverket") as client:
        err = await call_error(client, "skolverket_get_school_unit", {"school_unit_code": "99999999"})
        assert "Skolenheten 99999999 hittades inte" in err and "HTTP 404" in err
        assert "Angiven resurs hittades inte" in err
        err = await call_error(client, "skolverket_get_school_unit", {"school_unit_code": "1234"})
        assert "8 siffror" in err
        err = await call_error(
            client, "skolverket_get_school_unit", {"school_unit_code": "12345678", "as_of_date": "igår"}
        )
        assert "Ogiltigt datum" in err
    assert len(router.requests) == 1


# --- organizers -----------------------------------------------------------------------------------


async def test_search_organizers(router, make_client):
    router.add("GET", r"/v2/organizers(\?|$)", load_fixture("skolenhetsregistret_organizers.json"))
    async with make_client("skolverket") as client:
        result = await call(
            client, "skolverket_search_organizers", {"name": "ekero", "organizer_type": ["kommun", "fristående"]}
        )
        assert router.last().url.params.get_list("organizer_type") == ["KOMMUN", "ENSKILD"]
        assert result["items"] == [
            {"organization_number": "2120000126", "name": "EKERÖ KOMMUN", "organizer_type": "KOMMUN"}
        ]
        everything = await call(client, "skolverket_search_organizers", {"limit": 2})
        assert "organizer_type" not in router.last().url.params
        assert everything["total"] == 4 and everything["truncated"] is True and len(everything["items"]) == 2
        err = await call_error(client, "skolverket_search_organizers", {"organizer_type": "FÖRENING"})
        assert "KOMMFORB (Kommunalförbund)" in err


async def test_get_organizer(router, make_client):
    router.add("GET", r"/v2/organizers/2120000126", load_fixture("skolenhetsregistret_organizer.json"))
    async with make_client("skolverket") as client:
        org = await call(
            client, "skolverket_get_organizer", {"organization_number": "212000-0126", "as_of_date": "2025-03-05"}
        )
        err = await call_error(client, "skolverket_get_organizer", {"organization_number": "21200001"})
    assert "Ogiltigt organisationsnummer" in err
    request = router.requests[0]
    assert request.url.path == "/skolenhetsregistret/v2/organizers/2120000126"
    assert request.url.params["search_date"] == "2025-03-05"
    assert org["organization_number"] == "2120000126" and org["name"] == "EKERÖ KOMMUN"
    assert org["organizer_type"] == "KOMMUN" and org["organizer_type_label"] == "Kommunal"
    assert org["school_unit_codes"] == ["43038662", "23456789"] and org["school_unit_count"] == 2
    assert org["contracts"] == [{"organizer_organization_number": "2120000126", "education_provider_code": "87654321"}]
    assert org["emails"] == [
        {"value": "kommun@ekero.se", "provider_type": "SCB"},
        {"value": "skola@ekero.se", "provider_type": "SKOLVERKET"},
    ]
    assert org["phones"] == [{"value": "08-12457100", "provider_type": "SCB"}]
    assert org["municipalities"] == [{"code": "0125", "name": "Ekerö"}] and org["regions"] == []
    assert org["company_form"] == {"code": "81", "name": "Kommuner"}
    assert org["legal_entity_status"] == "INKLUDERAD_SKV" and org["company_status"] == "AKTIV"
    assert org["is_international"] is False and org["website"] == "https://www.ekero.se"
    assert org["address"]["postal_code"] == "178 23" and "latitude" not in org["address"]
    assert "care_of" not in org["address"]  # c/o name (e.g. a family name) is personuppgift
    assert org["meta"]["modified"] == "2025-03-05"


async def test_get_organizer_personal_data(router, make_client):
    router.add("GET", r"/v2/organizers/2120000126", load_fixture("skolenhetsregistret_organizer.json"))
    async with make_client("skolverket") as client:
        redacted = await call(
            client, "skolverket_get_organizer", {"organization_number": "2120000126", "include_raw": True}
        )
        opted_in = await call(
            client,
            "skolverket_get_organizer",
            {"organization_number": "2120000126", "include_raw": True, "include_personal_data": True},
        )
    assert "CO-EXEMPEL" not in json.dumps(redacted, ensure_ascii=False)
    assert redacted["raw"]["data"]["attributes"]["address"]["streetAddress"] == "Box 205"
    assert opted_in["address"]["care_of"] == "CO-EXEMPEL"
    assert opted_in["raw"]["data"]["attributes"]["address"]["careOfAddress"] == "CO-EXEMPEL"


async def test_get_organizer_school_units_from_related_links(router, make_client):
    payload = load_fixture("skolenhetsregistret_organizer.json")
    del payload["data"]["relationships"]["schoolunit"]["data"]
    router.add("GET", r"/v2/organizers/2120000126", payload)
    async with make_client("skolverket") as client:
        org = await call(client, "skolverket_get_organizer", {"organization_number": "2120000126"})
    assert org["school_unit_codes"] == ["43038662", "23456789"]


# --- education providers and contracts --------------------------------------------------------------


async def test_education_providers(router, make_client):
    router.add("GET", r"/v2/education-providers(\?|$)", load_fixture("skolenhetsregistret_education_providers.json"))
    router.add("GET", r"/v2/education-providers/87654321", load_fixture("skolenhetsregistret_education_provider.json"))
    async with make_client("skolverket") as client:
        found = await call(
            client, "skolverket_search_education_providers", {"name": "malardalens", "grading_rights": True}
        )
        assert router.last().url.params["grading_rights"] == "true"
        provider = await call(
            client, "skolverket_get_education_provider", {"education_provider_code": "87654321", "include_raw": False}
        )
        err = await call_error(client, "skolverket_get_education_provider", {"education_provider_code": "8765"})
    assert "anordnarkod" in err
    assert found["total"] == 1
    assert found["items"][0] == {
        "education_provider_code": "76543210",
        "organization_number": "8020002222",
        "name": "Föreningen Mälardalens folkhögskola",
        "folk_high_school_name": "Mälardalens folkhögskola",
        "grading_right": True,
    }
    assert provider["education_provider_code"] == "87654321" and provider["name"] == "Lärande i Sverige AB"
    assert provider["grading_rights"] is True and provider["grading_rights_to"] == "2025-12-12"
    assert provider["school_type_parts"] == [
        {"code": "VUXGY", "label": "Kommunal vuxenutbildning på gymnasial nivå"},
        {"code": "VUXSFI", "label": "Kommunal vuxenutbildning i svenska för invandrare"},
    ]
    assert provider["address"] == {
        "type": "BESOKSADRESS",
        "street": "Agatan 1",
        "postal_code": "234 67",
        "locality": "Borås",
    }
    # EducationProviderInfo has no organizationNumber in the spec; it is only in the list.
    assert "organization_number" not in provider
    assert provider["contracts"] == [
        {"organizer_organization_number": "2120000126", "education_provider_code": "87654321"}
    ]
    assert provider["company_form"] == {"code": "49", "name": "Övriga aktiebolag"}


async def test_education_provider_personal_data(router, make_client):
    router.add("GET", r"/v2/education-providers/87654321", load_fixture("skolenhetsregistret_education_provider.json"))
    async with make_client("skolverket") as client:
        redacted = await call(
            client, "skolverket_get_education_provider", {"education_provider_code": "87654321", "include_raw": True}
        )
        opted_in = await call(
            client,
            "skolverket_get_education_provider",
            {"education_provider_code": "87654321", "include_personal_data": True},
        )
    assert "CO-EXEMPEL-2" not in json.dumps(redacted, ensure_ascii=False)
    assert "careOfAddress" not in redacted["raw"]["data"]["attributes"]["address"]
    assert opted_in["address"]["care_of"] == "CO-EXEMPEL-2" and "raw" not in opted_in


def _email_routes(router):
    router.add("GET", r"/v2/school-units/12345678", load_fixture("skolenhetsregistret_school_unit.json"))
    router.add("GET", r"/v2/organizers/2120000126", load_fixture("skolenhetsregistret_organizer.json"))
    router.add("GET", r"/v2/education-providers/87654321", load_fixture("skolenhetsregistret_education_provider.json"))


DETAIL_CALLS = [
    ("skolverket_get_school_unit", {"school_unit_code": "12345678"}),
    ("skolverket_get_organizer", {"organization_number": "2120000126"}),
    ("skolverket_get_education_provider", {"education_provider_code": "87654321"}),
]
FIXTURE_EMAILS = ("storskolan@skolverket.se", "kommun@ekero.se", "skola@ekero.se", "info@larande.se")


async def test_emails_withheld_when_personal_data_off(router, make_client):
    _email_routes(router)
    async with make_client("skolverket", allow_personal_data=False) as client:
        results = {
            (tool, raw): await call(client, tool, {**args, "include_raw": raw})
            for tool, args in DETAIL_CALLS
            for raw in (False, True)
        }
    for (tool, raw), result in results.items():
        text = json.dumps(result, ensure_ascii=False)
        assert not any(address in text for address in FIXTURE_EMAILS), (tool, raw)
        assert "@" not in text and "email" not in result and not result.get("emails")
        assert any("FUZZY_MCP_PERSONAL_DATA=off" in note for note in result["notes"]), (tool, raw)
        assert ("raw" in result) is raw
        if raw:
            assert "email" not in result["raw"]["data"]["attributes"]
    # Phone numbers and web addresses stay.
    unit = results[("skolverket_get_school_unit", True)]
    assert unit["phone"] == "12345678" and unit["website"] == "https://www.skolverket.se"
    assert unit["raw"]["data"]["attributes"]["phoneNumber"] == "12345678"
    assert "headMaster" not in unit["raw"]["data"]["attributes"]  # names stay redacted as well
    org = results[("skolverket_get_organizer", True)]
    assert (
        org["phones"] == [{"value": "08-12457100", "provider_type": "SCB"}] and org["website"] == "https://www.ekero.se"
    )
    assert org["raw"]["data"]["attributes"]["phoneNumber"] == [{"providerType": "SCB", "phone": "08-12457100"}]
    provider = results[("skolverket_get_education_provider", False)]
    assert provider["phones"] == [{"value": "033-123456", "provider_type": "SCB"}]


async def test_emails_returned_by_default(router, make_client):
    _email_routes(router)
    async with make_client("skolverket") as client:
        results = [await call(client, tool, {**args, "include_raw": True}) for tool, args in DETAIL_CALLS]
    unit, org, provider = results
    assert unit["email"] == "storskolan@skolverket.se"
    assert unit["raw"]["data"]["attributes"]["email"] == "storskolan@skolverket.se"
    assert [e["value"] for e in org["emails"]] == ["kommun@ekero.se", "skola@ekero.se"]
    assert org["raw"]["data"]["attributes"]["email"][0]["email"] == "kommun@ekero.se"
    assert provider["emails"] == [{"value": "info@larande.se", "provider_type": "SCB"}]
    assert all("notes" not in result for result in results)


def test_redact_personal_emails_keeps_names_when_asked():
    payload = {"attributes": {"headMaster": "Förnamn", "email": "a@b.se", "url": "mailto:c@d.se", "x": ["e@f.se", "g"]}}
    assert redact_personal(payload, names=False, emails=True) == {"attributes": {"headMaster": "Förnamn", "x": ["g"]}}
    assert redact_personal(payload) == {"attributes": {"email": "a@b.se", "url": "mailto:c@d.se", "x": ["e@f.se", "g"]}}
    assert payload["attributes"]["email"] == "a@b.se"


async def test_education_provider_contracts_example_shape(router, make_client):
    # The spec's own example for EducationProviderInfoResponseDataRelationships uses `contracts` with a
    # single object for `data` and `related`.
    payload = load_fixture("skolenhetsregistret_education_provider.json")
    payload["data"]["relationships"] = {
        "contracts": {
            "links": {"related": {"href": f"{BASE}/contracts/2120000126/87654321", "rel": "related"}},
            "data": {"type": "contract", "organizationNumber": "2120000126", "educationProviderCode": "87654321"},
        }
    }
    router.add("GET", r"/v2/education-providers/87654321", payload)
    async with make_client("skolverket") as client:
        provider = await call(client, "skolverket_get_education_provider", {"education_provider_code": "87654321"})
    assert provider["contracts"] == [
        {"organizer_organization_number": "2120000126", "education_provider_code": "87654321"}
    ]


async def test_contracts(router, make_client):
    router.add("GET", r"/v2/contracts(\?|$)", load_fixture("skolenhetsregistret_contracts.json"))
    router.add("GET", r"/v2/contracts/2120000126/87654321", load_fixture("skolenhetsregistret_contract.json"))
    async with make_client("skolverket") as client:
        found = await call(
            client,
            "skolverket_search_contracts",
            {"organizer_organization_number": "212000-0126", "status": "AKTIV", "modified_since": "2024-01-01"},
        )
        params = router.last().url.params
        assert params["organizer_organization_number"] == "2120000126"
        assert params["meta_modified_after"] == "2024-01-01"
        assert "status" not in params and "education_provider_organization_number" not in params
        by_name = await call(client, "skolverket_search_contracts", {"name": "folkhogskola"})
        contract = await call(
            client,
            "skolverket_get_contract",
            {"organization_number": "2120000126", "education_provider_code": "87654321", "as_of_date": "2024-06-01"},
        )
        err = await call_error(client, "skolverket_search_contracts", {"status": "pausad"})
    assert "aktiv (Aktiv entreprenad)" in err
    assert found["total"] == 1 and found["items"][0]["education_provider_code"] == "87654321"
    assert found["filters"]["status"] == ["aktiv"]
    assert [i["education_provider_code"] for i in by_name["items"]] == ["76543210"]
    assert router.last().url.params["search_date"] == "2024-06-01"
    assert contract["organizer_organization_number"] == "2120000126"
    assert contract["education_provider_code"] == "87654321" and contract["status"] == "aktiv"
    assert contract["school_type_parts"] == [
        {
            "code": "VUXSFI",
            "label": "Kommunal vuxenutbildning i svenska för invandrare",
            "valid_from": "2023-12-12",
            "valid_to": "2025-12-12",
        },
        {"code": "VUXGY", "label": "Kommunal vuxenutbildning på gymnasial nivå", "valid_from": "2024-01-01"},
    ]
    assert contract["school_unit_codes"] == ["56789012"]


# --- changes, api-info and code resource --------------------------------------------------------------


async def test_school_unit_changes(router, make_client):
    router.add("GET", LIST_UNITS, load_fixture("skolenhetsregistret_school_units.json"))
    router.add("GET", r"/v2/organizers(\?|$)", load_fixture("skolenhetsregistret_organizers.json"))
    router.add("GET", r"/v2/contracts(\?|$)", load_fixture("skolenhetsregistret_contracts.json"))
    async with make_client("skolverket") as client:
        changes = await call(client, "skolverket_school_unit_changes", {"since": "2025-01-01", "limit": 2})
        assert len(router.requests) == 2
        units_request, organizers_request = router.requests
        assert units_request.url.params["meta_modified_after"] == "2025-01-01"
        # Default "all statuses" is sent explicitly so UPPHORT/PLANERAD are requested, not left to the API default.
        assert units_request.url.params.get_list("status") == ALL_STATUSES
        assert organizers_request.url.path.endswith("/v2/organizers")
        assert organizers_request.url.params["meta_modified_after"] == "2025-01-01"
        assert changes["since"] == "2025-01-01"
        assert changes["school_units"]["total"] == 6 and changes["school_units"]["truncated"] is True
        assert len(changes["school_units"]["items"]) == 2
        assert changes["organizers"]["total"] == 4 and "contracts" not in changes
        assert changes["truncated"] is True
        assert any("skolverket_search_school_units" in n and '"offset": 2' in n for n in changes["notes"])

        scoped = await call(
            client,
            "skolverket_school_unit_changes",
            {"since": "20250101", "entities": ["school_units", "contracts"], "municipality_code": "0125"},
        )
        assert router.requests[-2].url.params.get_list("municipality_code") == ["0125"]
        assert router.requests[-1].url.path.endswith("/v2/contracts")
        assert scoped["contracts"]["total"] == 2 and "organizers" not in scoped
        assert scoped["school_units"]["items"][0]["municipality_code"] == "0125"

        err = await call_error(client, "skolverket_school_unit_changes", {"since": "förra veckan"})
        assert "Ogiltigt datum" in err


async def test_api_info(router, make_client):
    router.add("GET", r"/v2/api-info$", load_fixture("skolenhetsregistret_api_info.json"))
    async with make_client("skolverket") as client:
        info = await call(client, "skolverket_school_unit_api_info")
    assert str(router.last().url) == f"{BASE}/api-info"
    assert info["api_name"] == "skolenhetsregistret" and info["api_version"] == "2.0"
    assert info["api_status"] == "beta" and info["base_url"] == BASE


async def test_codes_resource(make_client):
    async with make_client("skolverket") as client:
        resources = {str(r.uri) for r in (await client.list_resources()).resources}
        assert "fuzzy://skolverket/skolenhetsregistret/codes" in resources
        res = await client.read_resource("fuzzy://skolverket/skolenhetsregistret/codes")
    content = res.contents[0]
    assert content.mime_type == "application/json"
    doc = json.loads(content.text)
    assert doc["base_url"] == BASE
    statuses = {c["code"]: c["label"] for c in doc["code_lists"]["school_unit_status"]["codes"]}
    assert statuses == {"AKTIV": "Aktiv", "VILANDE": "Vilande", "UPPHORT": "Upphörd", "PLANERAD": "Planerad"}
    parts = [c["code"] for c in doc["code_lists"]["school_type_part_vux"]["codes"]]
    assert parts == ["VUXGR", "VUXGY", "VUXGRAN", "VUXGYAN", "VUXSFI"]


# --- output size caps --------------------------------------------------------------------------------


def _big_list(kind: str, rows: list[dict]) -> dict:
    return {"meta": {"extractDate": "2025-05-08T00:06:10.19+02:00"}, "data": {"type": kind, "attributes": rows}}


def _long_contracts(n: int) -> dict:
    return _big_list(
        "contract",
        [
            {
                "organizerOrganizationNumber": "2120000126",
                "organizerName": "SÖDERTÄLJE KOMMUN",
                "educationProviderOrganizationNumber": f"80200{i:05d}",
                "educationProviderName": "Föreningen Mälardalens folkhögskola och studieförbund",
                "educationProviderCode": f"{i:08d}",
                "folkHighschoolName": "Mälardalens folkhögskola Västerås",
                "status": "inaktiv",
            }
            for i in range(n)
        ],
    )


def _long_providers(n: int) -> dict:
    return _big_list(
        "educationProvider",
        [
            {
                "organizationNumber": f"80200{i:05d}",
                "educationProviderCode": f"{i:08d}",
                "displayName": "Föreningen Mälardalens folkhögskola och studieförbund",
                "folkHighschoolName": "Mälardalens folkhögskola Västerås",
                "gradingRight": True,
            }
            for i in range(n)
        ],
    )


def _long_units(n: int) -> dict:
    return _big_list(
        "schoolunit",
        [
            {"schoolUnitCode": f"{i:08d}", "name": "Kunskapsskolan Tumba gymnasium och grundskola", "status": "AKTIV"}
            for i in range(n)
        ],
    )


def _long_organizers(n: int) -> dict:
    return _big_list(
        "organizer",
        [
            {
                "organizationNumber": f"55656{i:05d}",
                "displayName": "Kunskapsskolan i Sverige Aktiebolag (publ)",
                "organizerType": "ENSKILD",
            }
            for i in range(n)
        ],
    )


def _size(result) -> int:
    return len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode())


async def test_list_limits_are_capped_per_type(router, make_client):
    router.add("GET", r"/v2/contracts(\?|$)", _long_contracts(1000))
    router.add("GET", r"/v2/education-providers(\?|$)", _long_providers(1000))
    router.add("GET", r"/v2/organizers(\?|$)", _long_organizers(1000))
    router.add("GET", LIST_UNITS, _long_units(1000))
    async with make_client("skolverket") as client:
        results = {
            tool: await call(client, tool, {"limit": 9999})
            for tool in (
                "skolverket_search_contracts",
                "skolverket_search_education_providers",
                "skolverket_search_organizers",
                "skolverket_search_school_units",
            )
        }
    expected = {
        "skolverket_search_contracts": MAX_LIMIT_CONTRACTS,
        "skolverket_search_education_providers": MAX_LIMIT_EDUCATION_PROVIDERS,
        "skolverket_search_organizers": MAX_LIMIT_ORGANIZERS,
        "skolverket_search_school_units": MAX_LIMIT_SCHOOL_UNITS,
    }
    for tool, result in results.items():
        assert result["limit"] == expected[tool] and len(result["items"]) == expected[tool], tool
        assert result["truncated"] is True and result["next_offset"] == expected[tool]
        # A full page stays well under the ~25k-token host cap (~60 kB of compact JSON).
        assert _size(result) < 60_000, (tool, _size(result))


async def test_school_unit_changes_byte_budget(router, make_client):
    router.add("GET", r"/v2/contracts(\?|$)", _long_contracts(1000))
    router.add("GET", r"/v2/education-providers(\?|$)", _long_providers(1000))
    router.add("GET", r"/v2/organizers(\?|$)", _long_organizers(1000))
    router.add("GET", LIST_UNITS, _long_units(1000))
    entities = ["school_units", "organizers", "education_providers", "contracts"]
    async with make_client("skolverket") as client:
        changes = await call(
            client, "skolverket_school_unit_changes", {"since": "2025-01-01", "entities": entities, "limit": 500}
        )
    assert _size(changes) < CHANGES_MAX_BYTES + 5_000, _size(changes)
    assert changes["truncated"] is True
    for key in entities:
        part = changes[key]
        assert part["total"] == 1000 and part["truncated"] is True
        assert len(part["items"]) > 0 and part["next_offset"] == len(part["items"])
    # Each register gets an even share, so wide contract rows do not starve the others.
    assert len(changes["contracts"]["items"]) < len(changes["organizers"]["items"])
    notes = " ".join(changes["notes"])
    assert "kortades" in notes
    assert 'skolverket_search_contracts({"modified_since": "2025-01-01", "offset": ' in notes
    assert '"status": ["AKTIV", "VILANDE", "UPPHORT", "PLANERAD"]' in notes
