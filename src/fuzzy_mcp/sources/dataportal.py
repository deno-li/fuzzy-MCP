# SPDX-FileCopyrightText: 2026 Deniz Özer
#
# SPDX-License-Identifier: MIT

"""Sveriges dataportal (dataportal.se) via EntryStore/EntryScape på admin.dataportal.se.

Base: https://admin.dataportal.se/store (no version segment; EntryStore does not follow the
DIGG REST API profile). Endpoints used:

* ``GET /search?type=solr&query=<solr>&limit=&offset=&sort=`` – Solr search. Response
  ``{"results": <total>, "limit", "offset", "resource": {"children": [...]}}``; every child has
  ``entryId``/``contextId`` (strings), ``info`` (entry graph holding the resource URI) and
  ``metadata`` (RDF/JSON of the entry's own graph only – distributions, publisher and contact
  point are separate entries).
* ``GET /{ctx}/metadata/{id}?recursive=dcat&format=application/json`` – one entry as RDF/JSON,
  with the related entities (distributions, agent, contact, ...) merged into the graph.
* ``GET /{ctx}/entry/{id}?format=application/json`` – entry information
  ``{"entryId", "info": {<entry URI>: {es:resource: [<resource URI>]}}}`` (spec §1.7); used to know
  which subject of the recursive graph the entry describes.

Solr fields for an RDF predicate are ``metadata.predicate.<kind>.<md5(predicate URI)[:8]>``.
Harvests run nightly (~04:00), so metadata can be up to a day old. Every dataportal host was
egress-blocked during research: shapes come from the EntryStore source and community captures.

Privacy: names and e-mail addresses of contact persons (``vcard:Individual``) and names of
publishers of type ``PrivateIndividual(s)`` are personuppgifter. They are left out of the output and
redacted from ``raw`` unless the caller opts in with ``include_personal_data``.
"""

import contextlib
import hashlib
import json
import re
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal
from urllib.parse import urlparse

from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult
from pydantic import BaseModel, Field

from ..core import READ_ONLY_LOCAL, READ_ONLY_OPEN, Services, clamp, personal_data, structured, tool_errors
from ..errors import InvalidInputError, UpstreamError

SOURCE = "dataportal"
PORTAL_URL = "https://www.dataportal.se"
ATTRIBUTION = "Källa: Sveriges dataportal (Digg)"
NAME_CACHE_TTL_SECONDS = 24 * 3600
SOLR_MAX_LIMIT = 100  # SOLR_MAX_LIMIT on admin.dataportal.se; larger values are silently reduced
MAX_SEARCH_LIMIT = 50  # ~1k characters per hit: keeps a search page well under the ~25k-token host cap
MAX_DISTRIBUTIONS = 100
MANY_DISTRIBUTIONS = 25  # above this, distribution descriptions are shortened further
LOOKUP_BATCH = 20  # entrystore-js loadEntriesByResourceURIs chunk size
MAX_QUERY_CHARS = 1500  # dataportal-web maxRequestUriLength (resource lookups)
MAX_SEARCH_QUERY_CHARS = 3000  # dataset search query (before URL encoding); keeps the URL well below 8 KB
MAX_TEXT_TOKENS = 8  # free-text words; each is repeated in four text fields
NGRAM_MAX = 15  # max ngram length of the text fields; longer tokens are truncated (entrystore-js)
SUMMARY_DESCRIPTION_CHARS = 300
SHORT_DESCRIPTION_CHARS = 120
DETAIL_DESCRIPTION_CHARS = 3000
OUTPUT_BUDGET_CHARS = 60_000  # compact JSON characters for one get_dataset result incl. raw (~20k tokens)
PORTAL_HOSTS = frozenset({"www.dataportal.se", "dataportal.se"})

# Namespaces
DCT = "http://purl.org/dc/terms/"
DCAT = "http://www.w3.org/ns/dcat#"
FOAF = "http://xmlns.com/foaf/0.1/"
VCARD = "http://www.w3.org/2006/vcard/ns#"
DCATAP = "http://data.europa.eu/r5r/"
RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
RDFS = "http://www.w3.org/2000/01/rdf-schema#"
ESTERMS = "http://entryscape.com/terms/"
ENTRYSTORE_RESOURCE = "http://entrystore.org/terms/resource"
RDF_TYPE = RDF + "type"

DCAT_DATASET = DCAT + "Dataset"
DCAT_DATASET_SERIES = DCAT + "DatasetSeries"
DCAT_DATA_SERVICE = DCAT + "DataService"
DCAT_DISTRIBUTION = DCAT + "Distribution"
INDEPENDENT_DATA_SERVICE = ESTERMS + "IndependentDataService"
FOAF_AGENT = FOAF + "Agent"
FOAF_ORGANIZATION = FOAF + "Organization"
VCARD_INDIVIDUAL = VCARD + "Individual"
# The entity types dataportal_get_dataset is meant for (the portal's dataset/API/series pages).
MAIN_TYPES: tuple[str, ...] = (DCAT_DATASET, DCAT_DATASET_SERIES, DCAT_DATA_SERVICE, INDEPENDENT_DATA_SERVICE)

EU_AUTHORITY = "http://publications.europa.eu/resource/authority/"
THEME_PREFIX = EU_AUTHORITY + "data-theme/"
ACCESS_RIGHT_PREFIX = EU_AUTHORITY + "access-right/"
FREQUENCY_PREFIX = EU_AUTHORITY + "frequency/"
LANGUAGE_PREFIX = EU_AUTHORITY + "language/"
HVD_PREFIX = "http://data.europa.eu/bna/"
PUBLISHER_TYPE_PREFIX = "http://purl.org/adms/publishertype/"

# DCAT-AP-SE publisher URI patterns: the harvesting rule (dataportal.se/organisation) and the
# legacy KB pattern still used by some publishers (e.g. SCB in 2023). Both are queried.
PUBLISHER_URI_PATTERNS = (
    "http://dataportal.se/organisation/SE{orgnr}",
    "http://id.kb.se/organisations/SE{orgnr}",
)

# Code lists from the DCAT-AP-SE bundle (code -> Swedish label, English label).
THEMES: dict[str, tuple[str, str]] = {
    "AGRI": ("Jordbruk, fiske, skogsbruk och livsmedel", "Agriculture, fisheries, forestry and food"),
    "ECON": ("Ekonomi och finans", "Economy and finance"),
    "EDUC": ("Utbildning, kultur och sport", "Education, culture and sport"),
    "ENER": ("Energi", "Energy"),
    "ENVI": ("Miljö", "Environment"),
    "GOVE": ("Regeringen och den offentliga sektorn", "Government and public sector"),
    "HEAL": ("Hälsa", "Health"),
    "INTR": ("Internationella frågor", "International issues"),
    "JUST": ("Rättvisa, rättsliga system och allmän säkerhet", "Justice, legal system and public safety"),
    "SOCI": ("Befolkning och samhälle", "Population and society"),
    "REGI": ("Regioner och städer", "Regions and cities"),
    "TECH": ("Vetenskap och teknik", "Science and technology"),
    "TRAN": ("Transport", "Transport"),
}
THEME_LABELS: dict[str, str] = {code: labels[0] for code, labels in THEMES.items()}
ACCESS_RIGHTS: dict[str, str] = {"PUBLIC": "Publik", "RESTRICTED": "Begränsad", "NON_PUBLIC": "Ej offentlig"}
FREQUENCIES: dict[str, str] = {
    "CONT": "kontinuerlig",
    "UPDATE_CONT": "uppdateras kontinuerligt",
    "DAILY_2": "två gånger per dag",
    "DAILY": "dagligen",
    "WEEKLY_3": "tre gånger per vecka",
    "WEEKLY_2": "två gånger per vecka",
    "WEEKLY": "veckovis",
    "MONTHLY_3": "tre gånger per månad",
    "BIWEEKLY": "varannan vecka",
    "MONTHLY_2": "två gånger per månad",
    "MONTHLY": "månatligen",
    "BIMONTHLY": "varannan månad",
    "QUARTERLY": "kvartalsvis",
    "ANNUAL_3": "tre gånger per år",
    "ANNUAL_2": "halvårsvis",
    "ANNUAL": "årligen",
    "BIENNIAL": "vartannat år",
    "TRIENNIAL": "vart tredje år",
    "IRREG": "oregelbundet",
    "OTHER": "annan",
    "UNKNOWN": "okänd",
    "NEVER": "aldrig",
}
LICENSES: dict[str, str] = {
    "CC0 1.0": "http://creativecommons.org/publicdomain/zero/1.0/",
    "CC BY 4.0": "http://creativecommons.org/licenses/by/4.0/",
    "CC BY-SA 4.0": "http://creativecommons.org/licenses/by-sa/4.0/",
    "CC BY-ND 4.0": "http://creativecommons.org/licenses/by-nd/4.0/",
    "CC BY-NC 4.0": "http://creativecommons.org/licenses/by-nc/4.0/",
    "CC BY-NC-SA 4.0": "http://creativecommons.org/licenses/by-nc-sa/4.0/",
    "CC BY-NC-ND 4.0": "http://creativecommons.org/licenses/by-nc-nd/4.0/",
}
HVD_CATEGORIES: dict[str, str] = {
    "c_ac64a52d": "Geospatiala data",
    "c_dd313021": "Jordobservation och miljö",
    "c_164e0bf5": "Meteorologiska data",
    "c_e1da4e07": "Statistik",
    "c_a9135398": "Företag och företagsägande",
    "c_b79e35eb": "Rörlighet",
}
PUBLISHER_TYPES: tuple[str, ...] = (
    "Academia-ScientificOrganisation",
    "Company",
    "IndustryConsortium",
    "LocalAuthority",  # kommun
    "NationalAuthority",  # nationell myndighet
    "NonGovernmentalOrganisation",
    "NonProfitOrganisation",
    "PrivateIndividual(s)",  # spelled like this in the DCAT-AP-SE summary; excluded from the portal's org list
    "RegionalAuthority",
    "StandardisationBody",
    "SupraNationalAuthority",
)
COMMON_FORMATS: tuple[str, ...] = (
    "text/csv",
    "text/csv+zip",
    "application/xml",
    "application/xml+zip",
    "application/json",
    "application/json+zip",
    "application/zip",
    "text/html",
    "application/vnd.ms-excel",
    "application/pdf",
    "text/plain",
    "application/n-triples",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.oasis.opendocument.spreadsheet",
    "application/rdf+xml",
)
FORMAT_ALIASES: dict[str, tuple[str, ...]] = {
    "csv": ("text/csv",),
    "json": ("application/json",),
    "xml": ("application/xml",),
    "zip": ("application/zip",),
    "html": ("text/html",),
    "pdf": ("application/pdf",),
    "txt": ("text/plain",),
    "xls": ("application/vnd.ms-excel",),
    "xlsx": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",),
    "excel": (
        "application/vnd.ms-excel",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ),
    "ods": ("application/vnd.oasis.opendocument.spreadsheet",),
    "rdf": ("application/rdf+xml",),
    "n-triples": ("application/n-triples",),
}
# Organisation numbers (10 digits, no hyphen) for the shortcuts. The publisher URIs are built
# from them with PUBLISHER_URI_PATTERNS and, if that finds nothing, resolved at runtime.
PUBLISHER_SHORTCUTS: dict[str, str] = {
    "scb": "2021000837",
    "skolverket": "2021004185",
    "fohm": "2021006545",
    "folkhalsomyndigheten": "2021006545",
    "folkhälsomyndigheten": "2021006545",
}

