# SPDX-FileCopyrightText: 2026 Deniz Özer
#
# SPDX-License-Identifier: MIT

"""Local lookups in SCB's DeSO and RegSO key files (no network).

Built on three bundled code lists: ``deso_regso_2018`` (DeSO 2018 → RegSO 2020),
``deso_regso_2025`` (DeSO 2025 → RegSO 2025) and ``deso_forandringar`` (SCB's
change log; every row is ``[fran, till, typ, datum]`` and a code may appear on
several rows). Codes, names and change types are SCB's own. The comparability
verdicts in :func:`comparability` are derived here from the log and carry
``verifiering: "härlett"``:

* a code in both versions is ``oförändrad`` when no row names it, otherwise
  ``ändrad gräns`` (a surviving code always has a row with ``fran == till``
  when it gives away territory; a code that only receives appears as ``till``);
  a row whose other code is in neither key file predates the files (Karlskoga's
  two rows of 2018-02-21) and leaves the code ``oförändrad`` with a note;
* a code only in DeSO 2018 is ``upphört``; ``summerbar_antal`` is true when every
  row for it has SCB's type ``Upphör; till nybildad`` and each receiving code has
  no other source, so the parts together cover the old code and *counts* for the
  parts add up to it (same period and definition);
* a code only in DeSO 2025 is ``nytt``; ``summerbar_antal_till`` names its parent
  when it has exactly one source and that parent is summerbar; ``syskon`` are the
  other new codes with the same parent (a receiving code that already existed
  in DeSO 2018 is not a sibling).

Summing is only ever about additive measures (counts). Shares, rates, means,
medians and indices cannot be summed over areas, split or not; they need the
source data per area or a weighting the source provides. Every note says so.
A 1:1 code change (SCB's type ``Kodändrad; annat än 20-22``) is deliberately
not counted as summerbar: the log does not say whether the boundary is unchanged.

``ssd_koder`` in a lookup gives the code as SCB's Statistikdatabasen spells it
per version (:data:`SSD_CODELISTS`, :data:`SSD_SUFFIX`): plain codes in the
code lists for DeSO 2018 and RegSO 2020, a version suffix in those for DeSO 2025
and RegSO 2025 (verified extract 2026-10-10, see docs/KALLKONTROLL.md).
"""

import functools
import re
from collections import defaultdict
from collections.abc import Iterable, Sequence
from typing import Any, NamedTuple

from ..errors import InvalidInputError
from . import codes

DESO_CODE = re.compile(r"^\d{4}[ABC]\d{4}$")
REGSO_CODE = re.compile(r"^\d{4}R\d{3}$")
# DeSO version year → bundled code list; each list names its RegSO version under "version".
VERSIONS: dict[str, str] = {"2018": "deso_regso_2018", "2025": "deso_regso_2025"}
LOG = "deso_forandringar"
# SCB's change types the derivation depends on (verbatim from the log).
SPLIT = "Upphör; till nybildad"
RECODED = "Kodändrad; annat än 20-22"
DERIVED = "härlett"
SOURCE_DATA = "myndighetswebb"
SOURCE_PAGE = "SCB, sidan Demografiska statistikområden (DeSO) under öppna geodata"
MAX_OPTIONS = 60  # RegSO alternatives listed in an error message
# How Statistikdatabasen spells the codes, per kind ("deso"/"regso") and DeSO version year: the code lists of the
# variable Region, the suffix the code carries there (none for the 2018/2020 pair) and the reference years each list
# covers. Verified extract 2026-10-10 (TAB6680 has all four lists); an individual table may have only one pair.
SSD_CODELISTS: dict[str, dict[str, str]] = {
    "deso": {"2018": "vs_DeSO2018", "2025": "vs_DeSO2025"},
    "regso": {"2018": "vs_RegSO2020", "2025": "vs_RegSO2025"},
}
SSD_SUFFIX: dict[str, dict[str, str]] = {"deso": {"2025": "_DeSO2025"}, "regso": {"2025": "_RegSO2025"}}
SSD_VALID: dict[str, str] = {"2018": "t.o.m. referensår 2023", "2025": "fr.o.m. referensår 2024"}
SSD_NOTE = (
    "ssd_koder följer SCB:s kodlistor i Statistikdatabasen (verifierat uttag 2026-10-10: TAB6680 har 5 984 rena "
    "DeSO 2018-koder, 6 160 DeSO 2025-koder med suffix _DeSO2025, 3 363 RegSO 2020 och 3 363 RegSO 2025 med suffix "
    "_RegSO2025; äldre tabeller märkta 'uppdateras ej' har vs_DeSoHE/vs_RegSo1 med rena koder t.o.m. 2023); en "
    "enskild tabell kan ha bara en av versionerna – kontrollera scb_get_table_metadata."
)
ADDITIVE_ONLY = (
    "Gäller bara antal (additiva mått) med samma period och definition; andelar, medelvärden, medianer och index "
    "kan inte summeras över områden."
)


