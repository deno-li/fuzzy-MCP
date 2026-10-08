# SPDX-FileCopyrightText: 2026 Deniz Özer
#
# SPDX-License-Identifier: MIT

"""Tool-facing models shared by the PxWeb based sources (SCB and FoHM)."""

from typing import Any

from pydantic import BaseModel, Field

from .jsonstat import DataTable
from .selection import PxCodelistRef, PxValue, PxVariable, ResolvedSelection


class RelatedLink(BaseModel):
    """A ``link.related`` entry (PxWebApi 2, from the table's META-ID), e.g. statistikens webbsida."""

    href: str
    label: str | None = None
    type: str | None = Field(default=None, description="MIME-typ, oftast text/html")
    relation: str | None = Field(
        default=None, description="T.ex. statistics-homepage, about-statistics eller definitions"
    )
    metaid: str | None = Field(default=None, description="META-ID som länken skapats från")
    category: str | None = Field(default=None, description="Värdekod när länken gäller ett enskilt värde")


class VariableView(BaseModel):
    code: str
    label: str
    kind: str = Field(description="time, contents, geo eller regular")
    elimination: bool = Field(description="True om variabeln kan utelämnas (summeras/elimineras)")
    value_count: int | None = Field(default=None, description="Antal värden (saknas om metadata inte listar dem)")
    values: list[PxValue] = Field(description="Värden (kod + text), ev. trunkerade – se values_truncated")
    values_truncated: bool = False
    values_withheld: bool = Field(
        default=False,
        description=(
            "True om API:t inte listar värdena (för många). Ange koder direkt, '*' eller 'TOP(n)'; "
            "koderna skickas vidare okontrollerade"
        ),
    )
    codelists: list[PxCodelistRef] = Field(default_factory=list)
    links: list[RelatedLink] = Field(default_factory=list, description="Definitioner/klassifikationer (META-ID)")


class TableMetadataView(BaseModel):
    source: str
    table_id: str
    title: str
    updated: str | None = None
    first_period: str | None = None
    last_period: str | None = None
    variables: list[VariableView]
    notes: list[str] = Field(default_factory=list)
    contacts: list[str] = Field(
        default_factory=list, description="Kontaktorganisation; kontaktpersoner bara med include_contacts=true"
    )
    official_statistics: bool | None = None
    links: list[RelatedLink] = Field(
        default_factory=list, description="Länkar till statistikens webbsida, beskrivning m.m. (META-ID)"
    )
    contents_info: dict[str, dict[str, Any]] = Field(
        default_factory=dict,
        description="Per innehållskod: enhet (base), decimaler, referensperiod, måttyp (Stock/Flow/Average), "
        "pristyp och basperiod – användbart för att definiera mått i t.ex. Power BI",
    )
    extra: dict[str, Any] = Field(default_factory=dict)
    selection_hint: str = Field(
        default=(
            "Ange urval som {variabelkod: [värdekoder]}; uttryck: '*', '20*', 'TOP(5)', 'BOTTOM(2)', "
            "'RANGE(a,b)', 'FROM(a)', 'TO(b)'. Variabler med elimination=true kan utelämnas."
        )
    )


class DataResult(BaseModel):
    source: str
    table_id: str
    selection: ResolvedSelection
    data: DataTable
    citation: str = Field(description="Källhänvisning att använda vid redovisning")
    request_url: str
    notices: list[str] = Field(default_factory=list, description="Ev. Deprecation/Sunset-meddelanden från API:t")


def variable_kind(variable: PxVariable) -> str:
    if variable.is_time:
        return "time"
    if variable.is_contents:
        return "contents"
    if variable.is_geo:
        return "geo"
    return "regular"


def to_view(variable: PxVariable, max_values: int, links: list[dict[str, Any]] | None = None) -> VariableView:
    values = variable.values if max_values <= 0 else variable.values[:max_values]
    return VariableView(
        code=variable.code,
        label=variable.label,
        kind=variable_kind(variable),
        elimination=variable.elimination,
        value_count=None if variable.opaque else len(variable.values),
        values=values,
        values_truncated=len(values) < len(variable.values),
        values_withheld=variable.opaque,
        codelists=variable.codelists,
        links=[RelatedLink(**link) for link in links or []],
    )
