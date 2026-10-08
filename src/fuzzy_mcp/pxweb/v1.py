# SPDX-FileCopyrightText: 2026 Deniz Özer
#
# SPDX-License-Identifier: MIT

"""Client for PxWeb API v1 installations (used by Folkhälsomyndigheten).

URL layout: ``{base}/{lang}/{database}/{level}/.../{table}``. GET on a level
lists nodes, GET on a table returns metadata and POST on a table returns
data. Reference: PxWeb API 1.0 specification (Statistics Sweden).
"""

import re
from typing import Any
from urllib.parse import quote

from pydantic import BaseModel, Field

from ..errors import InvalidInputError, UpstreamError
from ..http import ApiResponse, HttpClient
from .selection import PxValue, PxVariable

_TAGS = re.compile(r"<[^>]+>")
_CONTENTS_CODES = {"contentscode", "tabellinnehall", "tabellinnehåll", "matt", "mått", "measure"}


class PxNode(BaseModel):
    id: str
    type: str = Field(description="l = mapp (nivå), t = tabell, h = rubrik")
    text: str
    path: str = Field(description="Sökväg att använda i nästa anrop (databas/nivå/.../id)")
    updated: str | None = None


class PxSearchHit(BaseModel):
    id: str
    title: str
    path: str = Field(description="Tabellens fullständiga sökväg (databas/nivåer/tabell)")
    published: str | None = None
    score: float | None = None


def clean_text(text: Any) -> str:
    return _TAGS.sub("", str(text or "")).strip()


def split_path(path: str) -> list[str]:
    parts = [p for p in path.strip().strip("/").split("/") if p]
    if any(p in (".", "..") for p in parts):
        raise InvalidInputError(f"Ogiltig sökväg {path!r}")
    return parts


def join_url(base: str, lang: str, parts: list[str]) -> str:
    segments = [quote(lang, safe="")] + [quote(p, safe="") for p in parts]
    return base.rstrip("/") + "/" + "/".join(segments)


def parse_v1_metadata(doc: Any) -> tuple[str, list[PxVariable]]:
    if not isinstance(doc, dict) or not isinstance(doc.get("variables"), list):
        raise UpstreamError("pxweb", "Oväntat metadataformat (saknar 'variables'). Är sökvägen en tabell?")
    variables: list[PxVariable] = []
    for raw in doc["variables"]:
        # PxWeb omits (or nulls) the value list when it exceeds maxValues.
        opaque = not raw.get("values")
        codes = [str(c) for c in raw.get("values") or []]
        texts = [str(t) for t in raw.get("valueTexts") or []]
        if len(texts) != len(codes):
            texts = (texts + codes[len(texts) :])[: len(codes)]
        code = str(raw.get("code", ""))
        label = str(raw.get("text") or code)
        normalized = code.casefold().replace("_", "")
        variables.append(
            PxVariable(
                code=code,
                label=label,
                is_time=bool(raw.get("time", False)),
                is_contents=normalized in _CONTENTS_CODES or label.casefold() in _CONTENTS_CODES,
                is_geo=bool(raw.get("map")) or normalized in {"region", "lan", "län", "kommun"},
                elimination=bool(raw.get("elimination", False)),
                values=[PxValue(code=c, label=t) for c, t in zip(codes, texts, strict=True)],
                opaque=opaque,
            )
        )
    return clean_text(doc.get("title")), variables


_TOP = re.compile(r"^top\((\d+)\)$", re.IGNORECASE)


def _v1_selection(codes: list[str], raw: bool) -> dict[str, Any]:
    """PxWeb v1 filter for one variable. Locally expanded variables always use
    ``item``; unchecked (opaque) variables translate '*' patterns to ``all``
    and a single TOP(n) to ``top``."""
    if raw:
        if codes and all("*" in c for c in codes):
            return {"filter": "all", "values": list(codes)}
        if len(codes) == 1 and (match := _TOP.match(codes[0])):
            return {"filter": "top", "values": [match.group(1)]}
    return {"filter": "item", "values": list(codes)}


