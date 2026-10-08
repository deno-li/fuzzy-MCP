# SPDX-FileCopyrightText: 2026 Deniz Özer
#
# SPDX-License-Identifier: MIT

"""Check which PxWeb installations answer PxWebApi 2 (``/api/v2/config``).

Run from a machine with internet access:

    python scripts/check_pxwebapi2.py

When Folkhälsomyndigheten or Skolverket answers, switch the server to v2 with
``FUZZY_MCP_<KÄLLA>_API_VERSION=v2`` and ``FUZZY_MCP_<KÄLLA>_BASE_URL`` (see README).
"""

import sys

import httpx2

CANDIDATES: dict[str, list[str]] = {
    "scb (FUZZY_MCP_SCB_BASE_URL)": ["https://statistikdatabasen.scb.se/api/v2"],
    "fohm (FUZZY_MCP_FOHM_API_VERSION=v2)": [
        "https://fohm-app.folkhalsomyndigheten.se/Folkhalsodata/api/v2",
        "https://fohm-app.folkhalsomyndigheten.se/api/v2",
    ],
    "skolverket_statistik (FUZZY_MCP_SKOLVERKET_STATISTIK_API_VERSION=v2)": [
        "https://statistikdatabasen.skolverket.se/api/v2",
        "https://statistikdatabasen.skolverket.se/PxWeb/api/v2",
    ],
}


def main() -> int:
    found = 0
    with httpx2.Client(timeout=15, headers={"User-Agent": "fuzzy-mcp/check_pxwebapi2"}) as client:
        for source, urls in CANDIDATES.items():
            for base in urls:
                try:
                    response = client.get(f"{base}/config")
                    ok = response.status_code == 200 and "maxDataCells" in response.text
                    version = response.json().get("apiVersion") if ok else None
                except (httpx2.HTTPError, ValueError) as exc:
                    print(f"{source}: {base} – fel: {type(exc).__name__}")
                    continue
                status = f"PxWebApi {version}" if ok else f"HTTP {response.status_code}"
                print(f"{source}: {base} – {status}")
                found += ok
    return 0 if found else 1


if __name__ == "__main__":
    sys.exit(main())
