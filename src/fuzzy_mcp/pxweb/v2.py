# SPDX-FileCopyrightText: 2026 Deniz Özer
#
# SPDX-License-Identifier: MIT

"""Client for PxWebApi 2.0 (SCB Statistikdatabasen and any other PxWeb 2 installation).

Spec: https://github.com/PxTools/PxApiSpecs/blob/master/PxAPI-2.yml
Endpoints used: ``/config``, ``/tables``, ``/tables/{id}``,
``/tables/{id}/metadata``, ``/tables/{id}/defaultselection``,
``/tables/{id}/data`` (GET/POST) and ``/codelists/{id}``.
Table metadata is a JSON-stat 2.0 dataset without values.
"""

import re
from collections.abc import Sequence
from typing import Any
from urllib.parse import quote, urlencode

from ..errors import InvalidInputError, UpstreamError
from ..http import ApiResponse, HttpClient
from .selection import PxCodelistRef, PxValue, PxVariable, find_variable

OUTPUT_FORMATS = ("json-stat2", "csv", "xlsx", "px", "html", "json-px", "parquet")
# Research §6: params per format (json-stat2/px/json-px/parquet take none; Separator* only csv).
LABEL_PARAMS = ("UseCodes", "UseTexts", "UseCodesAndTexts")
SEPARATOR_PARAMS = {"SeparatorTab": "\t", "SeparatorSpace": " ", "SeparatorSemicolon": ";"}
FORMATS_WITH_PARAMS = ("csv", "xlsx", "html")

# Table and code-list ids: TAB638, vs_RegionLän07, agg_Ålder10årJ. A word character first rules out
# "." and ".." (httpx would collapse them into another path).
_ID = re.compile(r"^\w[\w.+\-]{0,99}$")


def _table_id(table_id: str) -> str:
    table_id = table_id.strip()
    if not _ID.fullmatch(table_id):
        raise InvalidInputError(f"Ogiltigt id {table_id!r} (exempel: 'TAB638' eller 'vs_RegionLän07')")
    return quote(table_id, safe="")


def validate_output_params(output_format: str, params: Sequence[str] | None) -> list[str]:
    """Check outputFormatParams against the format (the server answers 400 otherwise)."""
    if output_format not in OUTPUT_FORMATS:
        raise InvalidInputError(f"Okänt format {output_format!r}. Giltiga: {', '.join(OUTPUT_FORMATS)}")
    chosen = list(dict.fromkeys(params or []))
    if not chosen:
        return chosen
    if output_format not in FORMATS_WITH_PARAMS:
        raise InvalidInputError(f"outputFormatParams kan inte användas med {output_format} (bara csv, xlsx, html)")
    if len([p for p in chosen if p in LABEL_PARAMS]) > 1:
        raise InvalidInputError(f"Välj högst en av {', '.join(LABEL_PARAMS)}")
    separators = [p for p in chosen if p in SEPARATOR_PARAMS]
    if separators and output_format != "csv":
        raise InvalidInputError("Separator* gäller bara csv")
    if len(separators) > 1:
        raise InvalidInputError(f"Välj högst en av {', '.join(SEPARATOR_PARAMS)}")
    return chosen


def canonical_codelists(variables: Sequence[PxVariable], codelists: dict[str, str] | None) -> dict[str, str]:
    """Key code lists by the metadata's variable code.

    Metadata lookups match variable codes case-insensitively, but the data
    endpoints only apply ``codelist`` on an exact key match, so
    ``{'region': ...}`` must become ``{'Region': ...}``.
    """
    return {find_variable(variables, key).code: value for key, value in (codelists or {}).items()}


def _related_links(raw: Any) -> list[dict[str, Any]]:
    links: list[dict[str, Any]] = []
    related = raw.get("related") if isinstance(raw, dict) else None
    for link in related or []:
        if not isinstance(link, dict) or not link.get("href"):
            continue
        ext = link.get("extension") if isinstance(link.get("extension"), dict) else {}
        links.append(
            {
                "href": str(link["href"]),
                "label": link.get("label"),
                "type": link.get("type"),
                "relation": ext.get("relation"),
                "metaid": ext.get("metaid"),
                "category": ext.get("category"),
            }
        )
    return links


_CONTENTS_KEYS = {
    "refperiod": "refperiod",
    "measuringType": "measuring_type",
    "priceType": "price_type",
    "adjustment": "adjustment",
    "basePeriod": "base_period",
    "alternativeText": "alternative_text",
}


