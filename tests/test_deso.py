# SPDX-FileCopyrightText: 2026 Deniz Özer
#
# SPDX-License-Identifier: MIT

"""DeSO/RegSO lookups (ref_lookup_deso, ref_list_deso) and the invariants of SCB's key files."""

from collections import Counter

import pytest

from fuzzy_mcp.core import client_size, compact_json
from fuzzy_mcp.reference import codes, deso

from .conftest import call, call_error

pytestmark = pytest.mark.anyio

SOURCE_FILES = {
    "koppling-deso2018-regso2020_2026-03-25.xlsx",
    "koppling-deso2025-regso2025_2026-03-25.xlsx",
    "deso-historiska-forandringar-2025-09-19.xlsx",
}


# --- MCP tools --------------------------------------------------------------------------------------


async def test_lookup_deso_rejects_bad_and_unknown_codes(make_client):
    async with make_client("reference") as client:
        err = await call_error(client, "ref_lookup_deso", {"code": "0114X1010"})
        assert "DeSO-kod" in err and "RegSO-kod" in err and "0114C1010" in err and "0114R001" in err
        err = await call_error(client, "ref_lookup_deso", {"code": "0114"})
        assert "Ogiltig kod" in err
        err = await call_error(client, "ref_lookup_deso", {"code": "0114C9999"})
        assert "finns inte i DeSO 2018 eller DeSO 2025" in err and "förändringslogg" not in err
        err = await call_error(client, "ref_lookup_deso", {"code": "0114R999"})
        assert "finns inte i RegSO 2020 eller RegSO 2025" in err
        # A code in neither key file that the log still names: the answer repeats what the log says.
        err = await call_error(client, "ref_lookup_deso", {"code": "1883a0010"})
        assert "finns inte i DeSO 2018 eller DeSO 2025" in err
        assert "1883A0010 → 1883A0030 2018-02-21 ('Upphör; till nybildad')" in err


async def test_deso_tools_reject_long_input_before_lookup(make_client):
    """max_length on the fields: no fuzzy matching of, or echoing, arbitrarily long text."""
    async with make_client("reference") as client:
        err = await call_error(client, "ref_lookup_deso", {"code": "0114C1010" + "0" * 20})
        assert "20" in err and len(err) < 600
        err = await call_error(client, "ref_list_deso", {"municipality": "a" * 101})
        assert "100" in err and len(err) < 600
        err = await call_error(client, "ref_list_deso", {"municipality": "0114", "regso": "a" * 101})
        assert "100" in err and len(err) < 600
        err = await call_error(client, "ref_lookup_region", {"query": "a" * 101})
        assert "100" in err and len(err) < 600
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert tools["ref_lookup_deso"].input_schema["properties"]["code"]["maxLength"] == 20
    assert tools["ref_list_deso"].input_schema["properties"]["municipality"]["maxLength"] == 100


async def test_lookup_deso_ended_code_is_a_pure_split(make_client):
    async with make_client("reference") as client:
        hit = await call(client, "ref_lookup_deso", {"code": " 0114c1060 "})  # trimmed and upper-cased
    assert hit["kod"] == "0114C1060" and hit["typ"] == "deso"
    assert hit["kommunkod"] == "0114" and hit["kommun"] == "Upplands Väsby"
    assert hit["kategori"]["kod"] == "C" and "centralort" in hit["kategori"]["beskrivning"]
    assert hit["finns_i"] == ["DeSO 2018"]
    assert hit["versioner"] == [
        {"deso": "DeSO 2018", "regso": "RegSO 2020", "regsokod": "0114R006", "regso_namn": "Runby södra-Ed"}
    ]
    assert hit["forandringar"] == [
        {"fran": "0114C1060", "till": "0114C1061", "typ": "Upphör; till nybildad", "datum": "2025-01-01"},
        {"fran": "0114C1060", "till": "0114C1062", "typ": "Upphör; till nybildad", "datum": "2025-01-01"},
    ]
    verdict = hit["jamforbarhet"]
    assert verdict["status"] == "upphört" and verdict["verifiering"] == "härlett"
    assert verdict["ersatts_av"] == ["0114C1061", "0114C1062"] and verdict["summerbar_antal"] is True
    assert verdict["not"].startswith(
        "Upphört 2025-01-01: delades i 2 nya områden som tillsammans täcker koden; antal för delarna kan summeras"
    )
    assert "andelar, medelvärden, medianer och index kan inte summeras" in verdict["not"]
    assert hit["verifiering"] == "myndighetswebb" and "härlett" in hit["verifiering_not"]
    assert {f["namn"] for f in hit["kalla"]["filer"]} == SOURCE_FILES
    assert all(f["datum_i_fil"] in {"2026-03-25", "2025-09-19"} for f in hit["kalla"]["filer"])
    assert hit["kalla"]["kalla_url"].startswith("https://www.scb.se/") and hit["kalla"]["licens"] == "CC0 1.0"


