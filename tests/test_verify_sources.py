# SPDX-FileCopyrightText: 2026 Deniz Özer
#
# SPDX-License-Identifier: MIT

import base64
import hashlib
import importlib.util
import json
import urllib.error
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "verify_sources.py"
if not SCRIPT.exists():  # the sdist ships tests/ but not scripts/
    pytest.skip("scripts/verify_sources.py finns bara i repot", allow_module_level=True)
spec = importlib.util.spec_from_file_location("verify_sources", SCRIPT)
vs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vs)

FIXTURES = ROOT / "tests" / "fixtures"


def response(url: str, body: bytes | str | Any, status: int = 200, content_type: str = "application/json") -> dict:
    if not isinstance(body, (bytes, str)):
        body = json.dumps(body)
    if isinstance(body, str):
        body = body.encode("utf-8")
    return {"url": url, "final_url": url, "status": status, "content_type": content_type, "body": body, "headers": {}}


def lines(capsys: pytest.CaptureFixture[str]) -> list[tuple[str, str]]:
    out = capsys.readouterr().out.splitlines()
    return [tuple(line.split(" ", 1)) for line in out]


def verify_lines(capsys: pytest.CaptureFixture[str]) -> dict[str, dict]:
    result: dict[str, dict] = {}
    for tag, rest in lines(capsys):
        assert tag in {"VERIFY", "BODY", "TEXT", "B64"}
        if tag == "VERIFY":
            entry = json.loads(rest)
            result[entry["check"]] = entry
    return result


@pytest.mark.parametrize(
    ("url", "ok"),
    [
        ("https://geodata.scb.se/geoserver/stat/wfs", True),
        ("https://sdb.socialstyrelsen.se/api/v1/sv", True),
        ("http://geodata.scb.se/geoserver/stat/wfs", False),
        ("https://example.org/?q=geodata.scb.se", False),
        ("https://geodata.scb.se@example.org/", False),
        ("https://geodata.scb.se.example.org/", False),
        ("file:///etc/passwd", False),
    ],
)
def test_allowed_hosts(url: str, ok: bool) -> None:
    assert vs.allowed(url) is ok


def test_fetch_refuses_other_hosts_without_network() -> None:
    assert vs.fetch("https://example.org/") == {"url": "https://example.org/", "error": "otillåten värd"}


def test_redirect_to_other_host_is_refused() -> None:
    handler = vs._AllowedHostsRedirect()
    with pytest.raises(urllib.error.HTTPError):
        handler.redirect_request(None, None, 302, "Found", {}, "https://example.org/")


@pytest.mark.parametrize(
    ("code", "cls"),
    [
        ("00", "riket"),
        ("01", "lan"),
        ("0180", "kommun"),
        ("0180C1010", "deso"),
        ("0114A0010", "deso"),
        ("0114C1010_DeSO2025", "deso_suffix"),
        ("2584R014", "regso"),
        ("2584R014_RegSO2025", "regso_suffix"),
        ("0180D1010", "annan"),
        ("Stockholm", "annan"),
    ],
)
def test_classify_region_code(code: str, cls: str) -> None:
    assert vs.classify_region_code(code) == cls


def test_summarise_codes_counts_classes_and_suffixes() -> None:
    summary = vs.summarise_codes(["00", "0180", "0180C1010", "0180C1010_DeSO2025"], {"0180": "Stockholm"})
    assert summary["antal"] == 4
    assert summary["klasser"]["kommun"] == {"antal": 1, "exempel": [["0180", "Stockholm"]]}
    assert summary["suffix"] == ["DeSO2025"]


def test_ordered_codes_follows_index_object_and_array() -> None:
    assert vs.ordered_codes({"category": {"index": {"b": 1, "a": 0}}}) == ["a", "b"]
    assert vs.ordered_codes({"category": {"index": ["x", "y"]}}) == ["x", "y"]
    assert vs.ordered_codes({}) == []


def test_dimension_codelists_reads_both_spellings() -> None:
    assert vs.dimension_codelists({"extension": {"codelists": [{"id": "vs_A"}]}}) == [{"id": "vs_A"}]
    assert vs.dimension_codelists({"extension": {"codeLists": [{"id": "agg_B"}]}}) == [{"id": "agg_B"}]
    assert vs.SAFE_ID.match("vs_RegionLän07")
    assert not vs.SAFE_ID.match("../x")
    assert vs.path_segment("agg_Ålder5år") == "agg_%C3%85lder5%C3%A5r"


def test_chunks_reassemble() -> None:
    text = "x" * (vs.CHUNK * 2 + 5)
    parts = vs.chunks(text)
    assert len(parts) == 3
    assert "".join(parts) == text
    assert vs.chunks("") == [""]


