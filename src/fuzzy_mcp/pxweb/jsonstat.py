# SPDX-FileCopyrightText: 2026 Deniz Özer
#
# SPDX-License-Identifier: MIT

"""JSON-stat 2.0 (and legacy JSON-stat 1.x bundle) → tabular rows.

Both SCB (PxWebApi 2) and Folkhälsomyndigheten (PxWeb API v1) can answer
data queries in JSON-stat, so this module is the common decoder.

Reference: https://json-stat.org/format/
"""

from typing import Any, Literal

from pydantic import BaseModel, Field

from ..errors import UpstreamError

LabelMode = Literal["label", "code", "both"]

MAX_LISTED_CATEGORIES = 100


class DimensionInfo(BaseModel):
    code: str
    label: str
    role: str | None = Field(default=None, description="time, geo eller metric enligt JSON-stat 'role'")
    size: int
    categories: dict[str, str] = Field(
        description="Värdekod → värdetext i datamängdens ordning (kortad till använda koder om categories_truncated)"
    )
    categories_truncated: bool = Field(
        default=False,
        description="True om dimensionen har fler värden än vad som listas; bara koder i de returnerade raderna ingår",
    )
    units: dict[str, dict[str, Any]] | None = Field(
        default=None,
        description="Enhet per innehållskod (category.unit), t.ex. {'base': 'antal', 'decimals': 0}",
    )


class DataTable(BaseModel):
    """A JSON-stat dataset flattened to rows (one row per cell)."""

    title: str | None = None
    source: str | None = None
    updated: str | None = None
    dimensions: list[DimensionInfo]
    columns: list[str]
    rows: list[list[Any]]
    total_rows: int = Field(description="Antal celler i hela svaret (före trunkering)")
    truncated: bool = False
    notes: list[str] = Field(default_factory=list)
    status_legend: dict[str, str] | None = Field(
        default=None, description="Förklaring av statuskoder (t.ex. '..' = uppgift saknas)"
    )


def _ordered_category_ids(category: dict[str, Any]) -> list[str]:
    index = category.get("index")
    if isinstance(index, list):
        return [str(i) for i in index]
    if isinstance(index, dict):
        return [str(code) for code, _ in sorted(index.items(), key=lambda kv: kv[1])]
    labels = category.get("label")
    if isinstance(labels, dict):
        return [str(code) for code in labels]
    return []


def _normalise(document: Any) -> dict[str, Any]:
    """Return a JSON-stat 2.0 style dataset dict from 2.0 or 1.x input."""
    if not isinstance(document, dict):
        raise UpstreamError("jsonstat", "Svaret är inte ett JSON-stat-objekt")
    if document.get("class") == "dataset":
        return document
    if document.get("class") == "collection":
        items = (document.get("link") or {}).get("item") or []
        if items and isinstance(items[0], dict) and items[0].get("class") == "dataset":
            return items[0]
    # JSON-stat 1.x bundle: {"dataset": {...}} or {"<name>": {...}}
    candidates = [v for v in document.values() if isinstance(v, dict) and "dimension" in v and "value" in v]
    if "dataset" in document and isinstance(document["dataset"], dict):
        candidates = [document["dataset"]]
    if not candidates:
        raise UpstreamError("jsonstat", "Okänt JSON-stat-format (saknar dataset)")
    legacy = candidates[0]
    dimension = dict(legacy.get("dimension") or {})
    ids = dimension.pop("id", None) or []
    sizes = dimension.pop("size", None) or []
    role = dimension.pop("role", None)
    return {
        "class": "dataset",
        "label": legacy.get("label"),
        "source": legacy.get("source"),
        "updated": legacy.get("updated"),
        "id": ids,
        "size": sizes,
        "role": role,
        "dimension": dimension,
        "value": legacy.get("value"),
        "status": legacy.get("status"),
        "note": legacy.get("note"),
        "extension": legacy.get("extension"),
    }


def _cell(values: Any, position: int) -> Any:
    if isinstance(values, list):
        return values[position] if position < len(values) else None
    if isinstance(values, dict):
        return values.get(str(position))
    return values  # a single value/status applies to every cell


def _notes(dataset: dict[str, Any]) -> list[str]:
    notes: list[str] = []
    raw = dataset.get("note")
    if isinstance(raw, list):
        notes.extend(str(n) for n in raw if n)
    elif isinstance(raw, str) and raw:
        notes.append(raw)
    for dim_code, dim in (dataset.get("dimension") or {}).items():
        dim_note = dim.get("note") if isinstance(dim, dict) else None
        if isinstance(dim_note, list):
            notes.extend(f"{dim_code}: {n}" for n in dim_note if n)
        cat_notes = ((dim.get("category") or {}).get("note") or {}) if isinstance(dim, dict) else {}
        if isinstance(cat_notes, dict):
            for code, note in cat_notes.items():
                for text in note if isinstance(note, list) else [note]:
                    if text:
                        notes.append(f"{dim_code}={code}: {text}")
    return notes


