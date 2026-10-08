# SPDX-FileCopyrightText: 2026 Deniz Özer
#
# SPDX-License-Identifier: MIT

"""SCB Statistikdatabasen via PxWebApi 2.0.

Base: https://statistikdatabasen.scb.se/api/v2 (production since October
2025). Table ids look like ``TAB638``. The ``/navigation`` endpoint was
removed in production, so the subject tree is rebuilt from ``Table.paths``.
Limits (``/config``): 150 000 cells per request and 30 calls per 10 s.
csv is served as ``text/csv; charset=iso-8859-1``.

The tools come from :mod:`fuzzy_mcp.pxweb.v2_tools`; this module only
describes the installation.
"""

from typing import Any

from mcp.server.mcpserver import MCPServer

from ..core import Services
from ..pxweb.v2_tools import PxWebV2Source, register_pxweb_v2_source

SPEC = PxWebV2Source(
    source="scb",
    base_url_key="scb",
    prefix="scb",
    name="Statistikdatabasen",
    publisher="SCB",
    citation="Källa: SCB, Statistikdatabasen",
    default_max_cells=150_000,
    csv_encoding=28591,
    table_example="TAB638",
    topics="befolkning, utbildningsnivå, inkomster, arbetsmarknad, priser, val m.m.",
    top_subjects="AM, BE, HE, LE, UF ...",
    search_hint=(
        "Sökningen använder AND mellan ord. Svenska facktermer fungerar bäst (t.ex. 'folkmängd' snarare än "
        "'befolkning'). Trunkering med * fungerar på ordstammen ('folkmäng*'). Kontrollera last_period: "
        "SCB fryser ibland tabeller och fortsätter serien i ett nytt TAB-id."
    ),
)


def register(server: MCPServer[Any], services: Services) -> None:
    register_pxweb_v2_source(server, services, SPEC)
