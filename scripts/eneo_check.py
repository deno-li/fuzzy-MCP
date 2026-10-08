# SPDX-FileCopyrightText: 2026 Deniz Özer
#
# SPDX-License-Identifier: MIT

"""Check that an MCP server works with Eneo's MCP client (MCP Python SDK 1.x over Streamable HTTP).

Run it where Eneo runs, so the network path, the Host header and the token are tested exactly as Eneo will
use them. Eneo's backend image already contains the SDK. Pass the token on stdin so it never lands in Eneo's
container (its backend runs as uid 1000 and /tmp is a shared volume):

    docker cp eneo_check.py eneo_backend:/tmp/eneo_check.py
    sudo cat secrets/fuzzy_mcp_token.txt | docker exec -i eneo_backend \
        python /tmp/eneo_check.py --url http://fuzzy-mcp:8000/mcp --token-file - --server-name oppnadata
    docker exec -u 0 eneo_backend rm -f /tmp/eneo_check.py

Elsewhere: ``pip install "mcp>=1.2,<2"`` (or ``uvx --with "mcp>=1.2,<2" python eneo_check.py …``).
The script only uses the SDK 1.x client API that Eneo uses (``streamable_http_client`` + ``ClientSession``).
It never sends a question to the agencies' APIs; the tool call is answered from bundled reference data.
Exit code 0 = everything OK, 1 = a check failed.
"""

import argparse
import json
import os
import re
import sys
import time
from contextlib import AsyncExitStack
from typing import Any

import anyio
import httpx

try:
    import importlib.metadata as metadata

    SDK_VERSION = metadata.version("mcp")
except Exception:  # pragma: no cover - only for unusual installations
    SDK_VERSION = "okänd"

from mcp import ClientSession

try:  # newer 1.x (the API Eneo uses)
    from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

    HAS_NEW_API = True
except ImportError:  # older 1.x
    from mcp.client.streamable_http import streamablehttp_client

    HAS_NEW_API = False

# Limits Eneo's client applies (see docs/ENEO.md for the source).
MAX_TOOLS = 256
MAX_CATALOGUE_BYTES = 16 * 1024 * 1024
MAX_INITIALIZE_BYTES = 1024 * 1024
TOOL_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MAX_RESULT_CHARS = 32_768  # Eneo cuts a tool result here, measured on the JSON-encoded result
ENVELOPE_CHARS = 100  # Eneo's wrapper around the text, e.g. {"content": [{"type": "text", "text": ...}]}
# The model sees "<server name in lower case>__<tool>"; many model APIs allow at most 64 characters.
MAX_PREFIXED_NAME = 64


class Report:
    def __init__(self) -> None:
        self.failed = 0

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.failed += 0 if ok else 1
        print(f"{'OK  ' if ok else 'FEL '} {name}" + (f" – {detail}" if detail else ""), flush=True)
        return ok

    def info(self, name: str, detail: str) -> None:
        print(f"     {name}: {detail}", flush=True)


def read_token(args: argparse.Namespace) -> str | None:
    """First token line of --token-file ("-" = stdin, e.g. a new token during rotation) or FUZZY_MCP_AUTH_TOKEN."""
    if args.token_file:
        if args.token_file == "-":
            text = sys.stdin.read()
        else:
            with open(args.token_file, encoding="utf-8-sig") as handle:
                text = handle.read()
        lines = [line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith("#")]
        return lines[0] if lines else None
    return os.environ.get("FUZZY_MCP_AUTH_TOKEN") or None


def auth_headers(args: argparse.Namespace) -> dict[str, str]:
    token = read_token(args)
    if not token:
        return {}
    return {args.api_key_header: token} if args.api_key_header else {"Authorization": f"Bearer {token}"}


async def call_tool(session: ClientSession, report: Report, name: str, arguments: dict[str, Any]) -> Any:
    """A tool call; failures after a successful connection are reported as call errors, not connection errors."""
    try:
        return await session.call_tool(name, arguments)
    except Exception as exc:  # e.g. the SDK rejecting structured content
        report.check(f"Verktygsanrop {name}", False, f"{type(exc).__name__}: {exc}")
        return None