def _contents_info(dim: dict[str, Any], codes: list[str]) -> dict[str, dict[str, Any]]:
    """Unit, reference period and measuring type per contents code (PxWebApi 2 dimension extension)."""
    category = dim.get("category") or {}
    units = category.get("unit") if isinstance(category.get("unit"), dict) else {}
    ext = dim.get("extension") or {}
    out: dict[str, dict[str, Any]] = {}
    for code in codes:
        info: dict[str, Any] = {}
        unit = units.get(code)
        if isinstance(unit, dict):
            info.update({k: unit[k] for k in ("base", "decimals") if unit.get(k) is not None})
        for key, name in _CONTENTS_KEYS.items():
            value = ext.get(key)
            if isinstance(value, dict) and value.get(code) not in (None, ""):
                info[name] = value[code]
        if info:
            out[code] = info
    return out


def parse_v2_metadata(doc: Any) -> tuple[str, list[PxVariable], dict[str, Any]]:
    """Convert a PxWebApi 2 metadata document (JSON-stat 2.0) to variables."""
    if not isinstance(doc, dict) or "dimension" not in doc:
        raise UpstreamError("pxweb2", "Oväntat metadataformat (saknar 'dimension')")
    roles: dict[str, set[str]] = {}
    for role, members in (doc.get("role") or {}).items():
        for member in members or []:
            roles.setdefault(str(member), set()).add(role)

    ids = [str(i) for i in doc.get("id") or doc["dimension"].keys()]
    variables: list[PxVariable] = []
    variable_links: dict[str, list[dict[str, Any]]] = {}
    contents: dict[str, dict[str, Any]] = {}
    for code in ids:
        dim = doc["dimension"].get(code) or {}
        category = dim.get("category") or {}
        index = category.get("index")
        if isinstance(index, dict):
            codes = [c for c, _ in sorted(index.items(), key=lambda kv: kv[1])]
        elif isinstance(index, list):
            codes = [str(c) for c in index]
        else:
            codes = list((category.get("label") or {}).keys())
        labels = category.get("label") or {}
        ext = dim.get("extension") or {}
        codelists = [
            PxCodelistRef(id=str(c.get("id")), label=c.get("label"), type=c.get("type"))
            # Servers on models 2.0.0 (and the beta) spell it "codeLists".
            for c in ext.get("codelists") or ext.get("codeLists") or []
            if isinstance(c, dict) and c.get("id")
        ]
        dim_roles = roles.get(code, set())
        links = _related_links(dim.get("link"))
        if links:
            variable_links[code] = links
        if "metric" in dim_roles:
            contents.update(_contents_info(dim, codes))
        variables.append(
            PxVariable(
                code=code,
                label=str(dim.get("label") or code),
                is_time="time" in dim_roles,
                is_contents="metric" in dim_roles,
                # SCB does not set role.geo; the geography variable is "Region".
                is_geo="geo" in dim_roles or code.casefold() == "region",
                elimination=bool(ext.get("elimination", False)),
                values=[PxValue(code=str(c), label=str(labels.get(c, c))) for c in codes],
                codelists=codelists,
            )
        )

    extension = doc.get("extension") or {}
    px = extension.get("px") or {}
    info: dict[str, Any] = {
        "updated": doc.get("updated"),
        "source": doc.get("source"),
        "first_period": extension.get("firstPeriod"),
        "last_period": extension.get("lastPeriod"),
        "official_statistics": px.get("official-statistics"),
        "notes": [str(n) for n in doc.get("note") or []],
        # Contact persons (name, phone, e-mail) are personal data: tools show them only on request.
        "contacts": [
            str(c.get("raw") or ", ".join(str(v) for k, v in c.items() if k != "raw" and v))
            for c in extension.get("contact") or []
            if isinstance(c, dict)
        ],
        "contact_organizations": list(
            dict.fromkeys(
                str(c["organization"])
                for c in extension.get("contact") or []
                if isinstance(c, dict) and c.get("organization")
            )
        ),
        "px": {
            k: px.get(k)
            for k in (
                "tableid",
                "subject-code",
                "subject-area",
                "survey",
                "updateFrequency",
                "nextUpdate",
                "link",
                "contents",
                "matrix",
            )
            if px.get(k)
        },
        "discontinued": extension.get("discontinued"),
        "links": _related_links(doc.get("link")),
        "variable_links": variable_links,
        "contents": contents,
    }
    return str(doc.get("label") or ""), variables, info