def test_html_to_text_and_links() -> None:
    page = (
        "<html><head><script>var x = 1;</script><style>p{}</style></head><body>"
        "<h1>DeSO</h1><p>Text &amp; mer</p>"
        '<a href="/contentassets/a/koppling-deso2025-regso2025.xlsx">Koppling DeSO2025 – RegSO2025</a>'
        '<a href="https://example.org/x.pdf">Extern</a></body></html>'
    )
    text, links = vs.html_to_text_and_links(page, "https://www.scb.se/sida/")
    assert "var x" not in text
    assert "Text & mer" in text
    assert links[0] == (
        "https://www.scb.se/contentassets/a/koppling-deso2025-regso2025.xlsx",
        "Koppling DeSO2025 – RegSO2025",
    )
    assert vs.is_key_file(*links[0])
    assert not vs.is_key_file("https://www.scb.se/a/deso-karta.pdf", "Karta")
    assert vs.is_key_file("https://www.scb.se/a/fil.xlsx", "Historiska förändringar i DeSO")


def test_page_name_uses_the_last_two_segments() -> None:
    """Two of SCB's pages end in 'demografiska-statistikomraden-deso'; the parent segment keeps them apart."""
    names = [vs.page_name(url) for url in vs.SCB_PAGES]
    assert len(set(names)) == len(vs.SCB_PAGES)
    assert "regionala-indelningar.demografiska-statistikomraden-deso" in names
    assert "oppna-geodata.demografiska-statistikomraden-deso" in names
    assert vs.page_name("https://www.scb.se/a/b/c/") == "b.c"
    assert vs.page_name("https://www.scb.se/") == "rot"


def test_capabilities_summary_from_fixture() -> None:
    summary = vs.capabilities_summary((FIXTURES / "scb_geodata_capabilities.xml").read_bytes())
    assert "DeSO_2025" in summary["lager"]
    assert any(layer["namn"] == "DeSO_2025" for layer in summary["deso_regso"])
    assert vs.capabilities_summary(b"<inte xml")["parse_error"]


def test_record_emits_verify_and_body(capsys: pytest.CaptureFixture[str]) -> None:
    parsed = vs.record("prov", response("https://www.scb.se/x", {"a": 1}), body=True)
    assert parsed == {"a": 1}
    out = lines(capsys)
    verify = json.loads(out[0][1])
    assert verify["status"] == 200
    assert verify["sha256"] == hashlib.sha256(b'{"a": 1}').hexdigest()
    assert out[1] == ("BODY", 'prov {"a":1}')


def test_record_reports_network_errors(capsys: pytest.CaptureFixture[str]) -> None:
    assert vs.record("prov", {"url": "https://www.scb.se/x", "error": "URLError: nej"}) is None
    assert json.loads(lines(capsys)[0][1])["error"] == "URLError: nej"


def test_main_fails_only_without_any_response(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vs, "_responses", 0)
    monkeypatch.setitem(vs.GROUPS, "scb-geodata", lambda: None)
    assert vs.main(["--group", "scb-geodata"]) == 1
    with pytest.raises(SystemExit):
        vs.main(["--group", "https://example.org/"])


def fake_fetch(routes: dict[str, Any], default: Any = None):
    """A stand-in for fetch: the first route whose key occurs in the URL answers, otherwise ``default`` or 404."""
    calls: list[str] = []

    def fetch(url: str, accept: str | None = None) -> dict:
        assert vs.allowed(url), url
        calls.append(url)
        for needle, body in routes.items():
            if needle in url:
                return body(url) if callable(body) else response(url, body)
        if default is not None:
            return default(url)
        return response(url, b"", status=404, content_type="text/plain")

    return fetch, calls


