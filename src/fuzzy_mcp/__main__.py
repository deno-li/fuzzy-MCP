# SPDX-FileCopyrightText: 2026 Deniz Özer
#
# SPDX-License-Identifier: MIT

"""Command line entry point: ``fuzzy-mcp`` or ``python -m fuzzy_mcp``."""

import argparse
import logging
import os
import sys
from typing import TYPE_CHECKING, Any, NoReturn

from . import __version__

if TYPE_CHECKING:
    from mcp.server.transport_security import TransportSecuritySettings

TRANSPORTS = ("stdio", "streamable-http", "sse")
TRUE_VALUES = ("1", "true", "ja", "yes", "on")
FALSE_VALUES = ("", "0", "false", "nej", "no", "off")


def env_flag(name: str) -> bool:
    """A boolean environment variable. An unknown value is a configuration error, not a silent "false"."""
    value = os.environ.get(name, "").strip().lower()
    if value in TRUE_VALUES:
        return True
    if value in FALSE_VALUES:
        return False
    raise ValueError(f"{name}={value!r} är ogiltigt – använd true eller false")


def check_ca_settings() -> None:
    """The HTTP client loads SSL_CERT_FILE (or else SSL_CERT_DIR) on the first call to an agency; a missing,
    unreadable or non-PEM file would then fail every tool with an unclear error. Stop at start instead."""
    import ssl

    if cafile := os.environ.get("SSL_CERT_FILE"):
        try:
            ssl.create_default_context(cafile=cafile)
        except OSError as exc:  # also ssl.SSLError: not PEM, no certificates
            config_error(
                f"SSL_CERT_FILE={cafile!r} kan inte användas ({exc}) – ange en komplett CA-bundle i PEM-format"
            )
    elif (capath := os.environ.get("SSL_CERT_DIR")) and not any(map(os.path.isdir, capath.split(os.pathsep))):
        # OpenSSL accepts a list of directories (':' or ';' on Windows) and skips those that do not exist.
        config_error(f"SSL_CERT_DIR={capath!r} innehåller ingen katalog – rätta eller ta bort variabeln")


def config_error(message: str) -> NoReturn:
    """Stop with exit code 2, which service managers are told not to restart on (NSSM AppExit, systemd)."""
    print(f"Konfigurationsfel: {message}", file=sys.stderr)
    raise SystemExit(2)