Graph = dict[str, dict[str, list[dict[str, Any]]]]


# ---------------------------------------------------------------------------
# Solr query building
# ---------------------------------------------------------------------------


def predicate_hash(predicate_uri: str) -> str:
    """EntryStore's Solr field suffix: the first 8 hex digits of md5(predicate URI)."""
    return hashlib.md5(predicate_uri.encode("utf-8"), usedforsecurity=False).hexdigest()[:8]


def solr_field(kind: Literal["uri", "literal_s", "literal", "date"], predicate_uri: str, related: bool = False) -> str:
    name = f"metadata.predicate.{kind}.{predicate_hash(predicate_uri)}"
    return f"related.{name}" if related else name


PUBLISHER_FIELD = solr_field("uri", DCT + "publisher")
THEME_FIELD = solr_field("uri", DCAT + "theme")
FORMAT_FIELDS = (solr_field("literal_s", DCT + "format"), solr_field("literal_s", DCT + "format", related=True))
TEXT_FIELDS = ("title", "description", "tag.literal", "all")
# "Senast ändrad": the portal sorts on dcterms:modified (metadata.predicate.literal_s.3e2f60da desc, §1.3);
# the entry timestamp ``modified`` breaks ties. EntryStore splits on "," and then on " " (no space after ",").
MODIFIED_SORT = f"{solr_field('literal_s', DCT + 'modified')} desc,modified desc"
RELEVANCE_SORT = "score desc"

# Every Lucene/Solr query-syntax character plus whitespace (the set SolrJ ClientUtils.escapeQueryChars
# escapes). One regex pass, so a backslash in the value is escaped exactly once.
_SOLR_SPECIAL = re.compile(r'([\\+\-!():^\[\]"{}~*?|&;/\s])')


def esc(value: str) -> str:
    """Escape a value for a Solr string field (``resource``, ``rdfType``, ``metadata.predicate.uri.*``):
    backslash, every query-syntax character and whitespace get a backslash. This is a superset of
    entrystore-js ``encodeStr`` (``:``, ``(``, ``)``), so values can never add clauses or operators.
    httpx then URL-encodes the result; never put a literal ``+``."""
    return _SOLR_SPECIAL.sub(r"\\\1", value)


def quote(value: str) -> str:
    """Quote a value for an exact-match (string) field."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def uri_clause(solr_field_name: str, uris: list[str]) -> str:
    if len(uris) == 1:
        return f"{solr_field_name}:{esc(uris[0])}"
    return f"{solr_field_name}:(" + " OR ".join(esc(u) for u in uris) + ")"


_LUCENE_SPECIAL = re.compile(r'([+\-!(){}\[\]^"~*?:\\/&|])')
_EDGE_PUNCTUATION = "\"'(),.;:!?[]{}«»"


def text_tokens(text: str) -> list[str]:
    """Tokens for the ngram text fields, following entrystore-js ``solrFriendly``: split on
    whitespace, truncate to 15 characters, append ``*`` to tokens shorter than 3 characters.
    Lucene special characters are escaped so free text can never break the query."""
    tokens: list[str] = []
    for raw in text.split():
        wildcard = raw.endswith("*")
        word = raw.rstrip("*").strip(_EDGE_PUNCTUATION)
        if not word:
            continue
        if word in ("AND", "OR", "NOT"):
            word = word.lower()  # a plain word, not an operator
        word = word[:NGRAM_MAX]
        token = _LUCENE_SPECIAL.sub(r"\\\1", word)
        if wildcard or len(word) < 3:
            token += "*"
        tokens.append(token)
    return tokens


def text_clause(tokens: list[str]) -> str:
    """Portal-style text block ``(title:q OR description:q OR tag.literal:q OR all:q)``; several
    tokens are ANDed (OR between words explodes the hit count)."""
    term = tokens[0] if len(tokens) == 1 else "(" + " AND ".join(tokens) + ")"
    return "(" + " OR ".join(f"{name}:{term}" for name in TEXT_FIELDS) + ")"


def build_dataset_query(
    text: str | None = None,
    publisher_uris: list[str] | None = None,
    theme: str | None = None,
    media_types: tuple[str, ...] | list[str] | None = None,
) -> str:
    clauses = [f"rdfType:{esc(DCAT_DATASET)}", "public:true"]
    tokens = text_tokens(text or "")[:MAX_TEXT_TOKENS]
    if tokens:
        clauses.append(text_clause(tokens))
    if publisher_uris:
        clauses.append(uri_clause(PUBLISHER_FIELD, publisher_uris))
    if theme:
        clauses.append(f"{THEME_FIELD}:{esc(THEME_PREFIX + theme)}")
    if media_types:
        # Harvested datasets carry dcterms:format copied from their distributions; the related
        # field covers servers that index it from the distribution entries instead.
        parts = [f"{name}:{quote(m)}" for m in media_types for name in FORMAT_FIELDS]
        clauses.append("(" + " OR ".join(parts) + ")")
    return " AND ".join(clauses)


def type_clause(types: tuple[str, ...] | list[str]) -> str:
    return f"rdfType:{esc(types[0])}" if len(types) == 1 else "rdfType:(" + " OR ".join(esc(t) for t in types) + ")"


def resource_query(uris: list[str], types: tuple[str, ...] | list[str] | None = None) -> str:
    """Recipe 'look up any entity by its URI': ``public:true AND (resource:(a OR b))``, optionally
    restricted to some rdf:types."""
    group = esc(uris[0]) if len(uris) == 1 else "(" + " OR ".join(esc(u) for u in uris) + ")"
    query = f"public:true AND (resource:{group})"
    return f"{query} AND {type_clause(types)}" if types else query


def representable(uri: str, types: tuple[str, ...] | list[str] | None = None) -> bool:
    """Whether an (upstream) URI can be looked up: no control characters and short enough for a
    lookup query of its own. Everything else is escaped, so one odd URI cannot break a batch."""
    if not uri or any(unicodedata.category(ch) == "Cc" for ch in uri):
        return False
    return len(resource_query([uri], types)) <= MAX_QUERY_CHARS


def resource_batches(uris: list[str], types: tuple[str, ...] | list[str] | None = None) -> list[list[str]]:
    batches: list[list[str]] = []
    current: list[str] = []
    for uri in uris:
        candidate = [*current, uri]
        if current and (len(candidate) > LOOKUP_BATCH or len(resource_query(candidate, types)) > MAX_QUERY_CHARS):
            batches.append(current)
            candidate = [uri]
        current = candidate
    if current:
        batches.append(current)
    return batches


# ---------------------------------------------------------------------------
# Input normalisation
# ---------------------------------------------------------------------------

# ASCII digits only ([0-9], not \d): Unicode digits such as '４３' must never reach a URL path.
_ORGNR = re.compile(r"(?:SE)?([0-9]{6})-?([0-9]{4})", re.IGNORECASE)
_DIGITS = re.compile(r"[0-9]+")
_PORTAL_PATH = re.compile(r"/(?:(?:sv|en)/)?(?:datasets|dataservice|dataset-series)/([0-9]+)_([0-9]+)(?:/.*)?")
_STORE_PATH = re.compile(r"/([0-9]+)/(?:entry|metadata|resource)/([0-9]+)/?")  # after the base path
_SHORT_ID = re.compile(r"([0-9]+)_([0-9]+)")
# RFC 3986 characters (unreserved, reserved and '%'). Non-ASCII letters are accepted as IRI characters
# (RFC 3987; some harvested resource URIs are IRIs); whitespace, control characters and the ASCII
# characters RFC 3986 excludes (\ " < > { } | ^ `) are rejected.
_URI_ASCII = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~:/?#[]@!$&'()*+,;=%")


def _uri_char_ok(ch: str) -> bool:
    if ord(ch) < 128:
        return ch in _URI_ASCII
    return not ch.isspace() and unicodedata.category(ch)[0] not in ("C", "Z")


def check_uri(value: str, name: str) -> str:
    """Validate a URI given by the caller (InvalidInputError otherwise); it is Lucene-escaped later."""
    text = value.strip()
    bad = sorted({ch for ch in text if not _uri_char_ok(ch)})
    if bad:
        shown = ", ".join(repr(ch) for ch in bad[:8])
        raise InvalidInputError(
            f"{name} är ingen giltig URI: tecknen {shown} är inte tillåtna (RFC 3986). Procentkoda dem eller ange "
            "URI:n exakt som den står i dataportalen."
        )
    try:
        parsed = urlparse(text)
        host = parsed.hostname
    except ValueError as exc:
        raise InvalidInputError(f"{name} är ingen giltig URI: {exc}") from exc
    if parsed.scheme.lower() not in ("http", "https") or not host:
        raise InvalidInputError(f"{name} ska vara en http(s)-URI med värdnamn, fick {value!r}")
    if len(resource_query([text], MAIN_TYPES)) > MAX_QUERY_CHARS:
        raise InvalidInputError(f"URI:n i {name} är för lång ({len(text)} tecken) för en sökning i dataportalen")
    return text


def normalize_orgnr(value: str) -> str | None:
    match = _ORGNR.fullmatch(value.strip().replace(" ", ""))
    return match.group(1) + match.group(2) if match else None


def normalize_theme(value: str) -> str:
    text = value.strip()
    if text.lower().startswith(("http://", "https://")):
        text = text.rstrip("/").rsplit("/", 1)[-1]
    code = text.upper()
    if code in THEMES:
        return code
    folded = text.casefold()
    for key, (label_sv, label_en) in THEMES.items():
        if folded in (label_sv.casefold(), label_en.casefold()):
            return key
    raise InvalidInputError(
        f"Okänt tema {value!r}. Giltiga EU-temakoder: "
        + ", ".join(f"{k} ({v[0]})" for k, v in THEMES.items())
        + ". Se dataportal_theme_codes."
    )


def normalize_format(value: str) -> tuple[str, ...]:
    text = value.strip()
    if "/" in text:
        return (text,)
    alias = text.lower().lstrip(".")
    if alias in FORMAT_ALIASES:
        return FORMAT_ALIASES[alias]
    raise InvalidInputError(
        f"Okänt format {value!r}. Ange en MIME-typ (t.ex. 'text/csv') eller något av: " + ", ".join(FORMAT_ALIASES)
    )


def check_id(value: str | int, name: str) -> str:
    text = str(value).strip()
    if isinstance(value, bool) or not _DIGITS.fullmatch(text):
        raise InvalidInputError(f"{name} ska bestå av siffror (t.ex. '43'), fick {value!r}")
    return text


def child_ids(child: dict[str, Any]) -> tuple[str, str] | None:
    """contextId/entryId of a search hit, only if both are plain digits (they go into URL paths)."""
    try:
        return check_id(str(child.get("contextId") or ""), "contextId"), check_id(
            str(child.get("entryId") or ""), "entryId"
        )
    except InvalidInputError:
        return None


# ---------------------------------------------------------------------------
# RDF/JSON helpers
# ---------------------------------------------------------------------------


def as_graph(data: Any) -> Graph:
    """Keep only well-formed RDF/JSON (subject -> predicate -> [object dicts])."""
    graph: Graph = {}
    if not isinstance(data, dict):
        return graph
    for subject, predicates in data.items():
        if not isinstance(predicates, dict):
            continue
        node: dict[str, list[dict[str, Any]]] = {}
        for predicate, objs in predicates.items():
            if isinstance(objs, dict):  # tolerate a single object instead of a list
                objs = [objs]
            if isinstance(objs, list):
                node[str(predicate)] = [o for o in objs if isinstance(o, dict) and o.get("value") is not None]
        graph[str(subject)] = node
    return graph


def relabel_bnodes(graph: Graph, prefix: str) -> Graph:
    """Blank-node labels are only unique within one entry's graph; prefix them before merging."""

    def fix(value: str) -> str:
        return f"_:{prefix}{value[2:]}" if value.startswith("_:") else value

    return {
        fix(subject): {
            p: [{**o, "value": fix(str(o["value"]))} if o.get("type") == "bnode" else o for o in objs]
            for p, objs in node.items()
        }
        for subject, node in graph.items()
    }


