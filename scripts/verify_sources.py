# SPDX-FileCopyrightText: 2026 Deniz Özer
#
# SPDX-License-Identifier: MIT

"""Source check: a fixed list of read-only requests that verifies the agencies' API contracts and records the answers.

    python scripts/verify_sources.py --group scb-geodata|scb-pxweb|scb-nycklar|socialstyrelsen

Runs from the GitHub Actions workflow "Källkontroll" (or any machine with network access) and uses only the standard
library. Only GET, only the hosts in ALLOWED_HOSTS, redirects only within them, no input from the outside apart from
the group name. Every output line starts with a tag:

    VERIFY {json}               one request: check, url, status, content type, size, sha256 and a summary
    BODY <check> {json}         the whole response body (small JSON responses only)
    TEXT <check> <i>/<n> <text> a web page as plain text, in chunks
    B64 <name> <i>/<n> <data>   a downloaded file in base64 chunks (its sha256 is in the VERIFY line)

Exit code 1 only if no request got an HTTP response (e.g. no network); HTTP errors are results, not failures.
"""

import argparse
import base64
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from typing import Any

ALLOWED_HOSTS = frozenset(
    {"geodata.scb.se", "statistikdatabasen.scb.se", "www.scb.se", "scb.se", "sdb.socialstyrelsen.se"}
)
USER_AGENT = "fuzzy-mcp-kallkontroll (+https://github.com/deno-li/fuzzy-MCP)"
TIMEOUT = 60
MAX_BYTES = 25_000_000
MAX_B64_BYTES = 4_000_000
BODY_MAX_CHARS = 60_000
TEXT_MAX_CHARS = 30_000
CHUNK = 4_000
INTERESTING_HEADERS = frozenset(
    {
        "deprecation",
        "sunset",
        "link",
        "retry-after",
        "x-ratelimit-limit",
        "x-ratelimit-remaining",
        "last-modified",
        "etag",
    }
)

WFS = "https://geodata.scb.se/geoserver/stat/wfs"
WFS_NS = "{http://www.opengis.net/wfs}"
DESO_LAYERS = ("DeSO_2018", "DeSO_2025", "RegSO_2020", "RegSO_2025")
SAMPLE_LAYER = "DeSO_2025"
SAMPLE_MUNICIPALITY = "0180"  # Stockholm; a neutral example
# A point in central Stockholm in SWEREF 99 TM (EPSG:3006): easting, northing.
SAMPLE_POINT = (674032, 6580822)
SRS_VARIANTS = ("EPSG:3006", "EPSG:4326", "urn:ogc:def:crs:EPSG::4326")

PXWEB = "https://statistikdatabasen.scb.se/api/v2"
PXWEB_QUERIES = ("DeSO", "RegSO", "FolkmDesoAldKon")

SCB_PAGES = (
    "https://www.scb.se/hitta-statistik/regional-statistik-och-kartor/regionala-indelningar/"
    "demografiska-statistikomraden-deso/",
    "https://www.scb.se/hitta-statistik/regional-statistik-och-kartor/regionala-indelningar/"
    "demografiska-statistikomraden-deso/deso-tabellerna-i-ssd--information-och-instruktioner/",
    "https://www.scb.se/vara-tjanster/oppna-data/oppna-geodata/demografiska-statistikomraden-deso/",
    "https://www.scb.se/hitta-statistik/regional-statistik-och-kartor/regionala-indelningar/"
    "regionala-statistikomraden-regso/",
    "https://www.scb.se/hitta-statistik/regional-statistik-och-kartor/regionala-indelningar/"
    "regionala-statistikomraden-regso/regso-tabellerna-i-statistikdatabasen---information-och-instruktioner/",
)
FILE_SUFFIXES = (".xlsx", ".xls", ".csv", ".zip", ".pdf")
KEY_FILE_SUFFIXES = (".xlsx", ".xls", ".csv")
KEY_FILE_PATTERN = re.compile(r"koppling|nyckel|historisk|f[öo]r[äa]ndring", re.IGNORECASE)
MAX_KEY_FILES = 6