async def run(args: argparse.Namespace) -> int:
    report = Report()
    report.info(
        "MCP-SDK i den här miljön",
        f"{SDK_VERSION} ({'streamable_http_client' if HAS_NEW_API else 'streamablehttp_client'})",
    )
    headers = auth_headers(args)
    report.info("Autentisering", "Bearer" if "Authorization" in headers else args.api_key_header or "ingen")

    started = time.monotonic()
    async with AsyncExitStack() as stack:
        if HAS_NEW_API:
            http_client = await stack.enter_async_context(
                create_mcp_http_client(headers=headers, timeout=httpx.Timeout(args.timeout, read=300))
            )
            read, write, _ = await stack.enter_async_context(streamable_http_client(args.url, http_client=http_client))
        else:
            read, write, _ = await stack.enter_async_context(
                streamablehttp_client(args.url, headers=headers, timeout=args.timeout)
            )
        session = await stack.enter_async_context(ClientSession(read, write))
        init = await session.initialize()
        init_size = len(init.model_dump_json(by_alias=True, exclude_none=True).encode("utf-8"))
        report.check(
            "initialize", init.serverInfo.name == "fuzzy-mcp", f"{init.serverInfo.name} {init.serverInfo.version}"
        )
        report.info("Förhandlad protokollversion", str(init.protocolVersion))
        report.check("initialize-svarets storlek", init_size <= MAX_INITIALIZE_BYTES, f"{init_size:,} byte")
        report.check("Servern erbjuder verktyg", init.capabilities.tools is not None)

        tools: list[Any] = []
        cursor = None
        while True:
            page = await session.list_tools(cursor=cursor) if cursor else await session.list_tools()
            tools.extend(page.tools)
            cursor = page.nextCursor
            if not cursor:
                break
        catalogue = len(json.dumps([t.model_dump(by_alias=True, exclude_none=True) for t in tools]).encode("utf-8"))
        report.check("Antal verktyg", 0 < len(tools) <= MAX_TOOLS, f"{len(tools)} (Eneos gräns {MAX_TOOLS})")
        report.check("Verktygskatalogens storlek", catalogue <= MAX_CATALOGUE_BYTES, f"{catalogue:,} byte")
        bad_names = [t.name for t in tools if not TOOL_NAME.match(t.name)]
        report.check(
            "Verktygsnamn",
            not bad_names,
            ", ".join(bad_names[:5]) or f"längst {max(len(t.name) for t in tools)} tecken",
        )
        no_schema = [
            t.name for t in tools if not isinstance(t.inputSchema, dict) or t.inputSchema.get("type") != "object"
        ]
        report.check("Indatascheman", not no_schema, ", ".join(no_schema[:5]))

        names = {t.name for t in tools}
        if "ref_lookup_region" in names:
            result = await call_tool(
                session, report, "ref_lookup_region", {"query": "Gävle", "kind": "kommun", "limit": 1}
            )
            if result is not None:
                text = "".join(getattr(block, "text", "") for block in result.content)
                report.check(
                    "Verktygsanrop ref_lookup_region (utan myndighets-API)", not result.isError and "2180" in text
                )
                report.check("Strukturerat svar", result.structuredContent is not None)
        else:
            report.info("ref_lookup_region", "källan reference är avstängd – inget verktygsanrop provat")
        if "fuzzy_list_sources" in names:
            result = await call_tool(session, report, "fuzzy_list_sources", {})
            if result is not None:
                report.check("Verktygsanrop fuzzy_list_sources", not result.isError)
        if "ref_list_municipalities" in names:
            result = await call_tool(session, report, "ref_list_municipalities", {})
            text = "".join(getattr(block, "text", "") for block in result.content) if result else ""
            size = len(json.dumps(text, ensure_ascii=False)) + ENVELOPE_CHARS
            report.check(
                "Stort svar ryms i Eneos gräns (ref_list_municipalities)",
                result is not None and not result.isError and size <= MAX_RESULT_CHARS,
                f"{size:,} tecken som Eneo räknar dem (gräns {MAX_RESULT_CHARS:,}); "
                + ("ok" if size <= MAX_RESULT_CHARS else "sätt FUZZY_MCP_MAX_OUTPUT_CHARS=30000 på servern"),
            )
        longest = max(len(t.name) for t in tools)
        room = MAX_PREFIXED_NAME - 2 - longest
        report.check(
            "Plats för servernamnet i Eneo",
            len(args.server_name) <= room,
            f"servernamnet '{args.server_name}' får vara högst {room} tecken (längsta verktyg {longest} + '__')",
        )
        if args.show_tools:
            for tool in sorted(tools, key=lambda t: t.name):
                print(f"       {tool.name}")
    print(f"Klart på {time.monotonic() - started:.1f} s: {report.failed} fel.")
    return 1 if report.failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Kontrollera en MCP-server med Eneos klient (MCP SDK 1.x).")
    parser.add_argument(
        "--url", required=True, help="MCP-adressen exakt som den registreras i Eneo, t.ex. http://fuzzy-mcp:8000/mcp"
    )
    parser.add_argument("--token-file", default=os.environ.get("FUZZY_MCP_AUTH_TOKEN_FILE", ""), help="Fil med token")
    parser.add_argument("--api-key-header", default="", help="Skicka token i denna header i stället för Bearer")
    parser.add_argument("--timeout", type=float, default=30, help="Sekunder för anslutning och anrop")
    parser.add_argument(
        "--server-name", default="oppnadata", help="Namnet servern får i Eneo (blir prefix till verktygsnamnen)"
    )
    parser.add_argument("--show-tools", action="store_true", help="Lista verktygen")
    args = parser.parse_args()
    try:
        return anyio.run(run, args)
    except Exception as exc:
        while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
            exc = exc.exceptions[0]
        hint = ""
        text = str(exc)
        if "401" in text:
            hint = " (token saknas eller är fel)"
        elif "421" in text:
            hint = " (värdnamnet i URL:en saknas i FUZZY_MCP_ALLOWED_HOSTS)"
        elif "403" in text:
            hint = " (Origin avvisas – se FUZZY_MCP_ALLOWED_ORIGINS)"
        print(f"FEL  anslutning – {type(exc).__name__}: {exc}{hint}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
