# SPDX-FileCopyrightText: 2026 Deniz Özer
#
# SPDX-License-Identifier: MIT

import pytest

from .conftest import call, call_error

pytestmark = pytest.mark.anyio


async def test_lookup_region_fuzzy(make_client):
    async with make_client("reference") as client:
        hits = await call(client, "ref_lookup_region", {"query": "Hälsingborg", "kind": "kommun"})
        assert hits[0]["kod"] == "1283" and hits[0]["lan"] == "Skåne län"
        hits = await call(client, "ref_lookup_region", {"query": "AB"})
        assert hits[0]["kod"] == "01" and hits[0]["typ"] == "lan"
        hits = await call(client, "ref_lookup_region", {"query": "Stockholm", "limit": 2})
        assert [h["kod"] for h in hits] == ["0180", "01"]


async def test_list_municipalities_and_code_lists(make_client):
    async with make_client("reference") as client:
        assert len(await call(client, "ref_list_municipalities")) == 290
        vg = await call(client, "ref_list_municipalities", {"county_code": "14"})
        assert len(vg) == 49 and all(k["lanskod"] == "14" for k in vg)
        err = await call_error(client, "ref_list_municipalities", {"county_code": "99"})
        assert "Okänd länskod" in err
        lists = await call(client, "ref_list_code_lists")
        assert "regioner" in {entry["id"] for entry in lists}
        err = await call_error(client, "ref_get_code_list", {"name": "finnsinte"})
        assert "Okänd kodlista" in err


async def test_code_list_resources(make_client):
    async with make_client("reference") as client:
        templates = (await client.list_resource_templates()).resource_templates
        assert any(t.uri_template == "fuzzy://codes/{name}" for t in templates)
        res = await client.read_resource("fuzzy://codes/regioner")
        assert '"0180"' in res.contents[0].text


async def test_bundled_code_lists_are_complete(make_client):
    async with make_client("reference") as client:
        lists = {entry["id"]: entry for entry in await call(client, "ref_list_code_lists")}
        expected = {
            "regioner",
            "betygsskalor",
            "scb_betygskoder",
            "gymnasieprogram",
            "anpassad_gymnasieskola_program",
            "introduktionsprogram",
            "komvux_kurskoder",
            "betygsdokument",
            "kodstrukturer",
            "ss12000",
            "skolformer",
        }
        assert expected <= set(lists)
        assert all(lists[name]["kalla"] for name in expected)
        scales = await call(client, "ref_get_code_list", {"name": "betygsskalor"})
        assert {s["kod"] for s in scales["koder"]} >= {"A-F", "A-E", "G-IG", "1-10"}
        assert any(m["kod"] == "–" for m in scales["markeringar"])
        programs = await call(client, "ref_get_code_list", {"name": "gymnasieprogram", "search": "natur"})
        assert {p["kod"] for p in programs["koder"]} >= {"NA", "NA25"}
        crosswalk = await call(client, "ref_get_code_list", {"name": "skolformer", "search": "anpassad grundskola"})
        row = crosswalk["koder"][0]
        assert row["ss12000_v21"] == ["GRS", "TR"] and row["skolenhetsregistret"] == ["GRAN"]
        assert row["planerad_utbildning"] == ["gran"]
        catalogue = await call(client, "ref_entity_catalog", {"source": "skolverket"})
        assert any(k["nyckel"] == "skolenhetskod" for k in catalogue["kopplingsnycklar"])
        assert all(e["kalla"] == "skolverket" for e in catalogue["entiteter"])


async def test_unknown_code_list_resource_is_not_found(make_client):
    from mcp.shared.exceptions import MCPError

    async with make_client("reference") as client:
        with pytest.raises(MCPError) as info:
            await client.read_resource("fuzzy://codes/finnsinte")
    assert info.value.error.code == -32602 and "Okänd kodlista" in info.value.error.message


async def test_code_list_search_filters_every_list(make_client):
    async with make_client("reference") as client:
        regions = await call(client, "ref_get_code_list", {"name": "regioner", "search": "gävle"})
    assert [k["kod"] for k in regions["kommuner"]] == ["2180"]
    assert [lan["kod"] for lan in regions["lan"]] == ["21"]


