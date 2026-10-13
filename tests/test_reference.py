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
        # Official names in the genitive (#26): exact hits, the municipality before the county.
        hits = await call(client, "ref_lookup_region", {"query": "Göteborgs stad", "kind": "kommun"})
        assert hits[0]["kod"] == "1480" and hits[0]["likhet"] == 1.0 and all(h["likhet"] < 1 for h in hits[1:])
        hits = await call(client, "ref_lookup_region", {"query": "Gotlands kommun", "limit": 2})
        assert [(h["kod"], h["likhet"]) for h in hits] == [("0980", 1.0), ("09", 1.0)]


def test_lookup_region_counties_by_name():
    """A county's name, its base name and 'Region <base name>' match that county exactly and no other."""
    from fuzzy_mcp.reference import codes

    for county in codes.load("regioner")["lan"]:
        base = county["namn"].removesuffix(" län").removesuffix("s")
        for query in (county["namn"], base, f"Region {base}"):
            exact = [hit["kod"] for hit in codes.lookup_region(query, kind="lan") if hit["likhet"] == 1.0]
            assert exact == [county["kod"]], query


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


# --- issue #20: an answer the output cap (FUZZY_MCP_MAX_OUTPUT_CHARS) shortened never passes for complete ---------


async def test_list_municipalities_cut_is_flagged_and_every_county_is_whole(make_client):
    """The 263-character verification note all 290 municipalities share is left out of each entry (it is in the
    code list regioner), so all 290 fit Eneo's 30 000 and every county, Västra Götaland (49) the largest at about
    4.6k characters, fits whole at 15 000 (ENEO.md §5.1 for a 32k-token model) and 8 000. At those caps the whole
    list is a prefix flagged in _kortat with no pointer that could skip, the parameter no longer promises all 290,
    and the remedy it names (one county at a time) returns every municipality exactly once. With the note, county 14
    alone was cut below about 18.7k and its last municipalities were out of reach."""
    from fuzzy_mcp.core import client_size, compact_json
    from fuzzy_mcp.reference import codes

    regions = codes.load("regioner")
    shared = {m["verifiering_not"] for m in regions["kommuner"]}
    assert len(shared) == 1  # one note for every municipality: an entry of its own would need to keep it
    expected = [{k: v for k, v in m.items() if k != "verifiering_not"} for m in regions["kommuner"]]
    assert all(m["verifiering"] for m in expected)
    async with make_client("reference", max_output_chars=30_000) as client:
        assert await call(client, "ref_list_municipalities") == expected
        found = await call(client, "ref_get_code_list", {"name": "regioner", "search": "1480"})
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert [m["verifiering_not"] for m in found["kommuner"]] == list(shared)  # where the description points
    for cap in (15_000, 8000):
        async with make_client("reference", max_output_chars=cap) as client:
            cut = await call(client, "ref_list_municipalities")
            counties = [
                await call(client, "ref_list_municipalities", {"county_code": lan["kod"]}) for lan in regions["lan"]
            ]
        kept = cut["result"]
        assert set(cut) == {"result", "_kortat"} and client_size(compact_json(cut)) <= cap
        assert 0 < len(kept) < 290 and kept == expected[: len(kept)]
        note = cut["_kortat"]
        assert note["kortade"] == [{"falt": "result", "fore": 290, "efter": len(kept)}]
        assert "fortsatt" not in note and "Begränsa urvalet" in note["meddelande"]
        assert all(isinstance(county, list) for county in counties), cap  # no _kortat: every county fits whole
        seen = [m for county in counties for m in county]
        assert sorted(seen, key=lambda m: m["kod"]) == expected and len(seen) == 290
    description = tools["ref_list_municipalities"].input_schema["properties"]["county_code"]["description"]
    assert "290" not in description and "_kortat" in description
    assert "verifiering_not" in tools["ref_list_municipalities"].description


