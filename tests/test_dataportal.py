# SPDX-FileCopyrightText: 2026 Deniz Özer
#
# SPDX-License-Identifier: MIT

import copy
import json
import re
from collections.abc import Callable
from typing import Any

import httpx2
import pytest

from fuzzy_mcp.sources.dataportal import (
    MODIFIED_SORT,
    OUTPUT_BUDGET_CHARS,
    build_dataset_query,
    esc,
    predicate_hash,
    redact_graph,
    representable,
    resource_batches,
    resource_query,
    text_tokens,
)

from .conftest import call, call_error, load_fixture

pytestmark = pytest.mark.anyio

STORE = "https://admin.dataportal.se/store"
# Values are fully Lucene-escaped (':' and '/' included), so the recipes' rdfType clause reads:
DATASET = r"rdfType:http\:\/\/www.w3.org\/ns\/dcat#Dataset AND public:true"
SKARA = "https://catalog.skara.se/store/1/resource/"
SKARA_ESC = r"https\:\/\/catalog.skara.se\/store\/1\/resource\/"
SCB_LOOKUP = r"public:true AND (resource:http\:\/\/dataportal.se\/organisation\/SE2021000837)"
MAIN_TYPES = (
    r"rdfType:(http\:\/\/www.w3.org\/ns\/dcat#Dataset OR http\:\/\/www.w3.org\/ns\/dcat#DatasetSeries"
    r" OR http\:\/\/www.w3.org\/ns\/dcat#DataService OR http\:\/\/entryscape.com\/terms\/IndependentDataService)"
)
EMPTY = {"resource": {"children": []}, "results": 0, "limit": 20, "offset": 0}
VCARD = "http://www.w3.org/2006/vcard/ns#"
RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
DCT = "http://purl.org/dc/terms/"
FOAF_NAME = "http://xmlns.com/foaf/0.1/name"
PRIVATE = "http://purl.org/adms/publishertype/PrivateIndividual(s)"
# Lucene: a value that is one single term (every special character and whitespace escaped).
ONE_TERM = re.compile(r'(?:\\.|[^\\+\-!():^\[\]"{}~*?|&;/\s])*', re.DOTALL)


def params(request: httpx2.Request) -> dict[str, str]:
    return dict(request.url.params)


