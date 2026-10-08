# SPDX-FileCopyrightText: 2026 Deniz Özer
#
# SPDX-License-Identifier: MIT

"""Reusable query recipes (Power Query M, curl) for PxWeb selections, so the
same selection can be refreshed in Power BI/Excel without the MCP server."""

import json
from typing import Any


def _m_string(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


_M_DELIMITERS = {",": '","', ";": '";"', "\t": '"#(tab)"', " ": '" "'}


def power_query_get_csv(url: str, *, delimiter: str = ",", encoding: int = 28591, skip_title: bool = False) -> str:
    """Power Query M for a GET URL that returns CSV (PxWebApi 2).

    PxWebApi 2 serves csv in the table's code page; SCB answers
    ``text/csv; charset=iso-8859-1`` (code page 28591). ``IncludeTitle``
    adds a title line before the header row, which is skipped here.
    """
    m_delimiter = _M_DELIMITERS.get(delimiter, _M_DELIMITERS[","])
    source = (
        f"Csv.Document(Web.Contents({_m_string(url)}), [Delimiter={m_delimiter}, "
        f"Encoding={encoding}, QuoteStyle=QuoteStyle.Csv])"
    )
    steps = [f"    Källa = {source}"]
    previous = "Källa"
    if skip_title:
        steps.append("    UtanTitel = Table.Skip(Källa, 1)")
        previous = "UtanTitel"
    steps.append(f"    Rubriker = Table.PromoteHeaders({previous}, [PromoteAllScalars=true])")
    return "let\n" + ",\n".join(steps) + "\nin\n    Rubriker"


def power_query_post_jsonstat(url: str, body: dict[str, Any]) -> str:
    """Power Query M for a PxWeb v1 POST returning CSV (format 'csv')."""
    csv_body = dict(body, response={"format": "csv"})
    payload = json.dumps(csv_body, ensure_ascii=False, separators=(",", ":"))
    return (
        "let\n"
        f"    Fråga = {_m_string(payload)},\n"
        f"    Svar = Web.Contents({_m_string(url)}, [Content = Text.ToBinary(Fråga, 65001), "
        'Headers = [#"Content-Type" = "application/json; charset=utf-8"]]),\n'
        '    Källa = Csv.Document(Svar, [Delimiter=",", Encoding=1252, QuoteStyle=QuoteStyle.Csv]),\n'
        "    Rubriker = Table.PromoteHeaders(Källa, [PromoteAllScalars=true])\n"
        "in\n"
        "    Rubriker"
    )


def curl_post(url: str, body: dict[str, Any]) -> str:
    payload = json.dumps(body, ensure_ascii=False, separators=(",", ":")).replace("'", "'\\''")
    return f"curl -s -X POST -H 'Content-Type: application/json; charset=utf-8' -d '{payload}' '{url}'"
