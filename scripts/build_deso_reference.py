# SPDX-FileCopyrightText: 2026 Deniz Özer
#
# SPDX-License-Identifier: MIT

"""Build the bundled DeSO/RegSO reference data from SCB's three key files (xlsx).

    python -I scripts/build_deso_reference.py --hamtad 2026-10-10 \\
        koppling-deso2018-regso2020_*.xlsx koppling-deso2025-regso2025_*.xlsx deso-historiska-forandringar_*.xlsx

The files are published on SCB's page "Demografiska statistikområden (DeSO)" under öppna geodata (CC0 1.0):
"Koppling DeSO2018 - RegSO2020", "Koppling DeSO2025 - RegSO2025" and "Historiska förändringar i DeSO".
The script reads them with the standard library only, keeps SCB's own column values and terms, records the
sha256 and the date cell of each file, and writes three JSON files to ``src/fuzzy_mcp/reference/data``:
``deso_regso_2018.json``, ``deso_regso_2025.json`` and ``deso_forandringar.json``. Nothing is derived here
beyond counts; comparability rules live in ``fuzzy_mcp.reference.deso``.
"""

import argparse
import datetime
import hashlib
import json
import re
import sys
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "src" / "fuzzy_mcp" / "reference" / "data"
PAGE_URL = "https://www.scb.se/vara-tjanster/oppna-data/oppna-geodata/demografiska-statistikomraden-deso/"
LICENCE = "CC0 1.0"
DESO = re.compile(r"^\d{4}[ABC]\d{4}$")
REGSO = re.compile(r"^\d{4}R\d{3}$")
NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}

# SCB's description of the code (geodata.se metadata for DeSO, as quoted in SCB's guide to the WMS/WFS services,
# 2025-03-03): position 1–4 kommunkod, position 5 kategori, 6–8 löpnummer, 9 reservplats.
CATEGORIES = {
    "A": "Områden som till största del ligger utanför större befolkningskoncentrationer eller tätorter.",
    "B": "Områden som till största delen ligger i befolkningskoncentrationer eller tätorter men som inte är en "
    "centralort.",
    "C": "Områden som till största delen finns i kommunens centralort.",
}
CODE_NOTE = (
    "Nio positioner: kommunkod (1–4), kategori A/B/C (5), löpnummer som sorterar områdena geografiskt (6–8) och en "
    "reservplats för framtida delningar (9). DeSO har inga namn. RegSO: kommunkod + R + tre siffror; RegSO har namn, "
    "men enligt SCB är ett RegSO unikt bara tillsammans med kommunen, och namnen kan ändras över tid."
)
FILE_MARKERS = {
    "fil": {
        "verifiering": "myndighetswebb",
        "verifiering_not": "Filnamn, datum i filen och sha256 från den nedladdade filen; hamtad är nedladdningsdagen.",
    },
    "antal": {"verifiering": "härlett", "verifiering_not": "Räknat ur filen."},
}
HAMTNING = (
    "Nedladdad manuellt från SCB:s sida och konverterad med scripts/build_deso_reference.py. Filens URL är inte "
    "kontrollerad maskinellt; arbetsflödet Källkontroll (gruppen scb-nycklar) hämtar filerna och visar sha256."
)


# ----------------------------------------------------------------------------- xlsx (standard library only)


def _col_index(ref: str) -> int:
    n = 0
    for ch in re.match(r"[A-Z]+", ref).group(0):  # type: ignore[union-attr]
        n = n * 26 + ord(ch) - 64
    return n - 1