def objs(node: dict[str, list[dict[str, Any]]] | None, predicate: str) -> list[dict[str, Any]]:
    return list((node or {}).get(predicate) or [])


def all_values(node: dict[str, list[dict[str, Any]]] | None, predicate: str) -> list[str]:
    return list(dict.fromkeys(str(o["value"]) for o in objs(node, predicate)))


def first_value(node: dict[str, list[dict[str, Any]]] | None, predicate: str) -> str | None:
    values = all_values(node, predicate)
    return values[0] if values else None


def _lang(obj: dict[str, Any]) -> str:
    return str(obj.get("lang") or "").lower().split("-")[0]


def pick_literal(candidates: list[dict[str, Any]], prefer: tuple[str, ...]) -> str | None:
    """Preferred language first, then the other, then untagged (rdf:langString without a tag
    occurs in some captures), then anything."""
    for want in (*prefer, ""):
        for obj in candidates:
            if _lang(obj) == want:
                return str(obj["value"])
    return str(candidates[0]["value"]) if candidates else None


def literal(node: dict[str, list[dict[str, Any]]] | None, predicate: str, prefer: tuple[str, ...]) -> str | None:
    return pick_literal([o for o in objs(node, predicate) if o.get("type") != "bnode"], prefer)


def literals_in_lang(
    node: dict[str, list[dict[str, Any]]] | None, predicate: str, prefer: tuple[str, ...]
) -> list[str]:
    """Keywords in the preferred language (plus untagged ones); falls back to all languages."""
    candidates = objs(node, predicate)
    for want in prefer:
        chosen = [o for o in candidates if _lang(o) in (want, "")]
        if any(_lang(o) == want for o in chosen):
            return list(dict.fromkeys(str(o["value"]) for o in chosen))
    return list(dict.fromkeys(str(o["value"]) for o in candidates))


def shorten(text: str | None, limit: int) -> str | None:
    if text is None:
        return None
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def code_of(uri: str, prefix: str) -> str:
    normalized = uri.replace("https://", "http://", 1)
    return normalized[len(prefix) :] if normalized.startswith(prefix) else uri


def object_text(graph: Graph, obj: dict[str, Any], prefer: tuple[str, ...]) -> str:
    """Text for an object; blank nodes (e.g. a format node) are read via rdfs:label/rdf:value."""
    value = str(obj["value"])
    if obj.get("type") == "bnode":
        node = graph.get(value)
        return literal(node, RDFS + "label", prefer) or literal(node, RDF + "value", prefer) or value
    return value


def resource_uri_of(child: dict[str, Any]) -> str | None:
    """``child.info[<entry URI>]['http://entrystore.org/terms/resource'][0].value``."""
    info = child.get("info")
    if isinstance(info, dict):
        for node in as_graph(info).values():
            value = first_value(node, ENTRYSTORE_RESOURCE)
            if value:
                return value
    return None


def find_root(graph: Graph, hint: str | None = None) -> str | None:
    """The described resource in a (recursive) metadata graph. Prefer the known resource URI;
    otherwise the non-blank subject no other subject points to (data services pointing back via
    dcat:servesDataset are ignored), preferring DCAT-typed subjects with a title."""
    if hint and hint in graph:
        return hint
    referenced: set[str] = set()
    for subject, node in graph.items():
        for predicate, values in node.items():
            if predicate == DCAT + "servesDataset":
                continue
            for obj in values:
                if obj.get("type") in ("uri", "bnode") and obj["value"] != subject:
                    referenced.add(str(obj["value"]))
    named = [s for s in graph if not s.startswith("_:")]
    candidates = [s for s in named if s not in referenced] or named or list(graph)
    if not candidates:
        return None
    main_types = {DCAT_DATASET, DCAT_DATASET_SERIES, DCAT_DATA_SERVICE, INDEPENDENT_DATA_SERVICE}

    def score(subject: str) -> tuple[bool, bool, int]:
        node = graph[subject]
        return bool(main_types & set(all_values(node, RDF_TYPE))), DCT + "title" in node, len(node)

    return max(candidates, key=score)


def children_of(data: dict[str, Any]) -> list[dict[str, Any]]:
    resource = data.get("resource")
    children = resource.get("children") if isinstance(resource, dict) else None
    return [c for c in children or [] if isinstance(c, dict)]


def portal_path(types: list[str] | set[str]) -> str:
    """Landing-page path segment by rdf:type, as dataportal-web's hitSpecifications map them."""
    kinds = set(types)
    if DCAT_DATASET_SERIES in kinds:
        return "dataset-series"
    if DCAT_DATASET in kinds:
        return "datasets"
    if kinds & {DCAT_DATA_SERVICE, INDEPENDENT_DATA_SERVICE}:
        return "dataservice"
    if kinds & {FOAF_AGENT, FOAF_ORGANIZATION}:
        return "organisations"
    return "datasets"


def portal_url(context_id: str, entry_id: str, path: str = "datasets") -> str:
    return f"{PORTAL_URL}/{path}/{context_id}_{entry_id}"


# ---------------------------------------------------------------------------
# Personal data (personuppgifter)
# ---------------------------------------------------------------------------

# Redaction notes point to include_personal_data; with FUZZY_MCP_PERSONAL_DATA=off that opt-in is refused, so the
# notes say this instead.
PERSONAL_DATA_OFF_NOTE = "Personuppgifter är avstängda i den här installationen (FUZZY_MCP_PERSONAL_DATA=off)."


def is_private_individual(type_uri: str | None) -> bool:
    """adms publisher type PrivateIndividual(s) (the portal leaves these out of its organisation list)."""
    if not type_uri:
        return False
    return code_of(type_uri, PUBLISHER_TYPE_PREFIX).casefold().startswith("privateindividual")


def is_private_agent(node: dict[str, list[dict[str, Any]]] | None) -> bool:
    return any(is_private_individual(t) for t in all_values(node, DCT + "type"))