async def test_lookup_deso_new_code_points_back_to_its_parent(make_client):
    async with make_client("reference") as client:
        hit = await call(client, "ref_lookup_deso", {"code": "0114C1061"})
    assert hit["finns_i"] == ["DeSO 2025"]
    assert hit["versioner"][0]["regso"] == "RegSO 2025" and hit["versioner"][0]["regsokod"] == "0114R006"
    verdict = hit["jamforbarhet"]
    assert verdict["status"] == "nytt"
    assert verdict["bildat_av"] == ["0114C1060"]
    assert verdict["summerbar_antal_till"] == "0114C1060" and verdict["syskon"] == ["0114C1062"]
    assert "antal för delarna kan summeras till 0114C1060" in verdict["not"] and "andelar" in verdict["not"]


async def test_lookup_deso_changed_boundary(make_client):
    async with make_client("reference") as client:
        kiruna = await call(client, "ref_lookup_deso", {"code": "2584C1110"})
        bro = await call(client, "ref_lookup_deso", {"code": "0139A0010"})
        new_part = await call(client, "ref_lookup_deso", {"code": "0139B2070"})
    assert kiruna["finns_i"] == ["DeSO 2018", "DeSO 2025"] and kiruna["kommun"] == "Kiruna"
    assert [v["regso"] for v in kiruna["versioner"]] == ["RegSO 2020", "RegSO 2025"]
    assert {r["typ"] for r in kiruna["forandringar"]} == {"Utvidgas", "Upphör; till befintlig"}
    assert kiruna["jamforbarhet"]["status"] == "ändrad gräns"
    assert "gränsen ändrad 2025-01-01" in kiruna["jamforbarhet"]["not"]
    assert set(kiruna["jamforbarhet"]) == {"status", "not", "verifiering"}

    # In both versions; shrinks, gives a part to the new 0139B2070 and receives from two other codes.
    assert bro["jamforbarhet"]["status"] == "ändrad gräns"
    rows = bro["forandringar"]
    assert {"fran": "0139A0010", "till": "0139B2070", "typ": "Ger del till nybildad", "datum": "2025-01-01"} in rows
    assert any(r["fran"] == r["till"] == "0139A0010" for r in rows)
    assert [r["fran"] for r in rows] == sorted(r["fran"] for r in rows)  # sorted on (datum, fran, till)

    # The new part has one source that survives: it is not a split, so nothing can be summed back.
    verdict = new_part["jamforbarhet"]
    assert verdict["status"] == "nytt" and verdict["bildat_av"] == ["0139A0010"]
    assert verdict["summerbar_antal_till"] is None and verdict["syskon"] == []
    assert "finns kvar med ändrad gräns" in verdict["not"]


async def test_lookup_deso_unchanged_code(make_client):
    async with make_client("reference") as client:
        hit = await call(client, "ref_lookup_deso", {"code": "0114A0010"})
    assert hit["finns_i"] == ["DeSO 2018", "DeSO 2025"] and hit["forandringar"] == []
    assert hit["kategori"]["kod"] == "A"
    assert hit["jamforbarhet"]["status"] == "oförändrad"
    assert "ingen rad i SCB:s förändringslogg" in hit["jamforbarhet"]["not"]