class Row(NamedTuple):
    fran: str
    till: str
    typ: str
    datum: str

    def as_dict(self) -> dict[str, str]:
        return {"fran": self.fran, "till": self.till, "typ": self.typ, "datum": self.datum}


def _doc(version: str) -> dict[str, Any]:
    return codes.load(VERSIONS[version])


def labels(version: str) -> dict[str, str]:
    """``{"deso": "DeSO 2025", "regso": "RegSO 2025"}`` as the file names them."""
    return dict(_doc(version)["version"])


@functools.cache
def log() -> tuple[Row, ...]:
    """Every row of the change log, sorted by (datum, fran, till)."""
    rows = (Row(*(str(field) for field in row)) for row in codes.load(LOG)["forandringar"])
    return tuple(sorted(rows, key=lambda r: (r.datum, r.fran, r.till)))


@functools.cache
def _rows_by_code() -> dict[str, tuple[Row, ...]]:
    by_code: dict[str, list[Row]] = defaultdict(list)
    for row in log():
        by_code[row.fran].append(row)
        if row.till != row.fran:
            by_code[row.till].append(row)
    return {code: tuple(rows) for code, rows in by_code.items()}


@functools.cache
def _sources() -> dict[str, frozenset[str]]:
    sources: dict[str, set[str]] = defaultdict(set)
    for row in log():
        if row.fran != row.till:
            sources[row.till].add(row.fran)
    return {code: frozenset(found) for code, found in sources.items()}


@functools.cache
def _regso_members(version: str) -> dict[str, tuple[str, ...]]:
    members: dict[str, list[str]] = defaultdict(list)
    for deso, regso in _doc(version)["deso"].items():
        members[regso].append(deso)
    return {regso: tuple(sorted(found)) for regso, found in members.items()}


@functools.cache
def _municipalities() -> dict[str, str]:
    return {k["kod"]: k["namn"] for k in codes.load("regioner")["kommuner"]}


def rows_for(code: str) -> tuple[Row, ...]:
    """Log rows naming ``code`` as ``fran`` or ``till`` (a self row once), in log order."""
    return _rows_by_code().get(code, ())


def sources_of(code: str) -> frozenset[str]:
    """Codes that gave territory to ``code`` (rows with ``till == code`` and ``fran != code``)."""
    return _sources().get(code, frozenset())


def municipality_name(code: str) -> str | None:
    return _municipalities().get(code[:4])


def deso_versions(code: str) -> list[str]:
    return [version for version in VERSIONS if code in _doc(version)["deso"]]


def regso_versions(code: str) -> list[str]:
    return [version for version in VERSIONS if code in _doc(version)["regso"]]


def ssd_codes(kind: str, code: str, versions: Sequence[str]) -> dict[str, dict[str, str]]:
    """The code as Statistikdatabasen's variable Region spells it, keyed by the version name the key file uses
    (``DeSO 2018``/``DeSO 2025`` or ``RegSO 2020``/``RegSO 2025``), with the code list and the reference years."""
    return {
        labels(version)[kind]: {
            "kod": code + SSD_SUFFIX[kind].get(version, ""),
            "kodlista": SSD_CODELISTS[kind][version],
            "galler": SSD_VALID[version],
        }
        for version in versions
    }


def source_block(versions: Sequence[str], *, with_log: bool) -> dict[str, Any]:
    """Source reference for an answer: the files it was read from, with the date SCB put in each file."""
    docs = [_doc(version) for version in versions]
    if with_log:
        docs.append(codes.load(LOG))
    urls = sorted({doc["kalla_url"] for doc in docs})
    licenses = sorted({doc["licens"] for doc in docs})
    return {
        "kalla": f"{SOURCE_PAGE}: " + "; ".join(doc["titel"] for doc in docs),
        "kalla_url": urls[0] if len(urls) == 1 else urls,
        "licens": licenses[0] if len(licenses) == 1 else licenses,
        "filer": [{"namn": doc["fil"]["namn"], "datum_i_fil": doc["fil"]["datum_i_fil"]} for doc in docs],
    }


# --- Comparability (derived from the log) -----------------------------------------------------------