def _shared_strings(z: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in z.namelist():
        return []
    root = ET.fromstring(z.read("xl/sharedStrings.xml"))
    return ["".join(t.text or "" for t in si.iter(f"{{{NS['m']}}}t")) for si in root.findall("m:si", NS)]


def xlsx_rows(path: Path) -> list[list[str]]:
    """Rows of the first worksheet as lists of strings (empty cells as '')."""
    with zipfile.ZipFile(path) as z:
        shared = _shared_strings(z)
        root = ET.fromstring(z.read("xl/worksheets/sheet1.xml"))
    rows: list[list[str]] = []
    for row in root.iter(f"{{{NS['m']}}}row"):
        cells: dict[int, str] = {}
        for c in row.findall("m:c", NS):
            kind = c.get("t")
            v = c.find("m:v", NS)
            if kind == "s" and v is not None:
                cells[_col_index(c.get("r", "A"))] = shared[int(v.text or 0)]
            elif kind == "inlineStr":
                inline = c.find("m:is", NS)
                cells[_col_index(c.get("r", "A"))] = (
                    "".join(t.text or "" for t in inline.iter(f"{{{NS['m']}}}t")) if inline is not None else ""
                )
            elif v is not None:
                cells[_col_index(c.get("r", "A"))] = v.text or ""
        if cells:
            rows.append([cells.get(i, "").strip() for i in range(max(cells) + 1)])
    return rows


def excel_date(serial: str) -> str:
    return (datetime.date(1899, 12, 30) + datetime.timedelta(days=int(float(serial)))).isoformat()


def table(rows: list[list[str]]) -> tuple[str, str, list[dict[str, str]]]:
    """(title, date cell as ISO date, records) – the header row is the first row starting with 'Kommun'."""
    title = rows[0][0]
    serial = next((c for c in rows[0][1:] if re.fullmatch(r"\d{5}", c)), "")
    start = next(i for i, r in enumerate(rows) if r and r[0] == "Kommun")
    header = rows[start]
    records = [dict(zip(header, r + [""] * (len(header) - len(r)), strict=False)) for r in rows[start + 1 :]]
    return title, excel_date(serial) if serial else "", [r for r in records if any(r.values())]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ----------------------------------------------------------------------------- build


def municipality_names() -> dict[str, str]:
    doc = json.loads((DATA_DIR / "regioner.json").read_text(encoding="utf-8"))
    return {k["kod"]: k["namn"] for k in doc["kommuner"]}


def file_block(path: Path, date_in_file: str, fetched: str) -> dict[str, Any]:
    return {
        "namn": path.name,
        "datum_i_fil": date_in_file,
        "sha256": sha256(path),
        "hamtad": fetched,
        "hamtning": HAMTNING,
    }


def build_mapping(path: Path, deso_version: str, regso_version: str, fetched: str) -> dict[str, Any]:
    title, date_in_file, records = table(xlsx_rows(path))
    deso_col, regso_name_col, regso_code_col = (
        f"DeSO_{deso_version}",
        f"RegSO_{regso_version}",
        f"RegSOkod_{regso_version}",
    )
    missing = {deso_col, regso_name_col, regso_code_col, "Kommun", "Kommunnamn"} - set(records[0])
    if missing:
        raise SystemExit(f"{path.name}: saknar kolumner {sorted(missing)}; har {list(records[0])}")
    names = municipality_names()
    deso: dict[str, str] = {}
    regso: dict[str, str] = {}
    problems: list[str] = []
    for r in records:
        code, rcode, rname, kommun = r[deso_col], r[regso_code_col], r[regso_name_col], r["Kommun"]
        if not DESO.match(code) or not REGSO.match(rcode) or code[:4] != kommun or rcode[:4] != kommun:
            problems.append(f"oväntad kod: {r}")
            continue
        if code in deso:
            problems.append(f"dubblett: {code}")
        deso[code] = rcode
        if regso.setdefault(rcode, rname) != rname:
            problems.append(f"RegSO {rcode} har två namn: {regso[rcode]!r} och {rname!r}")
        if names.get(kommun) != r["Kommunnamn"]:
            problems.append(f"kommunnamn skiljer sig från regioner.json: {kommun} {r['Kommunnamn']!r}")
    if problems:
        raise SystemExit(f"{path.name}: " + "; ".join(problems[:10]))
    categories = Counter(code[4] for code in deso)
    note = (
        f"Ur SCB:s fil {path.name!r} ({title}, datum i filen {date_in_file}), kolumnerna {deso_col}, {regso_code_col} "
        f"och {regso_name_col}. Kommunnamnen stämmer med kodlistan regioner och är därför inte upprepade här."
    )
    return {
        "id": f"deso_regso_{deso_version}",
        "titel": f"Koppling DeSO {deso_version} – RegSO {regso_version}",
        "beskrivning": (
            f"Varje DeSO {deso_version} med sitt RegSO {regso_version} (kod), och varje RegSO-kod med SCB:s namn. "
            f"Kommunkoden är kodens fyra första tecken. {CODE_NOTE}"
        ),
        "kalla": f"SCB, filen '{title}' för {deso_col} och {regso_version} (xlsx) på sidan Demografiska "
        "statistikområden (DeSO) under öppna geodata.",
        "kalla_url": PAGE_URL,
        "licens": LICENCE,
        "fil": file_block(path, date_in_file, fetched),
        "version": {"deso": f"DeSO {deso_version}", "regso": f"RegSO {regso_version}"},
        "antal": {"deso": len(deso), "regso": len(regso), "kommuner": len({c[:4] for c in deso})},
        "kategorier": [
            {
                "kod": k,
                "namn": f"Kategori {k}",
                "antal": categories.get(k, 0),
                "beskrivning": v,
                "verifiering": "myndighetswebb",
                "verifiering_not": "Antal räknat ur filen. Beskrivningen är SCB:s text om kodens femte position "
                "(metadata på geodata.se, återgiven i SCB:s guide till WMS- och WFS-tjänsterna 2025-03-03).",
            }
            for k, v in CATEGORIES.items()
        ],
        "verifiering_samlingar": {
            **FILE_MARKERS,
            "version": {"verifiering": "myndighetswebb", "verifiering_not": "Enligt filens kolumnrubriker."},
            "deso": {"verifiering": "myndighetswebb", "verifiering_not": note},
            "regso": {"verifiering": "myndighetswebb", "verifiering_not": note},
        },
        "deso": dict(sorted(deso.items())),
        "regso": dict(sorted(regso.items())),
    }


def build_changes(path: Path, fetched: str) -> dict[str, Any]:
    title, date_in_file, records = table(xlsx_rows(path))
    expected = {"Kommun", "Kommunnamn", "Tidigare DeSO", "DeSO", "Förändringstyp", "Datum för förändring"}
    if missing := expected - set(records[0]):
        raise SystemExit(f"{path.name}: saknar kolumner {sorted(missing)}")
    changes = []
    for r in records:
        before, after = r["Tidigare DeSO"], r["DeSO"]
        if not DESO.match(before) or not DESO.match(after):
            raise SystemExit(f"{path.name}: oväntad kod i {r}")
        changes.append([before, after, r["Förändringstyp"], excel_date(r["Datum för förändring"])])
    types = Counter(c[2] for c in changes)
    return {
        "id": "deso_forandringar",
        "titel": "Historiska förändringar i DeSO",
        "beskrivning": (
            "SCB:s logg över förändringar av DeSO: varje rad är ett tidigare DeSO, det DeSO som berörs efter "
            "förändringen, SCB:s förändringstyp och datum. Samma kod före och efter betyder att området finns kvar "
            "med ändrad gräns. En kod kan förekomma på flera rader. Raderna är [fran, till, typ, datum]. Loggen "
            "beskriver koder och gränser, inte "
            "befolkning: att summera statistik över en förändring kräver att alla delar är kända "
            "(se ref_lookup_deso)."
        ),
        "kalla": f"SCB, filen '{title}' (xlsx) på sidan Demografiska statistikområden (DeSO) under öppna geodata.",
        "kalla_url": PAGE_URL,
        "licens": LICENCE,
        "fil": file_block(path, date_in_file, fetched),
        "antal": {"rader": len(changes), "datum": dict(sorted(Counter(c[3] for c in changes).items()))},
        "forandringstyper": [
            {
                "typ": typ,
                "antal": n,
                "verifiering": "myndighetswebb",
                "verifiering_not": "SCB:s egen benämning i filen; antal räknat ur filen.",
            }
            for typ, n in types.most_common()
        ],
        "forandringar_falt": "fran, till, typ, datum",
        "verifiering_samlingar": {
            **FILE_MARKERS,
            "forandringar": {
                "verifiering": "myndighetswebb",
                "verifiering_not": f"Ur SCB:s fil {path.name!r} (datum i filen {date_in_file}); varje rad är "
                "[tidigare DeSO, DeSO, förändringstyp, datum] med SCB:s egna ord. Kommun och kommunnamn är "
                "utelämnade: kommunkoden är kodens fyra första tecken, och tre rader går över en kommungräns.",
            },
        },
        "forandringar": changes,
    }


def write(doc: dict[str, Any], out_dir: Path) -> Path:
    target = out_dir / f"{doc['id']}.json"
    target.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0], allow_abbrev=False)
    parser.add_argument("deso2018", type=Path, help="Koppling DeSO2018 - RegSO2020 (xlsx)")
    parser.add_argument("deso2025", type=Path, help="Koppling DeSO2025 - RegSO2025 (xlsx)")
    parser.add_argument("forandringar", type=Path, help="Historiska förändringar i DeSO (xlsx)")
    parser.add_argument("--hamtad", required=True, help="Datum då filerna laddades ner (ÅÅÅÅ-MM-DD)")
    parser.add_argument("--out", type=Path, default=DATA_DIR, help=f"Målkatalog (standard: {DATA_DIR})")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", args.hamtad):
        parser.error("--hamtad ska vara ÅÅÅÅ-MM-DD")
    docs = [
        build_mapping(args.deso2018, "2018", "2020", args.hamtad),
        build_mapping(args.deso2025, "2025", "2025", args.hamtad),
        build_changes(args.forandringar, args.hamtad),
    ]
    for doc in docs:
        target = write(doc, args.out)
        size = target.stat().st_size // 1024
        print(f"{target.name}: {doc['antal']} ({size} kB, sha256 källfil {doc['fil']['sha256'][:12]}…)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
