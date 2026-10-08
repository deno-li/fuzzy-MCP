# SPDX-FileCopyrightText: 2026 Deniz Özer
#
# SPDX-License-Identifier: MIT

"""Skolverkets statistikdatabas (PxWeb API v1).

Base: https://statistikdatabasen.skolverket.se/PxWeb/api/v1/{lang}/ with the
database ``Skolverkets_statistikdatabas`` and two areas:

* ``Kommunala_jamforelsetal`` (KJT): 19 tables in total, one or more per school form, with the
  dimensions ``variable`` (mått, codes "0".."N"), ``level`` (kommun 4 digits,
  län 2 digits, riket ``00``, kommungrupper ``A1``–``C9``, aggregates and some
  huvudmän) and ``time`` (läsår coded by the autumn term year, 2024 = 2024/25,
  or calendar year for förskola/komvux).
* ``Underlag_for_analys_inom_det_nationella_kvalitetssystemet`` (UFA): 11
  tables with the national sub-goals. ``level`` is the huvudman's
  organisationsnummer (10 digits), a school unit as
  ``<organisationsnummer>-<skolenhetskod>`` or ``00`` for riket. Six UFA tables
  publish no code list for ``level`` (over 6 000 codes), so codes are passed
  through unchecked.

Verified against the live API 2026-09-25/28 (``?config``: maxCells 1 250 000,
maxValues 1 000, 10 calls per 10 s). Metadata has no ``elimination`` keys,
so every variable must be selected or is defaulted. Licence: CC0 1.0
according to Sveriges dataportal; the tables are not official statistics.

No PxWebApi 2 endpoint existed when verified (``/api/v2/config`` answered
404). When one does, set ``FUZZY_MCP_SKOLVERKET_STATISTIK_API_VERSION=v2``
and ``FUZZY_MCP_SKOLVERKET_STATISTIK_BASE_URL``; the ``skolverket_stat_*``
tools are then registered from :data:`SPEC_V2`.
"""

from typing import Any

from mcp.server.mcpserver import MCPServer

from ...core import Services
from ...pxweb import v1_tools
from ...pxweb.v1_tools import PxWebV1Source
from ...pxweb.v2_tools import PxWebV2Source, register_pxweb_v2_source

DATABASE = "Skolverkets_statistikdatabas"

SPEC = PxWebV1Source(
    source="skolverket",
    base_url_key="skolverket_statistik",
    prefix="skolverket_stat",
    name="Skolverkets statistikdatabas",
    publisher="Skolverket",
    default_database=DATABASE,
    default_max_cells=1_250_000,
    citation="Källa: Skolverket, Skolverkets statistikdatabas",
    path_example=f"{DATABASE}/Kommunala_jamforelsetal/Grundskola",
    table_example="Grundskola_skolor_elever.px",
    selection_example="{'variable': ['2'], 'level': ['2180', '00'], 'time': ['TOP(5)']}",
    subject_folders={
        "Kommunala_jamforelsetal": "Kommunala jämförelsetal (KJT): förskola, annan pedagogisk verksamhet, grundskola, "
        "fritidshem, gymnasieskola, anpassad gymnasieskola, komvux – per kommun, län, kommungrupp och riket",
        "Underlag_for_analys_inom_det_nationella_kvalitetssystemet": "Underlag för analys (UFA) med nationella "
        "delmål per huvudman (organisationsnummer) och skolenhet (organisationsnummer-skolenhetskod)",
    },
    notes="Dimensionen 'level': kommunkod (4 siffror), länskod (2), riket '00', kommungrupper 'A1'–'C9'; i "
    "UFA-tabeller huvudmannens organisationsnummer eller 'organisationsnummer-skolenhetskod'. 'time' = läsårets "
    "hösttermin (2024 = 2024/25) eller kalenderår. '..' = sekretess, '.' = uppgift saknas. Ej officiell statistik.",
)


# Used when FUZZY_MCP_SKOLVERKET_STATISTIK_API_VERSION=v2 (table ids and csv code page not yet known).
SPEC_V2 = PxWebV2Source(
    source="skolverket",
    base_url_key="skolverket_statistik",
    prefix="skolverket_stat",
    name="Skolverkets statistikdatabas",
    publisher="Skolverket",
    citation=SPEC.citation,
    default_max_cells=1_250_000,
    csv_encoding=1252,
    table_example="<tabell-id från skolverket_stat_search_tables>",
    topics="kommunala jämförelsetal (KJT) och underlag för analys (UFA) per skolform",
    subject_example="['Kommunala_jamforelsetal']",
    variables_example="variable, level, time",
    selection_example=SPEC.selection_example,
    codelist_example="{'level': '<kodlistans id från metadata>'}",
    codelist_data_example="{'level': '<kodlistans id från metadata>'}",
    codelist_id_example="ett id från fältet codelists i metadata",
    notes=SPEC.notes,
)


def register(server: MCPServer[Any], services: Services) -> None:
    if services.settings.api_version(SPEC.base_url_key) == "v2":
        register_pxweb_v2_source(server, services, SPEC_V2)
    else:
        v1_tools.register_pxweb_v1_source(server, services, SPEC)