def test_reference_data_is_clean():
    import json
    import re
    from pathlib import Path

    data = Path(__file__).parents[1] / "src" / "fuzzy_mcp" / "reference" / "data"
    for path in data.glob("*.json"):
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"\[(spec|search|ref-code|inferred)\b", text), path.name
        assert '?"' not in text.replace('"?"', ""), path.name
    documents = json.loads((data / "betygsdokument.json").read_text(encoding="utf-8"))
    assert all(e["namn"] for e in documents["koder"])
    known = {"GR", "GRAN", "SP", "SAM", "GY", "GYAN", "VUX", "VUXGY", "VUXGYAN"}
    assert all(code.split(" ")[0] in known for e in documents["koder"] for code in e["skolformer"])


async def test_catalogue_and_information_model_name_existing_tools(make_client):
    from fuzzy_mcp.reference import catalog

    async with make_client() as client:
        names = {t.name for t in (await client.list_tools()).tools}
        res = await client.read_resource("fuzzy://crosswalk/informationsmodell")
    referenced = {tool for e in catalog.ENTITIES for tool in e["verktyg"]}
    referenced |= {tool for e in catalog.INFORMATION_MODEL["entiteter"] for tool in e["verktyg"]}
    assert referenced - names == set()
    assert "Datakvalitet" in res.contents[0].text


def _objects(node, post_ids, inside=False, path="$"):
    """Yield (path, object, is_post, inside_post) for every object in a code list document."""
    if isinstance(node, dict):
        is_post = id(node) in post_ids
        yield path, node, is_post, inside
        for key, value in node.items():
            yield from _objects(value, post_ids, inside or is_post, f"{path}.{key}")
    elif isinstance(node, list):
        for i, value in enumerate(node):
            yield from _objects(value, post_ids, inside, f"{path}[{i}]")


def test_every_code_list_post_has_a_valid_verification():
    """Erosion guard: every file, collection and post is covered, structurally and per file."""
    import re
    from datetime import date

    from fuzzy_mcp.reference import catalog, codes

    for name in codes._files():
        doc = codes.load(name)
        collections = dict(codes.post_collections(doc))
        post_ids = {id(post) for items in collections.values() for post in items}
        assert post_ids, f"{name}: inga poster"
        markers = doc.get(codes.COLLECTION_MARKERS, {})
        # Every non-scalar top-level value is a post collection or has a collection-level value.
        for key, value in doc.items():
            if key == codes.COLLECTION_MARKERS or not isinstance(value, dict | list):
                continue
            marker = markers.get(key) or {}
            assert key in collections or marker.get("verifiering") in codes.VERIFIERING, (
                f"{name}.{key} saknar verifiering"
            )
        assert all(key in doc and key not in collections for key in markers), f"{name}: felaktig samlingsnyckel"
        for key, marker in markers.items():
            note = marker.get("verifiering_not", "")
            assert isinstance(note, str) and not re.search(r"\[[a-z:-]+\]|\bS\d{1,2}\b", note), (name, key)
        if isinstance(doc.get("koder"), list):
            assert len(collections["koder"]) == len(doc["koder"]), f"{name}: alla koder är inte poster"
        for post in (p for items in collections.values() for p in items):
            assert post.get("verifiering") in codes.VERIFIERING, (name, post.get("kod"), post.get("verifiering"))
            if "verifiering_not" in post:
                note = post["verifiering_not"]
                assert isinstance(note, str) and note.strip() and not re.search(r"\[[a-z:-]+\]|\bS\d{1,2}\b", note)
            if post["verifiering"] == "verifierat_uttag":
                assert "verifierad_datum" in post, (name, post.get("kod"))
            if "verifierad_datum" in post:
                value = post["verifierad_datum"]
                assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) and date.fromisoformat(value) <= date.today()
            if "kalla_url" in post:
                assert re.match(r"^https://\S+$", post["kalla_url"]), (name, post["kalla_url"])

        # Nested objects inherit their post's value; any object with a kod must be a post or inside one,
        # and any explicit value anywhere must be valid.
        for path, node, is_post, inside in _objects(doc, post_ids):
            assert "kod" not in node or is_post or inside, f"{name}{path}: objekt med kod utanför poster"
            assert "verifiering" not in node or node["verifiering"] in codes.VERIFIERING, (name, path)
    assert sum(1 for name in codes._files() for _ in codes.posts(codes.load(name))) >= 600
    assert all(k["verifiering"] in codes.VERIFIERING for k in catalog.SEMANTIC_BRIDGE["kopplingar"])
    assert list(codes.VERIFIERING) == [
        "författning",
        "officiell_spec",
        "verifierat_uttag",
        "myndighetswebb",
        "referenskod",
        "härlett",
        "okänd",
    ]