def http_settings(args: argparse.Namespace) -> None:
    """HTTP-only settings: values given on the command line win; otherwise the environment decides, and must be
    unambiguous. Not checked for stdio, where they do not apply."""
    try:
        if args.allow_unauthenticated is None:
            args.allow_unauthenticated = env_flag("FUZZY_MCP_ALLOW_UNAUTHENTICATED")
        if args.stateless is None:
            args.stateless = env_flag("FUZZY_MCP_STATELESS")
    except ValueError as exc:
        config_error(str(exc))
    if args.port is None:
        raw_port = os.environ.get("FUZZY_MCP_PORT", "").strip() or "8000"
        args.port = int(raw_port) if raw_port.isascii() and raw_port.isdigit() else -1
        if not 0 < args.port < 65536:
            config_error(f"FUZZY_MCP_PORT={raw_port!r} är ogiltigt – ange ett portnummer 1–65535")
    elif not 0 < args.port < 65536:
        config_error(f"--port {args.port} är ogiltigt – ange ett portnummer 1–65535")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="fuzzy-mcp",
        description="MCP-server för svensk öppen data (SCB, Skolverket, Folkhälsomyndigheten, dataportal.se)",
    )
    parser.add_argument(
        "--transport",
        choices=TRANSPORTS,
        default=os.environ.get("FUZZY_MCP_TRANSPORT", "stdio"),
        help="Transport (standard: stdio).",
    )
    parser.add_argument("--host", default=os.environ.get("FUZZY_MCP_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, help="Port för HTTP (standard: 8000).")
    parser.add_argument(
        "--sources",
        help="Kommaseparerad lista med källor att aktivera: scb, fohm, skolverket, dataportal, reference "
        "eller enskilda delkällor (scb.statistik, scb.geodata, skolverket.skolenhetsregistret, "
        "skolverket.syllabus, skolverket.planerad, skolverket.susa, skolverket.statistik). "
        "Åsidosätter FUZZY_MCP_SOURCES.",
    )
    parser.add_argument(
        "--allowed-hosts",
        default=os.environ.get("FUZZY_MCP_ALLOWED_HOSTS", ""),
        help="Kommaseparerade värdnamn som får anropa HTTP-transporten (skydd mot DNS-rebinding), "
        "t.ex. 'mcp.exempel.se'. Utan värde gäller SDK:ns standard (endast localhost vid --host 127.0.0.1).",
    )
    parser.add_argument(
        "--allowed-origins",
        default=os.environ.get("FUZZY_MCP_ALLOWED_ORIGINS", ""),
        help="Kommaseparerade Origin-värden för webbläsarklienter, t.ex. 'https://app.exempel.se'. "
        "Localhost-origins tillåts alltid.",
    )
    parser.add_argument(
        "--auth-token-file",
        default=os.environ.get("FUZZY_MCP_AUTH_TOKEN_FILE", ""),
        help="Fil med token(s) som HTTP-klienter måste skicka (Authorization: Bearer … eller API-nyckelheadern). "
        "En token per rad gör det möjligt att rotera. Alternativ: FUZZY_MCP_AUTH_TOKEN.",
    )
    parser.add_argument(
        "--api-key-header",
        default=os.environ.get("FUZZY_MCP_AUTH_HEADER", "X-API-Key"),
        help="Header för API-nyckel som alternativ till Bearer (standard: X-API-Key).",
    )
    parser.add_argument(
        "--allow-unauthenticated",
        action="store_true",
        default=None,
        help="Tillåt HTTP utan token (lokal utveckling, eller bakom en proxy som själv autentiserar).",
    )
    parser.add_argument(
        "--stateless",
        action="store_true",
        default=None,
        help="Streamable HTTP utan sessioner (för flera instanser bakom lastbalanserare utan sticky sessions).",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)
    # argparse checks choices only for values given on the command line, not for defaults from the environment.
    if args.transport not in TRANSPORTS:
        config_error(f"FUZZY_MCP_TRANSPORT={args.transport!r} är ogiltigt – välj bland {', '.join(TRANSPORTS)}")
    if args.transport != "stdio":
        http_settings(args)

    if args.sources:
        os.environ["FUZZY_MCP_SOURCES"] = args.sources

    from .config import Settings
    from .server import build_server

    check_ca_settings()
    try:
        settings = Settings.from_env()
    except ValueError as exc:
        print(f"Konfigurationsfel: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    server = build_server(settings)
    if args.transport == "stdio":
        server.run("stdio")
        return

    from .auth import AuthConfigError, load_tokens

    try:
        tokens = load_tokens(args.auth_token_file or None, os.environ.get("FUZZY_MCP_AUTH_TOKEN") or None)
    except AuthConfigError as exc:
        print(f"Konfigurationsfel: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    security = transport_security(args.allowed_hosts, args.allowed_origins)
    local = args.host in ("127.0.0.1", "localhost", "::1")
    if security is None and not local:
        print(
            f"Varning: --host {args.host} utan --allowed-hosts stänger av skyddet mot DNS-rebinding. "
            "Ange de värdnamn klienterna använder med --allowed-hosts (FUZZY_MCP_ALLOWED_HOSTS).",
            file=sys.stderr,
        )
    # Always a token over HTTP: even on 127.0.0.1 a reverse proxy on the same host (which may rewrite the Host
    # header to 127.0.0.1) can make the server reachable from the network, and the server cannot tell.
    if not tokens:
        if not args.allow_unauthenticated:
            print(
                "Konfigurationsfel: HTTP-transporten kräver en token – annars kan alla som når servern, även via en "
                "proxy, använda den. Sätt FUZZY_MCP_AUTH_TOKEN_FILE (se docs/DRIFT.md). Lokal utveckling eller en "
                "proxy som själv autentiserar: --allow-unauthenticated (FUZZY_MCP_ALLOW_UNAUTHENTICATED=true).",
                file=sys.stderr,
            )
            raise SystemExit(2)
        print(f"Varning: --host {args.host} utan token (uttryckligen tillåtet).", file=sys.stderr)
    serve_http(server, args, security, tokens)


def build_http_app(server: Any, args: argparse.Namespace, security: Any, tokens: list[str]) -> Any:
    """The Starlette app for the chosen HTTP transport, wrapped in token authentication when tokens are set."""
    if args.transport == "streamable-http":
        app = server.streamable_http_app(transport_security=security, host=args.host, stateless_http=args.stateless)
    elif args.transport == "sse":
        app = server.sse_app(transport_security=security, host=args.host)
    else:
        raise ValueError(f"Okänd HTTP-transport: {args.transport}")
    if tokens:
        from .auth import TokenAuthMiddleware

        app = TokenAuthMiddleware(app, tokens, api_key_header=args.api_key_header)
    return app


def serve_http(server: Any, args: argparse.Namespace, security: Any, tokens: list[str]) -> None:
    import anyio
    import uvicorn

    app = build_http_app(server, args, security, tokens)
    # Keep the shared HTTP client, cache and rate limiter for the whole process (SSE enters the server
    # lifespan per connection); release it after shutdown so the client is closed cleanly.
    services = server.fuzzy_services
    services.acquire()
    try:
        uvicorn.run(
            app,
            host=args.host,
            port=args.port,
            log_level=server.settings.log_level.lower(),
            log_config=uvicorn_log_config(),
            ws="none",  # no WebSocket endpoints; uvicorn would otherwise log upgrade requests with their query string
        )
    finally:
        anyio.run(services.release)


def uvicorn_log_config() -> dict[str, Any]:
    """uvicorn's default logging, but with the access log on stderr as well (uvicorn writes it to stdout), so a
    service manager that only captures stderr (e.g. NSSM with AppStderr) keeps every request line."""
    import copy

    from uvicorn.config import LOGGING_CONFIG

    config = copy.deepcopy(LOGGING_CONFIG)
    for handler in config["handlers"].values():
        if handler.get("stream") == "ext://sys.stdout":
            handler["stream"] = "ext://sys.stderr"
    config.setdefault("filters", {})["without_query"] = {"()": LogWithoutQuery}
    for handler in config["handlers"].values():
        handler.setdefault("filters", []).append("without_query")
    return config


class LogWithoutQuery(logging.Filter):
    """Log request targets without their query string: a misconfigured client may put a token in the URL.

    uvicorn passes the target as a separate argument: the third of five in the access log (also in absolute form,
    e.g. ``http%3A//host/mcp?…``), and a path starting with '/' in other records (e.g. WebSocket requests)."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if not isinstance(args, tuple):
            return True
        if record.name == "uvicorn.access" and len(args) == 5:
            record.args = tuple(_without_query(arg) if i == 2 else arg for i, arg in enumerate(args))
        else:
            record.args = tuple(
                _without_query(arg) if isinstance(arg, str) and arg.startswith("/") else arg for arg in args
            )
        return True


def _without_query(value: object) -> object:
    return value.split("?", 1)[0] + "?…" if isinstance(value, str) and "?" in value else value


LOCAL_HOSTS = ("127.0.0.1:*", "localhost:*", "[::1]:*")
LOCAL_ORIGINS = ("http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*")


def _split(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def transport_security(allowed_hosts: str, allowed_origins: str = "") -> "TransportSecuritySettings | None":
    """Explicit Host/Origin allowlist for HTTP transports.

    MCPServer enables DNS-rebinding protection for 127.0.0.1/localhost and
    rejects any other Host header with 421; binding to 0.0.0.0 turns the
    protection off. Listing the public host names keeps it on in containers.
    Origins: localhost is always allowed (as in the SDK default), plus any
    browser origins listed explicitly; other Origin headers get 403.
    """
    from mcp.server.transport_security import TransportSecuritySettings

    hosts = _split(allowed_hosts)
    origins = _split(allowed_origins)
    if not hosts and not origins:
        return None
    allowed = [*hosts, *(f"{h}:*" for h in hosts if ":" not in h), *LOCAL_HOSTS]
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=list(dict.fromkeys(allowed)),
        allowed_origins=list(dict.fromkeys([*origins, *LOCAL_ORIGINS])),
    )


if __name__ == "__main__":
    main()