def test_scb_geodata_group_with_stubbed_answers(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    describe = json.loads((FIXTURES / "scb_geodata_describe_deso.json").read_text())
    features = json.loads((FIXTURES / "scb_geodata_features.json").read_text())
    routes = {
        "GetCapabilities": (FIXTURES / "scb_geodata_capabilities.xml").read_text(),
        "DescribeFeatureType": describe,
        "resultType=hits": '<wfs:FeatureCollection numberOfFeatures="6160" numberMatched="6160"/>',
        "outputFormat=csv": "desokod,kommunkod\n0180C1010,0180\n",
        "GetFeature": features,
    }
    fetch, calls = fake_fetch(routes)
    monkeypatch.setattr(vs, "fetch", fetch)
    vs.check_scb_geodata()
    checks = verify_lines(capsys)
    # Expected values follow the fixtures, which mirror the WFS answers.
    properties = describe["featureTypes"][0]["properties"]
    attributes = [p["name"] for p in properties if not p["type"].startswith("gml:")]
    geometry = next(p["name"] for p in properties if p["type"].startswith("gml:"))
    codes = [f["properties"]["desokod"] for f in features["features"]]
    assert checks["wfs.hits.DeSO_2025"]["summary"]["numberOfFeatures"] == 6160
    assert checks["wfs.hits.version_2_0_0"]["summary"]["numberMatched"] == 6160
    assert checks["wfs.kommun.DeSO_2025"]["summary"]["filter"] == "kommunkod='0180'"
    assert checks["wfs.sida.jamforelse"]["start0_max4"] == codes
    assert checks["wfs.intersects.x_y"]["summary"]["filter"] == f"INTERSECTS({geometry},POINT(674032 6580822))"
    assert checks["wfs.intersects.y_x"]["summary"]["filter"] == f"INTERSECTS({geometry},POINT(6580822 674032))"
    # EWKT variants (unverified so far): both orders are tried so the next run tells which one, if any, works.
    assert checks["wfs.intersects.srid4326"]["summary"]["filter"] == (
        f"INTERSECTS({geometry},SRID=4326;POINT(18.06 59.33))"
    )
    assert checks["wfs.intersects.srid4326_latlon"]["summary"]["filter"] == (
        f"INTERSECTS({geometry},SRID=4326;POINT(59.33 18.06))"
    )
    assert all(
        isinstance(checks[f"wfs.intersects.{label}"]["summary"]["egenskaper"], list)
        for label in ("x_y", "y_x", "srid4326", "srid4326_latlon")
    )
    # Not yet verified against the live service: the point search in the other three layers and the parameter
    # combinations the server sends (filter + paging, sortBy=regsokod, propertyName with geometry + srsName).
    for other in ("DeSO_2018", "RegSO_2020", "RegSO_2025"):
        assert checks[f"wfs.intersects.y_x.{other}"]["summary"]["filter"] == (
            f"INTERSECTS({geometry},POINT(6580822 674032))"
        )
        assert f"typeName=stat:{other}" in checks[f"wfs.intersects.y_x.{other}"]["url"]
    assert checks["wfs.sida.filter"]["summary"] == {"filter": "kommunkod='0180'", "koder": codes}
    assert "sortBy=desokod" in checks["wfs.sida.filter"]["url"] and "startIndex=2" in checks["wfs.sida.filter"]["url"]
    assert checks["wfs.sida.regso"]["summary"]["sortBy"] == "regsokod"
    assert "typeName=stat:RegSO_2025" in checks["wfs.sida.regso"]["url"]
    assert f"propertyName=desokod,{geometry}" in checks["wfs.crs.propertyName"]["url"]
    assert "srsName=EPSG:4326" in checks["wfs.crs.propertyName"]["url"]
    assert checks["wfs.crs.propertyName"]["summary"]["koder"] == codes
    assert checks["wfs.csv.alla"]["summary"]["rubrik"] == "desokod,kommunkod"
    assert any(f"propertyName={','.join(attributes)}" in url for url in calls)


def test_scb_pxweb_group_with_stubbed_answers(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    metadata = json.loads((FIXTURES / "scb_metadata_tab638.json").read_text())
    metadata["dimension"]["Region"]["category"]["index"]["0180C1010_DeSO2025"] = 99
    routes = {
        "/config": {"apiVersion": "2.0.0"},
        "/tables?": (FIXTURES / "scb_tables.json").read_text(),
        "/metadata": metadata,
        "/codelists/": {"id": "vs_RegionLän07", "label": "Län", "type": "Valueset", "values": [{"code": "01"}]},
        "/data": {"version": "2.0", "class": "dataset", "dimension": {"Region": {"category": {"index": ["0180"]}}}},
    }
    fetch, calls = fake_fetch(routes)
    monkeypatch.setattr(vs, "fetch", fetch)
    vs.check_scb_pxweb()
    checks = verify_lines(capsys)
    meta = checks["pxweb.metadata.TAB638"]["summary"]["dimensioner"]
    assert meta["Region"]["koder"]["suffix"] == ["DeSO2025"]
    assert meta["Alder"]["codelists_nyckel"] == "codeLists"
    assert "pxweb.codelist.vs_RegionLän07" in checks
    assert "pxweb.codelist.agg_Ålder5år" in checks
    data_url = next(url for url in calls if "/data" in url and "*" not in url)
    assert "valueCodes%5BRegion%5D=0180C1010_DeSO2025,0180&" in data_url
    assert data_url.endswith("valueCodes%5BTid%5D=2023,2024")
    assert checks["pxweb.data.TAB638.wildcard"]["summary"]["filter"] == "0180C*"


def test_scb_key_files_are_emitted_as_base64(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    page = '<p>Nycklar</p><a href="/contentassets/k/koppling-deso2025-regso2025.xlsx">Koppling DeSO2025 - RegSO2025</a>'
    workbook = b"PK\x03\x04" + bytes(range(256)) * 20
    routes = {
        "koppling-deso2025-regso2025.xlsx": lambda url: response(
            url, workbook, content_type="application/vnd.ms-excel"
        ),
    }
    fetch, _ = fake_fetch(routes, default=lambda url: response(url, page, content_type="text/html"))
    monkeypatch.setattr(vs, "fetch", fetch)
    vs.check_scb_keys()
    out = lines(capsys)
    verify = {json.loads(rest)["check"]: json.loads(rest) for tag, rest in out if tag == "VERIFY"}
    assert verify["scb.nyckelfil1"]["sha256"] == hashlib.sha256(workbook).hexdigest()
    chunks = [rest.split(" ", 2) for tag, rest in out if tag == "B64"]
    assert all(name == "nyckelfil1" for name, _, _ in chunks)
    assert base64.b64decode("".join(data for _, _, data in chunks)) == workbook
    assert any(tag == "TEXT" and "Nycklar" in rest for tag, rest in out)
    # One VERIFY and one TEXT record per page, named by the last two path segments (no two pages share a name).
    pages = [check for check in verify if check.startswith("scb.sida.")]
    assert len(pages) == len(vs.SCB_PAGES) == len(set(pages))
    assert "scb.sida.oppna-geodata.demografiska-statistikomraden-deso" in pages
    text_names = {rest.split(" ", 1)[0] for tag, rest in out if tag == "TEXT"}
    assert text_names == set(pages)


def test_socialstyrelsen_group_with_stubbed_answers(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    routes = {
        "sdbapi.aspx": lambda url: response(url, "<h1>API</h1>", content_type="text/html"),
        "/resultat/": {"data": [{"ar": 2024, "varde": 1}], "sida": 1, "per_sida": 5, "sidor": 9},
        "/api/v1/sv/dodsorsaker/region": [{"id": 0, "text": "Riket"}, {"id": 1, "text": "Stockholms län"}],
        "/api/v1/sv/dodsorsaker/ar": [{"id": 2023, "text": "2023"}, {"id": 2024, "text": "2024"}],
        "/api/v1/sv/dodsorsaker": [{"namn": "region"}, {"namn": "ar"}, {"namn": "Ålder"}],
        "/api/v1/sv": [{"namn": "dodsorsaker", "text": "Dödsorsaker"}],
    }
    fetch, calls = fake_fetch(routes)
    monkeypatch.setattr(vs, "fetch", fetch)
    vs.check_socialstyrelsen()
    checks = verify_lines(capsys)
    assert checks["sdb.amne.lakemedel"] == {"check": "sdb.amne.lakemedel", "finns": False}
    assert checks["sdb.varden.dodsorsaker"]["hoppar_over_variabel"] == "Ålder"
    assert f"{vs.SDB}/api/v1/sv/dodsorsaker/resultat/region/0/ar/2024?per_sida=5" in calls
    assert checks["sdb.resultat.dodsorsaker.sidor"]["summary"]["sidor"] == 9


def test_job_summary_lists_each_check(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys) -> None:
    monkeypatch.setattr(vs, "_outcomes", [])
    vs.record("ok", response("https://www.scb.se/x", {}))
    vs.record("fel", {"url": "https://www.scb.se/y", "error": "URLError: a|b"})
    target = tmp_path / "summary.md"
    vs.write_job_summary("scb-nycklar", str(target))
    text = target.read_text(encoding="utf-8")
    assert "### Källkontroll: scb-nycklar" in text
    assert "| `ok` | 200 |" in text
    assert "| `fel` | URLError: a/b |" in text
    vs.write_job_summary("scb-nycklar", None)  # outside Actions: nothing is written


def test_every_record_is_one_line_and_text_round_trips(capsys: pytest.CaptureFixture[str]) -> None:
    text = "Rubrik\n::error::inte ett kommando\r\nslut" + "x" * vs.CHUNK
    vs.emit_chunks("TEXT", "sida", text, as_json=True)
    vs.emit("BODY", "prov", "rad1\nrad2")
    out = capsys.readouterr().out.splitlines()
    assert len(out) == 3
    assert all(line.startswith(("TEXT sida ", "BODY prov ")) for line in out)
    assert "".join(json.loads(line.split(" ", 3)[3]) for line in out[:2]) == text
    assert out[2] == "BODY prov rad1\\nrad2"