async def test_lookup_deso_recoded_pair_is_not_summable(make_client):
    """Mjölby's five codes changed category C→B with SCB's type 'Kodändrad; annat än 20-22'. The summerbar
    rule demands 'Upphör; till nybildad' on every row, so a recode is False: the log says that the code was
    replaced, not that the boundary is unchanged, and the strict rule is what reproduces the 88 pure splits
    derived from SCB's log when the code lists were built. The notes name the type so the reader can check the
    recode with SCB."""
    async with make_client("reference") as client:
        old = await call(client, "ref_lookup_deso", {"code": "0586C2010"})
        new = await call(client, "ref_lookup_deso", {"code": "0586B2010"})
    assert old["kommun"] == new["kommun"] == "Mjölby"
    assert old["kategori"]["kod"] == "C" and new["kategori"]["kod"] == "B"
    assert old["jamforbarhet"]["status"] == "upphört"
    assert old["jamforbarhet"]["ersatts_av"] == ["0586B2010"] and old["jamforbarhet"]["summerbar_antal"] is False
    assert "Kodändrad; annat än 20-22" in old["jamforbarhet"]["not"]
    assert new["jamforbarhet"]["status"] == "nytt" and new["jamforbarhet"]["bildat_av"] == ["0586C2010"]
    assert new["jamforbarhet"]["summerbar_antal_till"] is None and new["jamforbarhet"]["syskon"] == []
    assert "Kodändrad; annat än 20-22" in new["jamforbarhet"]["not"]


async def test_lookup_regso(make_client):
    async with make_client("reference") as client:
        perstorp = await call(client, "ref_lookup_deso", {"code": "1275R002"})
        vasby = await call(client, "ref_lookup_deso", {"code": "0114r010"})
        kiruna = await call(client, "ref_lookup_deso", {"code": "2584R001"})
    assert perstorp["typ"] == "regso" and perstorp["kommunkod"] == "1275" and perstorp["kommun"] == "Perstorp"
    assert perstorp["finns_i"] == ["RegSO 2020", "RegSO 2025"]
    assert perstorp["versioner"] == [
        {"regso": "RegSO 2020", "namn": "Perstorp nordöstra tätorten", "deso": ["1275C1020"]},
        {"regso": "RegSO 2025", "namn": "Perstorp sydöstra tätorten", "deso": ["1275C1020"]},
    ]
    assert perstorp["namn_andrat"] is True
    assert perstorp["not"] == "RegSO-namn är unika bara inom kommunen och kan ändras; använd koden."
    assert {f["namn"] for f in perstorp["kalla"]["filer"]} == SOURCE_FILES - {
        "deso-historiska-forandringar-2025-09-19.xlsx"
    }
    assert perstorp["verifiering"] == "myndighetswebb"

    assert vasby["kod"] == "0114R010" and vasby["namn_andrat"] is False
    assert vasby["versioner"][1] == {"regso": "RegSO 2025", "namn": "Upplands Väsby omland", "deso": ["0114A0010"]}

    # Only in RegSO 2020 (the one code that changed): nothing to compare the name with.
    assert kiruna["finns_i"] == ["RegSO 2020"] and kiruna["namn_andrat"] is None


