# SPDX-FileCopyrightText: 2026 Deniz Özer
#
# SPDX-License-Identifier: MIT

"""Bundled reference data (code lists) and fuzzy lookup helpers.

Each JSON file in ``data/`` is one code list with at least ``id``, ``titel``,
``beskrivning`` and ``kalla`` (source). Generic lists keep their entries
under ``koder`` as objects with ``kod`` and ``namn``.

Every post (see :func:`posts`) carries ``verifiering``: how the post was
checked, one of :data:`VERIFIERING` (defined in ``data/verifiering.json``).
``okänd`` means the post's origin is not documented – it is never replaced
by a guess. When a post combines facts with different evidence, the weakest
evidence decides and ``verifiering_not`` explains the parts. Objects nested
inside a post (e.g. inriktningar in a programme) inherit the post's value
unless they carry their own. Top-level collections that are not posts
(lookup tables, lists of strings) get ``{"verifiering": ..., "verifiering_not": ...}``
under ``verifiering_samlingar``.
Optional companions: ``verifiering_not``, ``verifierad_datum`` (YYYY-MM-DD of
a verified extract) and ``kalla_url``.
"""

import difflib
import json
import unicodedata
from collections import Counter
from collections.abc import Iterator
from functools import cache
from importlib import resources
from typing import Any

from ..errors import InvalidInputError

_DATA_PACKAGE = "fuzzy_mcp.reference.data"
_STRIP_WORDS = ("kommun", "kommuns", "stad", "lan", "region", "regionen", "landsting")
# Official municipality names put the name in the genitive before "kommun" or "stad" ("Lunds kommun", "Göteborgs
# stad"); a name ending in s is unchanged ("Borås stad") and Falun keeps the old genitive "Falu" ("Falu kommun").
# The words may themselves be in the genitive ("Lunds kommuns"), and a name in the genitive may also stand alone
# ("Lunds", "Falu"), see match_municipality.
_MUNICIPALITY_WORDS = ("kommun", "kommuns", "stad", "stads")
_GENITIVES = {"falu": "falun"}
_TRAILING_PUNCTUATION = ".,;:!?"
# A municipality name is short (the longest, "Upplands Väsby kommun", has 21 characters); a longer text is refused
# before any fuzzy comparison, whose cost grows with the length (#42). ref_list_deso has the same limit.
MAX_NAME_LENGTH = 100


@cache
def _files() -> dict[str, str]:
    root = resources.files(_DATA_PACKAGE)
    return {entry.name.removesuffix(".json"): entry.name for entry in root.iterdir() if entry.name.endswith(".json")}


@cache
def load(name: str) -> dict[str, Any]:
    files = _files()
    if name not in files:
        raise InvalidInputError(f"Okänd kodlista {name!r}. Tillgängliga: {', '.join(sorted(files))}")
    text = resources.files(_DATA_PACKAGE).joinpath(files[name]).read_text(encoding="utf-8")
    return json.loads(text)


COLLECTION_MARKERS = "verifiering_samlingar"
MISSING = "saknas"
INVALID = "ogiltig"


def _vocabulary() -> dict[str, str]:
    doc = load("verifiering")
    return {str(entry["kod"]): str(entry["beskrivning"]) for entry in doc["koder"]}


# Controlled vocabulary for ``verifiering`` (strongest evidence first), read from data/verifiering.json.
VERIFIERING: dict[str, str] = _vocabulary()


def post_collections(doc: dict[str, Any]) -> Iterator[tuple[str, list[dict[str, Any]]]]:
    """(collection, posts) for every top-level collection that holds posts.

    A post is an object in a top-level list (``koder``, ``lan``, ``kommuner``,
    ``markeringar`` ...), a top-level object with a ``kod`` (``riket``) or an
    object one level under a top-level object whose values are all objects
    (``andra_versioner`` in ss12000).
    """
    for key, value in doc.items():
        if key == COLLECTION_MARKERS:
            continue
        if isinstance(value, list):
            items = [item for item in value if isinstance(item, dict)]
            if items:
                yield key, items
        elif isinstance(value, dict):
            if "kod" in value:
                yield key, [value]
            elif value and all(isinstance(item, dict) for item in value.values()):
                yield key, list(value.values())