def is_individual_contact(node: dict[str, list[dict[str, Any]]] | None) -> bool:
    return VCARD_INDIVIDUAL in all_values(node, RDF_TYPE)


def reachable_bnodes(graph: Graph, start: list[str]) -> set[str]:
    seen: set[str] = set()
    stack = list(start)
    while stack:
        node = graph.get(stack.pop()) or {}
        for values in node.values():
            for obj in values:
                value = str(obj["value"])
                if obj.get("type") == "bnode" and value not in seen:
                    seen.add(value)
                    stack.append(value)
    return seen


def redact_graph(graph: Graph) -> Graph:
    """Raw graph without personuppgifter: contact persons (vcard:Individual) and private-individual
    publishers keep only their rdf:type/dcterms:type; blank nodes hanging off them are dropped."""
    personal = [s for s, node in graph.items() if is_individual_contact(node) or is_private_agent(node)]
    if not personal:
        return graph
    dropped = reachable_bnodes(graph, personal) - set(personal)
    kept = (RDF_TYPE, DCT + "type")
    return {
        subject: ({p: v for p, v in node.items() if p in kept} if subject in personal else node)
        for subject, node in graph.items()
        if subject not in dropped
    }


def root_subgraph(graph: Graph, root: str) -> Graph:
    """The root node plus the blank nodes reachable from it (periods, locations, ...)."""
    keep = {root} | reachable_bnodes(graph, [root])
    return {s: node for s, node in graph.items() if s in keep}


def json_chars(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


_CURIES = ((DCAT, "dcat:"), (ESTERMS, "esterms:"), (FOAF, "foaf:"), (DCT, "dcterms:"))


def curie(uri: str) -> str:
    for namespace, prefix in _CURIES:
        if uri.startswith(namespace):
            return prefix + uri[len(namespace) :]
    return uri


# ---------------------------------------------------------------------------
# Output models
# ---------------------------------------------------------------------------


class DatasetSummary(BaseModel):
    context_id: str
    entry_id: str
    portal_url: str = Field(description="Datasetets sida på www.dataportal.se")
    dataset_uri: str | None = Field(default=None, description="Utgivarens resurs-URI för datasetet")
    title: str | None = None
    description: str | None = None
    publisher_uri: str | None = None
    publisher_name: str | None = None
    publisher_name_redacted: bool | None = Field(
        default=None,
        description="True när utgivaren är en privatperson (PrivateIndividual): namnet är en personuppgift och "
        "visas bara med include_personal_data=true",
    )
    themes: list[str] = Field(default_factory=list, description="EU-temakoder, se dataportal_theme_codes")
    keywords: list[str] = Field(default_factory=list)
    modified: str | None = None
    licenses: list[str] = Field(default_factory=list)
    formats: list[str] = Field(default_factory=list)
    distribution_count: int = 0


class DatasetSearchResult(BaseModel):
    total: int
    offset: int
    limit: int
    returned: int
    truncated: bool = Field(description="True om fler träffar finns (använd next_offset)")
    next_offset: int | None = None
    query: str | None = None
    publisher: str | None = Field(default=None, description="Utgivarfiltret som angavs")
    publisher_uris: list[str] = Field(default_factory=list, description="Utgivar-URI:er som filtret matchade")
    theme: str | None = None
    media_types: list[str] = Field(default_factory=list)
    solr_query: str
    sort: str
    datasets: list[DatasetSummary] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    hint: str = (
        "Distributioner (nedladdnings-/API-länkar), kontakt och tidsperiod: dataportal_get_dataset med "
        "context_id + entry_id. " + ATTRIBUTION + "; ange även utgivare och licens."
    )


class CodeLabel(BaseModel):
    code: str
    label: str | None = None


class Publisher(BaseModel):
    uri: str | None = None
    name: str | None = None
    type: str | None = Field(default=None, description="adms:publishertype, t.ex. NationalAuthority")
    name_redacted: bool | None = Field(
        default=None,
        description="True när utgivaren är en privatperson: namnet (personuppgift) visas bara med "
        "include_personal_data=true",
    )


class ContactPoint(BaseModel):
    uri: str | None = None
    name: str | None = None
    email: str | None = None
    redacted: bool | None = Field(
        default=None,
        description="True när kontaktpunkten är en person (vcard:Individual): namn och e-post är personuppgifter "
        "och visas bara med include_personal_data=true",
    )


class Period(BaseModel):
    start: str | None = None
    end: str | None = None


class SpatialCoverage(BaseModel):
    uri: str | None = None
    bbox: str | None = Field(default=None, description="dcat:bbox (WKT)")


class Distribution(BaseModel):
    uri: str | None = None
    title: str | None = None
    description: str | None = None
    access_urls: list[str] = Field(default_factory=list)
    download_urls: list[str] = Field(default_factory=list)
    format: str | None = None
    media_type: str | None = None
    license: str | None = None
    conforms_to: list[str] = Field(default_factory=list)
    access_services: list[str] = Field(default_factory=list, description="dcat:accessService (se data_services)")
    metadata_missing: bool | None = None


class DataService(BaseModel):
    uri: str | None = None
    title: str | None = None
    endpoint_urls: list[str] = Field(default_factory=list)
    endpoint_descriptions: list[str] = Field(default_factory=list)
    conforms_to: list[str] = Field(default_factory=list)


class DatasetDetail(BaseModel):
    context_id: str
    entry_id: str
    portal_url: str
    metadata_url: str
    dataset_uri: str | None = None
    types: list[str] = Field(default_factory=list)
    title: str | None = None
    description: str | None = None
    publisher: Publisher | None = None
    contact_points: list[ContactPoint] = Field(default_factory=list)
    themes: list[CodeLabel] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    issued: str | None = None
    modified: str | None = None
    accrual_periodicity: CodeLabel | None = None
    access_rights: CodeLabel | None = None
    licenses: list[str] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=list)
    conforms_to: list[str] = Field(default_factory=list)
    landing_pages: list[str] = Field(default_factory=list, description="dcat:landingPage hos utgivaren")
    documentation: list[str] = Field(default_factory=list, description="foaf:page")
    hvd_categories: list[CodeLabel] = Field(default_factory=list, description="Värdefulla datamängder (HVD)")
    identifiers: list[str] = Field(default_factory=list)
    in_series: list[str] = Field(default_factory=list)
    temporal: list[Period] = Field(default_factory=list)
    spatial: list[SpatialCoverage] = Field(default_factory=list)
    endpoint_urls: list[str] = Field(default_factory=list, description="Om posten själv är en datatjänst")
    endpoint_descriptions: list[str] = Field(default_factory=list)
    distributions: list[Distribution] = Field(default_factory=list)
    distributions_total: int = 0
    distributions_truncated: bool = False
    data_services: list[DataService] = Field(default_factory=list)
    attribution: str | None = None
    notes: list[str] = Field(default_factory=list)
    raw: dict[str, Any] | None = Field(
        default=None,
        description="RDF/JSON-grafen (include_raw); personuppgifter borttagna om inte include_personal_data. "
        "Begränsas till rotnoden (eller utelämnas) när den är för stor – se notes.",
    )


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


@dataclass
class PublisherFilter:
    uris: list[str]
    orgnr: str | None = None
    names: list[str] = field(default_factory=list)
    hidden_private: int = 0  # matched private individuals whose names are not shown


@dataclass(frozen=True)
class AgentInfo:
    name: str | None = None
    private: bool = False  # adms PrivateIndividual(s): the name is a personuppgift