async def test_lookup_gives_the_codes_statistikdatabasen_uses(make_client):
    """ssd_koder: the code as the variable Region spells it per version. Verified extract 2026-10-10 (TAB6680):
    vs_DeSO2018 and vs_RegSO2020 have plain codes, vs_DeSO2025 and vs_RegSO2025 add _DeSO2025/_RegSO2025."""
    async with make_client("reference") as client:
        both = await call(client, "ref_lookup_deso", {"code": "0114C1010"})
        new = await call(client, "ref_lookup_deso", {"code": "0114C1061"})
        old = await call(client, "ref_lookup_deso", {"code": "0586C2010"})
        regso = await call(client, "ref_lookup_deso", {"code": "0114R010"})
    assert both["finns_i"] == ["DeSO 2018", "DeSO 2025"]
    assert both["ssd_koder"] == {
        "DeSO 2018": {"kod": "0114C1010", "kodlista": "vs_DeSO2018", "galler": "t.o.m. referensår 2023"},
        "DeSO 2025": {"kod": "0114C1010_DeSO2025", "kodlista": "vs_DeSO2025", "galler": "fr.o.m. referensår 2024"},
    }
    assert new["ssd_koder"] == {
        "DeSO 2025": {"kod": "0114C1061_DeSO2025", "kodlista": "vs_DeSO2025", "galler": "fr.o.m. referensår 2024"}
    }
    assert old["ssd_koder"] == {
        "DeSO 2018": {"kod": "0586C2010", "kodlista": "vs_DeSO2018", "galler": "t.o.m. referensår 2023"}
    }
    assert regso["ssd_koder"] == {
        "RegSO 2020": {"kod": "0114R010", "kodlista": "vs_RegSO2020", "galler": "t.o.m. referensår 2023"},
        "RegSO 2025": {"kod": "0114R010_RegSO2025", "kodlista": "vs_RegSO2025", "galler": "fr.o.m. referensår 2024"},
    }
    for hit in (both, new, old, regso):
        assert list(hit["ssd_koder"]) == hit["finns_i"]
        assert "ssd_koder följer SCB:s kodlistor i Statistikdatabasen" in hit["verifiering_not"]
        assert "TAB6680" in hit["verifiering_not"] and "scb_get_table_metadata" in hit["verifiering_not"]
    assert deso.SSD_SUFFIX == {"deso": {"2025": "_DeSO2025"}, "regso": {"2025": "_RegSO2025"}}
    assert deso.SSD_CODELISTS["deso"] == {"2018": "vs_DeSO2018", "2025": "vs_DeSO2025"}
    assert deso.SSD_CODELISTS["regso"] == {"2018": "vs_RegSO2020", "2025": "vs_RegSO2025"}


async def test_list_deso_by_name_and_code(make_client):
    async with make_client("reference") as client:
        vasby = await call(client, "ref_list_deso", {"municipality": "Upplands Väsby"})
        stockholm = await call(client, "ref_list_deso", {"municipality": "0180"})
    assert vasby["kommunkod"] == "0114" and vasby["kommun"] == "Upplands Väsby"
    assert vasby["version"] == {"deso": "DeSO 2025", "regso": "RegSO 2025"}
    assert vasby["urval"] == {"regso": None, "kategori": None}
    assert vasby["antal"] == sum(vasby["per_kategori"].values()) == sum(g["antal"] for g in vasby["regso"])
    assert [g["kod"] for g in vasby["regso"]] == sorted(g["kod"] for g in vasby["regso"])
    assert all(g["antal"] == len(g["deso"]) and g["deso"] == sorted(g["deso"]) for g in vasby["regso"])
    assert {"kod": "0114R010", "namn": "Upplands Väsby omland", "antal": 1, "deso": ["0114A0010"]} in vasby["regso"]
    assert all(c.startswith("0114") for g in vasby["regso"] for c in g["deso"])
    assert vasby["kalla"]["filer"] == [
        {"namn": "koppling-deso2025-regso2025_2026-03-25.xlsx", "datum_i_fil": "2026-03-25"}
    ]
    assert vasby["verifiering"] == "myndighetswebb"

    assert stockholm["kommun"] == "Stockholm" and stockholm["antal"] == 569
    assert stockholm["per_kategori"] == {"A": 0, "B": 0, "C": 569}
    assert client_size(compact_json(stockholm)) <= 30_000


