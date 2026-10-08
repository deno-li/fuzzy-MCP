# SPDX-FileCopyrightText: 2026 Deniz Özer
#
# SPDX-License-Identifier: MIT

import pytest

from fuzzy_mcp.errors import InvalidInputError, TooLargeError
from fuzzy_mcp.pxweb.selection import PxValue, PxVariable, expand_token, resolve_selection


def var(code, codes, **kw):
    labels = kw.pop("labels", {})
    return PxVariable(
        code=code,
        label=kw.pop("label", code.lower()),
        values=[PxValue(code=c, label=labels.get(c, c)) for c in codes],
        **kw,
    )


REGION = var(
    "Region",
    ["00", "01", "0114", "0180", "1480"],
    elimination=True,
    labels={"00": "Riket", "0180": "Stockholm", "1480": "Göteborg"},
)
KON = var("Kon", ["1", "2"], elimination=True, labels={"1": "män", "2": "kvinnor"})
CONTENTS = var("ContentsCode", ["BE0101N1", "BE0101N2"], is_contents=True)
TID = var("Tid", [str(y) for y in range(2015, 2026)], is_time=True)
VARS = [REGION, KON, CONTENTS, TID]


@pytest.mark.parametrize(
    "token,expected",
    [
        ("TOP(3)", ["2023", "2024", "2025"]),
        ("top(2,1)", ["2023", "2024"]),
        ("BOTTOM(2)", ["2015", "2016"]),
        ("RANGE(2018,2020)", ["2018", "2019", "2020"]),
        ("FROM(2024)", ["2024", "2025"]),
        ("TO(2016)", ["2015", "2016"]),
        ("202*", ["2020", "2021", "2022", "2023", "2024", "2025"]),
        ("201?", [str(y) for y in range(2015, 2020)]),
    ],
)
def test_expand_time_expressions(token, expected):
    assert expand_token(token, TID) == expected


def test_top_on_non_time_takes_first_values():
    assert expand_token("TOP(2)", REGION) == ["00", "01"]
    assert expand_token("BOTTOM(1)", REGION) == ["1480"]


def test_label_and_case_insensitive_fallbacks():
    assert expand_token("stockholm", REGION) == ["0180"]
    assert expand_token("be0101n1", CONTENTS) == ["BE0101N1"]


def test_unknown_code_lists_examples():
    with pytest.raises(InvalidInputError, match=r"Okänd värdekod '9999'.*0180 \(Stockholm\)"):
        expand_token("9999", REGION)


def test_resolve_fills_defaults_and_eliminates():
    res = resolve_selection(VARS, {"region": ["0180", "1480"]})
    assert res.values == {
        "Region": ["0180", "1480"],
        "ContentsCode": ["BE0101N1", "BE0101N2"],
        "Tid": ["2025"],
    }
    assert res.eliminated == ["Kon"]
    assert res.defaulted == {"ContentsCode": ["BE0101N1", "BE0101N2"], "Tid": ["2025"]}
    assert res.cells == 4


def test_resolve_strict_and_limits():
    with pytest.raises(InvalidInputError, match="måste väljas"):
        resolve_selection(VARS, {"Tid": ["TOP(1)"]}, fill_defaults=False)
    with pytest.raises(TooLargeError, match="överskrider"):
        resolve_selection(VARS, {"Region": ["*"], "Kon": ["*"], "Tid": ["*"]}, max_cells=100)
    with pytest.raises(InvalidInputError, match="Okänd variabel 'Alder'"):
        resolve_selection(VARS, {"Alder": ["*"]})
    with pytest.raises(InvalidInputError, match="matchar inga"):
        resolve_selection(VARS, {"Tid": ["19*"]})


def test_opaque_variable_passes_codes_through_and_marks_unknown_cells():
    level = PxVariable(code="level", label="huvudman/skolenhet", values=[], opaque=True)
    tid = var("time", ["2023", "2024"], is_time=True)
    res = resolve_selection([level, tid], {"level": ["2120002338", "2120002338-87313898"]})
    assert res.values["level"] == ["2120002338", "2120002338-87313898"] and res.unvalidated == ["level"]
    assert res.cells == 2 and res.values["time"] == ["2024"]
    res = resolve_selection([level, tid], {"level": ["*"]})
    assert res.cells == -1
    with pytest.raises(InvalidInputError, match="saknar värdelista"):
        resolve_selection([level, tid], {})


def test_large_mandatory_variable_must_be_chosen():
    big = var("Region", [f"{i:04d}" for i in range(150)])
    with pytest.raises(InvalidInputError, match="har 150 värden och måste väljas"):
        resolve_selection([big, TID], {"Tid": ["2025"]})
    assert resolve_selection([big, TID], {"Region": ["*"], "Tid": ["2025"]}).cells == 150