class DataportalService:
    """Per-server dataportal state: search helpers and a publisher-name cache."""

    def __init__(self, services: Services) -> None:
        self.services = services
        self._agents: dict[str, tuple[float, AgentInfo]] = {}

    @property
    def base(self) -> str:
        return self.services.settings.base_url("dataportal")

    @property
    def prefer(self) -> tuple[str, ...]:
        lang = self.services.settings.default_language
        return (lang, "en" if lang == "sv" else "sv")

    async def search(
        self, query: str, *, limit: int, offset: int = 0, sort: str | None = None, **extra: Any
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"type": "solr", "query": query, "limit": limit, "offset": offset, "sort": sort}
        params.update(extra)
        response = await self.services.http.get_json(SOURCE, f"{self.base}/search", params=params)
        if not isinstance(response.data, dict):
            raise UpstreamError(SOURCE, "Oväntat svar från sökningen (inte ett JSON-objekt)", url=response.url)
        return response.data

    async def lookup_resources(self, uris: list[str], types: tuple[str, ...] | None = None) -> list[dict[str, Any]]:
        """Entries by resource URI (recipe 8), batched. URIs are escaped; ones that cannot be
        represented in a query (control characters, too long) are skipped instead of failing the batch."""
        wanted = [u for u in dict.fromkeys(uris) if representable(u, types)]
        children: list[dict[str, Any]] = []
        for batch in resource_batches(wanted, types):
            data = await self.search(resource_query(batch, types), limit=SOLR_MAX_LIMIT)
            children.extend(children_of(data))
        return children

    async def publisher_info(self, uris: list[str]) -> dict[str, AgentInfo]:
        """foaf:name and private-individual flag for publisher URIs (cached for a day). Search hits
        never include the agent."""
        now = time.monotonic()
        result: dict[str, AgentInfo] = {}
        missing: list[str] = []
        for uri in dict.fromkeys(uris):
            cached = self._agents.get(uri)
            if cached and now - cached[0] < NAME_CACHE_TTL_SECONDS:
                result[uri] = cached[1]
            else:
                missing.append(uri)
        if missing:
            found: dict[str, AgentInfo] = {}
            for child in await self.lookup_resources(missing):
                graph = as_graph(child.get("metadata"))
                subject = resource_uri_of(child) or find_root(graph)
                node = graph.get(subject or "")
                name = literal(node, FOAF + "name", self.prefer)
                if subject and (name or is_private_agent(node)):
                    found.setdefault(subject, AgentInfo(name=name, private=is_private_agent(node)))
            for uri in missing:
                result[uri] = found.get(uri, AgentInfo())
                self._agents[uri] = (now, result[uri])
        return result

    async def resolve_publisher(self, value: str, include_personal_data: bool = False) -> PublisherFilter:
        text = value.strip()
        if not text:
            raise InvalidInputError("Ange en utgivare: organisationsnummer, URI eller namn")
        if text.lower().startswith(("http://", "https://")):
            return PublisherFilter(uris=[check_uri(text, "publisher")])
        orgnr = PUBLISHER_SHORTCUTS.get(text.casefold()) or normalize_orgnr(text)
        if orgnr:
            return PublisherFilter(uris=[p.format(orgnr=orgnr) for p in PUBLISHER_URI_PATTERNS], orgnr=orgnr)
        if re.fullmatch(r"(?:SE)?[0-9\s-]+", text, re.IGNORECASE):
            raise InvalidInputError(
                f"Ogiltigt organisationsnummer {value!r}: ange 10 siffror, t.ex. '202100-6545' eller '2021006545'"
            )
        return await self.publishers_by_name(text, include_personal_data)

    async def publishers_by_name(self, name: str, include_personal_data: bool = False) -> PublisherFilter:
        """Find organisations (foaf:Agent entries) by name; the title field includes foaf:name.
        Names of private individuals are not echoed unless include_personal_data."""
        tokens = text_tokens(name)[:MAX_TEXT_TOKENS]
        if not tokens:
            raise InvalidInputError(f"Kan inte söka utgivare med namnet {name!r}")
        term = tokens[0] if len(tokens) == 1 else "(" + " AND ".join(tokens) + ")"
        data = await self.search(f"rdfType:{esc(FOAF_AGENT)} AND public:true AND title:{term}", limit=50)
        wanted = name.casefold()
        exact: dict[str, AgentInfo] = {}
        partial: dict[str, AgentInfo] = {}
        for child in children_of(data):
            graph = as_graph(child.get("metadata"))
            subject = resource_uri_of(child) or find_root(graph)
            node = graph.get(subject or "")
            names = all_values(node, FOAF + "name")
            if not subject or not names or not representable(subject):
                continue
            info = AgentInfo(name=literal(node, FOAF + "name", self.prefer) or names[0], private=is_private_agent(node))
            if any(n.casefold() == wanted for n in names):
                exact.setdefault(subject, info)
            elif any(wanted in n.casefold() for n in names):
                partial.setdefault(subject, info)
        chosen = exact or partial

        def shown(infos: list[AgentInfo]) -> list[str]:
            return list(dict.fromkeys(i.name for i in infos if i.name and (include_personal_data or not i.private)))

        if not chosen:
            raise InvalidInputError(
                f"Hittade ingen utgivare med namnet {name!r} i dataportalen. Ange organisationsnummer "
                "(t.ex. '202100-6545') eller utgivarens URI."
            )
        if not exact and len(partial) > 10:
            sample = ", ".join(sorted(shown(list(partial.values())))[:8])
            raise InvalidInputError(
                f"Namnet {name!r} matchar {len(partial)} utgivare" + (f" (t.ex. {sample})" if sample else "") + ". "
                "Ange hela namnet, organisationsnummer eller URI."
            )
        now = time.monotonic()
        for uri, info in chosen.items():
            self._agents[uri] = (now, info)
        hidden = sum(1 for i in chosen.values() if i.private and not include_personal_data)
        return PublisherFilter(uris=list(chosen), names=shown(list(chosen.values())), hidden_private=hidden)

    async def publisher_uris_from_facet(self, orgnr: str) -> list[str]:
        """Runtime fallback: publisher URIs in the facet that contain the organisation number
        (covers suffixed URIs such as ``…/SE<orgnr>-xyz`` and other URI patterns)."""
        pattern = f"{orgnr[:6]}-?{orgnr[6:]}"
        data = await self.search(
            f"rdfType:{esc(DCAT_DATASET)} AND public:true",
            limit=0,
            facetFields=PUBLISHER_FIELD,
            facetLimit=1000,
            facetMatches=f".*{pattern}.*",  # Solr facet.matches; filtered client-side as well
        )
        matcher = re.compile(rf"(?<![0-9]){pattern}(?![0-9])")
        found: list[str] = []
        for facet in data.get("facetFields") or []:
            if not isinstance(facet, dict) or facet.get("name") != PUBLISHER_FIELD:
                continue
            for value in facet.get("values") or []:
                name = str(value.get("name") or "") if isinstance(value, dict) else ""
                if matcher.search(name) and representable(name):
                    found.append(name)
        return list(dict.fromkeys(found))

    def summarize(self, child: dict[str, Any], include_personal_data: bool = False) -> DatasetSummary:
        context_id = str(child.get("contextId") or "")
        entry_id = str(child.get("entryId") or "")
        graph = as_graph(child.get("metadata"))
        subject = resource_uri_of(child)
        node = graph.get(subject or "") if subject in graph else graph.get(find_root(graph) or "")
        prefer = self.prefer
        publisher = objs(node, DCT + "publisher")
        publisher_uri = publisher_name = None
        redacted = None
        if publisher:
            obj = publisher[0]
            if obj.get("type") == "uri":
                publisher_uri = str(obj["value"])
            elif obj.get("type") == "bnode":
                agent = graph.get(str(obj["value"]))
                if is_private_agent(agent) and not include_personal_data:
                    redacted = True
                else:
                    publisher_name = literal(agent, FOAF + "name", prefer)
            else:  # literal publisher (rare; its type is unknown)
                publisher_name = str(obj["value"])
        return DatasetSummary(
            context_id=context_id,
            entry_id=entry_id,
            portal_url=portal_url(context_id, entry_id),
            dataset_uri=subject,
            title=literal(node, DCT + "title", prefer),
            description=shorten(literal(node, DCT + "description", prefer), SUMMARY_DESCRIPTION_CHARS),
            publisher_uri=publisher_uri,
            publisher_name=publisher_name,
            publisher_name_redacted=redacted,
            themes=[code_of(t, THEME_PREFIX) for t in all_values(node, DCAT + "theme")],
            keywords=literals_in_lang(node, DCAT + "keyword", prefer)[:10],
            modified=first_value(node, DCT + "modified"),
            licenses=all_values(node, DCT + "license"),
            formats=[object_text(graph, o, prefer) for o in objs(node, DCT + "format")][:10],
            distribution_count=len(objs(node, DCAT + "distribution")),
        )

    async def run_search(
        self,
        *,
        text: str | None,
        publisher: str | None,
        theme: str | None,
        file_format: str | None,
        sort: str | None,
        limit: int,
        offset: int,
        resolve_names: bool,
        include_personal_data: bool = False,
    ) -> DatasetSearchResult:
        theme_code = normalize_theme(theme) if theme and theme.strip() else None
        media_types = normalize_format(file_format) if file_format and file_format.strip() else None
        notes: list[str] = []
        tokens = text_tokens(text or "")
        if len(tokens) > MAX_TEXT_TOKENS:
            notes.append(f"Bara de {MAX_TEXT_TOKENS} första sökorden användes.")
        pub = (
            await self.resolve_publisher(publisher, include_personal_data) if publisher and publisher.strip() else None
        )
        has_text = bool(tokens)
        if sort == "relevance" and has_text:
            sort_param = RELEVANCE_SORT
        elif sort == "modified" or not has_text:
            sort_param = MODIFIED_SORT
        else:
            sort_param = RELEVANCE_SORT
        if limit > MAX_SEARCH_LIMIT:
            notes.append(f"limit begränsades till {MAX_SEARCH_LIMIT} (svarsstorlek); bläddra med next_offset.")
        limit = clamp(limit, 1, MAX_SEARCH_LIMIT)
        offset = max(0, offset)

        def dataset_query() -> str:
            query = build_dataset_query(text, pub.uris if pub else None, theme_code, media_types)
            if len(query) > MAX_SEARCH_QUERY_CHARS:
                raise InvalidInputError(
                    f"Sökfrågan blev för lång ({len(query)} tecken). Använd färre eller kortare sökord, eller ett "
                    "snävare utgivarfilter (organisationsnummer eller URI)."
                )
            return query

        query = dataset_query()
        data = await self.search(query, limit=limit, offset=offset, sort=sort_param)
        if pub and pub.orgnr and not children_of(data) and offset == 0:
            extra = [u for u in await self.publisher_uris_from_facet(pub.orgnr) if u not in pub.uris]
            if extra:
                pub.uris.extend(extra)
                notes.append("Utgivar-URI:er hittades via utgivarfacetten: " + ", ".join(extra))
                query = dataset_query()
                data = await self.search(query, limit=limit, offset=offset, sort=sort_param)

        # Never more rows than asked for, even if the server ignores the limit (output size).
        rows = [self.summarize(child, include_personal_data) for child in children_of(data)[:limit]]
        if resolve_names:
            wanted = [r.publisher_uri for r in rows if r.publisher_uri and not r.publisher_name]
            if wanted:
                try:
                    infos = await self.publisher_info(wanted)
                except UpstreamError as exc:
                    infos = {}
                    notes.append(f"Utgivarnamn kunde inte slås upp: {exc.message}")
                for row in rows:
                    info = infos.get(row.publisher_uri or "")
                    if info is None or row.publisher_name:
                        continue
                    if info.private and not include_personal_data:
                        row.publisher_name_redacted = True
                    else:
                        row.publisher_name = info.name
        if any(r.publisher_name_redacted for r in rows):
            notes.append(
                "Utgivare som är privatpersoner visas utan namn (personuppgift); include_personal_data=true visar dem."
                if self.services.settings.allow_personal_data
                else f"Utgivare som är privatpersoner visas utan namn (personuppgift). {PERSONAL_DATA_OFF_NOTE}"
            )
        try:
            total = int(data.get("results"))
        except (TypeError, ValueError):
            total = offset + len(rows)
        more = offset + len(rows) < total
        if pub and pub.names:
            notes.append("Utgivare som matchade namnet: " + ", ".join(pub.names))
        if pub and pub.hidden_private:
            notes.append(
                f"{pub.hidden_private} matchande utgivare är privatpersoner; namnen visas inte (personuppgift)."
            )
        return DatasetSearchResult(
            total=total,
            offset=offset,
            limit=limit,
            returned=len(rows),
            truncated=more,
            next_offset=offset + len(rows) if more and rows else None,
            query=text.strip() if text and text.strip() else None,
            publisher=publisher.strip() if publisher and publisher.strip() else None,
            publisher_uris=pub.uris if pub else [],
            theme=theme_code,
            media_types=list(media_types or []),
            solr_query=query,
            sort=sort_param,
            datasets=rows,
            notes=notes,
        )

    # -- single dataset ------------------------------------------------------

    async def locate(
        self, dataset: str | None, context_id: str | int | None, entry_id: str | int | None
    ) -> tuple[str, str, str | None]:
        """(context_id, entry_id, resource URI if known) from ids, a portal/admin URL or a URI."""
        ctx_text = "" if context_id is None else str(context_id).strip()
        eid_text = "" if entry_id is None else str(entry_id).strip()
        if ctx_text or eid_text:
            if not (ctx_text and eid_text):
                raise InvalidInputError("Ange både context_id och entry_id (t.ex. '43' och '69395')")
            return check_id(ctx_text, "context_id"), check_id(eid_text, "entry_id"), None
        text = (dataset or "").strip()
        if not text:
            raise InvalidInputError(
                "Ange context_id + entry_id, en dataportal-länk (https://www.dataportal.se/datasets/43_69395) "
                "eller datasetets URI"
            )
        if match := _SHORT_ID.fullmatch(text):
            return match.group(1), match.group(2), None
        if not text.lower().startswith(("http://", "https://")):
            raise InvalidInputError(f"Känner inte igen {dataset!r}: ange en URL/URI eller '<context>_<entry>'")
        uri = check_uri(text, "dataset")
        parsed = urlparse(uri)
        host = (parsed.hostname or "").lower()
        # Exact hosts only: sandbox.admin.dataportal.se, editera.dataportal.se or look-alike domains
        # have their own (or no) ids and must never be mapped onto production entries.
        if host in PORTAL_HOSTS and (match := _PORTAL_PATH.fullmatch(parsed.path)):
            return match.group(1), match.group(2), None
        base = urlparse(self.base)
        base_path = base.path.rstrip("/")
        if (
            host == (base.hostname or "").lower()
            and parsed.path.startswith(base_path + "/")
            and (match := _STORE_PATH.fullmatch(parsed.path[len(base_path) :]))
        ):
            return match.group(1), match.group(2), None
        # Any other URI: the publisher's own resource URI (e.g. dataset_uri from a search hit),
        # restricted to datasets, series and data services. Other EntryStore instances (e.g.
        # catalog.skara.se/store/1/resource/9) have their own ids, so they are looked up by resource.
        for child in await self.lookup_resources([uri], MAIN_TYPES):
            ids = child_ids(child)
            resource = resource_uri_of(child) or uri
            # Double-check the type client-side (entry graph + metadata, like Solr's rdfType field).
            types = {t for node in as_graph(child.get("info")).values() for t in all_values(node, RDF_TYPE)}
            types |= set(all_values(as_graph(child.get("metadata")).get(resource), RDF_TYPE))
            if ids and (not types or types & set(MAIN_TYPES)):
                return ids[0], ids[1], resource
        raise InvalidInputError(
            f"Hittade inget dataset, ingen dataserie eller datatjänst i dataportalen med URI:n {uri!r}. "
            "Utgivar-URI:er används som publisher-filter i dataportal_search_datasets."
        )

    async def fetch_graph(self, context_id: str, entry_id: str) -> tuple[Graph, list[str]]:
        url = f"{self.base}/{context_id}/metadata/{entry_id}"
        notes: list[str] = []
        try:
            response = await self.services.http.get_json(
                SOURCE, url, params={"recursive": "dcat", "format": "application/json"}
            )
        except UpstreamError as exc:
            # An unknown/unsupported traversal profile answers 400; a traversal over a very large
            # graph can fail (5xx) or time out (no status). Fall back to the entry's own metadata
            # and fetch related entities by resource URI below. 401/403/404 are final.
            if exc.status is not None and exc.status != 400 and exc.status < 500:
                raise
            response = await self.services.http.get_json(SOURCE, url, params={"format": "application/json"})
            reason = f"HTTP {exc.status}" if exc.status is not None else "inget svar"
            notes.append(f"recursive=dcat misslyckades ({reason}); relaterade poster hämtades via resurssökning.")
        if not isinstance(response.data, dict):
            raise UpstreamError(SOURCE, "Metadata kom inte som RDF/JSON", url=response.url)
        return as_graph(response.data), notes

    async def entry_resource_uri(self, context_id: str, entry_id: str) -> str | None:
        """The entry's resource URI from its entry information (es:resource, spec §1.7)."""
        response = await self.services.http.get_json(
            SOURCE, f"{self.base}/{context_id}/entry/{entry_id}", params={"format": "application/json"}
        )
        return resource_uri_of(response.data) if isinstance(response.data, dict) else None

    async def merge_missing(self, graph: Graph, uris: list[str], prefix: str) -> list[str]:
        """Add entries referenced by URI but absent from the graph (search hits carry only their own
        metadata, so this is also the fallback when the traversal did not include them). Blank nodes
        are relabelled with ``prefix`` (distinct per call). Returns the URIs that were looked up."""
        missing = [u for u in dict.fromkeys(uris) if u and not u.startswith("_:") and u not in graph]
        if not missing:
            return []
        for index, child in enumerate(await self.lookup_resources(missing)):
            for subject, node in relabel_bnodes(as_graph(child.get("metadata")), f"{prefix}{index}_").items():
                graph.setdefault(subject, node)
        return missing

    async def get_dataset(
        self,
        dataset: str | None,
        context_id: str | int | None,
        entry_id: str | int | None,
        max_distributions: int,
        include_raw: bool,
        include_personal_data: bool = False,
    ) -> DatasetDetail:
        ctx, eid, hint = await self.locate(dataset, context_id, entry_id)
        graph, notes = await self.fetch_graph(ctx, eid)
        if hint is None:
            # The recursive graph also holds related entities (and possibly sibling datasets via
            # dcat:servesDataset or series links): ask EntryStore which subject the entry describes.
            try:
                hint = await self.entry_resource_uri(ctx, eid)
            except UpstreamError as exc:
                notes.append(f"Postinformationen kunde inte hämtas ({exc.message}); huvudresursen valdes heuristiskt.")
            else:
                if hint not in graph:
                    notes.append("Postens resurs-URI saknades i metadatagrafen; huvudresursen valdes heuristiskt.")
        root_uri = find_root(graph, hint)
        if root_uri is None:
            raise UpstreamError(SOURCE, "Posten saknar metadata", url=f"{self.base}/{ctx}/metadata/{eid}")
        prefer = self.prefer
        root = graph[root_uri]
        root_types = all_values(root, RDF_TYPE)
        if root_types and not set(root_types) & set(MAIN_TYPES):
            notes.append(
                "Posten är inte ett dataset, en dataserie eller en datatjänst (typ: "
                + ", ".join(curie(t) for t in root_types)
                + ")."
            )

        dist_refs = all_values(root, DCAT + "distribution")
        cap = clamp(max_distributions, 0, MAX_DISTRIBUTIONS)
        if max_distributions > MAX_DISTRIBUTIONS and len(dist_refs) > MAX_DISTRIBUTIONS:
            notes.append(f"max_distributions begränsades till {MAX_DISTRIBUTIONS} (svarsstorlek).")
        shown = dist_refs[:cap]
        publisher_uri = next((str(o["value"]) for o in objs(root, DCT + "publisher") if o.get("type") == "uri"), None)
        contact_refs = all_values(root, DCAT + "contactPoint")
        wanted = [*shown, *contact_refs, *([publisher_uri] if publisher_uri else [])]
        looked_up: list[str] = []
        try:
            looked_up = await self.merge_missing(graph, wanted, "r")
            if any(u in graph for u in looked_up):
                notes.append("Distributioner/kontakt/utgivare kompletterades via resurssökning.")
            services_refs = [s for d in shown for s in all_values(graph.get(d), DCAT + "accessService")]
            await self.merge_missing(graph, services_refs, "s")
        except UpstreamError as exc:
            notes.append(f"Relaterade poster kunde inte hämtas: {exc.message}")

        description_chars = SHORT_DESCRIPTION_CHARS if len(shown) > MANY_DISTRIBUTIONS else SUMMARY_DESCRIPTION_CHARS
        distributions = [self._distribution(graph, ref, prefer, description_chars) for ref in shown]
        missing = sum(1 for d in distributions if d.metadata_missing)
        if missing:
            notes.append(f"{missing} distribution(er) saknade metadata i svaret.")

        service_uris = [s for d in distributions for s in d.access_services]
        service_types = {DCAT_DATA_SERVICE, INDEPENDENT_DATA_SERVICE}
        service_uris += [
            s for s, node in graph.items() if s != root_uri and service_types & set(all_values(node, RDF_TYPE))
        ]
        data_services = [self._data_service(graph, uri, prefer) for uri in dict.fromkeys(service_uris) if uri in graph]

        publisher = self._publisher(graph, root, prefer, include_personal_data)
        if (
            publisher
            and publisher.uri
            and not publisher.name
            and not publisher.name_redacted
            and publisher.uri not in looked_up
        ):
            with contextlib.suppress(UpstreamError):  # the name is a nicety; never fail the tool for it
                info = (await self.publisher_info([publisher.uri])).get(publisher.uri) or AgentInfo()
                if info.private and not include_personal_data:
                    publisher.name_redacted = True
                else:
                    publisher.name = info.name
        allowed = self.services.settings.allow_personal_data
        if publisher and publisher.name_redacted:
            notes.append(
                "Utgivaren är en privatperson; namnet visas inte (personuppgift, se include_personal_data)."
                if allowed
                else f"Utgivaren är en privatperson; namnet visas inte (personuppgift). {PERSONAL_DATA_OFF_NOTE}"
            )
        contacts = [self._contact(graph, ref, prefer, include_personal_data) for ref in contact_refs]
        if any(c.redacted for c in contacts):
            notes.append(
                "Kontaktpersoners namn och e-post (vcard:Individual) visas inte (personuppgift, "
                "se include_personal_data)."
                if allowed
                else "Kontaktpersoners namn och e-post (vcard:Individual) visas inte (personuppgift). "
                + PERSONAL_DATA_OFF_NOTE
            )
        licenses = all_values(root, DCT + "license")
        if not licenses:
            licenses = list(dict.fromkeys(d.license for d in distributions if d.license))
        who = (publisher.name or publisher.uri) if publisher else None
        attribution = f"{ATTRIBUTION}; utgivare: {who or 'okänd'}; licens: " + (
            ", ".join(licenses) if licenses else "ej angiven i metadata"
        )

        description = literal(root, DCT + "description", prefer)
        if description and len(description) > DETAIL_DESCRIPTION_CHARS:
            notes.append(f"Beskrivningen kortades till {DETAIL_DESCRIPTION_CHARS} tecken.")
        frequency = first_value(root, DCT + "accrualPeriodicity")
        access = first_value(root, DCT + "accessRights")
        detail = DatasetDetail(
            context_id=ctx,
            entry_id=eid,
            portal_url=portal_url(ctx, eid, portal_path(root_types)),
            metadata_url=f"{self.base}/{ctx}/metadata/{eid}",
            dataset_uri=root_uri if not root_uri.startswith("_:") else None,
            types=[curie(t) for t in root_types],
            title=literal(root, DCT + "title", prefer),
            description=shorten(description, DETAIL_DESCRIPTION_CHARS),
            publisher=publisher,
            contact_points=contacts,
            themes=[self._code(t, THEME_PREFIX, THEME_LABELS) for t in all_values(root, DCAT + "theme")],
            keywords=literals_in_lang(root, DCAT + "keyword", prefer)[:50],
            issued=first_value(root, DCT + "issued"),
            modified=first_value(root, DCT + "modified"),
            accrual_periodicity=self._code(frequency, FREQUENCY_PREFIX, FREQUENCIES) if frequency else None,
            access_rights=self._code(access, ACCESS_RIGHT_PREFIX, ACCESS_RIGHTS) if access else None,
            licenses=licenses,
            languages=[code_of(v, LANGUAGE_PREFIX) for v in all_values(root, DCT + "language")],
            conforms_to=all_values(root, DCT + "conformsTo"),
            landing_pages=all_values(root, DCAT + "landingPage"),
            documentation=all_values(root, FOAF + "page"),
            hvd_categories=[
                self._code(v, HVD_PREFIX, HVD_CATEGORIES) for v in all_values(root, DCATAP + "hvdCategory")
            ],
            identifiers=all_values(root, DCT + "identifier"),
            in_series=all_values(root, DCAT + "inSeries"),
            temporal=[self._period(graph, o) for o in objs(root, DCT + "temporal")],
            spatial=[self._spatial(graph, o) for o in objs(root, DCT + "spatial")],
            endpoint_urls=all_values(root, DCAT + "endpointURL"),
            endpoint_descriptions=all_values(root, DCAT + "endpointDescription"),
            distributions=distributions,
            distributions_total=len(dist_refs),
            distributions_truncated=len(dist_refs) > len(shown),
            data_services=data_services,
            attribution=attribution,
            notes=notes,
        )
        if include_raw:
            self._attach_raw(detail, graph if include_personal_data else redact_graph(graph), root_uri)
        return detail

    @staticmethod
    def _attach_raw(detail: DatasetDetail, graph: Graph, root_uri: str) -> None:
        """Add the raw graph within the output budget: whole graph, else the root node with its blank
        nodes, else nothing (with a note pointing to metadata_url)."""
        budget = OUTPUT_BUDGET_CHARS - json_chars(detail.model_dump(mode="json", exclude_none=True)) - 300
        size = json_chars(graph)
        if size <= budget:
            detail.raw = graph
            return
        subgraph = root_subgraph(graph, root_uri)
        if json_chars(subgraph) <= budget:
            detail.raw = subgraph
            detail.notes.append(
                f"raw innehåller bara rotnoden och dess blanka noder (hela grafen är {size} tecken); "
                "hela grafen finns på metadata_url?recursive=dcat&format=application/json."
            )
            return
        detail.notes.append(
            f"raw utelämnades: grafen ({size} tecken) ryms inte i svaret; hämta metadata_url"
            "?recursive=dcat&format=application/json direkt."
        )

    @staticmethod
    def _code(uri: str, prefix: str, labels: dict[str, str]) -> CodeLabel:
        code = code_of(uri, prefix)
        return CodeLabel(code=code, label=labels.get(code))

    @staticmethod
    def _distribution(
        graph: Graph, ref: str, prefer: tuple[str, ...], description_chars: int = SUMMARY_DESCRIPTION_CHARS
    ) -> Distribution:
        node = graph.get(ref)
        uri = None if ref.startswith("_:") else ref
        if node is None:
            return Distribution(uri=uri, metadata_missing=True)
        formats = objs(node, DCT + "format")
        return Distribution(
            uri=uri,
            title=literal(node, DCT + "title", prefer),
            description=shorten(literal(node, DCT + "description", prefer), description_chars),
            access_urls=all_values(node, DCAT + "accessURL"),
            download_urls=all_values(node, DCAT + "downloadURL"),
            format=object_text(graph, formats[0], prefer) if formats else None,
            media_type=first_value(node, DCAT + "mediaType"),
            license=first_value(node, DCT + "license"),
            conforms_to=all_values(node, DCT + "conformsTo"),
            access_services=all_values(node, DCAT + "accessService"),
        )

    @staticmethod
    def _data_service(graph: Graph, uri: str, prefer: tuple[str, ...]) -> DataService:
        node = graph.get(uri)
        return DataService(
            uri=None if uri.startswith("_:") else uri,
            title=literal(node, DCT + "title", prefer),
            endpoint_urls=all_values(node, DCAT + "endpointURL"),
            endpoint_descriptions=all_values(node, DCAT + "endpointDescription"),
            conforms_to=all_values(node, DCT + "conformsTo"),
        )

    @staticmethod
    def _publisher(
        graph: Graph, root: dict[str, list[dict[str, Any]]], prefer: tuple[str, ...], include_personal_data: bool
    ) -> Publisher | None:
        found = objs(root, DCT + "publisher")
        if not found:
            return None
        obj = found[0]
        value = str(obj["value"])
        if obj.get("type") == "literal":  # its type is unknown
            return Publisher(name=value)
        node = graph.get(value)
        kind = first_value(node, DCT + "type")
        private = is_private_individual(kind) and not include_personal_data
        return Publisher(
            uri=None if value.startswith("_:") else value,
            name=None if private else literal(node, FOAF + "name", prefer),
            type=code_of(kind, PUBLISHER_TYPE_PREFIX) if kind else None,
            name_redacted=True if private else None,
        )

    @staticmethod
    def _contact(graph: Graph, ref: str, prefer: tuple[str, ...], include_personal_data: bool) -> ContactPoint:
        node = graph.get(ref)
        uri = None if ref.startswith("_:") else ref
        if is_individual_contact(node) and not include_personal_data:
            # A named person: vcard:fn and the e-mail address are personuppgifter.
            return ContactPoint(uri=None if (uri or "").lower().startswith("mailto:") else uri, redacted=True)
        email = first_value(node, VCARD + "hasEmail")
        if email and email.lower().startswith("mailto:"):
            email = email[len("mailto:") :]
        return ContactPoint(uri=uri, name=literal(node, VCARD + "fn", prefer), email=email)

    @staticmethod
    def _period(graph: Graph, obj: dict[str, Any]) -> Period:
        node = graph.get(str(obj["value"]))
        # DCAT-AP-SE uses dcat:startDate/endDate; schema.org is accepted defensively (older DCAT-AP).
        return Period(
            start=first_value(node, DCAT + "startDate") or first_value(node, "http://schema.org/startDate"),
            end=first_value(node, DCAT + "endDate") or first_value(node, "http://schema.org/endDate"),
        )

    @staticmethod
    def _spatial(graph: Graph, obj: dict[str, Any]) -> SpatialCoverage:
        value = str(obj["value"])
        if obj.get("type") == "uri":
            return SpatialCoverage(uri=value)
        return SpatialCoverage(bbox=first_value(graph.get(value), DCAT + "bbox"))


