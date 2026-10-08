# SPDX-FileCopyrightText: 2026 Deniz Özer
#
# SPDX-License-Identifier: MIT

"""Smoke test for an installed fuzzy-mcp server (after installation, deployment or an upgrade).

    python smoke_test.py                                  # HTTP: http://127.0.0.1:8000/mcp
    python smoke_test.py --url https://mcp.exempel.se/mcp
    python smoke_test.py --stdio                          # starts "python -m fuzzy_mcp" itself
    python smoke_test.py --live                           # also one small call per agency API
    python smoke_test.py --url … --token-file token.txt   # server with FUZZY_MCP_AUTH_TOKEN_FILE

Without --live nothing is sent to the agencies' APIs. Exit code 0 = all checks passed, 1 = a check failed.
Only needs the installed fuzzy-mcp package (it uses the MCP client in the same environment).
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from contextlib import AsyncExitStack
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import anyio
from mcp import Client, StdioServerParameters
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

# One small, cheap call per source; each needs the source to be enabled on the server.
LIVE_CALLS: list[tuple[str, dict[str, Any]]] = [
    ("scb_get_config", {}),
    ("scb_geodata_layers", {"search": "deso"}),
    ("fohm_list_databases", {}),
    ("skolverket_stat_list_databases", {}),
    ("skolverket_search_school_units", {"municipality_code": ["2180"], "limit": 1}),
    ("skolverket_list_subjects", {"schooltype": "GY", "limit": 1}),
    ("dataportal_search_datasets", {"query": "skola", "limit": 1}),
]


class Report:
    def __init__(self) -> None:
        self.failed = 0

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        self.failed += 0 if ok else 1
        print(f"{'OK  ' if ok else 'FEL '} {name}" + (f" – {detail}" if detail else ""), flush=True)

    def skip(self, name: str, detail: str) -> None:
        print(f"-    {name} – {detail}", flush=True)


def read_token(args: argparse.Namespace) -> str | None:
    if args.token_file:
        if args.token_file == "-":
            text = sys.stdin.read()
        else:
            with open(args.token_file, encoding="utf-8-sig") as handle:
                text = handle.read()
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        return next((line for line in lines if not line.startswith("#")), None)
    return os.environ.get("FUZZY_MCP_AUTH_TOKEN") or None


LOOPBACK = ("127.0.0.1", "localhost", "::1")


def bypass_proxy_for_loopback(url: str) -> None:
    """A proxy from the environment (HTTP(S)_PROXY) cannot reach this machine's loopback: never route it there."""
    if (urlsplit(url).hostname or "") in LOOPBACK:
        for var in ("NO_PROXY", "no_proxy"):
            os.environ[var] = ",".join(filter(None, [os.environ.get(var, ""), ",".join(LOOPBACK)]))


def healthz_url(mcp_url: str) -> str:
    parts = urlsplit(mcp_url)
    return urlunsplit((parts.scheme, parts.netloc, "/healthz", "", ""))


def check_healthz(url: str, report: Report) -> None:
    # Loopback never through a proxy (urllib does not match "[::1]" against NO_PROXY, so bypass it explicitly).
    handlers = [urllib.request.ProxyHandler({})] if (urlsplit(url).hostname or "") in LOOPBACK else []
    try:
        with urllib.request.build_opener(*handlers).open(url, timeout=10) as response:
            body = json.loads(response.read().decode("utf-8"))
        report.check("GET /healthz", body.get("status") == "ok")
    except (urllib.error.URLError, ValueError, OSError) as exc:
        report.check("GET /healthz", False, str(exc))


async def run(args: argparse.Namespace) -> int:
    async with AsyncExitStack() as stack:
        return await _run(args, stack)