def is_summable(code: str) -> bool:
    """True for a DeSO 2018 code that ended as a pure split: every row has type ``Upphör; till nybildad`` and
    every new part has this code as its only source."""
    rows = [row for row in log() if row.fran == code]
    return bool(rows) and all(row.typ == SPLIT and sources_of(row.till) == {code} for row in rows)


def _ended(code: str, rows: Iterable[Row], when: str) -> dict[str, Any]:
    replaced = sorted({row.till for row in rows if row.till != code})
    types = sorted({row.typ for row in rows})
    summable = is_summable(code)
    if summable:
        note = f"Upphört {when}: delades i {_count(len(replaced), 'nytt område', 'nya områden')} som tillsammans "
        note += f"täcker koden; antal för delarna kan summeras tillbaka till koden. {ADDITIVE_ONLY}"
    elif types == [RECODED]:
        note = (
            f"Upphört {when}: koden ersattes av {', '.join(replaced)} med SCB:s förändringstyp '{RECODED}'. "
            "Loggen säger inte om gränsen är oförändrad, så koden räknas inte som summerbar ens för antal; "
            "kontrollera kodbytet mot SCB innan serierna kopplas ihop."
        )
    else:
        note = (
            f"Upphört {when}: delar gick till {_count(len(replaced), 'område', 'områden')} ({_quoted(types)}). "
            "Kan inte summeras tillbaka ur DeSO 2025-statistik, inte ens antal: minst ett mottagande område har "
            "enligt loggen även andra delar eller fanns sedan tidigare."
        )
    return {"ersatts_av": replaced, "summerbar_antal": summable, "not": note}


def _new(code: str, rows: Iterable[Row], when: str) -> dict[str, Any]:
    parents = sorted(sources_of(code))
    out: dict[str, Any] = {"bildat_av": parents, "summerbar_antal_till": None, "syskon": []}
    if len(parents) != 1:
        out["not"] = (
            f"Nytt område {when} med delar från {len(parents)} tidigare områden: "
            "kan inte härledas ur DeSO 2018-statistik."
        )
        return out
    parent = parents[0]
    existing = _doc("2018")["deso"]
    siblings = sorted(
        {
            row.till
            for row in log()
            if row.fran == parent and row.till not in (parent, code) and row.till not in existing
        }
    )
    out["syskon"] = siblings
    parent_ended = parent in _doc("2018")["deso"] and parent not in _doc("2025")["deso"]
    types = sorted({row.typ for row in rows})
    if parent_ended and is_summable(parent):
        out["summerbar_antal_till"] = parent
        together = f" tillsammans med {', '.join(siblings)}" if siblings else ""
        note = f"Nytt område {when}: bildat genom delning av {parent}{together}; antal för delarna kan summeras till "
        note += f"{parent} i DeSO 2018. {ADDITIVE_ONLY}"
    elif types == [RECODED]:
        note = (
            f"Nytt område {when}: ersätter {parent} med SCB:s förändringstyp '{RECODED}'. Loggen säger inte om "
            "gränsen är oförändrad; kontrollera kodbytet mot SCB innan serierna kopplas ihop."
        )
    elif parent_ended:
        note = (
            f"Nytt område {when}: del av {parent}, som upphörde utan att delarna kan summeras tillbaka "
            "(inte ens antal); koden kan inte härledas ur DeSO 2018-statistik."
        )
    else:
        note = (
            f"Nytt område {when}: avskilt från {parent}, som finns kvar med ändrad gräns; "
            "koden kan inte härledas ur DeSO 2018-statistik."
        )
    out["not"] = note
    return out


def _count(n: int, singular: str, plural: str) -> str:
    return f"{n} {singular if n == 1 else plural}"


def _quoted(texts: Iterable[str]) -> str:
    """SCB's change types contain semicolons, so each one is quoted when several are listed."""
    return ", ".join(f"'{text}'" for text in texts)


def _counterpart(row: Row, code: str) -> str:
    return row.fran if row.till == code else row.till


def _unknown(code: str) -> InvalidInputError:
    """The code is in neither key file; if the log still names it (Karlskoga's 2018 codes), say what it says."""
    text = f"DeSO-koden {code!r} finns inte i DeSO 2018 eller DeSO 2025."
    if rows := rows_for(code):
        listed = "; ".join(f"{row.fran} → {row.till} {row.datum} ('{row.typ}')" for row in rows)
        text += f" Koden förekommer dock i SCB:s förändringslogg: {listed}."
    return InvalidInputError(text)


def _when(rows: Iterable[Row]) -> str:
    return ", ".join(sorted({row.datum for row in rows}))