def code_lists() -> dict[str, Any]:
    """The DCAT-AP-SE code lists used in dataportal metadata (served as fuzzy://dataportal/codes)."""
    return {
        "themes": {
            "uri_prefix": THEME_PREFIX,
            "codes": {k: {"sv": sv, "en": en} for k, (sv, en) in THEMES.items()},
        },
        "access_rights": {"uri_prefix": ACCESS_RIGHT_PREFIX, "codes": ACCESS_RIGHTS},
        "frequencies": {"uri_prefix": FREQUENCY_PREFIX, "codes": FREQUENCIES},
        "licenses": {
            "uris": LICENSES,
            "note": "Exakta URI:er enligt DCAT-AP-SE (http, avslutande snedstreck); andra licens-URI:er är tillåtna.",
        },
        "hvd_categories": {"uri_prefix": HVD_PREFIX, "codes": HVD_CATEGORIES},
        "publisher_types": {
            "uri_prefix": PUBLISHER_TYPE_PREFIX,
            "codes": list(PUBLISHER_TYPES),
            "note": "LocalAuthority = kommun, NationalAuthority = nationell myndighet",
        },
        "common_formats": list(COMMON_FORMATS),
        "format_aliases": {k: list(v) for k, v in FORMAT_ALIASES.items()},
        "publisher_shortcuts": PUBLISHER_SHORTCUTS,
        "publisher_uri_patterns": [p.replace("{orgnr}", "<orgnr>") for p in PUBLISHER_URI_PATTERNS],
    }


