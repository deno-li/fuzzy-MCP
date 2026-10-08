# SPDX-FileCopyrightText: 2026 Deniz Özer
#
# SPDX-License-Identifier: MIT

from fuzzy_mcp.pxweb.jsonstat import jsonstat_to_table

DATASET = {
    "version": "2.0",
    "class": "dataset",
    "label": "Folkmängd efter region, kön och år",
    "source": "SCB",
    "updated": "2026-02-21T07:00:00Z",
    "id": ["Region", "Kon", "ContentsCode", "Tid"],
    "size": [2, 2, 1, 2],
    "role": {"time": ["Tid"], "geo": ["Region"], "metric": ["ContentsCode"]},
    "dimension": {
        "Region": {
            "label": "region",
            "category": {"index": {"0180": 0, "1480": 1}, "label": {"0180": "Stockholm", "1480": "Göteborg"}},
        },
        "Kon": {"label": "kön", "category": {"index": ["1", "2"], "label": {"1": "män", "2": "kvinnor"}}},
        "ContentsCode": {
            "label": "tabellinnehåll",
            "category": {
                "index": {"BE0101N1": 0},
                "label": {"BE0101N1": "Folkmängd"},
                "unit": {"BE0101N1": {"base": "antal", "decimals": 0}},
            },
        },
        "Tid": {
            "label": "år",
            "category": {"index": {"2024": 0, "2025": 1}, "label": {"2024": "2024", "2025": "2025"}},
            "note": ["Avser 31 december"],
        },
    },
    "value": [10, 11, 12, 13, 20, 21, None, 23],
    "status": {"6": ".."},
    "extension": {"status": {"..": "Uppgift inte tillgänglig"}},
}


def test_flattens_row_major_with_labels():
    table = jsonstat_to_table(DATASET)
    assert table.columns == ["Region", "Kon", "ContentsCode", "Tid", "value", "status"]
    assert table.total_rows == 8
    assert table.rows[0] == ["Stockholm", "män", "Folkmängd", "2024", 10, None]
    assert table.rows[1] == ["Stockholm", "män", "Folkmängd", "2025", 11, None]
    assert table.rows[2] == ["Stockholm", "kvinnor", "Folkmängd", "2024", 12, None]
    assert table.rows[6] == ["Göteborg", "kvinnor", "Folkmängd", "2024", None, ".."]
    assert table.status_legend == {"..": "Uppgift inte tillgänglig"}
    roles = {d.code: d.role for d in table.dimensions}
    assert roles == {"Region": "geo", "Kon": None, "ContentsCode": "metric", "Tid": "time"}
    assert table.dimensions[2].units == {"BE0101N1": {"base": "antal", "decimals": 0}}
    assert "Tid: Avser 31 december" in table.notes


def test_code_and_both_modes():
    assert jsonstat_to_table(DATASET, label_mode="code").rows[0][:4] == ["0180", "1", "BE0101N1", "2024"]
    assert jsonstat_to_table(DATASET, label_mode="both").rows[0][:4] == [
        "0180 Stockholm",
        "1 män",
        "BE0101N1 Folkmängd",
        "2024",
    ]


def test_truncation_and_drop_empty():
    table = jsonstat_to_table(DATASET, max_rows=3)
    assert len(table.rows) == 3 and table.truncated and table.total_rows == 8
    sparse = dict(DATASET, value={"0": 5, "7": 9}, status=None)
    table = jsonstat_to_table(sparse, drop_empty=True)
    assert [r[-1] for r in table.rows] == [5, 9]
    assert table.total_rows == 2 and "status" not in table.columns


def test_legacy_jsonstat1_bundle():
    legacy = {
        "dataset": {
            "label": "x",
            "dimension": {
                "id": ["A", "B"],
                "size": [1, 2],
                "role": {"time": ["B"]},
                "A": {"label": "a", "category": {"label": {"a1": "A ett"}}},
                "B": {"label": "b", "category": {"index": {"2020": 0, "2021": 1}}},
            },
            "value": [1.5, 2.5],
        }
    }
    table = jsonstat_to_table(legacy)
    assert table.rows == [["A ett", "2020", 1.5], ["A ett", "2021", 2.5]]
    assert table.dimensions[1].role == "time"


def test_drop_empty_skips_missing_cells_even_with_status_symbol():
    # SCB marks missing values as value=null + status "..".
    dataset = dict(DATASET, value=[5, None, 7, None, 1, 2, 3, 4], status={"1": "..", "3": ".."})
    table = jsonstat_to_table(dataset, drop_empty=True)
    assert [r[-2] for r in table.rows] == [5, 7, 1, 2, 3, 4]
    assert table.total_rows == 6


def test_large_dimension_lists_only_categories_in_returned_rows():
    codes = [f"{i:05d}" for i in range(500)]
    dataset = {
        "class": "dataset",
        "id": ["Region", "Tid"],
        "size": [500, 1],
        "dimension": {
            "Region": {"label": "region", "category": {"index": codes, "label": {c: f"Område {c}" for c in codes}}},
            "Tid": {"label": "år", "category": {"index": ["2024"], "label": {"2024": "2024"}}},
        },
        "value": list(range(500)),
    }
    table = jsonstat_to_table(dataset, max_rows=10)
    region, tid = table.dimensions
    assert region.size == 500 and region.categories_truncated is True
    assert list(region.categories) == codes[:10]
    assert tid.categories == {"2024": "2024"} and tid.categories_truncated is False
    assert len(table.model_dump_json()) < 5_000


def test_many_category_notes_are_capped():
    codes = [f"k{i}" for i in range(60)]
    dataset = {
        "class": "dataset",
        "id": ["K"],
        "size": [60],
        "dimension": {"K": {"label": "k", "category": {"index": codes, "note": {c: [f"not {c}"] for c in codes}}}},
        "value": [1] * 60,
    }
    notes = jsonstat_to_table(dataset).notes
    assert len(notes) == 41 and notes[-1].startswith("… ytterligare 20 fotnoter")