async def test_list_deso_filters(make_client):
    async with make_client("reference") as client:
        by_code = await call(client, "ref_list_deso", {"municipality": "0114", "regso": "0114R006"})
        by_name = await call(client, "ref_list_deso", {"municipality": "0114", "regso": "runby södra-ed"})
        category = await call(client, "ref_list_deso", {"municipality": "0114", "category": "A", "version": "2018"})
        old = await call(client, "ref_list_deso", {"municipality": "0114", "version": "2018", "regso": "0114R006"})
    assert [g["kod"] for g in by_code["regso"]] == ["0114R006"] and by_code["urval"]["regso"] == "0114R006"
    assert by_name["regso"] == by_code["regso"]
    assert "0114C1061" in by_code["regso"][0]["deso"] and "0114C1060" not in by_code["regso"][0]["deso"]
    assert category["version"] == {"deso": "DeSO 2018", "regso": "RegSO 2020"}
    assert category["per_kategori"] == {"A": 1, "B": 0, "C": 0} and category["antal"] == 1
    assert all(c[4] == "A" for g in category["regso"] for c in g["deso"])
    # DeSO 2018 holds the parent that was split in 2025.
    assert "0114C1060" in old["regso"][0]["deso"] and "0114C1061" not in old["regso"][0]["deso"]
    assert old["kalla"]["filer"][0]["namn"] == "koppling-deso2018-regso2020_2026-03-25.xlsx"


async def test_list_deso_errors(make_client):
    async with make_client("reference") as client:
        err = await call_error(client, "ref_list_deso", {"municipality": "9999"})
        assert "Okänd kommun" in err
        err = await call_error(client, "ref_list_deso", {"municipality": "Hälsingborg"})
        assert "inte entydig" in err and "Helsingborg (1283)" in err
        err = await call_error(client, "ref_list_deso", {"municipality": "   "})
        assert "Ange kommunkod" in err
        err = await call_error(client, "ref_list_deso", {"municipality": "0114", "regso": "Finnsinte"})
        assert "finns inte i Upplands Väsby (0114)" in err and "0114R001 Bollstanäs" in err
        err = await call_error(client, "ref_list_deso", {"municipality": "0114", "regso": "0180R001"})
        assert "finns inte i Upplands Väsby" in err


async def test_code_list_tools_keep_the_deso_lists_within_one_answer(make_client):
    """ref_get_code_list leaves the lookup tables out (with a _kortat note) unless search narrows them; the index
    reports the file's own counts instead of antal None."""
    async with make_client("reference") as client:
        index = {entry["id"]: entry for entry in await call(client, "ref_list_code_lists")}
        whole = {name: await call(client, "ref_get_code_list", {"name": name}) for name in deso.VERSIONS.values()}
        whole[deso.LOG] = await call(client, "ref_get_code_list", {"name": deso.LOG})
        vasby = await call(client, "ref_get_code_list", {"name": "deso_regso_2025", "search": "0114"})
        rows = await call(client, "ref_get_code_list", {"name": deso.LOG, "search": "0114"})
        everything = await call(client, "ref_get_code_list", {"name": "deso_regso_2025", "search": "0"})
        regions = await call(client, "ref_get_code_list", {"name": "regioner"})
    assert index["deso_regso_2025"]["antal"] == {"deso": 6160, "regso": 3363, "kommuner": 290}
    assert index["deso_forandringar"]["antal"]["rader"] == 1234
    # The page address is the one the files were downloaded from (verifiering_not says so); not machine-checked.
    assert index["deso_regso_2025"]["verifiering"]["kalla_url"] == "myndighetswebb"
    for name, doc in whole.items():
        assert client_size(compact_json(doc)) <= 32_768, name
        assert doc["fil"]["namn"] in SOURCE_FILES and doc["antal"] and "kalla" in doc
        assert "deso" not in doc and "regso" not in doc and "forandringar" not in doc
        assert (
            "ref_lookup_deso" in doc["_kortat"]["meddelande"]
            and f"fuzzy://codes/{name}" in doc["_kortat"]["meddelande"]
        )
    assert [c["falt"] for c in whole["deso_regso_2025"]["_kortat"]["kortade"]] == ["deso", "regso"]
    assert whole[deso.LOG]["_kortat"]["kortade"] == [{"falt": "forandringar", "fore": 1234, "efter": 0}]
    # search filters the tables by key/value (deso, regso) and by any cell (forandringar), so the answer fits.
    assert vasby["search"] == "0114" and "_kortat" not in vasby and client_size(compact_json(vasby)) <= 32_768
    assert vasby["deso"] and all(c.startswith("0114") for c in vasby["deso"])
    assert vasby["regso"]["0114R010"] == "Upplands Väsby omland" and vasby["version"] == {
        "deso": "DeSO 2025",
        "regso": "RegSO 2025",
    }
    assert vasby["kategorier"] == []  # lists of posts are filtered on kod/namn as before
    assert rows["forandringar"] and all("0114" in (r[0], r[1]) or r[0][:4] == "0114" for r in rows["forandringar"])
    assert ["0114C1060", "0114C1061", "Upphör; till nybildad", "2025-01-01"] in rows["forandringar"]
    # A search that keeps nearly everything still gets the header and the note rather than 250 kB.
    assert "_kortat" in everything and "deso" not in everything and everything["search"] == "0"
    # Lists without lookup tables are returned as before (regioner is a list of posts, trimmed only by the cap).
    assert "_kortat" not in regions and len(regions["kommuner"]) == 290