def compact_result(model: BaseModel) -> CallToolResult:
    """Structured result without empty lists/objects (keeps per-row output small; every list
    field has a default, so omitted fields still validate against the output schema)."""
    return structured(model.model_dump(mode="json", by_alias=True, exclude_none=True))


def theme_list() -> list[dict[str, str]]:
    return [{"code": k, "label_sv": sv, "label_en": en, "uri": THEME_PREFIX + k} for k, (sv, en) in THEMES.items()]


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

PublisherParam = Annotated[
    str,
    Field(
        description="Utgivare (publisher): organisationsnummer ('202100-6545' eller '2021006545'), utgivar-URI "
        "(t.ex. 'http://dataportal.se/organisation/SE2021006545'), genvägarna 'scb', 'skolverket', "
        "'fohm'/'folkhalsomyndigheten', eller organisationens namn (t.ex. 'Trafikverket')"
    ),
]
ThemeParam = Annotated[
    str | None,
    Field(
        description="EU-tema (theme): kod som EDUC (utbildning), HEAL (hälsa), SOCI (befolkning), GOVE, REGI, ECON, "
        "ENVI, TRAN ... eller svensk/engelsk etikett. Se dataportal_theme_codes."
    ),
]
LimitParam = Annotated[int, Field(description="Antal träffar per sida (1–50)")]
OffsetParam = Annotated[int, Field(description="Hoppa över så många träffar (paginering, använd next_offset)")]
PersonalDataParam = Annotated[
    bool,
    Field(
        description="Ta med personuppgifter: namn på utgivare som är privatpersoner (PrivateIndividual) och namn/"
        "e-post för kontaktpersoner (vcard:Individual). Personuppgift – sätt bara true när uppgiften uttryckligen "
        "behövs. Gäller även raw."
    ),
]