SDB = "https://sdb.socialstyrelsen.se"
SDB_SUBJECTS = (
    "dodsorsaker",
    "lakemedel",
    "amning",
    "tandhalsa",
    "skadorochskadehandelserisverigeskommunerochlan",
    "yttreorsakertillskadorochforgiftningarbarn",
    "graviditeterforlossningarochnyfodda",
)
SDB_RESULT_SUBJECTS = ("dodsorsaker", "skadorochskadehandelserisverigeskommunerochlan")
SAFE_SEGMENT = re.compile(r"^[a-z0-9_]{1,80}$")
# Codes and codelist ids may contain Swedish letters (e.g. "vs_RegionLän07"); they are quoted before use in a URL.
SAFE_ID = re.compile(r"^[\w.\-]{1,60}$")

_responses = 0
_outcomes: list[tuple[str, str]] = []  # (check, status or error) for the job summary


# ----------------------------------------------------------------------------- output


def emit(tag: str, *parts: str) -> None:
    print(tag, *parts, flush=True)


def emit_json(tag: str, obj: Any) -> None:
    emit(tag, json.dumps(obj, ensure_ascii=False, separators=(",", ":")))


def chunks(text: str, size: int = CHUNK) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)] or [""]


def emit_chunks(tag: str, name: str, text: str) -> None:
    parts = chunks(text)
    for i, part in enumerate(parts, 1):
        emit(tag, name, f"{i}/{len(parts)}", part)


# ----------------------------------------------------------------------------- HTTP


def allowed(url: str) -> bool:
    parts = urllib.parse.urlsplit(url)
    return parts.scheme == "https" and (parts.hostname or "") in ALLOWED_HOSTS


class _AllowedHostsRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not allowed(newurl):
            raise urllib.error.HTTPError(newurl, code, f"omdirigering till otillåten värd: {newurl}", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_AllowedHostsRedirect)


def build_url(base: str, params: dict[str, Any] | None = None) -> str:
    if not params:
        return base
    return f"{base}?{urllib.parse.urlencode(params, safe=':,*()')}"


def fetch(url: str, accept: str | None = None) -> dict[str, Any]:
    """GET with one retry on network errors and 5xx; never raises."""
    global _responses
    if not allowed(url):
        return {"url": url, "error": "otillåten värd"}
    headers = {"User-Agent": USER_AGENT}
    if accept:
        headers["Accept"] = accept
    for attempt in (1, 2):
        try:
            with _opener.open(urllib.request.Request(url, headers=headers), timeout=TIMEOUT) as res:
                body = res.read(MAX_BYTES + 1)
                _responses += 1
                return {
                    "url": url,
                    "final_url": res.geturl(),
                    "status": res.status,
                    "content_type": res.headers.get("Content-Type", ""),
                    "body": body[:MAX_BYTES],
                    "truncated": len(body) > MAX_BYTES,
                    "headers": {k: v for k, v in res.headers.items() if k.lower() in INTERESTING_HEADERS},
                }
        except urllib.error.HTTPError as exc:
            if exc.code >= 500 and attempt == 1:
                time.sleep(3)
                continue
            _responses += 1
            return {
                "url": url,
                "status": exc.code,
                "content_type": exc.headers.get("Content-Type", "") if exc.headers else "",
                "body": exc.read(200_000) if exc.fp else b"",
            }
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if attempt == 1:
                time.sleep(3)
                continue
            return {"url": url, "error": f"{type(exc).__name__}: {exc}"}
    return {"url": url, "error": "okänt fel"}


def parse_json(data: bytes) -> Any:
    try:
        return json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def record(check: str, res: dict[str, Any], summary: Any = None, body: bool = False) -> Any:
    """Emit the VERIFY line (and BODY for small JSON) and return the parsed JSON body, or None."""
    line: dict[str, Any] = {"check": check, "url": res.get("url")}
    if "error" in res:
        line["error"] = res["error"]
        emit_json("VERIFY", line)
        _outcomes.append((check, str(res["error"])))
        return None
    data = res.get("body") or b""
    line.update(
        status=res["status"],
        content_type=res["content_type"],
        bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
    )
    if res.get("truncated"):
        line["truncated"] = True
    if res.get("final_url") and res["final_url"] != res["url"]:
        line["final_url"] = res["final_url"]
    if res.get("headers"):
        line["headers"] = res["headers"]
    parsed = parse_json(data)
    if summary is not None:
        line["summary"] = summary
    elif parsed is None and data:
        line["snippet"] = data[:500].decode("utf-8", "replace")
    emit_json("VERIFY", line)
    _outcomes.append((check, str(res["status"])))
    if body and parsed is not None:
        text = json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))
        emit("BODY", check, text if len(text) <= BODY_MAX_CHARS else json.dumps({"_truncated": len(text)}))
    return parsed


