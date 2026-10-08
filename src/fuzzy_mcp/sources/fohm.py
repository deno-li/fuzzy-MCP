# SPDX-FileCopyrightText: 2026 Deniz Özer
#
# SPDX-License-Identifier: MIT

"""Folkhälsomyndigheten – Folkhälsodata (PxWeb API v1).

Base: https://fohm-app.folkhalsomyndigheten.se/Folkhalsodata/api/v1/{lang}/
The database is ``A_Folkhalsodata`` with subject folders such as ``A_Mo8``
(Folkhälsan i Sverige), ``B_HLV`` (Nationella folkhälsoenkäten), ``C_HBSC``
(Skolbarns hälsovanor), ``H_Sminet`` (smittsamma sjukdomar) and ``L_Vaccin``.
Variable and value codes are often Swedish words with spaces and å/ä/ö and
must be sent exactly as the metadata lists them. Limits (``?config``,
verified 2026-09-25): 100 000 cells per request, 1 000 calls per 10 s.

Folkhälsodata had no PxWebApi 2 endpoint when verified (``/api/v2/config``
answered 404). When it gets one, set ``FUZZY_MCP_FOHM_API_VERSION=v2`` and
``FUZZY_MCP_FOHM_BASE_URL`` to the v2 address; the ``fohm_*`` tools are then
registered from :data:`SPEC_V2` (the PxWebApi 2 tool set) instead.
"""

from typing import Any

from mcp.server.mcpserver import MCPServer

from ..core import Services
from ..pxweb import v1_tools
from ..pxweb.v1_tools import PxWebV1Source
from ..pxweb.v2_tools import PxWebV2Source, register_pxweb_v2_source

SPEC = PxWebV1Source(
    source="fohm",
    base_url_key="fohm",
    prefix="fohm",
    name="Folkhälsodata",
    publisher="Folkhälsomyndigheten",
    default_database="A_Folkhalsodata",
    default_max_cells=100_000,
    citation="Källa: Folkhälsomyndigheten, Folkhälsodata",
    path_example="A_Folkhalsodata/B_HLV/dPsykhals",
    table_example="hlv1psyxreg.px",
    selection_example="{'Region': ['00','21'], 'Kön': ['1','2'], 'År': ['TOP(3)']}",
    subject_folders={
        "A_Mo8": "Folkhälsan i Sverige – indikatorer för de åtta målområdena och hälsoutfall",
        "ANDTS": "ANDTS-indikatorer (alkohol, narkotika, dopning, tobak, spel)",
        "B_HLV": "Nationella folkhälsoenkäten – Hälsa på lika villkor",
        "C_HBSC": "Skolbarns hälsovanor (HBSC)",
        "D_Antibiotika": "Antibiotikastatistik – försäljning och resistens",
        "E_HALT": "Vårdrelaterade infektioner och antibiotikaanvändning inom särskilt boende (Svenska HALT)",
        "H_Sminet": "Smittsamma sjukdomar (SmiNet), bl.a. covid-19, influensa, TBE",
        "I_miljohalsa": "Miljöhälsoenkäten (vuxna)",
        "I_miljohalsabarn": "Miljöhälsoenkäten barn",
        "K_Swelogs": "Spelundersökningen Swelogs",
        "L_Vaccin": "Vaccinationer",
        "Z_ovrigdata": "Övriga data (arbete, demografi, tandhälsa, BMI m.m.)",
    },
    notes="Regionala enkätdata avser ofta flerårsperioder (t.ex. '2021-2024'); värdetexter för Region har koden först.",
)


# Used when FUZZY_MCP_FOHM_API_VERSION=v2. Table ids and the csv code page are not yet known for
# a PxWebApi 2 Folkhälsodata (code page 1252 matches the v1 csv answers).
SPEC_V2 = PxWebV2Source(
    source="fohm",
    base_url_key="fohm",
    prefix="fohm",
    name="Folkhälsodata",
    publisher="Folkhälsomyndigheten",
    citation=SPEC.citation,
    default_max_cells=100_000,
    csv_encoding=1252,
    table_example="<tabell-id från fohm_search_tables>",
    topics="folkhälsoenkäten, skolbarns hälsovanor, smittsamma sjukdomar, vaccinationer, ANDTS m.m.",
    subject_example="['B_HLV']",
    variables_example="Region, Kön, År",
    selection_example=SPEC.selection_example,
    codelist_example="{'Region': '<kodlistans id från metadata>'}",
    codelist_data_example="{'Region': '<kodlistans id från metadata>'}",
    codelist_id_example="ett id från fältet codelists i metadata",
    notes=SPEC.notes,
)


def normalize_table_path(value: str) -> str:
    return v1_tools.normalize_table_path(value, SPEC.default_database)


def register(server: MCPServer[Any], services: Services) -> None:
    if services.settings.api_version(SPEC.base_url_key) == "v2":
        register_pxweb_v2_source(server, services, SPEC_V2)
    else:
        v1_tools.register_pxweb_v1_source(server, services, SPEC)