def posts(doc: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Every post of a code list (see :func:`post_collections`)."""
    for _, items in post_collections(doc):
        yield from items


def _bucket(value: Any) -> str:
    if value is None:
        return MISSING
    return value if value in VERIFIERING else INVALID


def verification_summary(doc: dict[str, Any]) -> dict[str, Any]:
    """Per collection: posts per ``verifiering`` value (missing → "saknas", unknown values → "ogiltig"),
    or the collection-level value for lookup tables listed in ``verifiering_samlingar``."""
    order = [*VERIFIERING, MISSING, INVALID]
    summary: dict[str, Any] = {}
    for key, items in post_collections(doc):
        counts = Counter(_bucket(item.get("verifiering")) for item in items)
        summary[key] = {value: counts[value] for value in order if counts[value]}
    for key, marker in (doc.get(COLLECTION_MARKERS) or {}).items():
        summary[key] = _bucket(marker.get("verifiering") if isinstance(marker, dict) else None)
    return summary


def list_code_lists() -> list[dict[str, Any]]:
    out = []
    for name in sorted(_files()):
        doc = load(name)
        entries = doc.get("koder")
        out.append(
            {
                "id": doc.get("id", name),
                "titel": doc.get("titel"),
                "beskrivning": doc.get("beskrivning"),
                "kalla": doc.get("kalla"),
                # Lists without ``koder`` may count their collections themselves (DeSO/RegSO: deso, regso, rader).
                "antal": len(entries) if isinstance(entries, list) else doc.get("antal"),
                "antal_poster": sum(1 for _ in posts(doc)),
                "verifiering": verification_summary(doc),
            }
        )
    return out


def normalize(text: str) -> str:
    """Lowercase, strip diacritics, read hyphens as spaces and drop administrative words (kommun, stad, län,
    region ...) so that 'Göteborg stad', 'goteborg' and 'Göteborg' compare equal. A genitive s stays
    ('Göteborgs stad' → 'goteborgs'); :func:`name_forms` gives the name without it."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    plain = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    plain = plain.replace("-", " ").replace("_", " ")
    words = [w for w in plain.split() if w not in _STRIP_WORDS]
    return " ".join(words)


def _words(text: str) -> list[str]:
    """``text`` casefolded and split into words as :func:`normalize` reads it (hyphens and underscores as spaces),
    without punctuation at the end ("Lunds kommun." → ["lunds", "kommun"]). Diacritics are kept."""
    plain = unicodedata.normalize("NFC", text).casefold().replace("-", " ").replace("_", " ")
    return plain.strip().rstrip(_TRAILING_PUNCTUATION).split()


def _spelling(text: str) -> str:
    """``text`` as spelled, in the form :func:`name_forms` returns, so that names can be compared letter by letter."""
    return " ".join(_words(text))


def _without_genitive_s(name: str) -> str | None:
    """``name`` without a genitive s ("lunds" → "lund"), or None when it has none. A name that ends in s takes no
    genitive s ("Borås stad"), so a double s ("boråss") is a misspelling, not a genitive."""
    if len(name) > 2 and name.endswith("s") and not name.endswith("ss"):
        return name[:-1]
    return None


def name_forms(text: str) -> list[str]:
    """``text`` as spelled (see :func:`_words`) and, when it ends in "kommun", "kommuns", "stad" or "stads", the
    name before that word as written and in base form: without a genitive s ("Lunds kommun" → "lund") or from
    :data:`_GENITIVES` ("Falu kommun" → "falun"). Diacritics are kept, so a form can be compared with a name as
    spelled (Håbo or Habo)."""
    words = _words(text)
    forms = [" ".join(words)]
    if len(words) > 1 and words[-1] in _MUNICIPALITY_WORDS:
        name = " ".join(words[:-1])
        forms.append(name)
        if base := _without_genitive_s(name):
            forms.append(base)
        if name in _GENITIVES:
            forms.append(_GENITIVES[name])
    return forms


def _region_entries() -> list[dict[str, str]]:
    doc = load("regioner")
    riket = doc["riket"]
    entries = [{"kod": riket["kod"], "namn": riket["namn"], "typ": "riket", "verifiering": riket["verifiering"]}]
    entries += [
        {
            "kod": lan["kod"],
            "namn": lan["namn"],
            "typ": "lan",
            "lansbokstav": lan["lansbokstav"],
            "verifiering": lan["verifiering"],
        }
        for lan in doc["lan"]
    ]
    lan_names = {lan["kod"]: lan["namn"] for lan in doc["lan"]}
    entries += [
        {
            "kod": k["kod"],
            "namn": k["namn"],
            "typ": "kommun",
            "lanskod": k["lanskod"],
            "lan": lan_names[k["lanskod"]],
            "verifiering": k["verifiering"],
        }
        for k in doc["kommuner"]
    ]
    return entries


def lookup_region(query: str, *, kind: str = "alla", limit: int = 5, fuzzy: bool = True) -> list[dict[str, Any]]:
    """Find regions by code or (fuzzy) name. ``kind``: alla | kommun | lan. A name may be the official one with
    the genitive ('Lunds kommun', 'Göteborgs stad', see :func:`name_forms`). ``fuzzy=False``: exact hits only."""
    query = query.strip()
    if not query:
        raise InvalidInputError("Ange ett namn eller en kod")
    entries = _region_entries()
    if kind != "alla":
        entries = [e for e in entries if e["typ"] == kind]

    by_code = [e for e in entries if e["kod"] == query or e.get("lansbokstav", "").casefold() == query.casefold()]
    if by_code:
        return [dict(e, traff="kod", likhet=1.0) for e in by_code[:limit]]

    targets = {normalize(form) for form in name_forms(query)}
    scored: list[tuple[float, dict[str, Any]]] = []
    for entry in entries:
        name = normalize(entry["namn"])
        # A county's name is in the genitive ("Stockholms län" → Stockholm); a municipality's is the name itself, so
        # "Bora" is not Borås.
        candidates = {name, name.removesuffix("s")} if entry["typ"] == "lan" else {name}
        if targets & candidates:
            score = 1.0
        elif not fuzzy:
            continue
        elif any(c.startswith(t) for c in candidates for t in targets):
            score = 0.9
        else:
            score = max(difflib.SequenceMatcher(None, t, c).ratio() for c in candidates for t in targets)
        if score >= 0.6:
            scored.append((score, entry))
    # Prefer municipalities over counties on equal scores ("Uppsala" → kommun first).
    order = {"kommun": 0, "lan": 1, "riket": 2}
    scored.sort(key=lambda item: (-item[0], order[item[1]["typ"]], item[1]["kod"]))
    return [dict(entry, traff="namn", likhet=round(score, 3)) for score, entry in scored[:limit]]


def _bare_genitives(text: str) -> list[str]:
    """Base forms of a name in the genitive without "kommun" or "stad" ("Lunds" → "lund", "Falu" → "falun"), as
    spelled. Empty when the text ends in such a word, which :func:`name_forms` already handles, or in another word
    such as "län": a county in the genitive is not one of its municipalities."""
    words = _words(text)
    if not words or words[-1] in _MUNICIPALITY_WORDS or words[-1] in _STRIP_WORDS or words[-1] == "län":
        return []
    name = " ".join(words)
    forms = [_GENITIVES[name]] if name in _GENITIVES else []
    if base := _without_genitive_s(name):
        forms.append(base)
    return forms


def match_municipality(query: str, *, limit: int = 5) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """(municipality, exact hits) for a code or a name that names one municipality, else (None, the ``limit``
    closest hits as suggestions). A name names a municipality only when it matches no other one exactly (see
    :func:`lookup_region`), never as the best fuzzy hit. A name with no exact hit is tried once more as a genitive
    without "kommun" or "stad" ("Lunds" → Lund, "Falu" → Falun); no municipality name is another one plus or minus
    an s, so this adds no ambiguity. A name longer than :data:`MAX_NAME_LENGTH` is refused."""
    if len(query) > MAX_NAME_LENGTH:
        raise InvalidInputError(
            f"Kommunnamnet är för långt ({len(query)} tecken, högst {MAX_NAME_LENGTH}). Ange kommunkod (4 siffror)."
        )
    every = len(load("regioner")["kommuner"])
    exact = lookup_region(query, kind="kommun", limit=every, fuzzy=False)
    spelled_as = query
    if not exact:
        for form in _bare_genitives(query):
            exact = lookup_region(form, kind="kommun", limit=every, fuzzy=False)
            if exact:
                spelled_as = form
                break
    # The match strips diacritics (Håbo == Habo), so an exact spelling decides when it names one municipality.
    forms = name_forms(spelled_as)
    spelled = [hit for hit in exact if _spelling(hit["namn"]) in forms]
    named = spelled if len(spelled) == 1 else exact
    if len(named) == 1:
        return named[0], exact
    return None, lookup_region(query, kind="kommun", limit=limit)


def municipalities_in_county(county_code: str) -> list[dict[str, str]]:
    doc = load("regioner")
    if county_code not in {lan["kod"] for lan in doc["lan"]}:
        raise InvalidInputError(f"Okänd länskod {county_code!r} (två siffror, t.ex. 01 för Stockholms län)")
    return [k for k in doc["kommuner"] if k["lanskod"] == county_code]
