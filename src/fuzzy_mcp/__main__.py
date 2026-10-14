# SPDX-FileCopyrightText: 2026 Deniz Özer
#
# SPDX-License-Identifier: MIT

"""Command line entry point: ``fuzzy-mcp`` or ``python -m fuzzy_mcp``."""

import argparse
import contextlib
import logging
import os
import re
import signal
import sys
import threading
from collections.abc import Iterator
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
    if args.transport == "stdio":
        run(args, None)
        return
    # From here a stop signal (Ctrl+C, SIGTERM) ends the process with exit code 0, also while the tools are built:
    # it used to kill the process without a log line, or end it with a KeyboardInterrupt traceback (#40).
    with stop_on_signals() as stop:
        run(args, stop)


def run(args: argparse.Namespace, stop: "Stop | None") -> None:
    """Start the server for parsed arguments; ``stop`` holds the stop signals for the HTTP transports."""
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
    quiet_logging()
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
    serve_http(server, args, security, tokens, stop)


def build_http_app(server: Any, args: argparse.Namespace, security: Any, tokens: list[str]) -> Any:
    """The Starlette app for the chosen HTTP transport, wrapped in token authentication when tokens are set."""
    if args.transport == "streamable-http":
        app = server.streamable_http_app(transport_security=security, host=args.host, stateless_http=args.stateless)
    elif args.transport == "sse":
        app = OneResponse(server.sse_app(transport_security=security, host=args.host))
    else:
        raise ValueError(f"Okänd HTTP-transport: {args.transport}")
    if tokens:
        from .auth import TokenAuthMiddleware

        app = TokenAuthMiddleware(app, tokens, api_key_header=args.api_key_header)
    return app


class OneResponse:
    """ASGI wrapper that sends one response per request. The MCP SDK's SSE endpoint returns an empty response after
    its event stream, and Starlette sends it; when a shutdown ends the stream mid-response, uvicorn then refuses the
    second ``http.response.start`` and logs "Exception in ASGI application" with a traceback (#40). The second start
    is dropped; its body, which is empty, ends the interrupted stream (or is dropped too if the stream had ended)."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = ended = False

        async def send_once(message: dict[str, Any]) -> None:
            nonlocal started, ended
            if message["type"] == "http.response.start":
                if started:
                    return
                started = True
            elif message["type"] == "http.response.body":
                if ended:
                    return
                ended = not message.get("more_body", False)
            await send(message)

        await self.app(scope, receive, send_once)


STOPPED_BEFORE_START = "Stoppsignal före start – servern startas inte."
# How long the shared HTTP client may take to close after uvicorn has stopped; a stop signal then no longer stops
# anything, so a hanging close would keep the process alive until SIGKILL (#40).
RELEASE_SECONDS = 5.0


def serve_http(
    server: Any, args: argparse.Namespace, security: Any, tokens: list[str], stop: "Stop | None" = None
) -> None:
    import anyio
    import uvicorn

    if stop is None:
        with stop_on_signals() as stop:
            serve_http(server, args, security, tokens, stop)
        return
    if stop.requested:  # stopped while the tools were built: exit 0 without starting
        print(STOPPED_BEFORE_START, file=sys.stderr)
        return
    app = build_http_app(server, args, security, tokens)
    http_server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=args.host,
            port=args.port,
            log_level=server.settings.log_level.lower(),
            log_config=uvicorn_log_config(),
            ws="none",  # no WebSocket endpoints; uvicorn would otherwise log upgrade requests with their query string
        )
    )
    # Keep the shared HTTP client, cache and rate limiter for the whole process (SSE enters the server
    # lifespan per connection). Release them in the server's own event loop: the client's connections belong
    # to it and cannot be closed once it has ended.
    services = server.fuzzy_services

    async def serve() -> None:
        services.acquire()
        try:
            await http_server.serve()
        finally:
            with anyio.move_on_after(RELEASE_SECONDS) as scope:
                await services.release()
            if scope.cancelled_caught:
                logging.getLogger(__name__).warning(
                    "HTTP-klienten mot myndigheterna stängdes inte inom %g s; avslutar ändå", RELEASE_SECONDS
                )

    stop.server = http_server  # from here a stop sets should_exit, which uvicorn reads once it has started
    if stop.requested:  # a stop while the app was built, before the handler knew the server (#40)
        print(STOPPED_BEFORE_START, file=sys.stderr)
        return
    try:
        anyio.run(serve)
    except SystemExit:
        if http_server.started:
            raise
    if not http_server.started:
        if stop.requested:
            # Stopped while uvicorn started (it raises the signals it caught again when it returns): exit 0, also if
            # the start then failed, so that a service manager does not restart a service it was told to stop (#40).
            print("Stoppsignal under starten – avslutar med kod 0.", file=sys.stderr)
            return
        # Startup failed, e.g. the address cannot be used (port in use, unknown host). uvicorn exits with 1 or 3
        # depending on its version; always exit with 3, which service managers restart after a delay (docs/DRIFT.md).
        raise SystemExit(3)


# What uvicorn handles while it serves: Ctrl+C (also NSSM's stop), SIGTERM (Docker, systemd), Ctrl+Break (Windows).
STOP_SIGNALS = tuple(getattr(signal, name) for name in ("SIGINT", "SIGTERM", "SIGBREAK") if hasattr(signal, name))


class Stop:
    """The stop signals' handler: records that a stop was asked for and asks the server to stop, if it is running."""

    def __init__(self) -> None:
        self.requested = False
        self.server: Any = None

    def __call__(self, signum: int, frame: object) -> None:
        self.requested = True
        if self.server is not None:
            self.server.should_exit = True