def build_v1_query(
    values: dict[str, list[str]], response_format: str = "json-stat2", raw_variables: set[str] | None = None
) -> dict[str, Any]:
    raw_variables = raw_variables or set()
    return {
        "query": [
            {"code": code, "selection": _v1_selection(codes, code in raw_variables)} for code, codes in values.items()
        ],
        "response": {"format": response_format},
    }


class PxWebV1Client:
    def __init__(self, http: HttpClient, base_url: str, source: str) -> None:
        self.http = http
        self.base_url = base_url.rstrip("/")
        self.source = source

    def url(self, lang: str, path: str = "") -> str:
        return join_url(self.base_url, lang, split_path(path))

    async def list_databases(self, lang: str) -> list[dict[str, str]]:
        res = await self.http.get_json(self.source, self.url(lang) + "/")
        if not isinstance(res.data, list):
            raise UpstreamError(self.source, "Oväntat svar vid listning av databaser", url=res.url)
        return [{"id": str(d.get("dbid", d.get("id", ""))), "text": clean_text(d.get("text"))} for d in res.data]

    async def list_level(self, lang: str, path: str) -> list[PxNode]:
        parts = split_path(path)
        if not parts:
            raise InvalidInputError("Ange minst en databas, t.ex. 'A_Folkhalsodata'")
        res = await self.http.get_json(self.source, join_url(self.base_url, lang, parts))
        if not isinstance(res.data, list):
            raise InvalidInputError(f"{path!r} är en tabell, inte en mapp – använd metadataverktyget i stället")
        prefix = "/".join(parts)
        return [
            PxNode(
                id=str(node.get("id", "")),
                type=str(node.get("type", "")),
                text=clean_text(node.get("text")),
                path=f"{prefix}/{node.get('id', '')}",
                updated=node.get("updated"),
            )
            for node in res.data
        ]

    async def search(self, lang: str, path: str, query: str, *, search_filter: str | None = None) -> list[PxSearchHit]:
        parts = split_path(path)
        if not parts:
            raise InvalidInputError("Ange databas att söka i")
        params: dict[str, Any] = {"query": query}
        if search_filter:
            params["filter"] = search_filter
        res = await self.http.get_json(self.source, join_url(self.base_url, lang, parts), params=params)
        if not isinstance(res.data, list):
            raise UpstreamError(self.source, "Oväntat svar från sökningen", url=res.url)
        # Hit paths are relative to the searched node: table = {node}{path}/{id}.
        node = "/".join(parts)
        hits = []
        for hit in res.data:
            hit_path = str(hit.get("path", "")).strip("/")
            full = "/".join(p for p in (node, hit_path, str(hit.get("id", ""))) if p)
            hits.append(
                PxSearchHit(
                    id=str(hit.get("id", "")),
                    title=clean_text(hit.get("title")),
                    path=full,
                    published=hit.get("published"),
                    score=hit.get("score"),
                )
            )
        return hits

    async def get_metadata(self, lang: str, table_path: str) -> tuple[str, list[PxVariable], ApiResponse]:
        res = await self.http.get_json(self.source, self.url(lang, table_path))
        if isinstance(res.data, list):
            raise InvalidInputError(f"{table_path!r} är en mapp, inte en tabell – bläddra vidare")
        title, variables = parse_v1_metadata(res.data)
        return title, variables, res

    async def get_data(
        self,
        lang: str,
        table_path: str,
        values: dict[str, list[str]],
        response_format: str = "json-stat2",
        raw_variables: set[str] | None = None,
    ) -> ApiResponse:
        return await self.http.post_json(
            self.source, self.url(lang, table_path), build_v1_query(values, response_format, raw_variables)
        )

    async def get_config(self, lang: str) -> Any:
        # PxWeb checks for a bare "config" key: "?config=" or "?config=1" do not match.
        res = await self.http.get_json(self.source, self.url(lang) + "/?config")
        return res.data