async def test_deso_tools_are_registered_as_local_read_only(make_client):
    async with make_client("reference") as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    for name in ("ref_lookup_deso", "ref_list_deso"):
        tool = tools[name]
        assert tool.annotations.read_only_hint is True and tool.annotations.open_world_hint is False
        assert tool.description and not tool.description.startswith(" ")
    assert set(tools["ref_lookup_deso"].input_schema["required"]) == {"code"}
    assert tools["ref_list_deso"].input_schema["properties"]["version"]["enum"] == ["2025", "2018"]


# --- Pure functions ---------------------------------------------------------------------------------


def test_normalize_code():
    assert deso.normalize_code(" 0180c1010 ") == "0180C1010"
    assert deso.normalize_code("0180r001") == "0180R001"
    for bad in ("", "0180", "0180D1010", "0180C101", "0180R0001", "018C1010A"):
        with pytest.raises(deso.InvalidInputError):
            deso.normalize_code(bad)


def test_resolve_municipality():
    assert deso.resolve_municipality("0180") == ("0180", "Stockholm")
    assert deso.resolve_municipality("goteborg") == ("1480", "Göteborg")
    # Diacritics are stripped in the fuzzy match, so the exact spelling must win when it matches one name.
    assert deso.resolve_municipality("Håbo") == ("0305", "Håbo")
    assert deso.resolve_municipality("Habo") == ("0643", "Habo")
    with pytest.raises(deso.InvalidInputError, match="inte entydig"):
        deso.resolve_municipality("Upsala")


def test_karlskoga_rows_from_2018_predate_the_key_files():
    """Two rows are dated 2018-02-21: 1883A0010 and 1883A0020 ended into 1883A0030, and neither parent is in
    DeSO 2018 or DeSO 2025. The DeSO 2018 file already holds the merged code, so between the bundled versions the
    log shows no change: 'oförändrad' with a note about the earlier change, and the parents are explained."""
    rows = [r for r in deso.log() if r.datum == "2018-02-21"]
    assert [(r.fran, r.till, r.typ) for r in rows] == [
        ("1883A0010", "1883A0030", "Upphör; till nybildad"),
        ("1883A0020", "1883A0030", "Upphör; till nybildad"),
    ]
    assert deso.deso_versions("1883A0010") == deso.deso_versions("1883A0020") == []
    verdict = deso.comparability("1883A0030")
    assert verdict["status"] == "oförändrad" and "jämför inte" not in verdict["not"]
    assert "(2018-02-21) avser 1883A0010, 1883A0020, som inte finns i någon av nyckelfilerna" in verdict["not"]
    assert "ingen förändring mellan versionerna är loggad" in verdict["not"]
    assert deso.lookup("1883A0030")["forandringar"] == [r.as_dict() for r in rows]
    with pytest.raises(deso.InvalidInputError, match="1883A0010 → 1883A0030 2018-02-21"):
        deso.comparability("1883A0010")
    with pytest.raises(deso.InvalidInputError, match="förändringslogg"):
        deso.lookup("1883A0020")