@contextlib.contextmanager
def stop_on_signals() -> Iterator[Stop]:
    """Stop signals end the process normally: exit code 0, after the shared HTTP client is closed.

    uvicorn handles them while it serves, then restores the previous handlers and raises the signal again. Python's
    defaults would then kill the process (SIGTERM) or interrupt the cleanup with KeyboardInterrupt (Ctrl+C), and
    service managers would see a failure. These handlers only ask the server to stop, also when a signal comes before
    uvicorn takes over (main installs them before it builds the tools)."""
    stop = Stop()
    if threading.current_thread() is not threading.main_thread():  # signals are handled in the main thread only
        yield stop
        return
    previous = {sig: signal.signal(sig, stop) for sig in STOP_SIGNALS}
    try:
        yield stop
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


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


# The query runs to the next quote, "|", angle bracket or line end, spaces included: a decoded query such as
# "CQL_FILTER=POINT(6580822 674032)" must not leak its second half. The query part is optional so that a URL without
# one is consumed in a single match: a required "?" would make a long run of URL-like text (e.g. "https://https://…")
# take quadratic time on every log line.
_URL_QUERY = re.compile(r"(https?://[^\s?#\"'|<>]+)(\?[^\"'|<>\r\n]*)?")


def _redact_query(match: re.Match[str]) -> str:
    query = match[2]
    if query is None:
        return match[0]
    return f"{match[1]}?…{query[len(query.rstrip()) :]}"  # keep the space before a following "| …"


class RedactUrlQueries(logging.Filter):
    """Drop the query string of every URL in a log line. Tool arguments derived from users' questions (search text,
    names, coordinates) travel in the agencies' query strings, and the MCP SDK logs a failed tool call with its
    error text, which names the upstream URL."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # a malformed record: the handler reports it (handleError), as without the filter
            return True
        redacted = _URL_QUERY.sub(_redact_query, message)
        if redacted != message:
            record.msg, record.args = redacted, None
        return True


def quiet_logging() -> None:
    """Keep users' questions out of the log. MCPServer sets up logging at INFO, and httpx2 then logs every upstream
    request with its full URL; only its warnings are kept. Every other line passes RedactUrlQueries."""
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    for handler in logging.getLogger().handlers:
        if not any(isinstance(f, RedactUrlQueries) for f in handler.filters):
            handler.addFilter(RedactUrlQueries())


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