def comparability(code: str) -> dict[str, Any]:
    """Derived verdict on comparing ``code`` between DeSO 2018 and DeSO 2025 (see the module docstring)."""
    in_2018 = code in _doc("2018")["deso"]
    in_2025 = code in _doc("2025")["deso"]
    rows = rows_for(code)
    when = _when(rows)
    if in_2018 and in_2025:
        # A row whose other code is in neither key file predates the files: between the bundled versions it is no
        # change (Karlskoga's 1883A0030, formed 2018-02-21 from two codes absent from the DeSO 2018 file).
        between = [row for row in rows if deso_versions(_counterpart(row, code))]
        if between:
            status = "ändrad gräns"
            note = (
                f"Samma kod i båda versionerna men gränsen ändrad {_when(between)}: "
                "jämför inte över tid utan att kontrollera förändringen."
            )
        elif rows:
            status = "oförändrad"
            earlier = ", ".join(sorted({_counterpart(row, code) for row in rows}))
            note = (
                f"Samma kod i DeSO 2018 och DeSO 2025. Raderna om koden i SCB:s förändringslogg ({when}) avser "
                f"{earlier}, som inte finns i någon av nyckelfilerna: förändringen ligger före DeSO 2018 så som filen "
                "är, och ingen förändring mellan versionerna är loggad. Jämförbar över versionerna så långt loggen "
                f"visar; statistik för perioder före {when} kan vara redovisad på de tidigare koderna."
            )
        else:
            status = "oförändrad"
            note = (
                "Samma kod i DeSO 2018 och DeSO 2025 och ingen rad i SCB:s förändringslogg: "
                "jämförbar över versionerna så långt loggen visar."
            )
        return {"status": status, "not": note, "verifiering": DERIVED}
    if in_2018:
        return {"status": "upphört", **_ended(code, rows, when), "verifiering": DERIVED}
    if in_2025:
        return {"status": "nytt", **_new(code, rows, when), "verifiering": DERIVED}
    raise _unknown(code)


# --- Lookups ------------------------------------------------------------------------------------------


def normalize_code(code: str) -> str:
    key = code.strip().upper()
    if DESO_CODE.match(key) or REGSO_CODE.match(key):
        return key
    raise InvalidInputError(
        f"Ogiltig kod {code!r}. Ange en DeSO-kod (kommunkod + A/B/C + fyra siffror, t.ex. '0114C1010') "
        "eller en RegSO-kod (kommunkod + R + tre siffror, t.ex. '0114R001')."
    )


def lookup(code: str) -> dict[str, Any]:
    """Describe a DeSO or RegSO code (format decides which) from the bundled files."""
    key = normalize_code(code)
    return lookup_deso(key) if DESO_CODE.match(key) else lookup_regso(key)


def lookup_deso(code: str) -> dict[str, Any]:
    versions = deso_versions(code)
    if not versions:
        raise _unknown(code)
    latest = _doc(versions[-1])
    category = next(k for k in latest["kategorier"] if k["kod"] == code[4])
    per_version = []
    for version in versions:
        doc = _doc(version)
        regso = doc["deso"][code]
        per_version.append(
            {
                "deso": doc["version"]["deso"],
                "regso": doc["version"]["regso"],
                "regsokod": regso,
                "regso_namn": doc["regso"][regso],
            }
        )
    return {
        "kod": code,
        "typ": "deso",
        "kommunkod": code[:4],
        "kommun": municipality_name(code),
        "kategori": {"kod": category["kod"], "beskrivning": category["beskrivning"]},
        "finns_i": [labels(version)["deso"] for version in versions],
        "versioner": per_version,
        "ssd_koder": ssd_codes("deso", code, versions),
        "forandringar": [row.as_dict() for row in rows_for(code)],
        "jamforbarhet": comparability(code),
        "kalla": source_block(list(VERSIONS), with_log=True),
        "verifiering": SOURCE_DATA,
        "verifiering_not": "Koder, RegSO-namn och förändringsrader är SCB:s egna ur filerna under kalla. "
        "jamforbarhet är härledd i kod ur förändringsloggen (verifiering 'härlett') och säger inget om "
        f"befolkning eller gränsernas läge. {SSD_NOTE}",
    }