def build_selection_body(
    values: dict[str, list[str]],
    codelists: dict[str, str] | None = None,
    placement: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    codelists = codelists or {}
    body: dict[str, Any] = {
        "selection": [
            {
                "variableCode": code,
                "valueCodes": list(codes),
                **({"codelist": codelists[code]} if code in codelists else {}),
            }
            for code, codes in values.items()
        ]
    }
    if placement:
        body["placement"] = placement
    return body


def build_get_url(
    base_url: str,
    table_id: str,
    values: dict[str, list[str]],
    *,
    lang: str,
    output_format: str = "csv",
    output_format_params: list[str] | None = None,
    codelists: dict[str, str] | None = None,
) -> str:
    """A shareable GET URL for the same selection (for Power BI/Excel/R).

    Expressions containing commas must be bracketed in GET form, e.g.
    ``valueCodes[Tid]=[range(2018,2023)]``; we only emit concrete codes.
    """
    output_params = validate_output_params(output_format, output_format_params)
    params: list[tuple[str, str]] = [("lang", lang), ("outputFormat", output_format)]
    for param in output_params:
        params.append(("outputFormatParams", param))
    for code, codes in values.items():
        # Items containing a comma (RANGE(a,b), TOP(n,o)) must be bracketed in GET form.
        items = [f"[{c}]" if "," in c else c for c in codes]
        params.append((f"valueCodes[{code}]", ",".join(items)))
    for code, codelist in (codelists or {}).items():
        params.append((f"codelist[{code}]", codelist))
    return f"{base_url.rstrip('/')}/tables/{_table_id(table_id)}/data?{urlencode(params, safe='[],*()')}"


def build_post_url(
    base_url: str, table_id: str, *, lang: str, output_format: str, output_format_params: list[str] | None = None
) -> str:
    output_params = validate_output_params(output_format, output_format_params)
    params: list[tuple[str, str]] = [("lang", lang), ("outputFormat", output_format)]
    params.extend(("outputFormatParams", p) for p in output_params)
    return f"{base_url.rstrip('/')}/tables/{_table_id(table_id)}/data?{urlencode(params)}"


class PxWebV2Client:
    def __init__(self, http: HttpClient, base_url: str, source: str = "scb") -> None:
        self.http = http
        self.base_url = base_url.rstrip("/")
        self.source = source

    async def config(self) -> dict[str, Any]:
        res = await self.http.get_json(self.source, f"{self.base_url}/config")
        return res.data

    async def search_tables(
        self,
        *,
        lang: str,
        query: str | None = None,
        past_days: int | None = None,
        include_discontinued: bool = False,
        page_number: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "lang": lang,
            "query": query or None,
            "pastDays": past_days,
            "includeDiscontinued": include_discontinued,
            "pageNumber": page_number,
            "pageSize": page_size,
        }
        res = await self.http.get_json(self.source, f"{self.base_url}/tables", params=params)
        return res.data

    async def get_table(self, table_id: str, *, lang: str) -> dict[str, Any]:
        res = await self.http.get_json(
            self.source, f"{self.base_url}/tables/{_table_id(table_id)}", params={"lang": lang}
        )
        return res.data

    async def get_metadata(
        self, table_id: str, *, lang: str, codelists: dict[str, str] | None = None
    ) -> tuple[str, list[PxVariable], dict[str, Any]]:
        params: dict[str, Any] = {"lang": lang}
        for code, codelist in (codelists or {}).items():
            params[f"codelist[{code}]"] = codelist
        res = await self.http.get_json(
            self.source, f"{self.base_url}/tables/{_table_id(table_id)}/metadata", params=params
        )
        return parse_v2_metadata(res.data)

    async def get_default_selection(self, table_id: str, *, lang: str) -> dict[str, Any]:
        res = await self.http.get_json(
            self.source,
            f"{self.base_url}/tables/{_table_id(table_id)}/defaultselection",
            params={"lang": lang},
        )
        return res.data

    async def get_data(
        self,
        table_id: str,
        values: dict[str, list[str]],
        *,
        lang: str,
        codelists: dict[str, str] | None = None,
        output_format: str = "json-stat2",
    ) -> ApiResponse:
        return await self.http.post_json(
            self.source,
            f"{self.base_url}/tables/{_table_id(table_id)}/data",
            build_selection_body(values, codelists),
            params={"lang": lang, "outputFormat": output_format},
        )

    async def get_codelist(self, codelist_id: str, *, lang: str) -> dict[str, Any]:
        res = await self.http.get_json(
            self.source, f"{self.base_url}/codelists/{_table_id(codelist_id)}", params={"lang": lang}
        )
        return res.data