async def _run(args: argparse.Namespace, stack: AsyncExitStack) -> int:
    report = Report()
    if args.stdio:
        # The SDK passes only a few "safe" variables to a stdio server; pass them all, so that HTTPS_PROXY,
        # SSL_CERT_FILE and FUZZY_MCP_* reach the server as they do under a real client. --transport stdio wins over
        # a FUZZY_MCP_TRANSPORT=streamable-http in the environment (e.g. inside the container).
        server = ["-m", "fuzzy_mcp", "--transport", "stdio", *args.server_args]
        target: Any = StdioServerParameters(command=sys.executable, args=server, env=dict(os.environ))
    else:
        bypass_proxy_for_loopback(args.url)
        target = args.url
        token = read_token(args)
        if token:
            header = {"Authorization": f"Bearer {token}"} if not args.api_key_header else {args.api_key_header: token}
            http_client = await stack.enter_async_context(create_mcp_http_client(headers=header))
            target = streamable_http_client(args.url, http_client=http_client)
        check_healthz(healthz_url(args.url), report)

    started = time.monotonic()
    client = await stack.enter_async_context(Client(target, read_timeout_seconds=args.timeout))
    report.check("initialize", client.server_info.name == "fuzzy-mcp", f"{client.server_info.name}")
    names = {tool.name for tool in (await client.list_tools()).tools}
    report.check("tools/list", bool(names), f"{len(names)} verktyg")
    if "fuzzy_list_sources" in names:
        result = await client.call_tool("fuzzy_list_sources", {})
        report.check("fuzzy_list_sources", not result.is_error)
    if "ref_lookup_region" in names:
        result = await client.call_tool("ref_lookup_region", {"query": "Gävle", "kind": "kommun", "limit": 1})
        text = "".join(getattr(c, "text", "") for c in result.content)
        report.check("ref_lookup_region (lokalt, utan nätverk)", not result.is_error and "2180" in text)
    else:
        report.skip("ref_lookup_region", "källan reference är inte aktiverad")
    if args.live:
        for tool, arguments in LIVE_CALLS:
            if tool not in names:
                report.skip(tool, "källan är inte aktiverad")
                continue
            result = await client.call_tool(tool, arguments)
            text = "".join(getattr(c, "text", "") for c in result.content)
            report.check(f"{tool} (live)", not result.is_error, "" if not result.is_error else text[:200])
    print(f"Klart på {time.monotonic() - started:.1f} s: {report.failed} fel.")
    return 1 if report.failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default="http://127.0.0.1:8000/mcp", help="MCP-endpoint (HTTP-transport)")
    parser.add_argument("--stdio", action="store_true", help="Starta servern själv över stdio i stället för --url")
    parser.add_argument("--live", action="store_true", help="Gör även ett litet anrop per myndighets-API")
    parser.add_argument(
        "--token-file",
        default=os.environ.get("FUZZY_MCP_AUTH_TOKEN_FILE", ""),
        help='Fil med token (första raden används, "-" = stdin); alternativt FUZZY_MCP_AUTH_TOKEN',
    )
    parser.add_argument("--api-key-header", default="", help="Skicka token i denna header i stället för Bearer")
    parser.add_argument("--timeout", type=float, default=60, help="Sekunder att vänta på svar")
    parser.add_argument(
        "server_args", nargs="*", help="Argument till servern vid --stdio, t.ex. -- --sources reference,scb"
    )
    args = parser.parse_args()
    try:
        return anyio.run(run, args)
    except Exception as exc:  # report connection problems as a failed check, not a traceback
        while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
            exc = exc.exceptions[0]
        text = str(exc)
        hint = ""
        if "401" in text or "error response" in text:
            hint = " (kontrollera token: --token-file eller FUZZY_MCP_AUTH_TOKEN)"
        elif "421" in text:
            hint = " (värdnamnet i --url saknas i FUZZY_MCP_ALLOWED_HOSTS)"
        elif "403" in text:
            hint = " (Origin avvisas – se FUZZY_MCP_ALLOWED_ORIGINS)"
        print(f"FEL  anslutning – {type(exc).__name__}: {exc}{hint}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
