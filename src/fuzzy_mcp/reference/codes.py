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
    """Lowercase, strip diacritics and administrative suffixes so that
    'Göteborgs stad', 'goteborg' and 'Göteborg' compare equal."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    plain = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    plain = plain.replace("-", " ").replace("_", " ")
    words = [w for w in plain.split() if w not in _STRIP_WORDS]
    return " ".join(words)


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


def lookup_region(query: str, *, kind: str = "alla", limit: int = 5) -> list[dict[str, Any]]:
    """Find regions by code or (fuzzy) name. ``kind``: alla | kommun | lan."""
    query = query.strip()
    if not query:
        raise InvalidInputError("Ange ett namn eller en kod")
    entries = _region_entries()
    if kind != "alla":
        entries = [e for e in entries if e["typ"] == kind]

    by_code = [e for e in entries if e["kod"] == query or e.get("lansbokstav", "").casefold() == query.casefold()]
    if by_code:
        return [dict(e, traff="kod", likhet=1.0) for e in by_code[:limit]]

    target = normalize(query)
    scored: list[tuple[float, dict[str, Any]]] = []
    for entry in entries:
        name = normalize(entry["namn"])
        candidates = {name, name.removesuffix("s")}
        if target in candidates:
            score = 1.0
        elif any(c.startswith(target) for c in candidates):
            score = 0.9
        else:
            score = max(difflib.SequenceMatcher(None, target, c).ratio() for c in candidates)
        if score >= 0.6:
            scored.append((score, entry))
    # Prefer municipalities over counties on equal scores ("Uppsala" → kommun first).
    order = {"kommun": 0, "lan": 1, "riket": 2}
    scored.sort(key=lambda item: (-item[0], order[item[1]["typ"]], item[1]["kod"]))
    return [dict(entry, traff="namn", likhet=round(score, 3)) for score, entry in scored[:limit]]


def municipalities_in_county(county_code: str) -> list[dict[str, str]]:
    doc = load("regioner")
    if county_code not in {lan["kod"] for lan in doc["lan"]}:
        raise InvalidInputError(f"Okänd länskod {county_code!r} (två siffror, t.ex. 01 för Stockholms län)")
    return [k for k in doc["kommuner"] if k["lanskod"] == county_code]