def test_siblings_are_only_the_new_parts_of_the_parent():
    """1280C2080 ended into four new codes and the existing 1280C2320 ('Upphör; till befintlig'): the existing
    receiver is listed under forandringar but is not a sibling of the new parts."""
    verdict = deso.comparability("1280C2083")
    assert verdict["status"] == "nytt" and verdict["bildat_av"] == ["1280C2080"]
    assert verdict["summerbar_antal_till"] is None
    assert verdict["syskon"] == ["1280C2081", "1280C2082", "1280C2084"]
    assert deso.deso_versions("1280C2320") == ["2018", "2025"]
    assert {r.till for r in deso.rows_for("1280C2080")} == {*verdict["syskon"], "1280C2083", "1280C2320"}
    s18 = set(codes.load("deso_regso_2018")["deso"])
    new = (c for c in codes.load("deso_regso_2025")["deso"] if c not in s18)
    assert all(not s18 & set(deso.comparability(c)["syskon"]) for c in new)


def test_key_file_invariants():
    """The figures SCB's files give, as counted when the code lists were built."""
    d18, d25 = codes.load("deso_regso_2018"), codes.load("deso_regso_2025")
    s18, s25 = set(d18["deso"]), set(d25["deso"])
    assert (len(s18), len(s25), len(s18 & s25), len(s18 - s25), len(s25 - s18)) == (5984, 6160, 5835, 149, 325)
    assert d18["antal"]["deso"] == 5984 and d25["antal"]["deso"] == 6160
    assert len(d18["regso"]) == len(d25["regso"]) == 3363
    assert set(d18["regso"]) - set(d25["regso"]) == {"2584R001"} and set(d25["regso"]) - set(d18["regso"]) == {
        "2523R011"
    }
    assert sum(1 for k in d18["regso"] if k in d25["regso"] and d18["regso"][k] != d25["regso"][k]) == 10
    assert sum(1 for n in Counter(d25["regso"].values()).values() if n > 1) == 17
    assert sum(1 for c in s25 if c[8] != "0") == 316 and all(c[8] == "0" for c in s18)
    assert all(deso.DESO_CODE.match(c) for c in s18 | s25)
    assert all(deso.REGSO_CODE.match(c) for c in set(d18["regso"]) | set(d25["regso"]))
    municipalities = {k["kod"] for k in codes.load("regioner")["kommuner"]}
    assert {c[:4] for c in s18 | s25} == municipalities == {c[:4] for c in d25["regso"]}
    assert set(d18["deso"].values()) <= set(d18["regso"]) and set(d25["deso"].values()) <= set(d25["regso"])
    assert all(c[:4] == r[:4] for doc in (d18, d25) for c, r in doc["deso"].items())
    assert {k["kod"] for k in d25["kategorier"]} == {"A", "B", "C"}
    assert d25["antal"]["deso"] == sum(k["antal"] for k in d25["kategorier"])


