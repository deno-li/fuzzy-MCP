# SPDX-FileCopyrightText: 2026 Deniz Özer
#
# SPDX-License-Identifier: MIT

import copy
import json
from typing import Any

import httpx2
import pytest
from mcp import Client

from fuzzy_mcp.config import Settings
from fuzzy_mcp.server import build_server
from fuzzy_mcp.sources.skolverket.susa_navet import (
    EMAIL_WITHHELD_NOTE,
    ENGLISH,
    ROW_TITLE_CHARS,
    code_of,
    event_row,
    normalize_municipality_codes,
    normalize_school_types,
    text_of,
    texts_of,
    url_of,
)

from .conftest import call, call_error, load_fixture

pytestmark = pytest.mark.anyio

BASE_PATH = "/susa-navet/emil3"
EVENTS = r"/susa-navet/emil3/educationEvents\?"
SEARCH_EVENTS = "skolverket_susa_search_education_events"


def paged_events(total_pages: int = 3):
    """Responder serving the events fixture as page ``page`` of ``total_pages`` with page-unique ids."""

    def responder(request: httpx2.Request) -> httpx2.Response:
        page = int(request.url.params["page"])
        data = copy.deepcopy(load_fixture("susa_navet_events.json"))
        for item in data["educationEvents"]:
            item["id"] = f"{item['id']}.p{page}"
            if item["content"]:
                item["content"]["identifier"] = item["id"]
        data["page"].update(pageNumber=page, totalPages=total_pages, totalElements=4 * total_pages)
        return httpx2.Response(200, json=data)

    return responder


def event_rows(copies: int) -> list[dict[str, Any]]:
    """``copies`` × the four fixture events with copy-unique ids (two of each four lie in county 22)."""
    rows = []
    for n in range(copies):
        for item in copy.deepcopy(load_fixture("susa_navet_events.json")["educationEvents"]):
            item["id"] = f"{item['id']}.c{n}"
            if item["content"]:
                item["content"]["identifier"] = item["id"]
            rows.append(item)
    return rows