def test_verification_summary_reports_missing_and_invalid_values():
    from fuzzy_mcp.reference import codes

    doc = {"koder": [{"kod": "1"}, {"kod": "2", "verifiering": "spec"}, {"kod": "3", "verifiering": "okänd"}]}
    assert codes.verification_summary(doc) == {"koder": {"okänd": 1, "saknas": 1, "ogiltig": 1}}


def test_restored_provenance_matches_the_research_tags():
    """The research tags in commit ba76abf (removed from the user-facing text in bd0b27e) live on as verifiering."""
    from fuzzy_mcp.reference import codes

    documents = codes.load("betygsdokument")["koder"]
    assert len(documents) == 15 and {d["verifiering"] for d in documents} == {"författning"}
    # The lagrum is checked against statute; the skolform codes are the project's notation (see the note).
    assert all("Skolenhetsregistret" in d["verifiering_not"] for d in documents)
    scales = codes.load("betygsskalor")
    by_code = {k["kod"]: k["verifiering"] for k in scales["koder"]}
    # E-only: the komvuxarbete use rests on a web-search summary only – the weakest evidence decides.
    assert by_code.pop("E-only") == "myndighetswebb"
    assert set(by_code.values()) == {"författning"}
    e_only = next(k for k in scales["koder"] if k["kod"] == "E-only")
    assert "skollagen" in e_only["verifiering_not"] and "webbsökning" in e_only["verifiering_not"]
    assert [m["verifiering"] for m in scales["markeringar"]] == ["författning"] * 3
    versions = codes.load("ss12000")["andra_versioner"]
    assert {k: v["verifiering"] for k, v in versions.items()} == {
        "ss12000_v2_0_draft": "referenskod",
        "ss12000_2018_egil": "referenskod",
        "skolverket_dnp_subset": "officiell_spec",
    }


async def test_code_list_index_reports_verification(make_client):
    async with make_client("reference") as client:
        lists = {entry["id"]: entry for entry in await call(client, "ref_list_code_lists")}
        scales = lists["betygsskalor"]
        assert scales["antal"] == 8 and scales["antal_poster"] == 19
        assert scales["verifiering"]["koder"] == {"författning": 7, "myndighetswebb": 1}
        assert scales["verifiering"]["markeringar"] == {"författning": 3}
        assert lists["betygsdokument"]["verifiering"] == {"koder": {"författning": 15}}
        assert set(lists["ss12000"]["verifiering"]) >= {"koder", "andra_versioner", "uppräkningar"}
        vocabulary = await call(client, "ref_get_code_list", {"name": "verifiering"})
        hits = await call(client, "ref_lookup_region", {"query": "Gävle", "kind": "kommun"})
    assert vocabulary["koder"][0]["kod"] == "författning"
    assert hits[0]["verifiering"] in {k["kod"] for k in vocabulary["koder"]}


async def test_eneo_assistant_instruction_names_existing_tools(make_client):
    import re
    from pathlib import Path

    text = (Path(__file__).parents[1] / "docs" / "eneo" / "assistentinstruktion.md").read_text(encoding="utf-8")
    named = {n for n in re.findall(r"`([a-z]+_[a-z0-9_]+)`", text) if "*" not in n and not n.startswith("include_")}
    async with make_client() as client:
        tools = {t.name for t in (await client.list_tools()).tools}
    assert named and named <= tools, named - tools