def lookup_regso(code: str) -> dict[str, Any]:
    versions = regso_versions(code)
    if not versions:
        raise InvalidInputError(f"RegSO-koden {code!r} finns inte i RegSO 2020 eller RegSO 2025.")
    names = {version: _doc(version)["regso"][code] for version in versions}
    return {
        "kod": code,
        "typ": "regso",
        "kommunkod": code[:4],
        "kommun": municipality_name(code),
        "finns_i": [labels(version)["regso"] for version in versions],
        "versioner": [
            {
                "regso": labels(version)["regso"],
                "namn": names[version],
                "deso": list(_regso_members(version).get(code, ())),
            }
            for version in versions
        ],
        "ssd_koder": ssd_codes("regso", code, versions),
        # None when the code is in one version only: there is nothing to compare.
        "namn_andrat": len(set(names.values())) > 1 if len(versions) > 1 else None,
        "not": "RegSO-namn är unika bara inom kommunen och kan ändras; använd koden.",
        "kalla": source_block(versions, with_log=False),
        "verifiering": SOURCE_DATA,
        "verifiering_not": "Koder, namn och DeSO-tillhörighet är SCB:s egna ur filerna under kalla; "
        f"namn_andrat är härlett genom att jämföra filerna. {SSD_NOTE}",
    }


def resolve_municipality(query: str) -> tuple[str, str]:
    """(kommunkod, namn) for a code or a name that matches exactly one municipality."""
    query = query.strip()
    if not query:
        raise InvalidInputError("Ange kommunkod (4 siffror, t.ex. '0180') eller kommunnamn.")
    hits = codes.lookup_region(query, kind="kommun", limit=5)
    # The fuzzy match strips diacritics (Håbo == Habo), so an exact spelling decides when it names one municipality.
    spelled = [hit for hit in hits if hit["namn"].casefold() == query.casefold()]
    exact = spelled if len(spelled) == 1 else [hit for hit in hits if hit["traff"] == "kod" or hit["likhet"] == 1.0]
    if len(exact) == 1:
        return exact[0]["kod"], exact[0]["namn"]
    if not hits:
        raise InvalidInputError(f"Okänd kommun {query!r}: ange kommunkod (4 siffror, t.ex. '0180') eller kommunnamn.")
    options = ", ".join(f"{hit['namn']} ({hit['kod']})" for hit in hits)
    raise InvalidInputError(f"Kommunen {query!r} är inte entydig. Menade du: {options}? Ange gärna kommunkoden.")


def _regso_options(members: dict[str, tuple[str, ...]], names: dict[str, str]) -> str:
    listed = sorted(members)
    text = ", ".join(f"{code} {names[code]}" for code in listed[:MAX_OPTIONS])
    if len(listed) > MAX_OPTIONS:
        text += f" … ({len(listed)} totalt; ref_list_deso utan regso visar alla)"
    return text


def list_deso(
    municipality: str, version: str = "2025", regso: str | None = None, category: str | None = None
) -> dict[str, Any]:
    """All DeSO of a municipality grouped by RegSO, optionally one RegSO (code or exact name) or one category."""
    if version not in VERSIONS:
        raise InvalidInputError(f"Okänd version {version!r}. Välj {' eller '.join(repr(v) for v in VERSIONS)}.")
    if category is not None and category not in ("A", "B", "C"):
        raise InvalidInputError(f"Okänd kategori {category!r}. Välj A, B eller C.")
    code, name = resolve_municipality(municipality)
    doc = _doc(version)
    names: dict[str, str] = doc["regso"]
    members = {r: m for r, m in _regso_members(version).items() if r.startswith(code)}
    if not members:
        raise InvalidInputError(f"Inga DeSO för {name} ({code}) i {labels(version)['deso']}.")
    if regso is not None:
        key = regso.strip()
        if REGSO_CODE.match(key.upper()):
            selected = [key.upper()] if key.upper() in members else []
        else:
            selected = [r for r in members if names[r].casefold() == key.casefold()]
        if not selected:
            raise InvalidInputError(
                f"RegSO {regso!r} finns inte i {name} ({code}) i {labels(version)['regso']}. "
                f"RegSO i kommunen: {_regso_options(members, names)}"
            )
        members = {r: members[r] for r in selected}
    if category is not None:
        filtered = {r: tuple(c for c in m if c[4] == category) for r, m in members.items()}
        members = {r: m for r, m in filtered.items() if m}
    all_codes = [c for m in members.values() for c in m]
    return {
        "kommunkod": code,
        "kommun": name,
        "version": labels(version),
        "urval": {"regso": regso, "kategori": category},
        "antal": len(all_codes),
        "per_kategori": {k: sum(1 for c in all_codes if c[4] == k) for k in "ABC"},
        "regso": [{"kod": r, "namn": names[r], "antal": len(m), "deso": list(m)} for r, m in sorted(members.items())],
        "kalla": source_block([version], with_log=False),
        "verifiering": SOURCE_DATA,
        "verifiering_not": "Koder och RegSO-namn är SCB:s egna ur filen under kalla; kommunnamnet kommer från "
        "kodlistan regioner.",
    }