def dataset(items_key: str, rows: list[dict[str, Any]]):
    """Responder that pages ``rows`` honouring ``page`` and ``size`` like the real API."""

    def responder(request: httpx2.Request) -> httpx2.Response:
        page, size = int(request.url.params["page"]), int(request.url.params["size"])
        chunk = copy.deepcopy(rows[page * size : (page + 1) * size])
        meta = {"size": size, "totalElements": len(rows), "totalPages": -(-len(rows) // size), "pageNumber": page}
        return httpx2.Response(200, json={"links": [], items_key: chunk, "page": meta})

    return responder


async def follow(client: Client, tool: str, args: dict[str, Any], key: str) -> tuple[list[str], int]:
    """Call ``tool`` and keep following next_page/next_skip_matches; returns all returned ids and the call count."""
    ids: list[str] = []
    cursor: dict[str, Any] = {}
    for calls in range(1, 50):
        result = await call(client, tool, {**args, **cursor})
        ids += [row["id"] for row in result[key]]
        if "next_page" not in result:
            return ids, calls
        cursor = {"start_page": result["next_page"], "skip_matches": result.get("next_skip_matches", 0)}
    raise AssertionError("next_page never ended")


async def test_search_events_without_client_filters_requests_only_limit(router, make_client):
    router.add("GET", EVENTS, load_fixture("susa_navet_events.json"))
    async with make_client("skolverket") as client:
        result = await call(client, SEARCH_EVENTS, {"school_type": ["yh", "FHS"], "limit": 4})
    assert len(router.requests) == 1
    request = router.last()
    assert request.url.host == "api.skolverket.se"
    assert request.url.path == f"{BASE_PATH}/educationEvents"
    assert request.url.params.get_list("schoolType") == ["YH", "FHS"]
    assert request.url.params["page"] == "0" and request.url.params["size"] == "4"
    assert "providerId" not in request.url.params and "updatedSince" not in request.url.params
    assert request.headers["accept"] == "application/json"

    assert result["total_upstream"] == 10 and result["scanned"] == 4 and result["returned"] == 4
    assert result["next_page"] == 1 and result["complete_scan"] is False and result["truncated"] is True
    assert "start_page=1" in result["hint"] and "limit=4" in result["hint"] and "skip_matches" not in result["hint"]
    assert "next_skip_matches" not in result and "excluded" not in result
    assert result["filters"] == {"school_type": ["YH", "FHS"]}
    kth, yh, fhs, deleted = result["events"]
    assert kth == {
        "id": "e.uoh.kth.dd1420.60090.20251",
        "education_id": "i.uoh.kth.dd1420.60090.20251",
        "provider_ids": ["p.uoh.kth"],
        "title": "Grundläggande datalogi",
        "start": "2025-01-20",
        "end": "2025-06-08",
        "municipality_codes": ["0180"],
        "towns": ["Stockholm"],
        "pace_percent": 50.0,
        "distance": False,
        "languages": ["swe"],
        "application_last": "2024-10-15",
        "url": "https://www.kth.se/student/kurser/kurs/DD1420",
    }
    assert yh["distance"] is True and yh["url"] == "https://www.exempel-yh.se/ytpl"
    assert fhs["cancelled"] is True and "end" not in fhs
    assert deleted == {"id": "e.sv.deleted.1", "status": "DELETED", "inactive": True}


async def test_search_events_client_side_filters(router, make_client):
    router.add("GET", EVENTS, load_fixture("susa_navet_events.json"))

    async def ids(args):
        result = await call(client, SEARCH_EVENTS, {"max_pages": 1, **args})
        return [e["id"] for e in result["events"]], result

    async with make_client("skolverket") as client:
        found, result = await ids(
            {"municipality_code": "Sundsvall", "provider_id": "p.sv.68897220", "updated_since": "2026-03-01"}
        )
        # With updated_since the deleted row passes the client filters (flagged), so delta syncs see the deletion.
        assert found == ["e.sv.yh.ytpl.2281.20252", "e.sv.deleted.1"]
        assert result["events"][1] == {"id": "e.sv.deleted.1", "status": "DELETED", "inactive": True}
        params = router.last().url.params
        assert params["providerId"] == "p.sv.68897220" and params["updatedSince"] == "2026-03-01"
        assert params["size"] == "500" and params["page"] == "0"
        assert result["filters"]["municipality_codes"] == ["2281"]
        assert result["scanned"] == 4 and result["matched"] == 2 and result["next_page"] == 1
        assert "excluded" not in result and "1 borttagna/inaktiva poster (inactive) togs med" in result["hint"]

        found, result = await ids({"distance_only": True})
        assert found == ["e.sv.yh.ytpl.2281.20252"]
        # Without updated_since the content-less row cannot be tested: it is counted, not silently dropped.
        assert result["excluded"] == {"no_content": 1}
        assert "1 utan innehåll" in result["hint"] and "Ange updated_since" in result["hint"]
        assert (await ids({"text": "goteborg ALLMAN"}))[0] == ["e.sv.fhs.allm.2262.20252"]
        assert (await ids({"text": "logistik"}))[0] == ["e.sv.yh.ytpl.2281.20252"]  # keywords
        assert (await ids({"county_code": "22"}))[0] == ["e.sv.yh.ytpl.2281.20252", "e.sv.fhs.allm.2262.20252"]
        assert (await ids({"start_from": "2025-08-01", "start_to": "2025-08-31"}))[0] == ["e.sv.yh.ytpl.2281.20252"]
        assert (await ids({"language": "en"}))[0] == ["e.sv.fhs.allm.2262.20252"]
        assert (await ids({"municipality_code": ["0180", "2262"], "language": "swe"}))[0] == [
            "e.uoh.kth.dd1420.60090.20251"
        ]


async def test_search_events_pages_until_cap_and_continues(router, make_client):
    router.add("GET", EVENTS, paged_events(total_pages=3))
    async with make_client("skolverket") as client:
        first = await call(client, SEARCH_EVENTS, {"text": "finns inte", "max_pages": 2, "page_size": 4})
        assert [r.url.params["page"] for r in router.requests] == ["0", "1"]
        assert all(r.url.params["size"] == "4" for r in router.requests)
        assert first["scanned"] == 8 and first["pages_scanned"] == 2 and first["matched"] == 0
        assert first["next_page"] == 2 and first["complete_scan"] is False and first["truncated"] is True
        assert first["total_upstream"] == 12 and "start_page=2" in first["hint"]

        rest = await call(client, SEARCH_EVENTS, {"text": "datalogi", "start_page": 2, "page_size": 4, "max_pages": 5})
        assert router.last().url.params["page"] == "2" and len(router.requests) == 3
        # The last page was reached, but pages 0–1 were not scanned in this call: not a complete scan.
        assert rest["complete_scan"] is False and "next_page" not in rest and rest["truncated"] is False
        assert [e["id"] for e in rest["events"]] == ["e.uoh.kth.dd1420.60090.20251.p2"]
        assert "Sidorna före start_page=2" in rest["hint"]

        router.requests.clear()
        whole = await call(client, SEARCH_EVENTS, {"text": "datalogi", "page_size": 4, "max_pages": 5, "limit": 5})
        assert len(router.requests) == 3 and whole["complete_scan"] is True and "next_page" not in whole
        assert whole["matched"] == 3 and whole["truncated"] is False


async def test_search_events_stops_at_limit_and_hard_page_cap(router, make_client):
    router.add("GET", EVENTS, paged_events(total_pages=100))
    async with make_client("skolverket") as client:
        result = await call(client, SEARCH_EVENTS, {"county_code": "22", "limit": 1})
        assert len(router.requests) == 1
        assert result["matched"] == 2 and result["returned"] == 1 and result["truncated"] is True
        # The second match on page 0 was not returned: resume on page 0 after one match.
        assert result["next_page"] == 0 and result["next_skip_matches"] == 1
        assert "start_page=0, skip_matches=1" in result["hint"] and "höj limit" in result["hint"]

        router.requests.clear()
        capped = await call(client, SEARCH_EVENTS, {"text": "finns inte", "max_pages": 50, "page_size": 9999})
    assert len(router.requests) == 10  # hard max pages
    assert router.requests[0].url.params["size"] == "2000"  # API maximum
    assert capped["pages_scanned"] == 10 and capped["next_page"] == 10


async def test_search_input_validation(router, make_client):
    router.add("GET", EVENTS, load_fixture("susa_navet_events.json"))
    async with make_client("skolverket") as client:
        assert "Okänd skolform 'XYZ'" in await call_error(client, SEARCH_EVENTS, {"school_type": "XYZ"})
        assert "fyra siffror" in await call_error(client, SEARCH_EVENTS, {"municipality_code": "12"})
        assert "Kunde inte tolka 'Atlantis'" in await call_error(
            client, SEARCH_EVENTS, {"municipality_code": "Atlantis"}
        )
        assert "två siffror" in await call_error(client, SEARCH_EVENTS, {"county_code": "123"})
        assert "Ogiltigt updated_since" in await call_error(client, SEARCH_EVENTS, {"updated_since": "2025-13-01"})
        assert "start_from" in await call_error(
            client, SEARCH_EVENTS, {"start_from": "2025-09-01", "start_to": "2025-01-01"}
        )
        assert "Ogiltigt datum" in await call_error(client, SEARCH_EVENTS, {"start_from": "1 sept"})
        assert "språkkod" in await call_error(client, SEARCH_EVENTS, {"language": "x"})
        assert "start_page" in await call_error(client, SEARCH_EVENTS, {"start_page": -1})
        assert "skip_matches" in await call_error(client, SEARCH_EVENTS, {"skip_matches": -1})
        assert router.requests == []

        await call(client, SEARCH_EVENTS, {"school_type": "VUX", "updated_since": "2025-04-24 11:39"})
    params = router.last().url.params
    assert params.get_list("schoolType") == ["VUXGR", "VUXGRAN", "VUXGY", "VUXGYAN", "VUXSFI"]
    assert params["updatedSince"] == "2025-04-24T11:39:00"


async def test_get_education_event_flattens_detail(router, make_client):
    fixture = load_fixture("susa_navet_event.json")
    router.add("GET", r"/emil3/educationEvents/[^?]+$", fixture)
    async with make_client("skolverket") as client:
        detail = await call(
            client, "skolverket_susa_get_education_event", {"event_id": " E.UOH.KTH.DD1420.60090.20251 "}
        )
        raw = await call(
            client,
            "skolverket_susa_get_education_event",
            {"event_id": "e.uoh.kth.dd1420.60090.20251", "include_raw": True},
        )
    # The id is passed through as given and compared case-insensitively with the returned identifier.
    assert router.requests[0].url.path == f"{BASE_PATH}/educationEvents/E.UOH.KTH.DD1420.60090.20251"
    assert "notes" not in detail and "raw" not in detail
    assert detail["id"] == "e.uoh.kth.dd1420.60090.20251" and detail["status"] == "ACTIVE"
    assert detail["title"] == "Grundläggande datalogi"
    assert detail["description"] == "Kursen ger grunderna i datalogi.\n- Algoritmer\n- Datastrukturer & komplexitet"
    assert detail["url"] == "https://www.kth.se/student/kurser/kurs/DD1420"
    assert detail["education_id"] == "i.uoh.kth.dd1420.60090.20251"
    assert detail["provider_ids"] == ["p.uoh.kth"] and detail["examining_body_ids"] == ["p.uoh.kth"]
    assert detail["execution_condition"] == {
        "type": "C_ExecutionCondition",
        "code": "1",
        "label": "Beslutad, genomförs mellan angivet start- och slutdatum",
    }
    assert detail["time_of_study"] == {"type": "C_TimeOfStudy", "code": "dag", "label": "Dagtid"}
    assert detail["languages"] == ["swe", "eng"] and detail["places"] == 120 and detail["cancelled"] is False
    assert detail["locations"] == [
        {
            "municipality_code": "0180",
            "town": "Stockholm",
            "street_address": "Brinellvägen 8",
            "country": "SE",
            "study_location": "KTH Campus",
        }
    ]
    assert detail["fees"] == [
        {
            "total_amount": 65000.0,
            "currency": "SEK",
            "first_installment": 32500.0,
            "condition": "Gäller studenter utanför EU/EES",
        }
    ]
    assert detail["distance"] is True
    assert detail["distance_details"] == {
        "mandatory_sessions": 2,
        "optional_sessions": 1,
        "description": "Två obligatoriska träffar på campus",
    }
    assert detail["application"] == {
        "code": "KTH-60090",
        "first": "2024-09-16",
        "last": "2024-10-15",
        "continuous": False,
        "url": "https://www.antagning.se",
        "email": "antagning@kth.se",
    }
    # keywords is ONE LangString with a node per word (spec); every Swedish word is kept.
    assert detail["keywords"] == ["programmering", "algoritmer"]
    assert detail["extensions"] == [
        {
            "type": "UHEventExtension",
            "id": "0",
            "startPeriod": {"week": 4, "period": {"year": "2025", "periodNumber": 3, "semester": "vt"}},
            "tuitionFee": {"value": True, "first": 32500, "total": 65000},
            "textualDescription": "Undervisning dagtid",
            "itdistance": False,
            "courseRound": 60090,
        }
    ]
    assert raw["raw"] == fixture


async def test_get_education_info_codes_and_language_preference(router, make_client):
    router.add("GET", r"/emil3/educationInfos/i\.uoh\.uu\.1te686\.14483\.20242$", load_fixture("susa_navet_info.json"))
    async with make_client("skolverket") as client:
        info = await call(client, "skolverket_susa_get_education_info", {"education_id": "i.uoh.uu.1te686.14483.20242"})
    assert router.last().url.path == f"{BASE_PATH}/educationInfos/i.uoh.uu.1te686.14483.20242"
    assert info["title"] == "Elektronik för ingenjörer"  # Swedish preferred although English comes first
    assert info["url"] == "https://www.uu.se/1TE686"
    assert info["code"] == "1TE686" and info["education_base"] == "i.uoh.uu.1te686"
    assert info["school_type"] == {
        "type": "C_SchoolType",
        "code": "HS",
        "label": "Högskola/universitet (inkl. polisutbildning)",
    }
    assert info["education_levels"] == [
        {"type": "C_EducationLevel", "code": "ISCED_6", "label": "ISCED 6 – kandidat eller motsvarande"},
        {"type": "UH_EducationLevel", "code": "G1N"},
    ]
    assert info["orientation"] == {"type": "C_Orientation", "code": "2"}
    assert info["subjects"] == [{"type": "UH_Subject", "code": "EL1"}, {"type": "C_Subject_ISCED2013", "code": "0714"}]
    assert info["credits"] == {"value": 7.5, "system": {"type": "C_Credits", "code": "hp", "label": "Högskolepoäng"}}
    assert info["extent"] == {"length": 10, "unit": {"type": "C_TimeType", "code": "weeks", "label": "veckor"}}
    assert info["degrees"] == ["Ingen examen"] and info["result_is_degree"] is False
    assert "is_vocational" not in info
    assert info["qualification_level"]["label"] == "SeQF-nivå 6 (tolkning)"
    assert info["eligible_for_student_aid"]["label"] == "Ger rätt till studiemedel"
    assert info["eligibility"] == "Grundläggande behörighet samt Matematik 3c"
    assert info["extensions"] == [{"type": "UHInfoExtension", "id": "1", "focusIdentifier": "EL"}]

    async with make_client("skolverket", default_language="en") as client:
        english = await call(
            client, "skolverket_susa_get_education_info", {"education_id": "i.uoh.uu.1te686.14483.20242"}
        )
    assert english["title"] == "Electronics for Engineers" and english["url"] == "https://www.uu.se/en/1TE686"
    # Falls back to Swedish when there is no English text.
    assert english["description"] == "Kursen behandlar analog och digital elektronik."
    assert english["degrees"] == ["No degree"]


async def test_get_education_provider_and_deleted_record(router, make_client):
    router.add("GET", r"/emil3/educationProviders/p\.uoh\.kth$", load_fixture("susa_navet_provider.json"))
    router.add(
        "GET", r"/emil3/educationProviders/p\.sv\.gone$", {"id": "p.sv.gone", "status": "DELETED", "content": None}
    )
    async with make_client("skolverket") as client:
        provider = await call(client, "skolverket_susa_get_education_provider", {"provider_id": "p.uoh.kth"})
        gone = await call(client, "skolverket_susa_get_education_provider", {"provider_id": "p.sv.gone"})
    assert provider["name"] == "Kungliga Tekniska högskolan" and provider["organisation_number"] == "2021006237"
    assert provider["responsible_body"] == {
        "type": {"type": "C_Body", "code": "statlig", "label": "Statlig huvudman"},
        "name": "Kungliga Tekniska högskolan",
    }
    assert provider["contact_address"] == {
        "municipality_code": "0180",
        "town": "Stockholm",
        "street_address": "Brinellvägen 8",
        "postal_code": "100 44",
    }
    assert provider["visit_addresses"][0]["street_address"] == "Valhallavägen 79"
    assert provider["phones"] == [{"number": "08-790 60 00", "function": "Växel"}]
    assert provider["email_addresses"] == ["info@kth.se"] and provider["school_years"] == ["10"]
    assert provider["url"] == "https://www.kth.se"
    assert gone["id"] == "p.sv.gone" and gone["status"] == "DELETED" and gone["inactive"] is True
    assert "borttagen" in gone["notes"][0]


SUSA_DETAIL_CALLS = [
    ("skolverket_susa_get_education_event", {"event_id": "e.uoh.kth.dd1420.60090.20251"}),
    ("skolverket_susa_get_education_info", {"education_id": "i.uoh.uu.1te686.14483.20242"}),
    ("skolverket_susa_get_education_provider", {"provider_id": "p.uoh.kth"}),
]


def _susa_email_routes(router) -> dict[str, Any]:
    fixtures = {
        "event": load_fixture("susa_navet_event.json"),
        "info": load_fixture("susa_navet_info.json"),
        "provider": load_fixture("susa_navet_provider.json"),
    }
    # The info fixture has no e-mail: add one under an e-mail key and one as a mailto: link in an extension.
    fixtures["info"]["content"]["extensions"].append(
        {
            "type": "UHInfoExtension",
            "id": "2",
            "contactEmail": "studievagledning@uu.se",
            "contact": {"urls": [{"lang": "swe", "value": "mailto:studievagledning@uu.se"}]},
        }
    )
    router.add("GET", r"/emil3/educationEvents/[^?]+$", fixtures["event"])
    router.add("GET", r"/emil3/educationInfos/i\.uoh\.uu\.1te686\.14483\.20242$", fixtures["info"])
    router.add("GET", r"/emil3/educationProviders/p\.uoh\.kth$", fixtures["provider"])
    return fixtures


async def test_emails_withheld_when_personal_data_off(router, make_client):
    _susa_email_routes(router)
    async with make_client("skolverket", allow_personal_data=False) as client:
        results = {
            (tool, raw): await call(client, tool, {**args, "include_raw": raw})
            for tool, args in SUSA_DETAIL_CALLS
            for raw in (False, True)
        }
    for key, result in results.items():
        text = json.dumps(result, ensure_ascii=False)
        assert "@" not in text and "mailto" not in text, key
        assert result["notes"] == [EMAIL_WITHHELD_NOTE], key
        assert ("raw" in result) is key[1]
    event = results[("skolverket_susa_get_education_event", True)]
    assert "email" not in event["application"] and event["application"]["url"] == "https://www.antagning.se"
    assert "email" not in event["raw"]["content"]["application"] and event["raw"]["content"]["places"] == 120
    info = results[("skolverket_susa_get_education_info", False)]
    assert info["extensions"] == [
        {"type": "UHInfoExtension", "id": "1", "focusIdentifier": "EL"},
        {"type": "UHInfoExtension", "id": "2"},
    ]
    # Phone numbers and web addresses stay.
    provider = results[("skolverket_susa_get_education_provider", True)]
    assert "email_addresses" not in provider and "emailAddresses" not in provider["raw"]["content"]
    assert provider["phones"] == [{"number": "08-790 60 00", "function": "Växel"}]
    assert provider["url"] == "https://www.kth.se"


async def test_emails_returned_by_default(router, make_client):
    fixtures = _susa_email_routes(router)
    async with make_client("skolverket") as client:
        event, info, provider = [
            await call(client, tool, {**args, "include_raw": True}) for tool, args in SUSA_DETAIL_CALLS
        ]
    assert event["application"]["email"] == "antagning@kth.se" and event["raw"] == fixtures["event"]
    assert info["extensions"][1]["contactEmail"] == "studievagledning@uu.se"
    assert info["extensions"][1]["contact"] == "mailto:studievagledning@uu.se" and info["raw"] == fixtures["info"]
    assert provider["email_addresses"] == ["info@kth.se"] and provider["raw"] == fixtures["provider"]
    assert all("notes" not in result for result in (event, info, provider))


async def test_search_education_infos(router, make_client):
    router.add("GET", r"/emil3/educationInfos\?", load_fixture("susa_navet_infos.json"))
    async with make_client("skolverket") as client:
        result = await call(client, "skolverket_susa_search_education_infos", {"text": "elektronik"})
        assert router.last().url.path == f"{BASE_PATH}/educationInfos"
        assert router.last().url.params["size"] == "500" and "schoolType" not in router.last().url.params
        assert result["complete_scan"] is True and result["total_upstream"] == 2 and result["truncated"] is False
        assert result["infos"] == [
            {
                "id": "i.uoh.uu.1te686.14483.20242",
                "code": "1TE686",
                "title": "Elektronik för ingenjörer",
                "school_type": "HS",
                "configuration": "kurs",
                "credits": 7.5,
                "credits_system": "hp",
            }
        ]
        by_code = await call(client, "skolverket_susa_search_education_infos", {"text": "yh00123", "school_type": "yh"})
        assert router.last().url.params.get_list("schoolType") == ["YH"]
        assert by_code["infos"][0]["education_levels"] == ["ISCED_5"] and by_code["infos"][0]["is_vocational"] is True
        everything = await call(client, "skolverket_susa_search_education_infos", {"limit": 10})
        assert router.last().url.params["size"] == "10" and everything["returned"] == 2


async def test_search_education_providers(router, make_client):
    router.add("GET", r"/emil3/educationProviders\?", load_fixture("susa_navet_providers.json"))
    tool = "skolverket_susa_search_education_providers"
    async with make_client("skolverket") as client:
        in_sundsvall = await call(client, tool, {"municipality_code": "2281", "school_type": "FHS"})
        assert router.last().url.path == f"{BASE_PATH}/educationProviders"
        assert router.last().url.params.get_list("schoolType") == ["FHS"]
        assert in_sundsvall["providers"] == [
            {
                "id": "p.sv.fhs.timra",
                "name": "Timrå folkhögskola",
                "responsible_body_type": "region",
                "municipality_codes": ["2262", "2281"],
                "towns": ["Timrå", "Sundsvall"],
                "url": "https://www.timra.fhsk.se",
            }
        ]
        by_name = await call(client, tool, {"text": "tekniska HOGSKOLAN"})
        assert [p["id"] for p in by_name["providers"]] == ["p.uoh.kth"]
        by_orgnr = await call(client, tool, {"text": "2021006237"})
        assert [p["id"] for p in by_orgnr["providers"]] == ["p.uoh.kth"]


async def test_upstream_errors_surface_as_tool_errors(router, make_client):
    not_found = {
        "timestamp": "2025-02-18T15:57:56+01:00",
        "status": 404,
        "type": "Not Found",
        "message": "EducationEvent with id 'i.uoh.uu.1te686' not found",
    }
    unavailable = {
        "timestamp": "2025-02-18T15:57:56+01:00",
        "status": 503,
        "type": "Service Unavailable",
        "message": "The system is currently updating education resources. Please try again later.",
    }
    router.add("GET", r"/emil3/educationEvents/", httpx2.Response(404, json=not_found))
    router.add("GET", r"/emil3/educationInfos\?", httpx2.Response(503, json=unavailable))
    async with make_client("skolverket") as client:
        err = await call_error(client, "skolverket_susa_get_education_event", {"event_id": "i.uoh.uu.1te686"})
        assert "Hittade inget utbildningstillfälle med id 'i.uoh.uu.1te686'" in err
        assert "skolverket_susa_get_education_info" in err and "HTTP 404" in err and "not found" in err
        err = await call_error(client, "skolverket_susa_search_education_infos", {})
        assert "HTTP 503" in err and "updating education resources" in err
        assert "Ange ett id" in await call_error(client, "skolverket_susa_get_education_info", {"education_id": " "})


async def test_api_info_and_codes_resource(router, make_client):
    router.add("GET", r"/susa-navet/emil3/api-info$", load_fixture("susa_navet_api_info.json"))
    async with make_client("skolverket") as client:
        info = await call(client, "skolverket_susa_api_info")
        resource = await client.read_resource("fuzzy://skolverket/susa-navet/codes")
    assert router.last().url.path == f"{BASE_PATH}/api-info"
    assert info == {
        "publisher": "Skolverket",
        "name": "susa-navet",
        "api_version": "1.0.0",
        "emil_schema_version": "1.0.0",
        "released": "2025-10-07",
        "documentation": "https://www.skolverket.se/om-skolverket/webbplatser-och-tjanster/oppna-data/"
        "api-for-utbildningstillfallen-susa-navet",
        "status": "beta",
        "base_url": "https://api.skolverket.se/susa-navet/emil3",
        "codes_resource": "fuzzy://skolverket/susa-navet/codes",
    }
    content = resource.contents[0]
    assert content.mime_type == "application/json"
    codes = json.loads(content.text)["lists"]
    school_types = {v["code"]: v for v in codes["C_SchoolType"]["values"]}
    assert school_types["YH"]["populated"] is True and school_types["GY"]["populated"] is False
    assert "VUX" not in school_types and len(school_types) == 23
    assert [v["code"] for v in codes["C_Credits"]["values"]] == ["hp", "yh", "gy", "fup", "vp", "nyp"]
    assert "A 1 gr" in {v["code"] for v in codes["C_StudentAid"]["values"]}
    assert {v["code"] for v in codes["C_ExecutionCondition"]["values"]} == {"0", "1", "2", "3"}
    for name in ("C_Body", "C_EducationLevel", "C_Qualification", "C_Orientation", "C_TimeOfStudy", "SemesterType"):
        assert codes[name]["values"]


def test_tolerant_parsing_helpers():
    # dict-or-list tolerance, numeric areaCode and a bare provider string
    row = event_row(
        {
            "id": "e.x.1",
            "status": "active",
            "content": {
                "identifier": "e.x.1",
                "providers": "p.x.abc",
                "title": {"strings": {"lang": "swe", "value": "Svetsning"}},
                "locations": {"town": "Stockholm", "areaCode": 180},
                "languageOfInstructions": "swe",
                "execution": {"start": "2026-01-12T00:00:00"},
            },
        },
        ("swe",),
    )
    assert row.provider_ids == ["p.x.abc"] and row.title == "Svetsning"
    assert row.municipality_codes == ["0180"] and row.languages == ["swe"] and row.start == "2026-01-12"
    assert row.inactive is None and row.status is None
    # Legacy text in ``content``; unflagged HTML is stripped; preference falls back to first text.
    assert text_of({"strings": [{"lang": "swe", "content": "Gammal text"}]}) == "Gammal text"
    assert text_of({"strings": [{"lang": "swe", "value": "A<br/>B &amp; C"}]}) == "A\nB & C"
    assert text_of({"strings": [{"lang": "fin", "value": "Suomeksi"}]}) == "Suomeksi"
    assert text_of({"strings": [{"lang": "swe", "value": "x < y"}]}) == "x < y"
    assert url_of({"urls": {"lang": "swe", "value": "//www.example.se"}}) == "https://www.example.se"
    assert url_of({"urls": [{"value": "mailto:a@b.se"}]}) == "mailto:a@b.se"
    unknown = code_of({"type": "C_Credits", "code": "xyz"})
    assert unknown is not None and unknown.code == "xyz" and unknown.label is None
    bare = code_of("YH", "C_SchoolType")
    assert bare is not None and bare.label == "Yrkeshögskola"
    assert normalize_school_types("vux, yh") == ["VUXGR", "VUXGRAN", "VUXGY", "VUXGYAN", "VUXSFI", "YH"]
    assert normalize_school_types('["HS","HS"]') == ["HS"]
    assert normalize_municipality_codes("0180, Upplands Väsby") == ["0180", "0114"]
    assert normalize_municipality_codes(["0180 2281", "180"]) == ["0180", "2281"]
    # Multi-valued LangStrings (keywords, degrees): spec shape, legacy list shape, language preference and fallback.
    keywords = {
        "strings": [{"lang": "swe", "value": "a"}, {"lang": "eng", "value": "b"}, {"lang": "swe", "value": "c"}]
    }
    assert texts_of(keywords) == ["a", "c"] and texts_of(keywords, ENGLISH) == ["b"]
    assert texts_of([{"strings": [{"lang": "swe", "value": "a"}]}, {"strings": [{"lang": "swe", "value": "b"}]}]) == [
        "a",
        "b",
    ]
    assert texts_of({"strings": [{"lang": "fin", "value": "x"}, {"lang": "deu", "value": "y"}]}) == ["x", "y"]
    assert texts_of({"strings": [{"lang": "swe", "value": "a"}, {"value": "neutral"}]}, ENGLISH) == ["a", "neutral"]


async def test_following_next_page_returns_every_match_exactly_once(router, make_client):
    rows = event_rows(3)  # 12 rows; with page_size 4 every page holds two county-22 matches
    router.add("GET", EVENTS, dataset("educationEvents", rows))
    in_county = [r["id"] for r in rows if r["content"] and r["content"]["locations"][0]["areaCode"].startswith("22")]
    assert len(in_county) == 6
    async with make_client("skolverket") as client:
        for limit in (1, 2, 3, 5):
            found, calls = await follow(
                client, SEARCH_EVENTS, {"county_code": "22", "limit": limit, "page_size": 4}, "events"
            )
            assert found == in_county, limit
        assert calls == 2  # limit 5: page 0–2 (5 matches, one left on page 2), then page 2 after 2 matches

        # Without client filters the API page size is ``limit`` and one page is fetched per call.
        router.requests.clear()
        found, calls = await follow(client, SEARCH_EVENTS, {"limit": 6, "page_size": 4}, "events")
        assert found == [r["id"] for r in rows] and calls == 2
        assert [(r.url.params["page"], r.url.params["size"]) for r in router.requests] == [("0", "6"), ("1", "6")]

        # skip_matches applies to the first scanned page only.
        router.requests.clear()
        skipped = await call(client, SEARCH_EVENTS, {"limit": 6, "skip_matches": 2})
        assert [e["id"] for e in skipped["events"]] == [r["id"] for r in rows[2:8]]
        assert skipped["skip_matches"] == 2 and skipped["next_page"] == 1 and skipped["next_skip_matches"] == 2
        assert skipped["matched"] == 10 and skipped["returned"] == 6 and len(router.requests) == 2


async def test_missing_data_is_counted_not_silently_dropped(router, make_client):
    data = load_fixture("susa_navet_events.json")
    kth, yh, fhs, _deleted = data["educationEvents"]
    del kth["content"]["languageOfInstructions"], kth["content"]["title"]
    yh["content"]["execution"] = {"condition": {"type": "C_ExecutionCondition", "code": "3"}}  # flexible start
    del fhs["content"]["locations"]
    router.add("GET", EVENTS, data)

    async def run(args):
        result = await call(client, SEARCH_EVENTS, {"max_pages": 1, **args})
        return [e["id"] for e in result["events"]], result.get("excluded")

    async with make_client("skolverket") as client:
        assert await run({"county_code": "22"}) == (["e.sv.yh.ytpl.2281.20252"], {"no_location": 1, "no_content": 1})
        assert await run({"start_from": "2025-01-01"}) == (
            ["e.uoh.kth.dd1420.60090.20251", "e.sv.fhs.allm.2262.20252"],
            {"no_start_date": 1, "no_content": 1},
        )
        assert await run({"language": "swe"}) == (["e.sv.yh.ytpl.2281.20252"], {"no_language": 1, "no_content": 1})
        assert await run({"text": "datalogi"}) == ([], {"no_title": 1, "no_content": 1})
        # A row that fails another filter is a plain non-match, not "missing data" (KTH: 0180 is outside county 22).
        assert await run({"county_code": "22", "language": "eng"}) == ([], {"no_location": 1, "no_content": 1})
        result = await call(client, SEARCH_EVENTS, {"county_code": "22", "max_pages": 1})
    assert "2 poster kunde inte prövas" in result["hint"] and "1 utan adress med kommunkod" in result["hint"]


async def test_updated_since_passes_inactive_rows_through_client_filters(router, make_client):
    infos = load_fixture("susa_navet_infos.json")
    infos["educationInfos"].append({"id": "i.sv.gone", "status": "DELETED", "content": None})
    router.add("GET", r"/emil3/educationInfos\?", infos)
    providers = load_fixture("susa_navet_providers.json")
    gone = copy.deepcopy(providers["educationProviders"][0])
    gone.update(id="p.sv.gone", status="DELETED")
    gone["content"]["identifier"] = "p.sv.gone"
    providers["educationProviders"].append(gone)  # inactive but with content (in 0180, not 2281)
    router.add("GET", r"/emil3/educationProviders\?", providers)
    async with make_client("skolverket") as client:
        plain = await call(client, "skolverket_susa_search_education_infos", {"text": "elektronik"})
        assert [i["id"] for i in plain["infos"]] == ["i.uoh.uu.1te686.14483.20242"]
        assert plain["excluded"] == {"no_content": 1}
        delta = await call(
            client, "skolverket_susa_search_education_infos", {"text": "elektronik", "updated_since": "2026-01-01"}
        )
        assert [i["id"] for i in delta["infos"]] == ["i.uoh.uu.1te686.14483.20242", "i.sv.gone"]
        assert delta["infos"][1]["inactive"] is True and "excluded" not in delta

        tool = "skolverket_susa_search_education_providers"
        plain = await call(client, tool, {"municipality_code": "2281"})
        assert [p["id"] for p in plain["providers"]] == ["p.sv.fhs.timra"]
        delta = await call(client, tool, {"municipality_code": "2281", "updated_since": "2026-01-01"})
        assert [p["id"] for p in delta["providers"]] == ["p.sv.fhs.timra", "p.sv.gone"]
        assert delta["providers"][1]["status"] == "DELETED" and delta["providers"][1]["inactive"] is True


async def test_large_list_pages_are_not_cached(router):
    router.add("GET", EVENTS, load_fixture("susa_navet_events.json"))
    settings = Settings(enabled_sources=frozenset({"skolverket"}), max_retries=0, cache_ttl_seconds=300)
    server = build_server(settings, transport=httpx2.MockTransport(router), rate_limits={})
    async with Client(server, raise_exceptions=False) as client:
        for _ in range(2):
            await call(client, SEARCH_EVENTS, {"text": "datalogi", "max_pages": 1})  # page_size 500
        assert len(router.requests) == 2
        for _ in range(2):
            await call(client, SEARCH_EVENTS, {"text": "datalogi", "max_pages": 1, "page_size": 200})
        assert len(router.requests) == 3
        for _ in range(2):
            await call(client, SEARCH_EVENTS, {"limit": 4})  # unfiltered: size = limit, cached
        assert len(router.requests) == 4


async def test_limit_is_capped_and_row_titles_are_cut(router, make_client):
    router.add("GET", EVENTS, load_fixture("susa_navet_events.json"))
    async with make_client("skolverket") as client:
        await call(client, SEARCH_EVENTS, {"limit": 500})
    assert router.last().url.params["size"] == "100"
    long_title = "Lång titel " * 40
    row = event_row({"id": "e.x.1", "status": "ACTIVE", "content": {"title": {"strings": [{"value": long_title}]}}}, ())
    assert row.title is not None and row.title.endswith("…[förkortad]")
    assert len(row.title) <= ROW_TITLE_CHARS + len(" …[förkortad]")


async def test_by_id_rejects_dot_segments_and_non_wrapper_bodies(router, make_client):
    router.add("GET", r"/emil3/educationEvents/e\.x\.list$", load_fixture("susa_navet_events.json"))
    router.add("GET", r"/emil3/educationEvents/e\.x\.info$", load_fixture("susa_navet_api_info.json"))
    router.add("GET", r"/emil3/educationInfos/", load_fixture("susa_navet_info.json"))
    tool = "skolverket_susa_get_education_event"
    async with make_client("skolverket") as client:
        for bad in (".", "..", " .. ", "../educationInfos", "e.x/y", "e.x?y=1", "e x", "_x"):
            assert "Ogiltigt id" in await call_error(client, tool, {"event_id": bad}), bad
        assert router.requests == []
        for ident in ("e.x.list", "e.x.info"):
            err = await call_error(client, tool, {"event_id": ident})
            assert "Oväntat svar" in err and "ingen id/status/content" in err
        await call(client, "skolverket_susa_get_education_info", {"education_id": "i.AF.100754_10026327_322311"})
    assert router.last().url.path == f"{BASE_PATH}/educationInfos/i.AF.100754_10026327_322311"