def test_change_log_invariants():
    """Where the log's codes live: every ``fran`` is a DeSO 2018 code (except Karlskoga's two 2018-02-21 rows),
    every ``till`` is a DeSO 2025 code, and a ``fran`` that is also in DeSO 2025 is exactly a code with a
    self row (``fran == till``: the surviving code shrinks or grows)."""
    s18, s25 = set(codes.load("deso_regso_2018")["deso"]), set(codes.load("deso_regso_2025")["deso"])
    rows = deso.log()
    assert len(rows) == 1234 and Counter(r.datum for r in rows) == {"2025-01-01": 1232, "2018-02-21": 2}
    fran, till = {r.fran for r in rows}, {r.till for r in rows}
    assert fran - s18 == {"1883A0010", "1883A0020"}
    assert till <= s25
    self_rows = {r.fran for r in rows if r.fran == r.till}
    assert len(self_rows) == 603 and self_rows <= s18 & s25 and fran & s25 == self_rows
    assert {r.typ for r in rows if r.fran == r.till} == {
        "Minskas och ger del till befintlig eller nybildad",
        "Utvidgas till territorialvattengräns",
        "Utvidgas",
    }
    assert s18 - s25 <= fran and s25 - s18 <= till  # every ended and new code is in the log
    assert not (till & (s18 - s25)) and not (fran & (s25 - s18))  # ended codes never receive, new never give
    cross = [r for r in rows if r.fran[:4] != r.till[:4]]
    assert [(r.fran, r.till) for r in cross] == [
        ("0182C1030", "0138C1170"),
        ("0184C1180", "0180C5261"),
        ("0184C1180", "0180C5263"),
    ]
    recoded = [r for r in rows if r.typ == deso.RECODED]
    assert len(recoded) == 5 and all(r.fran[:5] == "0586C" and r.till == "0586B" + r.fran[5:] for r in recoded)
    types = {t["typ"]: t["antal"] for t in codes.load("deso_forandringar")["forandringstyper"]}
    assert Counter(r.typ for r in rows) == types


def test_comparability_totals_match_the_counted_figures():
    s18, s25 = set(codes.load("deso_regso_2018")["deso"]), set(codes.load("deso_regso_2025")["deso"])
    verdicts = {code: deso.comparability(code) for code in s18 | s25}
    status = Counter(v["status"] for v in verdicts.values())
    assert status == {"oförändrad": 5148, "ändrad gräns": 687, "upphört": 149, "nytt": 325}
    assert status["oförändrad"] + status["ändrad gräns"] == 5835
    # 603 codes have a self row; the other 84 only receive (catalog.JOIN_KEYS and CHANGELOG quote both figures).
    changed = {c for c, v in verdicts.items() if v["status"] == "ändrad gräns"}
    self_rows = {r.fran for r in deso.log() if r.fran == r.till}
    assert len(changed & self_rows) == 603 and len(changed - self_rows) == 84
    assert {r.typ for c in changed - self_rows for r in deso.rows_for(c)} == {
        "Ger del/delar till befintlig",
        "Upphör; till befintlig",
    }
    ended = {c: v for c, v in verdicts.items() if v["status"] == "upphört"}
    new = {c: v for c, v in verdicts.items() if v["status"] == "nytt"}
    assert sum(v["summerbar_antal"] for v in ended.values()) == 88
    assert sum(len(v["bildat_av"]) > 1 for v in new.values()) == 53
    assert all(v["ersatts_av"] and set(v["ersatts_av"]) <= s25 for v in ended.values())
    assert all(v["bildat_av"] and set(v["bildat_av"]) <= s18 for v in new.values())
    # Parts and parents agree: every part of a summable split points back to it and only to it.
    parts = {part: parent for parent, v in ended.items() if v["summerbar_antal"] for part in v["ersatts_av"]}
    assert parts == {c: v["summerbar_antal_till"] for c, v in new.items() if v["summerbar_antal_till"]}
    assert all(set(ended[p]["ersatts_av"]) == {c, *new[c]["syskon"]} for c, p in parts.items())
    assert all(v["verifiering"] == "härlett" and v["not"] for v in verdicts.values())
    assert all("jämför inte över tid" in v["not"] for v in verdicts.values() if v["status"] == "ändrad gräns")


def test_lookup_output_lists_are_sorted_and_complete():
    doc = codes.load("deso_regso_2025")
    hit = deso.lookup("0180R001")
    members = hit["versioner"][1]["deso"]
    assert members == sorted(c for c, r in doc["deso"].items() if r == "0180R001") and len(members) > 1
    listing = deso.list_deso("1480")  # Göteborg has the most RegSO
    assert len(listing["regso"]) == 147 and listing["antal"] == sum(1 for c in doc["deso"] if c.startswith("1480"))
    with pytest.raises(deso.InvalidInputError, match="Okänd version"):
        deso.list_deso("0180", version="2020")
    with pytest.raises(deso.InvalidInputError, match="Okänd kategori"):
        deso.list_deso("0180", category="D")