# ----------------------------------------------------------------------------- helpers


REGION_CLASSES = (
    ("riket", re.compile(r"^00$")),
    ("lan", re.compile(r"^\d{2}$")),
    ("kommun", re.compile(r"^\d{4}$")),
    ("deso", re.compile(r"^\d{4}[ABC]\d{4}$")),
    ("deso_suffix", re.compile(r"^\d{4}[ABC]\d{4}_[A-Za-z]+\d{4}$")),
    ("regso", re.compile(r"^\d{4}R\d{3}$")),
    ("regso_suffix", re.compile(r"^\d{4}R\d{3}_[A-Za-z]+\d{4}$")),
)


def classify_region_code(code: str) -> str:
    """Rough class of an SCB Region code, used only to summarise what a table mixes."""
    for name, pattern in REGION_CLASSES:
        if pattern.match(code):
            return name
    return "annan"


def summarise_codes(codes: list[str], labels: dict[str, str] | None = None) -> dict[str, Any]:
    classes: dict[str, list[str]] = {}
    for code in codes:
        classes.setdefault(classify_region_code(code), []).append(code)
    labels = labels or {}
    return {
        "antal": len(codes),
        "klasser": {
            name: {"antal": len(items), "exempel": [[c, labels.get(c, "")] for c in items[:6]]}
            for name, items in classes.items()
        },
        "suffix": sorted({c.split("_", 1)[1] for c in codes if "_" in c})[:20],
    }


def dimension_codelists(dimension: dict[str, Any]) -> list[dict[str, Any]]:
    """Codelist references of a PxWebApi 2 dimension; older servers spell the key "codeLists"."""
    extension = as_dict(dimension.get("extension"))
    return [as_dict(cl) for cl in as_list(extension.get("codelists") or extension.get("codeLists"))]


def path_segment(value: str) -> str:
    return urllib.parse.quote(value, safe="")


def ordered_codes(dimension: dict[str, Any]) -> list[str]:
    """Category codes of a JSON-stat dimension in index order (index may be an object or an array)."""
    index = as_dict(dimension.get("category")).get("index") or []
    if isinstance(index, dict):
        return sorted(index, key=lambda code: index[code])
    return [str(code) for code in index]