def solr(router, *routes: tuple[Callable[[dict[str, str]], bool], Any]) -> None:
    """Route GET /store/search by its decoded parameters; first matching predicate wins."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        p = params(request)
        for matches, payload in routes:
            if matches(p):
                return payload if isinstance(payload, httpx2.Response) else httpx2.Response(200, json=payload)
        return httpx2.Response(400, json={"error": "Search failed due to wrong parameters"})

    router.add("GET", r"/store/search\?", handler)


def searches(router) -> list[dict[str, str]]:
    return [params(r) for r in router.requests if r.url.path == "/store/search"]


def child(ctx: str, entry: str, uri: str, metadata: dict[str, Any]) -> dict[str, Any]:
    return {
        "entryId": entry,
        "contextId": ctx,
        "info": {
            f"{STORE}/{ctx}/entry/{entry}": {"http://entrystore.org/terms/resource": [{"type": "uri", "value": uri}]}
        },
        "metadata": metadata,
    }


def results(*children: dict[str, Any]) -> dict[str, Any]:
    return {"resource": {"children": list(children)}, "results": len(children), "limit": 100, "offset": 0}


def entry_info(ctx: str, entry: str, resource: str) -> dict[str, Any]:
    """GET /store/{ctx}/entry/{id}?format=application/json (spec §1.7)."""
    return {
        "entryId": entry,
        "info": {
            f"{STORE}/{ctx}/entry/{entry}": {
                "http://entrystore.org/terms/resource": [{"type": "uri", "value": resource}]
            }
        },
    }


def skara_routes(router, graph: dict[str, Any] | None = None) -> None:
    router.add("GET", r"/store/83/metadata/9\?", graph or load_fixture("dataportal_metadata_recursive.json"))
    router.add("GET", r"/store/83/entry/9\?", load_fixture("dataportal_entry_83_9.json"))


def literal(value: str, lang: str | None = None) -> dict[str, str]:
    return {"type": "literal", "value": value, **({"lang": lang} if lang else {})}


def uri(value: str) -> dict[str, str]:
    return {"type": "uri", "value": value}


# --- pure helpers -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "predicate,expected",
    [
        ("http://purl.org/dc/terms/publisher", "9259d4c1"),
        ("http://www.w3.org/ns/dcat#theme", "0ce6b231"),
        ("http://purl.org/dc/terms/license", "3494e2ce"),
        ("http://purl.org/dc/terms/format", "b5d28d0a"),
        ("http://purl.org/dc/terms/modified", "3e2f60da"),
        ("http://www.w3.org/ns/dcat#inSeries", "0f683595"),
        ("http://data.europa.eu/r5r/hvdCategory", "a8c277a7"),
    ],
)
def test_predicate_hash_matches_documented_fields(predicate, expected):
    assert predicate_hash(predicate) == expected


def test_query_building_follows_the_recipes():
    assert esc("http://www.w3.org/ns/dcat#Dataset (x)") == r"http\:\/\/www.w3.org\/ns\/dcat#Dataset\ \(x\)"
    # Recipe 1: all public datasets.
    assert build_dataset_query() == DATASET
    # Recipe 2 (portal text block) for a single word.
    assert build_dataset_query("vaccin") == (
        DATASET + " AND (title:vaccin OR description:vaccin OR tag.literal:vaccin OR all:vaccin)"
    )
    assert build_dataset_query(theme="HEAL", media_types=["text/csv"]) == (
        DATASET
        + r" AND metadata.predicate.uri.0ce6b231:http\:\/\/publications.europa.eu\/resource\/authority"
        + r"\/data\-theme\/HEAL"
        + ' AND (metadata.predicate.literal_s.b5d28d0a:"text/csv"'
        + ' OR related.metadata.predicate.literal_s.b5d28d0a:"text/csv")'
    )
    assert resource_query(["http://id.kb.se/organisations/SE2021000837"]) == (
        r"public:true AND (resource:http\:\/\/id.kb.se\/organisations\/SE2021000837)"
    )
    # "Senast ändrad" sorts like the portal: dcterms:modified, then the entry timestamp (no space after ",").
    assert MODIFIED_SORT == "metadata.predicate.literal_s.3e2f60da desc,modified desc"


def test_esc_escapes_backslash_first_and_every_special_character():
    assert esc("a\\b") == "a\\\\b"
    assert esc(r"http://x\) OR \(public\:false") == r"http\:\/\/x\\\)\ OR\ \\\(public\\\:false"
    for ch in '\\+-!():^[]"{}~*?|&;/ \t\n\u00a0':
        assert esc(ch) == "\\" + ch, ch
    assert esc("https://example.org/~user/data") == r"https\:\/\/example.org\/\~user\/data"
    nasty = [
        r"http://x\) OR \(public\:false",
        "http://dataportal.se/organisation/SE2021006545 OR rdfType:*",
        'https://example.org/a b"c\\d',
        "https://example.org/{x}|[y]^z~1 && !q",
        "x\\",
    ]
    for value in nasty:
        assert ONE_TERM.fullmatch(esc(value)), value


def test_text_tokens_are_solr_friendly():
    tokens = text_tokens('IT och skolverksamhetsstatistik "(2024)" a:b skol* AND miljö-')
    assert tokens == ["IT*", "och", "skolverksamhets", "2024", r"a\:b", "skol*", "and", r"miljö\-"]
    assert text_tokens("  ") == []


def test_resource_batches_respect_chunk_size_and_skip_unrepresentable():
    uris = [f"http://dataportal.se/organisation/SE{n:010d}" for n in range(45)]
    batches = resource_batches(uris)
    assert [len(b) for b in batches] == [20, 20, 5]
    assert all(len(resource_query(b)) <= 1500 for b in batches)
    assert representable('https://example.org/a b"c\\d')
    assert not representable("https://example.org/ctl\x01")
    assert not representable("https://example.org/" + "x" * 1600)
    assert not representable("")


def test_redact_graph_removes_personal_data():
    graph = {
        "https://x/contact": {
            RDF_TYPE: [uri(VCARD + "Individual")],
            VCARD + "fn": [literal("Anna Andersson")],
            VCARD + "hasTelephone": [{"type": "bnode", "value": "_:t"}],
        },
        "_:t": {VCARD + "hasValue": [uri("tel:+46000000")]},
        "https://x/agent": {DCT + "type": [uri(PRIVATE)], FOAF_NAME: [literal("Per Persson")]},
        "https://x/org": {FOAF_NAME: [literal("Skara kommun")]},
    }
    redacted = redact_graph(graph)
    assert redacted == {
        "https://x/contact": {RDF_TYPE: [uri(VCARD + "Individual")]},
        "https://x/agent": {DCT + "type": [uri(PRIVATE)]},
        "https://x/org": {FOAF_NAME: [literal("Skara kommun")]},
    }


# --- dataportal_search_datasets -------------------------------------------------------------------


async def test_search_datasets_builds_exact_query_and_parses_rows(router, make_client):
    solr(
        router,
        (lambda p: p["query"] == SCB_LOOKUP, load_fixture("dataportal_publisher_lookup.json")),
        (lambda p: p["query"].startswith(DATASET), load_fixture("dataportal_search_datasets.json")),
    )
    async with make_client("dataportal") as client:
        result = await call(
            client,
            "dataportal_search_datasets",
            {
                "query": "skola statistik",
                "publisher": "SCB",
                "theme": "educ",
                "format": "csv",
                "limit": 10,
                "offset": 20,
            },
        )

    text = "(skola AND statistik)"
    expected = (
        DATASET
        + f" AND (title:{text} OR description:{text} OR tag.literal:{text} OR all:{text})"
        + r" AND metadata.predicate.uri.9259d4c1:(http\:\/\/dataportal.se\/organisation\/SE2021000837"
        + r" OR http\:\/\/id.kb.se\/organisations\/SE2021000837)"
        + r" AND metadata.predicate.uri.0ce6b231:http\:\/\/publications.europa.eu\/resource\/authority"
        + r"\/data\-theme\/EDUC"
        + ' AND (metadata.predicate.literal_s.b5d28d0a:"text/csv"'
        + ' OR related.metadata.predicate.literal_s.b5d28d0a:"text/csv")'
    )
    first = router.requests[0]
    assert str(first.url).startswith(
        f"{STORE}/search?type=solr&query=rdfType%3Ahttp%5C%3A%5C%2F%5C%2Fwww.w3.org%5C%2Fns%5C%2Fdcat%23Dataset"
        "+AND+public%3Atrue+AND+"
    )
    assert first.headers["accept"] == "application/json"
    assert params(first) == {"type": "solr", "query": expected, "limit": "10", "offset": "20", "sort": "score desc"}
    # Publisher names are resolved with one resource lookup (only URI publishers).
    assert searches(router)[1] == {"type": "solr", "query": SCB_LOOKUP, "limit": "100", "offset": "0"}
    assert len(router.requests) == 2

    assert result["solr_query"] == expected
    assert (result["total"], result["offset"], result["limit"], result["returned"]) == (57, 20, 10, 2)
    assert result["truncated"] is True and result["next_offset"] == 22
    assert result["theme"] == "EDUC" and result["media_types"] == ["text/csv"]
    assert result["publisher_uris"] == [
        "http://dataportal.se/organisation/SE2021000837",
        "http://id.kb.se/organisations/SE2021000837",
    ]
    scb, umea = result["datasets"]
    assert scb["context_id"] == "66" and scb["entry_id"] == "71946"
    assert scb["portal_url"] == "https://www.dataportal.se/datasets/66_71946"
    assert scb["dataset_uri"] == "https://statistikdatabasen.scb.se/dataset/tab6296"
    assert scb["title"] == "Folkmängd efter region och kön"  # Swedish preferred over the first (English) title
    assert scb["description"].startswith("Folkmängden efter region") and scb["description"].endswith("…")
    assert len(scb["description"]) <= 300
    assert scb["publisher_uri"] == "http://dataportal.se/organisation/SE2021000837"
    assert scb["publisher_name"] == "Statistiska centralbyrån"
    assert scb["themes"] == ["SOCI", "EDUC"]
    assert scb["keywords"] == ["befolkning", "skola"]
    assert scb["modified"] == "2026-02-21T08:00:00Z"
    assert scb["licenses"] == ["http://creativecommons.org/publicdomain/zero/1.0/"]
    assert scb["formats"] == ["text/csv", "application/json"]
    assert scb["distribution_count"] == 2
    assert umea["title"] == "Luftkvalitetsmätningar Västra esplanaden"
    assert umea["publisher_name"] == "Umeå kommun" and "publisher_uri" not in umea
    assert umea["portal_url"] == "https://www.dataportal.se/datasets/43_69395"


@pytest.mark.parametrize(
    "publisher,clause",
    [
        (
            "202100-6545",
            r"metadata.predicate.uri.9259d4c1:(http\:\/\/dataportal.se\/organisation\/SE2021006545"
            r" OR http\:\/\/id.kb.se\/organisations\/SE2021006545)",
        ),
        (
            "fohm",
            r"metadata.predicate.uri.9259d4c1:(http\:\/\/dataportal.se\/organisation\/SE2021006545"
            r" OR http\:\/\/id.kb.se\/organisations\/SE2021006545)",
        ),
        (
            "Folkhälsomyndigheten",
            r"metadata.predicate.uri.9259d4c1:(http\:\/\/dataportal.se\/organisation\/SE2021006545"
            r" OR http\:\/\/id.kb.se\/organisations\/SE2021006545)",
        ),
        (
            "SE2021004185",
            r"metadata.predicate.uri.9259d4c1:(http\:\/\/dataportal.se\/organisation\/SE2021004185"
            r" OR http\:\/\/id.kb.se\/organisations\/SE2021004185)",
        ),
        (
            "http://dataportal.se/organisation/SE2021006545",
            r"metadata.predicate.uri.9259d4c1:http\:\/\/dataportal.se\/organisation\/SE2021006545",
        ),
        (  # '~' is Lucene's fuzzy operator: it must be escaped, not interpreted
            "https://example.org/~user/data",
            r"metadata.predicate.uri.9259d4c1:https\:\/\/example.org\/\~user\/data",
        ),
    ],
)
async def test_search_publisher_forms_without_text(router, make_client, publisher, clause):
    solr(router, (lambda p: True, load_fixture("dataportal_search_datasets.json")))
    async with make_client("dataportal") as client:
        result = await call(
            client, "dataportal_search_datasets", {"publisher": publisher, "resolve_publisher_names": False}
        )
    assert len(router.requests) == 1
    assert params(router.last()) == {
        "type": "solr",
        "query": f"{DATASET} AND {clause}",
        "limit": "20",
        "offset": "0",
        "sort": "metadata.predicate.literal_s.3e2f60da desc,modified desc",
    }
    assert "publisher_name" not in result["datasets"][0]


@pytest.mark.parametrize(
    "publisher",
    [
        r"http://x\) OR \(public\:false",  # backslash would undo the escaping
        "http://dataportal.se/organisation/SE2021006545 OR rdfType:*",  # whitespace would add a clause
        'http://x/"a"',
        "http://x/<a>",
        "http://x/a|b",
        "http://x/{a}",
        "http://x/a^b",
        "http://x/a`b",
        "http://x/a\u00a0b",
        "http:///no-host",
        "http://" + "x" * 1600,
    ],
)
async def test_user_uri_injection_is_rejected(router, make_client, publisher):
    async with make_client("dataportal") as client:
        err = await call_error(client, "dataportal_search_datasets", {"publisher": publisher})
        assert "URI" in err
        err = await call_error(client, "dataportal_get_dataset", {"dataset": publisher})
        assert "URI" in err
    assert router.requests == []


async def test_search_sort_and_limit_cap(router, make_client):
    solr(router, (lambda p: True, EMPTY))
    async with make_client("dataportal") as client:
        capped = await call(client, "dataportal_search_datasets", {"limit": 100, "resolve_publisher_names": False})
        await call(client, "dataportal_search_datasets", {"query": "skola"})
        await call(client, "dataportal_search_datasets", {"query": "skola", "sort": "modified"})
        await call(client, "dataportal_list_publisher_datasets", {"publisher": "skolverket", "query": "skola"})
    sent = [s for s in searches(router) if "facetFields" not in s]
    assert sent[0]["limit"] == "50" and capped["limit"] == 50
    assert any("limit begränsades till 50" in n for n in capped["notes"])
    assert [s["sort"] for s in sent] == [MODIFIED_SORT, "score desc", MODIFIED_SORT, MODIFIED_SORT]


async def test_search_text_is_bounded(router, make_client):
    solr(router, (lambda p: True, EMPTY))
    words = " ".join(f"ord{i}" for i in range(12))
    async with make_client("dataportal") as client:
        result = await call(client, "dataportal_search_datasets", {"query": words})
    assert "ord7" in result["solr_query"] and "ord8" not in result["solr_query"]
    assert any("8 första sökorden" in n for n in result["notes"])


async def test_search_query_too_long_is_rejected(router, make_client):
    agents = results(
        *[
            child(
                "7",
                str(i),
                f"http://example.org/agents/{'a' * 90}/{i}",
                {f"http://example.org/agents/{'a' * 90}/{i}": {FOAF_NAME: [literal("Kommunen")]}},
            )
            for i in range(30)
        ]
    )
    solr(router, (lambda p: "title:" in p["query"], agents), (lambda p: True, EMPTY))
    async with make_client("dataportal") as client:
        err = await call_error(client, "dataportal_search_datasets", {"publisher": "Kommunen"})
    assert "för lång" in err
    assert len(router.requests) == 1  # never sent


async def test_search_resolves_publisher_by_name(router, make_client):
    agent_query = r"rdfType:http\:\/\/xmlns.com\/foaf\/0.1\/Agent AND public:true AND title:Trafikverket"
    exact = "http://dataportal.se/organisation/SE1111111111"
    agents = results(
        child("7", "1", exact, {exact: {FOAF_NAME: [literal("Trafikverket", "sv")]}}),
        child(
            "7",
            "2",
            "http://example.org/agent/2",
            {"http://example.org/agent/2": {FOAF_NAME: [literal("Trafikverket Region Väst")]}},
        ),
    )
    solr(
        router,
        (lambda p: p["query"] == agent_query, agents),
        (lambda p: p["query"].startswith(DATASET), EMPTY),
    )
    async with make_client("dataportal") as client:
        result = await call(client, "dataportal_search_datasets", {"publisher": "Trafikverket", "query": "väg"})
    assert searches(router)[0]["limit"] == "50"
    assert searches(router)[1]["query"] == (
        DATASET
        + " AND (title:väg OR description:väg OR tag.literal:väg OR all:väg)"
        + r" AND metadata.predicate.uri.9259d4c1:http\:\/\/dataportal.se\/organisation\/SE1111111111"
    )
    assert result["publisher_uris"] == [exact]
    assert result["total"] == 0 and result["truncated"] is False
    assert any("Trafikverket" in n for n in result["notes"])
    assert len(router.requests) == 2  # no facet fallback for name matches


async def test_publisher_name_match_hides_private_individuals(router, make_client):
    agents = results(
        child(
            "7",
            "1",
            "http://example.org/agent/person",
            {"http://example.org/agent/person": {FOAF_NAME: [literal("Anna Andersson")], DCT + "type": [uri(PRIVATE)]}},
        ),
        child(
            "7",
            "2",
            "http://example.org/agent/company",
            {"http://example.org/agent/company": {FOAF_NAME: [literal("Anderssons Åkeri AB")]}},
        ),
    )
    solr(router, (lambda p: "title:" in p["query"], agents), (lambda p: True, EMPTY))
    async with make_client("dataportal") as client:
        hidden = await call(client, "dataportal_search_datasets", {"publisher": "Andersson"})
        shown = await call(
            client, "dataportal_search_datasets", {"publisher": "Andersson", "include_personal_data": True}
        )
    assert len(hidden["publisher_uris"]) == 2
    assert "Anna Andersson" not in json.dumps(hidden, ensure_ascii=False)
    assert any("Anderssons Åkeri AB" in n for n in hidden["notes"])
    assert any("privatpersoner" in n for n in hidden["notes"])
    assert any("Anna Andersson" in n for n in shown["notes"])


async def test_search_redacts_private_individual_publishers(router, make_client):
    person = "http://dataportal.se/organisation/SE1234567890"
    hits = results(
        child(
            "5",
            "1",
            "https://x/ds1",
            {"https://x/ds1": {DCT + "title": [literal("A")], DCT + "publisher": [uri(person)]}},
        ),
        child(
            "5",
            "2",
            "https://x/ds2",
            {
                "https://x/ds2": {
                    DCT + "title": [literal("B")],
                    DCT + "publisher": [{"type": "bnode", "value": "_:p"}],
                },
                "_:p": {FOAF_NAME: [literal("Per Persson")], DCT + "type": [uri(PRIVATE)]},
            },
        ),
    )
    agent = results(
        child("9", "9", person, {person: {FOAF_NAME: [literal("Anna Andersson")], DCT + "type": [uri(PRIVATE)]}})
    )
    solr(router, (lambda p: "resource:" in p["query"], agent), (lambda p: True, hits))
    async with make_client("dataportal") as client:
        default = await call(client, "dataportal_search_datasets", {"query": "test"})
        opted = await call(client, "dataportal_search_datasets", {"query": "test", "include_personal_data": True})
    text = json.dumps(default, ensure_ascii=False)
    assert "Anna Andersson" not in text and "Per Persson" not in text
    one, two = default["datasets"]
    assert one["publisher_uri"] == person and one["publisher_name_redacted"] is True
    assert two["publisher_name_redacted"] is True and "publisher_name" not in two
    assert any("privatpersoner" in n for n in default["notes"])
    assert [d["publisher_name"] for d in opted["datasets"]] == ["Anna Andersson", "Per Persson"]


async def test_publisher_name_errors(router, make_client):
    many = results(
        *[
            child(
                "9",
                str(i),
                f"http://example.org/k{i}",
                {f"http://example.org/k{i}": {FOAF_NAME: [literal(f"Kommun nummer {i}")]}},
            )
            for i in range(12)
        ]
    )
    solr(router, (lambda p: "title:kommun" in p["query"], many), (lambda p: True, results()))
    async with make_client("dataportal") as client:
        err = await call_error(client, "dataportal_search_datasets", {"publisher": "kommun"})
        assert "matchar 12 utgivare" in err
        err = await call_error(client, "dataportal_search_datasets", {"publisher": "Okänd myndighet"})
        assert "Hittade ingen utgivare" in err


async def test_publisher_facet_fallback_for_org_number(router, make_client):
    variant = "https://dataportal.se/organisation/SE2021004185"
    facets = {
        "resource": {"children": []},
        "results": 0,
        "limit": 0,
        "offset": 0,
        "facetFields": [
            {
                "name": "metadata.predicate.uri.9259d4c1",
                "valueCount": 4,
                "values": [
                    {"name": variant, "count": 41},
                    {"name": "http://dataportal.se/organisation/SE2021004185", "count": 3},
                    {"name": "http://dataportal.se/organisation/SE20210041851", "count": 1},
                    {"name": "http://dataportal.se/organisation/SE2021004185/\x00", "count": 1},  # unusable
                ],
            }
        ],
    }
    solr(
        router,
        (lambda p: "facetFields" in p, facets),
        (lambda p: p["query"] == SCB_LOOKUP, load_fixture("dataportal_publisher_lookup.json")),
        (lambda p: r"https\:\/\/dataportal.se" in p["query"], load_fixture("dataportal_search_datasets.json")),
        (lambda p: p["query"].startswith(DATASET), EMPTY),
    )
    async with make_client("dataportal") as client:
        result = await call(client, "dataportal_list_publisher_datasets", {"publisher": "skolverket"})

    first, facet, retry = searches(router)[:3]
    assert first == {
        "type": "solr",
        "query": DATASET
        + r" AND metadata.predicate.uri.9259d4c1:(http\:\/\/dataportal.se\/organisation\/SE2021004185"
        + r" OR http\:\/\/id.kb.se\/organisations\/SE2021004185)",
        "limit": "25",
        "offset": "0",
        "sort": MODIFIED_SORT,
    }
    assert facet == {
        "type": "solr",
        "query": DATASET,
        "limit": "0",
        "offset": "0",
        "facetFields": "metadata.predicate.uri.9259d4c1",
        "facetLimit": "1000",
        "facetMatches": ".*202100-?4185.*",
    }
    assert retry["query"].endswith(r" OR https\:\/\/dataportal.se\/organisation\/SE2021004185)")
    assert result["publisher_uris"][-1] == variant and len(result["publisher_uris"]) == 3
    assert any("utgivarfacetten" in n for n in result["notes"])
    assert result["returned"] == 2 and result["datasets"][0]["publisher_name"] == "Statistiska centralbyrån"


async def test_search_validation_errors(router, make_client):
    async with make_client("dataportal") as client:
        err = await call_error(client, "dataportal_search_datasets", {"theme": "XYZ"})
        assert "Okänt tema" in err and "EDUC" in err
        err = await call_error(client, "dataportal_search_datasets", {"publisher": "12345-678"})
        assert "Ogiltigt organisationsnummer" in err
        # Full-width digits are never read as an organisation number (only a name search is made).
        await call_error(client, "dataportal_search_datasets", {"publisher": "２０２１００-６５４５"})
        err = await call_error(client, "dataportal_search_datasets", {"format": "foo"})
        assert "Okänt format" in err
    assert all(r.url.path == "/store/search" and "title:" in params(r)["query"] for r in router.requests)


async def test_search_upstream_error_surfaces(router, make_client):
    router.add("GET", r"/store/search\?", httpx2.Response(400, json={"error": "Search failed due to wrong parameters"}))
    async with make_client("dataportal") as client:
        err = await call_error(client, "dataportal_search_datasets", {"query": "skola"})
    assert "HTTP 400" in err and "Search failed due to wrong parameters" in err


async def test_personal_data_opt_in_is_described(make_client):
    async with make_client("dataportal") as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    for name in ("dataportal_search_datasets", "dataportal_list_publisher_datasets", "dataportal_get_dataset"):
        prop = tools[name].input_schema["properties"]["include_personal_data"]
        assert "personuppgift" in prop["description"].lower() and prop["default"] is False


# --- dataportal_get_dataset ---------------------------------------------------------------------


async def test_get_dataset_flattens_recursive_metadata(router, make_client):
    skara_routes(router)
    async with make_client("dataportal") as client:
        detail = await call(client, "dataportal_get_dataset", {"dataset": "https://www.dataportal.se/sv/datasets/83_9"})
        short = await call(
            client, "dataportal_get_dataset", {"context_id": "83", "entry_id": "9", "max_distributions": 1}
        )

    request, entry = router.requests[:2]
    assert request.url.path == "/store/83/metadata/9"
    assert params(request) == {"recursive": "dcat", "format": "application/json"}
    assert request.headers["accept"] == "application/json"
    # The entry information tells which subject of the recursive graph is the dataset (spec §1.7).
    assert entry.url.path == "/store/83/entry/9" and params(entry) == {"format": "application/json"}
    assert len(router.requests) == 4  # everything was in the recursive graph: no extra lookups

    assert detail["context_id"] == "83" and detail["entry_id"] == "9"
    assert detail["portal_url"] == "https://www.dataportal.se/datasets/83_9"
    assert detail["metadata_url"] == f"{STORE}/83/metadata/9"
    assert detail["dataset_uri"] == SKARA + "9"
    assert detail["types"] == ["dcat:Dataset", "esterms:ServedByDataService"]
    assert detail["title"] == "Badplatser"
    assert detail["publisher"] == {
        "uri": "http://dataportal.se/organisation/SE2120001702",
        "name": "Skara kommun",
        "type": "LocalAuthority",
    }
    # vcard:Organization: a functional contact, not a personuppgift.
    assert detail["contact_points"] == [
        {"uri": SKARA + "3", "name": "Skara kommun, kontaktcenter", "email": "kontakt@example.org"}
    ]
    assert detail["themes"] == [{"code": "ENVI", "label": "Miljö"}]
    assert detail["keywords"] == ["bad", "friluftsliv"]
    assert detail["accrual_periodicity"] == {"code": "ANNUAL", "label": "årligen"}
    assert detail["access_rights"] == {"code": "PUBLIC", "label": "Publik"}
    assert detail["licenses"] == ["http://creativecommons.org/publicdomain/zero/1.0/"]
    assert detail["languages"] == ["SWE"]
    assert detail["hvd_categories"] == [{"code": "c_ac64a52d", "label": "Geospatiala data"}]
    assert detail["issued"] == "2021-06-18" and detail["modified"] == "2026-05-02"
    assert detail["temporal"] == [{"start": "2021-06-18"}]
    assert detail["spatial"] == [
        {"uri": "http://sws.geonames.org/2675397/"},
        {"bbox": "POLYGON((13.30 58.30,13.50 58.30,13.50 58.45,13.30 58.45,13.30 58.30))"},
    ]
    csv, api = detail["distributions"]
    assert csv == {
        "uri": SKARA + "11",
        "title": "Badplatser (CSV)",
        "access_urls": [SKARA + "11"],
        "download_urls": [SKARA + "11/badplatser.csv"],
        "format": "text/csv",
        "license": "http://creativecommons.org/publicdomain/zero/1.0/",
        "conforms_to": ["https://www.dataportal.se/specifications/badplatser/1.0"],
    }
    assert api["access_services"] == [SKARA + "20"] and api["format"] == "application/json"
    assert detail["data_services"] == [
        {
            "uri": SKARA + "20",
            "title": "Badplats-API",
            "endpoint_urls": ["https://api.example.org/badplatser"],
            "endpoint_descriptions": ["https://api.example.org/badplatser/openapi.json"],
        }
    ]
    assert detail["distributions_total"] == 2 and detail["distributions_truncated"] is False
    assert detail["attribution"] == (
        "Källa: Sveriges dataportal (Digg); utgivare: Skara kommun; "
        "licens: http://creativecommons.org/publicdomain/zero/1.0/"
    )
    assert "raw" not in detail and "notes" not in detail
    assert short["distributions_total"] == 2 and short["distributions_truncated"] is True
    assert len(short["distributions"]) == 1


async def test_get_dataset_uses_entry_resource_uri_not_a_sibling(router, make_client):
    """Record 124/2966 'Skolenhetsregistret' (title, publisher SE2021004185, CC0 1.0 and access right PUBLIC as
    verified in VERIFIERING §9.8; resource URI and the rest of the graph are illustrative). The recursive graph
    also holds a sibling dataset (124/2967 'Syllabus API', reached via dcat:servesDataset) with more properties,
    which the structural heuristic would pick."""
    router.add("GET", r"/store/124/metadata/2966\?", load_fixture("dataportal_metadata_124_2966.json"))
    router.add("GET", r"/store/124/entry/2966\?", load_fixture("dataportal_entry_124_2966.json"))
    async with make_client("dataportal") as client:
        detail = await call(
            client, "dataportal_get_dataset", {"dataset": "https://www.dataportal.se/datasets/124_2966"}
        )
    assert [r.url.path for r in router.requests] == ["/store/124/metadata/2966", "/store/124/entry/2966"]
    assert detail["dataset_uri"] == f"{STORE}/124/resource/2966"
    assert detail["title"] == "Skolenhetsregistret"
    assert detail["licenses"] == ["http://creativecommons.org/publicdomain/zero/1.0/"]
    assert detail["access_rights"] == {"code": "PUBLIC", "label": "Publik"}
    assert detail["publisher"] == {
        "uri": "http://dataportal.se/organisation/SE2021004185",
        "name": "Skolverket",
        "type": "NationalAuthority",
    }
    assert detail["portal_url"] == "https://www.dataportal.se/datasets/124_2966"
    assert [d["access_urls"] for d in detail["distributions"]] == [["https://api.skolverket.se/skolenhetsregistret"]]
    assert [s["title"] for s in detail["data_services"]] == ["Skolverkets öppna API:er"]
    assert "notes" not in detail


async def test_get_dataset_falls_back_to_heuristic_when_entry_info_fails(router, make_client):
    router.add("GET", r"/store/83/metadata/9\?", load_fixture("dataportal_metadata_recursive.json"))
    router.add("GET", r"/store/83/entry/9\?", httpx2.Response(500, json={"error": "error"}))
    async with make_client("dataportal") as client:
        detail = await call(client, "dataportal_get_dataset", {"context_id": "83", "entry_id": "9"})
    assert detail["title"] == "Badplatser"
    assert any("heuristiskt" in n for n in detail["notes"])


async def test_get_dataset_data_service_entry(router, make_client):
    # /dataservice/83_20: the traversal from the service also reaches dataset 9 (servesDataset), but the
    # entry says the resource is the service itself.
    router.add("GET", r"/store/83/metadata/20\?", load_fixture("dataportal_metadata_recursive.json"))
    router.add("GET", r"/store/83/entry/20\?", entry_info("83", "20", SKARA + "20"))
    async with make_client("dataportal") as client:
        detail = await call(
            client, "dataportal_get_dataset", {"dataset": "https://www.dataportal.se/dataservice/83_20"}
        )
    assert detail["dataset_uri"] == SKARA + "20"
    assert detail["title"] == "Badplats-API" and detail["types"] == ["dcat:DataService"]
    assert detail["portal_url"] == "https://www.dataportal.se/dataservice/83_20"
    assert detail["endpoint_urls"] == ["https://api.example.org/badplatser"]


async def test_get_dataset_portal_path_follows_type(router, make_client):
    agent = "http://dataportal.se/organisation/SE2021004185"
    graph = {agent: {RDF_TYPE: [uri("http://xmlns.com/foaf/0.1/Agent")], FOAF_NAME: [literal("Skolverket", "sv")]}}
    router.add("GET", r"/store/827/metadata/207\?", graph)
    router.add("GET", r"/store/827/entry/207\?", entry_info("827", "207", agent))
    series = {"https://x/series": {RDF_TYPE: [uri("http://www.w3.org/ns/dcat#DatasetSeries")]}}
    router.add("GET", r"/store/5/metadata/6\?", series)
    router.add("GET", r"/store/5/entry/6\?", entry_info("5", "6", "https://x/series"))
    async with make_client("dataportal") as client:
        org = await call(client, "dataportal_get_dataset", {"context_id": "827", "entry_id": "207"})
        ser = await call(client, "dataportal_get_dataset", {"dataset": "5_6"})
    assert org["portal_url"] == "https://www.dataportal.se/organisations/827_207"
    assert any("inte ett dataset" in n and "foaf:Agent" in n for n in org["notes"])
    assert ser["portal_url"] == "https://www.dataportal.se/dataset-series/5_6"


@pytest.mark.parametrize("status", [400, 500])
async def test_get_dataset_falls_back_when_recursive_fails(router, make_client, status):
    graph = load_fixture("dataportal_metadata_recursive.json")
    plain = {k: graph[k] for k in (SKARA + "9", "_:b0", "_:b1")}
    csv_node = copy.deepcopy(graph[SKARA + "11"])
    csv_node["http://purl.org/dc/terms/format"] = [{"type": "bnode", "value": "_:b0"}]  # label clashes with dataset's
    related = results(
        child(
            "83",
            "11",
            SKARA + "11",
            {
                SKARA + "11": csv_node,
                "_:b0": {"http://www.w3.org/2000/01/rdf-schema#label": [literal("CSV")]},
            },
        ),
        child("83", "12", SKARA + "12", {SKARA + "12": graph[SKARA + "12"]}),
        child("83", "3", SKARA + "3", {SKARA + "3": graph[SKARA + "3"]}),
    )
    service_node = copy.deepcopy(graph[SKARA + "20"])
    service_node["http://www.w3.org/ns/dcat#contactPoint"] = [{"type": "bnode", "value": "_:b0"}]  # same label again
    service = results(
        child(
            "83",
            "20",
            SKARA + "20",
            {SKARA + "20": service_node, "_:b0": {VCARD + "fn": [literal("API-support")]}},
        )
    )
    first_lookup = (
        rf"public:true AND (resource:({SKARA_ESC}11 OR {SKARA_ESC}12 OR {SKARA_ESC}3"
        r" OR http\:\/\/dataportal.se\/organisation\/SE2120001702))"
    )
    service_lookup = rf"public:true AND (resource:{SKARA_ESC}20)"
    solr(router, (lambda p: p["query"] == first_lookup, related), (lambda p: p["query"] == service_lookup, service))

    def metadata(request: httpx2.Request) -> httpx2.Response:
        if "recursive" in params(request):
            return httpx2.Response(status, json={"error": "error"})
        return httpx2.Response(200, json=plain)

    router.add("GET", r"/store/83/metadata/9\?", metadata)
    router.add("GET", r"/store/83/entry/9\?", load_fixture("dataportal_entry_83_9.json"))
    async with make_client("dataportal") as client:
        detail = await call(
            client, "dataportal_get_dataset", {"context_id": "83", "entry_id": "9", "include_raw": True}
        )

    assert params(router.requests[1]) == {"format": "application/json"}
    # The agent was not returned by the lookup; it is not asked for a second time.
    assert [p["query"] for p in searches(router)] == [first_lookup, service_lookup]
    assert detail["distributions"][0]["format"] == "CSV"
    assert detail["temporal"] == [{"start": "2021-06-18"}]  # dataset's _:b0 not overwritten by the child's _:b0
    assert detail["contact_points"][0]["email"] == "kontakt@example.org"
    assert detail["data_services"][0]["endpoint_urls"] == ["https://api.example.org/badplatser"]
    assert detail["publisher"] == {"uri": "http://dataportal.se/organisation/SE2120001702"}
    assert any("recursive=dcat" in n and f"HTTP {status}" in n for n in detail["notes"])
    # Blank nodes from the two lookup rounds get distinct labels (no collision in raw).
    raw = detail["raw"]
    assert raw["_:r0_b0"] == {"http://www.w3.org/2000/01/rdf-schema#label": [literal("CSV")]}
    assert raw["_:s0_b0"] == {VCARD + "fn": [literal("API-support")]}
    assert raw[SKARA + "20"]["http://www.w3.org/ns/dcat#contactPoint"] == [{"type": "bnode", "value": "_:s0_b0"}]


async def test_get_dataset_skips_unrepresentable_upstream_uris(router, make_client):
    odd = 'https://example.org/a b"c\\d'
    root = {
        RDF_TYPE: [uri("http://www.w3.org/ns/dcat#Dataset")],
        DCT + "title": [literal("Udda")],
        "http://www.w3.org/ns/dcat#distribution": [
            uri(SKARA + "11"),
            uri(odd),
            uri("https://example.org/ctl\x01"),
            uri("https://example.org/" + "x" * 1600),
        ],
    }
    router.add("GET", r"/store/1/metadata/2\?", {"https://x/ds": root})
    router.add("GET", r"/store/1/entry/2\?", entry_info("1", "2", "https://x/ds"))
    lookup = rf"public:true AND (resource:({SKARA_ESC}11 OR https\:\/\/example.org\/a\ b\"c\\d))"
    graph = load_fixture("dataportal_metadata_recursive.json")
    solr(
        router,
        (lambda p: p["query"] == lookup, results(child("83", "11", SKARA + "11", {SKARA + "11": graph[SKARA + "11"]}))),
    )
    async with make_client("dataportal") as client:
        detail = await call(client, "dataportal_get_dataset", {"dataset": "1_2"})
    assert [p["query"] for p in searches(router)] == [lookup]
    assert detail["distributions"][0]["title"] == "Badplatser (CSV)"
    assert [d.get("metadata_missing") for d in detail["distributions"]] == [None, True, True, True]


@pytest.mark.parametrize(
    "dataset",
    [
        "83_9",
        "https://www.dataportal.se/datasets/83_9",
        "https://dataportal.se/datasets/83_9",
        "https://www.dataportal.se/en/datasets/83_9/badplatser",
        f"{STORE}/83/entry/9",
        f"{STORE}/83/metadata/9",
    ],
)
async def test_get_dataset_accepts_portal_and_admin_addresses(router, make_client, dataset):
    skara_routes(router)
    async with make_client("dataportal") as client:
        detail = await call(client, "dataportal_get_dataset", {"dataset": dataset})
    assert detail["title"] == "Badplatser"
    assert [r.url.path for r in router.requests] == ["/store/83/metadata/9", "/store/83/entry/9"]


async def test_get_dataset_accepts_integer_ids(router, make_client):
    skara_routes(router)
    async with make_client("dataportal") as client:
        detail = await call(client, "dataportal_get_dataset", {"context_id": 83, "entry_id": 9})
    assert detail["context_id"] == "83" and detail["entry_id"] == "9" and detail["title"] == "Badplatser"


@pytest.mark.parametrize(
    "dataset",
    [
        "https://sandbox.admin.dataportal.se/store/1/entry/5",
        "https://editera.dataportal.se/store/5/entry/7",
        "https://evil-dataportal.se/datasets/43_69395",
        "https://admin.dataportal.se.example.org/store/43/entry/69395",
        "https://admin.dataportal.se/other/43/entry/69395",
        "https://www.dataportal.se/datasets/４３_９",
    ],
)
async def test_get_dataset_only_parses_exact_hosts_and_ascii_digits(router, make_client, dataset):
    solr(router, (lambda p: True, results()))
    async with make_client("dataportal") as client:
        err = await call_error(client, "dataportal_get_dataset", {"dataset": dataset})
    assert "Hittade inget dataset" in err
    # Looked up as a resource URI (restricted to datasets/series/services), never mapped to an entry.
    assert [r.url.path for r in router.requests] == ["/store/search"]
    assert params(router.last())["query"] == f"public:true AND (resource:{esc(dataset)}) AND {MAIN_TYPES}"


async def test_get_dataset_by_publisher_resource_uri(router, make_client):
    # catalog.skara.se is another EntryStore: its /store/1/resource/9 must be looked up, not parsed.
    lookup = rf"public:true AND (resource:{SKARA_ESC}9) AND {MAIN_TYPES}"
    graph = load_fixture("dataportal_metadata_recursive.json")
    solr(
        router,
        (lambda p: p["query"] == lookup, results(child("83", "9", SKARA + "9", {SKARA + "9": graph[SKARA + "9"]}))),
    )
    router.add("GET", r"/store/83/metadata/9\?", graph)
    async with make_client("dataportal") as client:
        detail = await call(client, "dataportal_get_dataset", {"dataset": SKARA + "9", "include_raw": True})
    assert searches(router)[0]["query"] == lookup
    # The resource URI is already known from the lookup: no entry-information request.
    assert [r.url.path for r in router.requests] == ["/store/search", "/store/83/metadata/9"]
    assert detail["dataset_uri"] == SKARA + "9" and detail["context_id"] == "83"
    assert SKARA + "20" in detail["raw"]


async def test_get_dataset_tilde_uri_is_escaped(router, make_client):
    target = "https://example.org/~user/data"
    lookup = rf"public:true AND (resource:https\:\/\/example.org\/\~user\/data) AND {MAIN_TYPES}"
    solr(router, (lambda p: p["query"] == lookup, results(child("4", "8", target, {}))))
    router.add("GET", r"/store/4/metadata/8\?", {target: {DCT + "title": [literal("Tilde")]}})
    async with make_client("dataportal") as client:
        detail = await call(client, "dataportal_get_dataset", {"dataset": target})
    assert detail["title"] == "Tilde" and detail["dataset_uri"] == target


@pytest.mark.parametrize("server_applies_types", [True, False])
async def test_get_dataset_publisher_uri_is_not_a_dataset(router, make_client, server_applies_types):
    agent = "http://dataportal.se/organisation/SE2021004185"
    agent_hit = results(child("827", "207", agent, {agent: {RDF_TYPE: [uri("http://xmlns.com/foaf/0.1/Agent")]}}))
    if server_applies_types:  # like Solr: the agent does not match the type-restricted query
        solr(router, (lambda p: "rdfType:" in p["query"], results()), (lambda p: True, agent_hit))
    else:  # the hit's own rdf:type is checked client-side as well
        solr(router, (lambda p: True, agent_hit))
    async with make_client("dataportal") as client:
        err = await call_error(client, "dataportal_get_dataset", {"dataset": agent})
    assert "Hittade inget dataset" in err and "publisher" in err
    assert MAIN_TYPES in searches(router)[0]["query"]
    assert all("/metadata/" not in r.url.path for r in router.requests)


async def test_get_dataset_ignores_non_numeric_upstream_ids(router, make_client):
    target = "https://example.org/ds"
    solr(router, (lambda p: True, results(child("1/../2", "5", target, {}), child("４３", "9", target, {}))))
    async with make_client("dataportal") as client:
        err = await call_error(client, "dataportal_get_dataset", {"dataset": target})
    assert "Hittade inget dataset" in err
    assert [r.url.path for r in router.requests] == ["/store/search"]


def _personal_graph() -> dict[str, Any]:
    """The Skara graph with a contact person (vcard:Individual) and a private-individual publisher."""
    graph = load_fixture("dataportal_metadata_recursive.json")
    contact = graph[SKARA + "3"]
    contact[RDF_TYPE] = [uri(VCARD + "Individual")]
    contact[VCARD + "fn"] = [literal("Anna Andersson")]
    contact[VCARD + "hasEmail"] = [uri("mailto:anna.andersson@example.org")]
    contact[VCARD + "hasTelephone"] = [{"type": "bnode", "value": "_:b9"}]
    graph["_:b9"] = {VCARD + "hasValue": [uri("tel:+46000000000")]}
    agent = graph["http://dataportal.se/organisation/SE2120001702"]
    agent[FOAF_NAME] = [literal("Per Persson")]
    agent[DCT + "type"] = [uri(PRIVATE)]
    return graph


async def test_get_dataset_redacts_personal_data(router, make_client):
    skara_routes(router, _personal_graph())
    async with make_client("dataportal") as client:
        default = await call(client, "dataportal_get_dataset", {"dataset": "83_9", "include_raw": True})
        opted = await call(
            client, "dataportal_get_dataset", {"dataset": "83_9", "include_raw": True, "include_personal_data": True}
        )
    text = json.dumps(default, ensure_ascii=False)
    for secret in ("Anna Andersson", "anna.andersson", "Per Persson", "+46000000000"):
        assert secret not in text, secret
    assert default["contact_points"] == [{"uri": SKARA + "3", "redacted": True}]
    assert default["publisher"] == {
        "uri": "http://dataportal.se/organisation/SE2120001702",
        "type": "PrivateIndividual(s)",
        "name_redacted": True,
    }
    assert "utgivare: http://dataportal.se/organisation/SE2120001702;" in default["attribution"]
    assert sum("personuppgift" in n for n in default["notes"]) == 2
    assert "_:b9" not in default["raw"] and default["raw"][SKARA + "3"] == {RDF_TYPE: [uri(VCARD + "Individual")]}

    assert opted["contact_points"] == [
        {"uri": SKARA + "3", "name": "Anna Andersson", "email": "anna.andersson@example.org"}
    ]
    assert opted["publisher"]["name"] == "Per Persson"
    assert "Anna Andersson" in json.dumps(opted["raw"], ensure_ascii=False)


async def test_get_dataset_personal_data_off_notes_do_not_suggest_opt_in(router, make_client):
    skara_routes(router, _personal_graph())
    async with make_client("dataportal", allow_personal_data=False) as client:
        detail = await call(client, "dataportal_get_dataset", {"dataset": "83_9", "include_raw": True})
    text = json.dumps(detail, ensure_ascii=False)
    for secret in ("Anna Andersson", "anna.andersson", "Per Persson", "+46000000000"):
        assert secret not in text, secret
    redaction_notes = [n for n in detail["notes"] if "personuppgift" in n]
    assert len(redaction_notes) == 2
    assert all("FUZZY_MCP_PERSONAL_DATA=off" in n and "include_personal_data" not in n for n in redaction_notes)


def _big_graph(count: int) -> dict[str, Any]:
    graph = load_fixture("dataportal_metadata_recursive.json")
    root = graph[SKARA + "9"]
    refs = []
    for k in range(count):
        ref = f"{SKARA}d{k}"
        refs.append(uri(ref))
        graph[ref] = {
            DCT + "title": [literal(f"Badplatser {k} – kommunens mätdata för säsongen (CSV)", "sv")],
            DCT + "description": [literal("Mätdata " * 40, "sv")],
            "http://www.w3.org/ns/dcat#accessURL": [uri(ref)],
            "http://www.w3.org/ns/dcat#downloadURL": [uri(f"{ref}/badplatser_{k}.csv")],
            DCT + "format": [literal("text/csv")],
            DCT + "license": [uri("http://creativecommons.org/publicdomain/zero/1.0/")],
        }
    root["http://www.w3.org/ns/dcat#distribution"] = refs
    return graph


async def test_get_dataset_output_stays_within_budget(router, make_client):
    skara_routes(router, _big_graph(300))
    async with make_client("dataportal") as client:
        full = await client.call_tool("dataportal_get_dataset", {"dataset": "83_9", "max_distributions": 500})
        many = full.structured_content
        root_only = await call(client, "dataportal_get_dataset", {"dataset": "83_9", "include_raw": True})
        dropped = await call(
            client, "dataportal_get_dataset", {"dataset": "83_9", "include_raw": True, "max_distributions": 100}
        )
    assert len(many["distributions"]) == 100 and many["distributions_total"] == 300
    assert many["distributions_truncated"] is True
    assert any("max_distributions begränsades till 100" in n for n in many["notes"])
    assert all(len(d["description"]) <= 120 for d in many["distributions"])  # shortened when many
    assert len(full.content[-1].text) < OUTPUT_BUDGET_CHARS

    assert set(root_only["raw"]) == {SKARA + "9", "_:b0", "_:b1"}
    assert any("bara rotnoden" in n for n in root_only["notes"])
    assert "raw" not in dropped and any("raw utelämnades" in n for n in dropped["notes"])
    for result in (root_only, dropped):
        assert len(json.dumps(result, ensure_ascii=False, separators=(",", ":"))) <= OUTPUT_BUDGET_CHARS


async def test_get_dataset_errors(router, make_client):
    router.add("GET", r"/store/1/metadata/404\?", httpx2.Response(404, json={"error": "Entry not found"}))
    solr(router, (lambda p: True, results()))
    async with make_client("dataportal") as client:
        err = await call_error(client, "dataportal_get_dataset", {"context_id": "1", "entry_id": "404"})
        assert "HTTP 404" in err and "Entry not found" in err
        assert "Ange context_id" in await call_error(client, "dataportal_get_dataset", {})
        assert "både context_id och entry_id" in await call_error(client, "dataportal_get_dataset", {"context_id": "1"})
        assert "siffror" in await call_error(client, "dataportal_get_dataset", {"context_id": "a", "entry_id": "1"})
        assert "siffror" in await call_error(client, "dataportal_get_dataset", {"context_id": "４３", "entry_id": "1"})
        assert "siffror" in await call_error(client, "dataportal_get_dataset", {"context_id": -1, "entry_id": 1})
        assert "Känner inte igen" in await call_error(client, "dataportal_get_dataset", {"dataset": "badplatser"})
        assert "Känner inte igen" in await call_error(client, "dataportal_get_dataset", {"dataset": "４３_９"})
        err = await call_error(client, "dataportal_get_dataset", {"dataset": "https://example.org/dataset/x"})
        assert "Hittade inget dataset" in err
    # Only the 404 metadata request and the one resource lookup reached the network.
    assert [r.url.path for r in router.requests] == ["/store/1/metadata/404", "/store/search"]


# --- code lists ------------------------------------------------------------------------------------


async def test_theme_codes_tool_and_codes_resource(make_client):
    async with make_client("dataportal") as client:
        names = {t.name for t in (await client.list_tools()).tools}
        assert {
            "dataportal_search_datasets",
            "dataportal_get_dataset",
            "dataportal_list_publisher_datasets",
            "dataportal_theme_codes",
        } <= names
        themes = await call(client, "dataportal_theme_codes")
        assert len(themes["themes"]) == 13
        educ = next(t for t in themes["themes"] if t["code"] == "EDUC")
        assert educ == {
            "code": "EDUC",
            "label_sv": "Utbildning, kultur och sport",
            "label_en": "Education, culture and sport",
            "uri": "http://publications.europa.eu/resource/authority/data-theme/EDUC",
        }
        res = await client.read_resource("fuzzy://dataportal/codes")
    text = res.contents[0].text
    for needle in ('"HEAL"', '"NON_PUBLIC"', '"ANNUAL_2"', "creativecommons.org/licenses/by/4.0/", '"c_e1da4e07"'):
        assert needle in text