async def test_entity_catalog_cut_keeps_caveats_and_join_keys(make_client):
    """forbehall (personal data, suppression) and kopplingsnycklar never lose an entry, since a missing warning
    reads as no warning: the cap shortens entiteter (flagged in _kortat), then may halve a long text inside the two
    lists (listed in _kortat.kortade with enhet "tecken", as at 6 500), and rejects the answer when even that does
    not fit. The behaviour comes from core.PROTECTED_KEYS; this guards it for the catalog."""
    from fuzzy_mcp.core import CUT_MARKER

    def halved(whole, cut, path):
        """Paths (in _kortat's form, lists as []) of the strings the cap halved."""
        if isinstance(whole, dict):
            return [p for key in whole for p in halved(whole[key], cut[key], f"{path}.{key}")]
        if isinstance(whole, list):
            return [p for a, b in zip(whole, cut, strict=True) for p in halved(a, b, f"{path}[]")]
        if whole == cut:
            return []
        assert cut.endswith(CUT_MARKER) and whole.startswith(cut.removesuffix(CUT_MARKER)), path
        return [path]

    async with make_client("reference") as client:
        whole = await call(client, "ref_entity_catalog", {"source": "skolverket"})
    async with make_client("reference", max_output_chars=8000) as client:
        cut = await call(client, "ref_entity_catalog", {"source": "skolverket"})
    async with make_client("reference", max_output_chars=6500) as client:
        tight = await call(client, "ref_entity_catalog", {"source": "skolverket"})
    async with make_client("reference", max_output_chars=4000) as client:
        err = await call_error(client, "ref_entity_catalog", {"source": "skolverket"})
    assert "_kortat" not in whole and len(whole["entiteter"]) == 13
    assert cut["forbehall"] == whole["forbehall"] and cut["kopplingsnycklar"] == whole["kopplingsnycklar"]
    kept = cut["entiteter"]
    assert 0 < len(kept) < 13 and kept == whole["entiteter"][: len(kept)]
    assert cut["_kortat"]["kortade"] == [{"falt": "entiteter", "fore": 13, "efter": len(kept)}]
    # At 6 500 the two lists keep every entry, and each text the cap halved in them is named in kortade.
    protected = ("forbehall", "kopplingsnycklar")
    assert [len(tight[key]) for key in protected] == [len(whole[key]) for key in protected]
    texts = {entry["falt"] for entry in tight["_kortat"]["kortade"] if entry.get("enhet") == "tecken"}
    cut_texts = {p for key in protected for p in halved(whole[key], tight[key], key)}
    assert cut_texts and cut_texts <= texts, cut_texts - texts
    assert "för stort" in err and "4000" in err


async def test_api_notices_are_whole_or_rejected(make_client):
    """notices is never shortened, so count never describes notices the answer does not hold."""
    from fuzzy_mcp.core import client_size
    from fuzzy_mcp.http import MAX_SHARED_NOTICES

    sources = ("scb", "fohm", "skolverket", "skolverket.skolenhetsregistret", "dataportal")
    notices = [
        f"{sources[i % 5]}: {'Sunset' if i % 2 else 'Deprecation'}: 2027-{1 + i // 5:02}-01T00:00:00Z"
        for i in range(MAX_SHARED_NOTICES)
    ]

    async def notices_at(cap):
        async with make_client("reference", max_output_chars=cap) as client:
            client.server.fuzzy_services.http.recent_notices.update(dict.fromkeys(notices))
            return await client.call_tool("fuzzy_api_notices", {})

    whole = await notices_at(30_000)
    assert whole.structured_content == {"notices": notices, "count": MAX_SHARED_NOTICES}
    cap = client_size(whole.content[0].text) - 200  # room for most notices, not for all of them
    rejected = await notices_at(cap)
    assert rejected.is_error and rejected.structured_content is None
    assert "för stort" in rejected.content[0].text and f"({cap} tecken)" in rejected.content[0].text