class _TextAndLinks(HTMLParser):
    BLOCK_TAGS = frozenset({"p", "br", "li", "tr", "h1", "h2", "h3", "h4", "div", "td", "th"})
    SKIP_TAGS = frozenset({"script", "style", "noscript"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.links: list[tuple[str, str]] = []
        self._skip = 0
        self._href: str | None = None
        self._link_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.SKIP_TAGS:
            self._skip += 1
        elif tag == "a":
            self._href = dict(attrs).get("href")
            self._link_text = []
        elif tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP_TAGS and self._skip:
            self._skip -= 1
        elif tag == "a" and self._href is not None:
            self.links.append((self._href, " ".join("".join(self._link_text).split())))
            self._href = None

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        self.parts.append(data)
        if self._href is not None:
            self._link_text.append(data)


def html_to_text_and_links(page: str, base_url: str) -> tuple[str, list[tuple[str, str]]]:
    """Visible text (one block per line) and absolute links with their link text."""
    parser = _TextAndLinks()
    parser.feed(page)
    text = re.sub(r"[ \t\r\f\v]+", " ", html.unescape("".join(parser.parts)))
    text = re.sub(r"\n\s*\n+", "\n", text).strip()
    links = [(urllib.parse.urljoin(base_url, href), label) for href, label in parser.links if href]
    return text, links


def is_key_file(url: str, label: str) -> bool:
    path = urllib.parse.unquote(urllib.parse.urlsplit(url).path)
    return path.lower().endswith(KEY_FILE_SUFFIXES) and bool(KEY_FILE_PATTERN.search(f"{label} {path}"))


# ----------------------------------------------------------------------------- SCB geodata (WFS)


def wfs(params: dict[str, Any], version: str = "1.1.0") -> str:
    return build_url(WFS, {"service": "WFS", "version": version, **params})


def capabilities_summary(xml: bytes) -> dict[str, Any]:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        return {"parse_error": str(exc)}
    layers = []
    for feature_type in root.iter(f"{WFS_NS}FeatureType"):
        layers.append(
            {
                "namn": (feature_type.findtext(f"{WFS_NS}Name") or "").split(":", 1)[-1],
                "titel": feature_type.findtext(f"{WFS_NS}Title"),
                "srs": feature_type.findtext(f"{WFS_NS}DefaultSRS"),
            }
        )
    formats = {
        (child.text or "").strip()
        for element in root.iter()
        if element.tag.endswith("ResultFormat") or element.tag.endswith("OutputFormats")
        for child in element
        if (child.text or "").strip()
    }
    operators = {
        element.get("name") or (element.text or "").strip()
        for element in root.iter()
        if element.tag.endswith("SpatialOperator")
    }
    return {
        "antal_lager": len(layers),
        "lager": [layer["namn"] for layer in layers],
        "deso_regso": [layer for layer in layers if re.search(r"deso|regso", layer["namn"], re.IGNORECASE)],
        "output_formats": sorted(formats)[:40],
        "spatial_operators": sorted(operators),
    }


def check_scb_geodata() -> None:
    res = fetch(wfs({"request": "GetCapabilities"}), accept="application/xml, text/xml")
    record("wfs.capabilities", res, capabilities_summary(res.get("body") or b""))

    attributes: dict[str, list[str]] = {}
    geometry: dict[str, str] = {}
    for layer in DESO_LAYERS:
        params = {"request": "DescribeFeatureType", "typeName": f"stat:{layer}", "outputFormat": "application/json"}
        described = as_dict(record(f"wfs.describe.{layer}", fetch(wfs(params)), body=True))
        props = as_list(as_dict((as_list(described.get("featureTypes")) or [{}])[0]).get("properties"))
        props = [as_dict(p) for p in props]
        attributes[layer] = [p["name"] for p in props if p.get("name") and not str(p.get("type")).startswith("gml:")]
        geometry[layer] = next((p["name"] for p in props if str(p.get("type")).startswith("gml:")), "")

    for layer in DESO_LAYERS:
        base: dict[str, Any] = {
            "request": "GetFeature",
            "typeName": f"stat:{layer}",
            "outputFormat": "application/json",
        }
        if attributes[layer]:
            base["propertyName"] = ",".join(attributes[layer])
        record(f"wfs.features.{layer}", fetch(wfs({**base, "maxFeatures": 3})), body=True)
        hits = fetch(wfs({"request": "GetFeature", "typeName": f"stat:{layer}", "resultType": "hits"}))
        found = re.search(rb'numberOfFeatures="(\d+)"', hits.get("body") or b"")
        record(f"wfs.hits.{layer}", hits, {"numberOfFeatures": int(found.group(1)) if found else None})
        municipality = next((a for a in attributes[layer] if a.lower().startswith("kommun") and "namn" not in a), None)
        if municipality:
            cql = f"{municipality}='{SAMPLE_MUNICIPALITY}'"
            got = fetch(wfs({**base, "CQL_FILTER": cql}))
            features = as_list(as_dict(parse_json(got.get("body") or b"")).get("features"))
            summary = {
                "filter": cql,
                "antal": len(features),
                "forsta": [as_dict(f).get("properties") for f in features[:3]],
            }
            record(f"wfs.kommun.{layer}", got, summary)

    hits20 = fetch(wfs({"request": "GetFeature", "typeNames": f"stat:{SAMPLE_LAYER}", "resultType": "hits"}, "2.0.0"))
    found = re.search(rb'numberMatched="(\d+)"', hits20.get("body") or b"")
    record("wfs.hits.version_2_0_0", hits20, {"numberMatched": int(found.group(1)) if found else None})

    layer = SAMPLE_LAYER
    code = next((a for a in attributes[layer] if a.lower().startswith("deso")), None)
    if code:
        base = {"request": "GetFeature", "typeName": f"stat:{layer}", "outputFormat": "application/json"}
        base |= {"propertyName": code, "sortBy": code}
        pages = {}
        for label, start, size in (("start0_max4", 0, 4), ("start2_max2", 2, 2)):
            got = as_dict(record(f"wfs.sida.{label}", fetch(wfs({**base, "maxFeatures": size, "startIndex": start}))))
            pages[label] = [as_dict(as_dict(f).get("properties")).get(code) for f in as_list(got.get("features"))]
        emit_json("VERIFY", {"check": "wfs.sida.jamforelse", **pages})

    for srs in SRS_VARIANTS:
        params = {"request": "GetFeature", "typeName": f"stat:{layer}", "outputFormat": "application/json"}
        got = fetch(wfs({**params, "maxFeatures": 1, "srsName": srs}))
        parsed = as_dict(parse_json(got.get("body") or b""))
        coordinates = first_coordinates(parsed)
        features = as_list(parsed.get("features"))
        summary = {
            "crs": parsed.get("crs"),
            "bbox": parsed.get("bbox"),
            "forsta_koordinat": coordinates[:1],
            "antal_koordinater": len(coordinates),
            "egenskaper": as_dict(features[0]).get("properties") if features else None,
        }
        record(f"wfs.crs.{srs}", got, summary)

    if geometry[layer]:
        x, y = SAMPLE_POINT
        for label, point in (("x_y", f"{x} {y}"), ("y_x", f"{y} {x}")):
            cql = f"INTERSECTS({geometry[layer]},POINT({point}))"
            params = {"request": "GetFeature", "typeName": f"stat:{layer}", "outputFormat": "application/json"}
            params |= {"maxFeatures": 3, "CQL_FILTER": cql}
            if attributes[layer]:
                params["propertyName"] = ",".join(attributes[layer])
            got = fetch(wfs(params))
            features = as_list(as_dict(parse_json(got.get("body") or b"")).get("features"))
            record(
                f"wfs.intersects.{label}",
                got,
                {"filter": cql, "egenskaper": [as_dict(f).get("properties") for f in features]},
            )

    for label, with_properties in (("alla", False), ("urval", True)):
        params = {"request": "GetFeature", "typeName": f"stat:{layer}", "outputFormat": "csv", "maxFeatures": 2}
        if with_properties and attributes[layer]:
            params["propertyName"] = ",".join(attributes[layer])
        got = fetch(wfs(params))
        lines = (got.get("body") or b"").decode("utf-8-sig", "replace").splitlines()
        record(
            f"wfs.csv.{label}", got, {"rubrik": lines[0][:400] if lines else "", "rader": [r[:300] for r in lines[1:3]]}
        )
    for fmt in ("shape-zip", "geopackage"):
        got = fetch(wfs({"request": "GetFeature", "typeName": f"stat:{layer}", "outputFormat": fmt, "maxFeatures": 1}))
        record(f"wfs.format.{fmt}", got, {"forsta_bytes": (got.get("body") or b"")[:8].hex()})


def first_coordinates(geojson: dict[str, Any]) -> list[list[float]]:
    """All coordinate pairs of the first feature's geometry, flattened."""
    features = as_list(geojson.get("features"))
    if not features:
        return []
    flat: list[list[float]] = []

    def walk(node: Any) -> None:
        if isinstance(node, list) and node and isinstance(node[0], (int, float)):
            flat.append(node)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(as_dict(as_dict(features[0]).get("geometry")).get("coordinates"))
    return flat


# ----------------------------------------------------------------------------- SCB Statistikdatabasen (PxWebApi 2)


def check_scb_pxweb() -> None:
    record("pxweb.config", fetch(f"{PXWEB}/config"), body=True)
    found: list[dict[str, Any]] = []
    for query in PXWEB_QUERIES:
        url = build_url(f"{PXWEB}/tables", {"lang": "sv", "query": query, "pageSize": 100})
        got = as_dict(record(f"pxweb.sok.{query}", fetch(url)))
        tables = [as_dict(t) for t in as_list(got.get("tables"))]
        keys = ("id", "label", "firstPeriod", "lastPeriod", "updated", "discontinued", "variableNames")
        emit_json(
            "VERIFY",
            {
                "check": f"pxweb.sok.{query}.tabeller",
                "antal": len(tables),
                "page": got.get("page"),
                "tabeller": [{k: t.get(k) for k in keys} for t in tables],
            },
        )
        if query == "DeSO":
            found = tables

    chosen = [t for t in found if not t.get("discontinued")][:6] + [t for t in found if t.get("discontinued")][:2]
    codelists: set[str] = set()
    for position, table in enumerate(chosen):
        table_id = str(table.get("id", ""))
        if not SAFE_ID.match(table_id):
            continue
        meta = fetch(build_url(f"{PXWEB}/tables/{path_segment(table_id)}/metadata", {"lang": "sv"}))
        parsed = as_dict(parse_json(meta.get("body") or b""))
        dimensions = as_dict(parsed.get("dimension"))
        summary, region_codes = metadata_summary(parsed)
        codelists |= {
            str(cl["id"])
            for dim in dimensions.values()
            for cl in dimension_codelists(as_dict(dim))
            if SAFE_ID.match(str(cl.get("id", "")))
        }
        record(f"pxweb.metadata.{table_id}", meta, summary)
        if position == 0 and region_codes:
            tiny_data(table_id, dimensions, region_codes)

    for codelist in sorted(codelists)[:12]:
        got = fetch(build_url(f"{PXWEB}/codelists/{path_segment(codelist)}", {"lang": "sv"}))
        parsed = as_dict(parse_json(got.get("body") or b""))
        values = [as_dict(v) for v in as_list(parsed.get("values"))]
        summary = {
            "label": parsed.get("label"),
            "type": parsed.get("type"),
            "antal": len(values),
            "exempel": [{k: v.get(k) for k in ("code", "label", "valueMap")} for v in values[:5]],
        }
        record(f"pxweb.codelist.{codelist}", got, summary)


def metadata_summary(metadata: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Summary of a JSON-stat 2 metadata answer and the codes of its Region dimension (if any)."""
    summary: dict[str, Any] = {
        "label": metadata.get("label"),
        "updated": metadata.get("updated"),
        "noter": [str(n)[:400] for n in as_list(metadata.get("note"))][:10],
        "dimensioner": {},
    }
    region_codes: list[str] = []
    for dim_id, raw in as_dict(metadata.get("dimension")).items():
        dim = as_dict(raw)
        codes = ordered_codes(dim)
        labels = as_dict(as_dict(dim.get("category")).get("label"))
        extension = as_dict(dim.get("extension"))
        entry: dict[str, Any] = {
            "label": dim.get("label"),
            "antal": len(codes),
            "elimination": extension.get("elimination"),
            "codelists": [{k: cl.get(k) for k in ("id", "label", "type")} for cl in dimension_codelists(dim)],
            "codelists_nyckel": next((k for k in ("codelists", "codeLists") if k in extension), None),
        }
        if dim_id.lower() == "region":
            entry["koder"] = summarise_codes(codes, labels)
            region_codes = codes
        else:
            entry["exempel"] = [[c, labels.get(c, "")] for c in codes[:8]] + ([["…", ""]] if len(codes) > 8 else [])
            if dim_id.lower() in ("tid", "time"):
                entry["sista"] = codes[-4:]
        summary["dimensioner"][dim_id] = entry
    return summary, region_codes


def tiny_data(table_id: str, dimensions: dict[str, Any], region_codes: list[str]) -> None:
    """A few cells: one suffixed DeSO code, one plain DeSO code and one municipality, latest two periods."""
    first_of_class: dict[str, str] = {}
    for code in region_codes:
        first_of_class.setdefault(classify_region_code(code), code)
    picked = [first_of_class[cls] for cls in ("deso_suffix", "deso", "kommun") if cls in first_of_class]
    params: dict[str, Any] = {"lang": "sv", "outputFormat": "json-stat2"}
    for dim_id, dim in dimensions.items():
        codes = ordered_codes(as_dict(dim))
        if dim_id.lower() == "region":
            params[f"valueCodes[{dim_id}]"] = ",".join(picked)
        elif dim_id.lower() in ("tid", "time"):
            params[f"valueCodes[{dim_id}]"] = ",".join(codes[-2:])
        elif codes:
            params[f"valueCodes[{dim_id}]"] = codes[0]
    record(
        f"pxweb.data.{table_id}", fetch(build_url(f"{PXWEB}/tables/{path_segment(table_id)}/data", params)), body=True
    )
    region_key = next((k for k in params if k.lower() == "valuecodes[region]"), "valueCodes[Region]")
    wildcard = {**params, region_key: f"{SAMPLE_MUNICIPALITY}C*"}
    got = fetch(build_url(f"{PXWEB}/tables/{path_segment(table_id)}/data", wildcard))
    parsed = as_dict(parse_json(got.get("body") or b""))
    region = as_dict(as_dict(parsed.get("dimension")).get(region_key[len("valueCodes[") : -1]))
    record(
        f"pxweb.data.{table_id}.wildcard", got, {"filter": wildcard[region_key], "regioner": len(ordered_codes(region))}
    )


# ----------------------------------------------------------------------------- SCB pages and key files


def check_scb_keys() -> None:
    files: dict[str, str] = {}
    for page_url in SCB_PAGES:
        got = fetch(page_url, accept="text/html")
        page = (got.get("body") or b"").decode("utf-8", "replace")
        text, links = html_to_text_and_links(page, got.get("final_url") or page_url)
        file_links = [
            (url, label)
            for url, label in links
            if allowed(url) and urllib.parse.urlsplit(url).path.lower().endswith(FILE_SUFFIXES)
        ]
        name = page_url.rstrip("/").rsplit("/", 1)[-1]
        record(f"scb.sida.{name}", got, {"tecken": len(text), "fillankar": [list(link) for link in file_links][:60]})
        emit_chunks("TEXT", f"scb.sida.{name}", text[:TEXT_MAX_CHARS])
        for url, label in file_links:
            if is_key_file(url, label):
                files.setdefault(url, label)

    for i, (url, label) in enumerate(list(files.items())[:MAX_KEY_FILES], 1):
        got = fetch(url)
        data = got.get("body") or b""
        name = f"nyckelfil{i}"
        filename = urllib.parse.unquote(urllib.parse.urlsplit(url).path).rsplit("/", 1)[-1]
        too_big = len(data) > MAX_B64_BYTES
        record(f"scb.{name}", got, {"lanktext": label, "filnamn": filename, "b64": not too_big})
        if got.get("status") == 200 and data and not too_big:
            emit_chunks("B64", name, base64.b64encode(data).decode("ascii"))
    if len(files) > MAX_KEY_FILES:
        emit_json("VERIFY", {"check": "scb.nyckelfiler.ej_hamtade", "lankar": list(files)[MAX_KEY_FILES:]})


# ----------------------------------------------------------------------------- Socialstyrelsen (statistikdatabasen)


def check_socialstyrelsen() -> None:
    got = fetch(f"{SDB}/sdbapi.aspx", accept="text/html")
    page = (got.get("body") or b"").decode("utf-8", "replace")
    text, links = html_to_text_and_links(page, got.get("final_url") or f"{SDB}/sdbapi.aspx")
    record(
        "sdb.dokumentation",
        got,
        {"tecken": len(text), "lankar": [list(link) for link in links if allowed(link[0])][:80]},
    )
    emit_chunks("TEXT", "sdb.dokumentation", text[:TEXT_MAX_CHARS])

    subjects: list[Any] = []
    for path in ("/api", "/api/v1", "/api/v1/sv", "/api/v1/en"):
        parsed = record(f"sdb{path.replace('/', '.')}", fetch(f"{SDB}{path}"), body=True)
        if path == "/api/v1/sv":
            subjects = as_list(parsed)
    record("sdb.api.v1.sv.xml", fetch(f"{SDB}/api/v1/sv", accept="application/xml"), {"accept": "application/xml"})

    available = {str(as_dict(s).get("namn")) for s in subjects}
    for subject in SDB_SUBJECTS:
        if subject not in available:
            emit_json("VERIFY", {"check": f"sdb.amne.{subject}", "finns": False})
            continue
        variables = as_list(record(f"sdb.amne.{subject}", fetch(f"{SDB}/api/v1/sv/{subject}"), body=True))
        values: dict[str, list[dict[str, Any]]] = {}
        for variable in variables:
            name = str(as_dict(variable).get("namn", ""))
            if not SAFE_SEGMENT.match(name):
                emit_json("VERIFY", {"check": f"sdb.varden.{subject}", "hoppar_over_variabel": name})
                continue
            res = fetch(f"{SDB}/api/v1/sv/{subject}/{name}")
            items = [as_dict(v) for v in as_list(parse_json(res.get("body") or b""))]
            values[name] = items
            full = name in ("region", "kon", "matt", "ar") or len(items) <= 40
            record(f"sdb.varden.{subject}.{name}", res, {"antal": len(items), "exempel": items[:15]}, body=full)
        if subject in SDB_RESULT_SUBJECTS and values:
            sdb_result(subject, values)

    record("sdb.fel.okant_amne", fetch(f"{SDB}/api/v1/sv/finnsinte"), {"note": "okänt ämne – felsvarets form"})


def sdb_selection(values: dict[str, list[dict[str, Any]]], only: tuple[str, ...] = ()) -> list[str]:
    """Path segments variable/value: the latest year, the whole country, otherwise the first value."""
    segments: list[str] = []
    for name, items in values.items():
        if only and name not in only:
            continue
        ids = [str(v.get("id")) for v in items if SAFE_ID.match(str(v.get("id", "")))]
        if not ids:
            continue
        chosen = ids[-1] if name == "ar" else ("0" if name == "region" and "0" in ids else ids[0])
        segments += [name, path_segment(chosen)]
    return segments


def sdb_result(subject: str, values: dict[str, list[dict[str, Any]]]) -> None:
    url = f"{SDB}/api/v1/sv/{subject}/resultat/{'/'.join(sdb_selection(values))}?per_sida=5"
    record(f"sdb.resultat.{subject}", fetch(url), body=True)
    url = f"{SDB}/api/v1/sv/{subject}/resultat/{'/'.join(sdb_selection(values, ('ar', 'matt')))}?per_sida=3"
    got = fetch(url)
    parsed = as_dict(parse_json(got.get("body") or b""))
    summary = {k: parsed.get(k) for k in ("sida", "per_sida", "sidor", "nasta_sida", "foregaende_sida")}
    record(f"sdb.resultat.{subject}.sidor", got, {**summary, "rader": as_list(parsed.get("data"))[:3]})


# ----------------------------------------------------------------------------- main

GROUPS = {
    "scb-geodata": check_scb_geodata,
    "scb-pxweb": check_scb_pxweb,
    "scb-nycklar": check_scb_keys,
    "socialstyrelsen": check_socialstyrelsen,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0], allow_abbrev=False)
    parser.add_argument("--group", required=True, choices=sorted(GROUPS))
    args = parser.parse_args(argv)
    started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    emit_json("VERIFY", {"check": "start", "group": args.group, "time": started})
    GROUPS[args.group]()
    emit_json("VERIFY", {"check": "slut", "group": args.group, "svar": _responses})
    write_job_summary(args.group, os.environ.get("GITHUB_STEP_SUMMARY"))
    return 0 if _responses else 1


def write_job_summary(group: str, path: str | None) -> None:
    """A table of checks and HTTP status for the Actions run page; the details stay in the job log."""
    if not path:
        return
    rows = "".join(f"| `{check}` | {outcome.replace('|', '/')[:120]} |\n" for check, outcome in _outcomes)
    with open(path, "a", encoding="utf-8") as summary:
        summary.write(f"### Källkontroll: {group}\n\n{len(_outcomes)} anrop, {_responses} svar.\n\n")
        summary.write(f"| Kontroll | Status |\n| --- | --- |\n{rows}\n")


if __name__ == "__main__":
    sys.exit(main())