def register(server: MCPServer[Any], services: Services) -> None:
    portal = DataportalService(services)

    @server.tool(name="dataportal_search_datasets", title="Dataportalen: sök dataset", annotations=READ_ONLY_OPEN)
    @tool_errors
    async def dataportal_search_datasets(
        query: Annotated[
            str | None,
            Field(
                description="Fritext (sökord) i titel, beskrivning och nyckelord, t.ex. 'skolenheter' eller "
                "'vaccination barn'. Flera ord kombineras med AND; delord matchar. Utelämna för att lista nyaste."
            ),
        ] = None,
        publisher: Annotated[
            str | None,
            Field(
                description="Filtrera på utgivare (publisher): organisationsnummer, utgivar-URI, 'scb', "
                "'skolverket', 'fohm' eller organisationens namn"
            ),
        ] = None,
        theme: ThemeParam = None,
        format: Annotated[
            str | None,
            Field(description="Filformat: MIME-typ ('text/csv') eller csv, json, xml, xlsx, xls, excel, pdf, zip ..."),
        ] = None,
        sort: Annotated[
            Literal["relevance", "modified"] | None,
            Field(
                description="Sortering: relevans (standard med sökord) eller senast ändrad (modified) enligt "
                "metadatans dcterms:modified som på dataportal.se (standard utan sökord)"
            ),
        ] = None,
        limit: LimitParam = 20,
        offset: OffsetParam = 0,
        resolve_publisher_names: Annotated[
            bool, Field(description="Slå upp utgivarnas namn (ett extra anrop, cachas)")
        ] = True,
        include_personal_data: PersonalDataParam = False,
    ) -> Annotated[CallToolResult, DatasetSearchResult]:
        """Sök dataset (DCAT-AP-SE) i Sveriges dataportal – katalogen över öppna data från alla svenska
        myndigheter, regioner och kommuner. Returnerar per träff context_id/entry_id, titel (svenska i första
        hand), kort beskrivning, utgivare, EU-teman, nyckelord, senast ändrad, licens, format, antal
        distributioner och länk till dataportal.se, samt total/offset/limit (högst 50 per sida). Använd för att
        hitta vilka datamängder som finns (även från SCB, Skolverket och Folkhälsomyndigheten) och gå vidare med
        dataportal_get_dataset för nedladdningslänkar och API:er. Namn på utgivare som är privatpersoner är
        personuppgifter och visas bara med include_personal_data=true."""
        include_personal_data = personal_data(services, include_personal_data)
        result = await portal.run_search(
            text=query,
            publisher=publisher,
            theme=theme,
            file_format=format,
            sort=sort,
            limit=limit,
            offset=offset,
            resolve_names=resolve_publisher_names,
            include_personal_data=include_personal_data,
        )
        return compact_result(result)

    @server.tool(
        name="dataportal_list_publisher_datasets",
        title="Dataportalen: en utgivares dataset",
        annotations=READ_ONLY_OPEN,
    )
    @tool_errors
    async def dataportal_list_publisher_datasets(
        publisher: PublisherParam,
        query: Annotated[str | None, Field(description="Valfria sökord för att smalna av")] = None,
        theme: ThemeParam = None,
        limit: LimitParam = 25,
        offset: OffsetParam = 0,
        include_personal_data: PersonalDataParam = False,
    ) -> Annotated[CallToolResult, DatasetSearchResult]:
        """Lista en utgivares (myndighets/kommuns) dataset i Sveriges dataportal, senast ändrade först
        (metadatans dcterms:modified, som på dataportal.se), med paginering (total, next_offset).
        Bekvämlighetsvariant av dataportal_search_datasets för t.ex. 'vilka öppna data publicerar Skolverket?'."""
        include_personal_data = personal_data(services, include_personal_data)
        result = await portal.run_search(
            text=query,
            publisher=publisher,
            theme=theme,
            file_format=None,
            sort="modified",
            limit=limit,
            offset=offset,
            resolve_names=True,
            include_personal_data=include_personal_data,
        )
        return compact_result(result)

    @server.tool(name="dataportal_get_dataset", title="Dataportalen: datasetdetaljer", annotations=READ_ONLY_OPEN)
    @tool_errors
    async def dataportal_get_dataset(
        dataset: Annotated[
            str | None,
            Field(
                description="Länk på dataportal.se (https://www.dataportal.se/datasets/43_69395, även "
                "/dataservice/ och /dataset-series/), admin-URI (https://admin.dataportal.se/store/43/entry/69395), "
                "'43_69395' eller datasetets/datatjänstens egen URI (dataset_uri från sökningen). Alternativ till "
                "context_id + entry_id."
            ),
        ] = None,
        context_id: Annotated[
            str | int | None, Field(description="contextId från sökträffen, t.ex. '43' (siffror)")
        ] = None,
        entry_id: Annotated[
            str | int | None, Field(description="entryId från sökträffen, t.ex. '69395' (siffror)")
        ] = None,
        max_distributions: Annotated[int, Field(description="Max antal distributioner att visa (0–100)")] = 50,
        include_raw: Annotated[
            bool,
            Field(
                description="Ta med RDF/JSON-grafen (raw). Stora grafer kortas till rotnoden eller utelämnas "
                "(se notes); personuppgifter tas bort om inte include_personal_data=true"
            ),
        ] = False,
        include_personal_data: PersonalDataParam = False,
    ) -> Annotated[CallToolResult, DatasetDetail]:
        """Hämta ett dataset (eller en datatjänst/dataserie) från Sveriges dataportal med alla distributioner:
        titel, beskrivning, utgivare, kontaktpunkt, teman, nyckelord, uppdateringsfrekvens, åtkomsträttigheter,
        licens, tidsperiod och geografisk täckning, samt per distribution accessURL/downloadURL, format/mediatyp,
        licens och conformsTo, och datatjänster (API) med endpointURL/endpointDescription. Använd efter
        dataportal_search_datasets för att få faktiska nedladdnings- och API-länkar. Kontaktpersoners namn och
        e-post samt namn på privatpersoner som utgivare är personuppgifter och visas bara med
        include_personal_data=true."""
        include_personal_data = personal_data(services, include_personal_data)
        detail = await portal.get_dataset(
            dataset, context_id, entry_id, max_distributions, include_raw, include_personal_data
        )
        return compact_result(detail)

    @server.tool(name="dataportal_theme_codes", title="Dataportalen: EU-teman", annotations=READ_ONLY_LOCAL)
    @tool_errors
    async def dataportal_theme_codes() -> dict[str, Any]:
        """Lista EU:s datateman (dcat:theme) som används i DCAT-AP-SE och dataportalen: kod (t.ex. EDUC, HEAL,
        SOCI), svensk och engelsk etikett och URI. Koden används som theme-filter i dataportal_search_datasets.
        Fler kodlistor (åtkomsträttigheter, frekvenser, licenser, HVD-kategorier) finns i resursen
        fuzzy://dataportal/codes."""
        return {"uri_prefix": THEME_PREFIX, "themes": theme_list(), "see_also": "fuzzy://dataportal/codes"}

    @server.resource(
        "fuzzy://dataportal/codes",
        name="dataportal-codes",
        title="Dataportalen: DCAT-AP-SE-kodlistor",
        description="EU-teman, åtkomsträttigheter, uppdateringsfrekvenser, licens-URI:er, HVD-kategorier, "
        "utgivartyper och vanliga format som används i Sveriges dataportal.",
        mime_type="application/json",
    )
    def dataportal_codes() -> dict[str, Any]:
        return code_lists()