def _capped_notes(notes: list[str], limit: int = 40) -> list[str]:
    if len(notes) <= limit:
        return notes
    return [*notes[:limit], f"… ytterligare {len(notes) - limit} fotnoter utelämnade (se tabellens metadata)"]


def jsonstat_to_table(
    document: Any,
    *,
    max_rows: int = 1000,
    label_mode: LabelMode = "label",
    drop_empty: bool = False,
) -> DataTable:
    """Flatten a JSON-stat dataset into rows.

    Each row holds one category per dimension (in the dataset's ``id``
    order, last dimension varying fastest) followed by the value, and the
    status column when the dataset carries status flags.
    """
    dataset = _normalise(document)
    ids: list[str] = [str(i) for i in dataset.get("id") or []]
    sizes: list[int] = [int(s) for s in dataset.get("size") or []]
    dims_raw: dict[str, Any] = dataset.get("dimension") or {}
    if len(ids) != len(sizes):
        raise UpstreamError("jsonstat", "Ogiltig JSON-stat: 'id' och 'size' har olika längd")

    roles: dict[str, str] = {}
    for role_name, members in (dataset.get("role") or {}).items():
        for member in members or []:
            roles[str(member)] = role_name

    dims_meta: list[tuple[str, dict[str, Any], int, dict[str, Any] | None]] = []
    category_ids: list[list[str]] = []
    category_labels: list[dict[str, str]] = []
    for dim_code, size in zip(ids, sizes, strict=True):
        dim = dims_raw.get(dim_code) or {}
        category = dim.get("category") or {}
        cats = _ordered_category_ids(category)
        if len(cats) != size:
            # Single-category dimensions may omit the index entirely.
            cats = cats[:size] if len(cats) > size else cats + [str(i) for i in range(len(cats), size)]
        labels_raw = category.get("label") or {}
        labels = {code: str(labels_raw.get(code, code)) for code in cats}
        units = category.get("unit") if isinstance(category.get("unit"), dict) else None
        dims_meta.append((dim_code, dim, size, units))
        category_ids.append(cats)
        category_labels.append(labels)

    values = dataset.get("value")
    status = dataset.get("status")
    has_status = status is not None and status != {} and status != []

    total = 1
    for size in sizes:
        total *= size
    if not ids:
        total = 1 if values not in (None, [], {}) else 0

    columns = list(ids) + ["value"] + (["status"] if has_status else [])
    rows: list[list[Any]] = []
    used: list[set[int]] = [set() for _ in ids]
    counted = 0
    for position in range(total):
        value = _cell(values, position)
        cell_status = _cell(status, position) if has_status else None
        if drop_empty and value is None:
            continue
        counted += 1
        if len(rows) >= max_rows:
            continue
        row: list[Any] = []
        remainder = position
        coords: list[int] = []
        for size in reversed(sizes):
            coords.append(remainder % size if size else 0)
            remainder = remainder // size if size else 0
        coords.reverse()
        for dim_index, coord in enumerate(coords):
            used[dim_index].add(coord)
            code = category_ids[dim_index][coord]
            label = category_labels[dim_index][code]
            if label_mode == "code":
                row.append(code)
            elif label_mode == "both":
                row.append(code if label == code else f"{code} {label}")
            else:
                row.append(label)
        row.append(value)
        if has_status:
            row.append(cell_status)
        rows.append(row)

    dimensions: list[DimensionInfo] = []
    for dim_index, (dim_code, dim, size, units) in enumerate(dims_meta):
        labels = category_labels[dim_index]
        truncated_categories = size > MAX_LISTED_CATEGORIES
        if truncated_categories:
            cats = category_ids[dim_index]
            labels = {cats[i]: labels[cats[i]] for i in sorted(used[dim_index])[:MAX_LISTED_CATEGORIES]}
        if units and len(units) > MAX_LISTED_CATEGORIES:
            units = {code: unit for code, unit in units.items() if code in labels}
        dimensions.append(
            DimensionInfo(
                code=dim_code,
                label=str(dim.get("label") or dim_code),
                role=roles.get(dim_code),
                size=size,
                categories=labels,
                categories_truncated=truncated_categories and len(labels) < size,
                units=units,
            )
        )

    extension = dataset.get("extension") or {}
    legend = None
    px_ext = extension.get("px") if isinstance(extension, dict) else None
    if isinstance(extension, dict) and isinstance(extension.get("status"), dict):
        legend = {str(k): str(v) for k, v in extension["status"].items()}

    title = dataset.get("label")
    source = dataset.get("source")
    if isinstance(px_ext, dict):
        title = title or px_ext.get("tablename")
    return DataTable(
        title=title,
        source=source,
        updated=dataset.get("updated"),
        dimensions=dimensions,
        columns=columns,
        rows=rows,
        total_rows=counted,
        truncated=counted > len(rows),
        notes=_capped_notes(_notes(dataset)),
        status_legend=legend,
    )
