# SPDX-FileCopyrightText: 2026 Deniz Özer
#
# SPDX-License-Identifier: MIT

"""Stopping the HTTP transports the way service managers do (no network needed).

NSSM stops a Windows service with Ctrl+C (SIGINT), Docker and systemd send SIGTERM. Either must end the process with
exit code 0 and close the shared HTTP client, which then has an open connection to an agency's API (here a local
stand-in), in the server's own event loop. A port in use still exits with 3.
"""

import argparse
import asyncio
import http.client
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Awaitable, Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from anyio.lowlevel import checkpoint

from fuzzy_mcp import __main__ as cli

TOKEN = "stopptest-0123456789abcdefghijklmnopqrstuvwxyz"
CLIENT_CLOSED = "HTTP-klienten mot myndigheterna är stängd"
TIMEOUT = 30.0
SERVER = [sys.executable, "-m", "fuzzy_mcp", "--transport", "streamable-http", "--stateless", "--host", "127.0.0.1"]
SSE_SERVER = [sys.executable, "-m", "fuzzy_mcp", "--transport", "sse", "--host", "127.0.0.1"]


class FakeServices:
    """Records how the HTTP entry point holds the shared services, and in which event loop it releases them."""

    def __init__(self) -> None:
        self.events: list[str] = []
        self.loops: list[asyncio.AbstractEventLoop] = []

    def acquire(self) -> None:
        self.events.append("acquire")

    async def release(self) -> None:
        await checkpoint()  # closing the HTTP client's connections needs the loop they were opened in
        self.loops.append(asyncio.get_running_loop())
        self.events.append("release")


def run_http(monkeypatch: pytest.MonkeyPatch, services: FakeServices, serve: Callable[[Any], Awaitable[None]]) -> Any:
    """serve_http with uvicorn's Server replaced: ``serve(server)`` stands in for ``Server.serve()``."""
    import uvicorn

    servers = []

    class Server:
        def __init__(self, config: dict[str, Any]) -> None:
            self.config, self.started, self.should_exit = config, False, False
            servers.append(self)

        async def serve(self) -> None:
            services.loops.append(asyncio.get_running_loop())
            services.events.append("serve")
            await serve(self)

    monkeypatch.setattr(uvicorn, "Config", lambda app, **kwargs: kwargs)
    monkeypatch.setattr(uvicorn, "Server", Server)
    monkeypatch.setattr(cli, "build_http_app", lambda *args: object())
    server = SimpleNamespace(fuzzy_services=services, settings=SimpleNamespace(log_level="INFO"))
    cli.serve_http(server, argparse.Namespace(host="127.0.0.1", port=8000), None, [])
    return servers[0]


def test_http_client_is_released_in_the_servers_own_loop(monkeypatch):
    async def serve(server: Any) -> None:
        server.started = True

    services = FakeServices()
    run_http(monkeypatch, services, serve)
    assert services.events == ["acquire", "serve", "release"]
    assert services.loops[0] is services.loops[1]


# uvicorn up to at least 0.44 exits with 1 when the port is in use, and 0.31 returns without starting when the app's
# startup fails; newer versions exit with 3 in both cases.
@pytest.mark.parametrize("exit_code", [None, 1, 3])
def test_startup_failure_exits_3_after_release(monkeypatch, exit_code):
    async def serve(server: Any) -> None:
        if exit_code is not None:
            sys.exit(exit_code)

    services = FakeServices()
    with pytest.raises(SystemExit) as exc:
        run_http(monkeypatch, services, serve)
    assert exc.value.code == 3
    assert services.events == ["acquire", "serve", "release"]


def test_a_hanging_release_does_not_keep_the_process(monkeypatch, caplog):
    """After uvicorn has stopped, a stop signal no longer stops anything: closing the HTTP client is limited (#40)."""
    import anyio

    class Hanging(FakeServices):
        async def release(self) -> None:
            await anyio.sleep_forever()

    async def serve(server: Any) -> None:
        server.started = True

    monkeypatch.setattr(cli, "RELEASE_SECONDS", 0.2)
    started = time.monotonic()
    run_http(monkeypatch, Hanging(), serve)
    assert time.monotonic() - started < 5
    assert any("stängdes inte inom 0.2 s" in record.getMessage() for record in caplog.records)


def test_stop_while_the_tools_are_built_exits_0_without_starting(monkeypatch, capsys):
    """main holds the stop signals from the start: a stop while the tools are built ends the process with exit code 0
    and the server does not start (it used to be killed, or end with a KeyboardInterrupt traceback) (#40)."""
    from fuzzy_mcp import server as server_module

    if threading.current_thread() is not threading.main_thread():
        pytest.skip("signaler hanteras bara i huvudtråden")
    before = {sig: signal.getsignal(sig) for sig in cli.STOP_SIGNALS}

    def build_server(settings: Any) -> Any:
        signal.raise_signal(cli.STOP_SIGNALS[0])  # Ctrl+C while the tools are built
        return SimpleNamespace(fuzzy_services=FakeServices(), settings=settings)

    def uvicorn_server(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("servern startades fast den stoppats")

    import uvicorn

    monkeypatch.setattr(server_module, "build_server", build_server)
    monkeypatch.setattr(uvicorn, "Server", uvicorn_server)
    monkeypatch.setattr(cli, "quiet_logging", lambda: None)
    monkeypatch.setenv("FUZZY_MCP_AUTH_TOKEN", TOKEN)
    # Should main's handler be missing, the signal reaches this one instead of interrupting pytest itself.
    reached: list[int] = []
    guard = signal.signal(cli.STOP_SIGNALS[0], lambda signum, frame: reached.append(signum))
    try:
        cli.main(["--transport", "streamable-http", "--host", "127.0.0.1", "--port", "8000"])
    finally:
        signal.signal(cli.STOP_SIGNALS[0], guard)
    assert not reached, "stoppsignalen nådde inte mains hanterare"
    assert "Stoppsignal före start" in capsys.readouterr().err
    assert {sig: signal.getsignal(sig) for sig in cli.STOP_SIGNALS} == before


def test_stop_while_the_app_is_built_exits_0_without_starting(monkeypatch, capsys):
    """A stop after the tools are built but before the handler knows the server (while the app and uvicorn's server
    are created) is not lost: the server does not start (#40)."""
    import uvicorn

    if threading.current_thread() is not threading.main_thread():
        pytest.skip("signaler hanteras bara i huvudtråden")

    def build_http_app(*args: Any) -> Any:
        signal.raise_signal(cli.STOP_SIGNALS[0])
        return object()

    class Server:
        def __init__(self, config: Any) -> None:
            self.started = self.should_exit = False

        async def serve(self) -> None:
            raise AssertionError("servern startades fast den stoppats")

    monkeypatch.setattr(uvicorn, "Config", lambda app, **kwargs: kwargs)
    monkeypatch.setattr(uvicorn, "Server", Server)
    monkeypatch.setattr(cli, "build_http_app", build_http_app)
    server = SimpleNamespace(fuzzy_services=FakeServices(), settings=SimpleNamespace(log_level="INFO"))
    cli.serve_http(server, argparse.Namespace(host="127.0.0.1", port=8000), None, [])
    assert cli.STOPPED_BEFORE_START in capsys.readouterr().err


@pytest.mark.anyio
async def test_sse_sends_one_response_per_request():
    """The SDK's SSE endpoint sends an empty response after its stream; a second start is dropped, and its empty body
    ends a stream that a shutdown interrupted, or is dropped too if the stream had ended (#40)."""
    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    async def interrupted(scope: Any, receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"event: endpoint\n\n", "more_body": True})
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    await cli.OneResponse(interrupted)({"type": "http"}, None, send)
    assert [m["type"] for m in sent] == ["http.response.start", "http.response.body", "http.response.body"]
    assert sent[-1] == {"type": "http.response.body", "body": b""}

    async def ended(scope: Any, receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    sent.clear()
    await cli.OneResponse(ended)({"type": "http"}, None, send)
    assert [m["type"] for m in sent] == ["http.response.start", "http.response.body"]


def test_stop_during_a_failed_startup_exits_0(monkeypatch, capsys):
    """A stop while uvicorn starts, which then fails (port in use): exit 0, not the 3 that makes a service manager
    restart the service it was told to stop (#40). uvicorn raises the signals it caught again when it returns."""

    async def serve(server: Any) -> None:
        handler = signal.getsignal(cli.STOP_SIGNALS[0])
        handler(cli.STOP_SIGNALS[0], None)  # as uvicorn's capture_signals does after a stop during its start
        sys.exit(3)  # uvicorn: the address cannot be used

    services = FakeServices()
    run_http(monkeypatch, services, serve)  # no SystemExit
    assert services.events == ["acquire", "serve", "release"]
    assert "Stoppsignal under starten" in capsys.readouterr().err


def test_stop_signals_only_ask_the_server_to_stop(monkeypatch):
    before = {sig: signal.getsignal(sig) for sig in cli.STOP_SIGNALS}

    async def serve(server: Any) -> None:
        server.started = True
        for sig in cli.STOP_SIGNALS:  # where uvicorn sends the signal again after serve()
            handler = signal.getsignal(sig)
            assert callable(handler) and handler is not before[sig]
            handler(sig, None)

    services = FakeServices()
    assert run_http(monkeypatch, services, serve).should_exit
    assert services.events == ["acquire", "serve", "release"]
    assert {sig: signal.getsignal(sig) for sig in cli.STOP_SIGNALS} == before


class Agency(BaseHTTPRequestHandler):
    """Stand-in for an agency API that keeps the connection open after answering (HTTP/1.1), as the real ones do."""

    protocol_version = "HTTP/1.1"
    requests = 0

    def do_GET(self) -> None:
        type(self).requests += 1
        body = b'{"apiVersion": "2.0.0"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture
def agency() -> Iterator[ThreadingHTTPServer]:
    Agency.requests = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), Agency)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def start(port: int, log: Path, command: list[str] = SERVER, **env: str) -> subprocess.Popen[bytes]:
    # Nothing from the caller's environment that changes the server or sends its upstream calls through a proxy.
    base = {k: v for k, v in os.environ.items() if not k.startswith("FUZZY_MCP_") and not k.lower().endswith("_proxy")}
    with log.open("wb") as stderr:
        return subprocess.Popen(
            [*command, "--port", str(port), "--sources", "scb.statistik"],
            env={**base, "FUZZY_MCP_AUTH_TOKEN": TOKEN, **env},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=stderr,
        )


def wait_until_ready(process: subprocess.Popen[bytes], port: int, log: Path) -> None:
    deadline = time.monotonic() + TIMEOUT
    while time.monotonic() < deadline:
        if process.poll() is not None:
            pytest.fail(f"Servern avslutades med {process.returncode} innan den svarade:\n{log.read_text('utf-8')}")
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        try:
            conn.request("GET", "/healthz")
            if conn.getresponse().status == 200:
                return
        except OSError:
            pass
        finally:
            conn.close()
        time.sleep(0.05)
    pytest.fail(f"Servern svarade inte inom {TIMEOUT:g} s:\n{log.read_text('utf-8')}")


def call_tool(port: int, name: str) -> str:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=TIMEOUT)
    try:
        conn.request(
            "POST",
            "/mcp",
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": {}}}),
            {
                "Authorization": f"Bearer {TOKEN}",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
        )
        response = conn.getresponse()
        body = response.read().decode("utf-8")
        assert response.status == 200, body
        return body
    finally:
        conn.close()


def stop(process: subprocess.Popen[bytes], sig: int) -> int:
    process.send_signal(sig)
    try:
        return process.wait(timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        raise


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows: SIGINT/SIGTERM kan inte skickas till en annan process (NSSM skickar Ctrl+C via konsolen)",
)
@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM], ids=["SIGINT", "SIGTERM"])
def test_stop_signal_closes_http_client_and_exits_0(tmp_path, agency, sig):
    port = free_port()
    log = tmp_path / "stderr.log"
    process = start(port, log, FUZZY_MCP_SCB_BASE_URL=f"http://127.0.0.1:{agency.server_port}/api/v2")
    try:
        wait_until_ready(process, port, log)
        assert "apiVersion" in call_tool(port, "scb_get_config")  # the shared HTTP client now holds a connection
        assert Agency.requests >= 1
        returncode = stop(process, sig)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
    stderr = log.read_text("utf-8")
    assert returncode == 0, stderr
    assert "Traceback" not in stderr
    assert "Event loop is closed" not in stderr
    assert CLIENT_CLOSED in stderr


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows: SIGINT/SIGTERM kan inte skickas till en annan process (NSSM skickar Ctrl+C via konsolen)",
)
@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM], ids=["SIGINT", "SIGTERM"])
def test_stop_signal_with_an_open_sse_stream_exits_0_without_traceback(tmp_path, sig):
    """With a client on /sse a stop used to log "Exception in ASGI application" and a traceback: the SDK's SSE
    endpoint sends a second response after the interrupted stream (#40)."""
    port = free_port()
    log = tmp_path / "stderr.log"
    process = start(port, log, SSE_SERVER)
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=TIMEOUT)
    try:
        wait_until_ready(process, port, log)
        conn.request("GET", "/sse", headers={"Authorization": f"Bearer {TOKEN}", "Accept": "text/event-stream"})
        response = conn.getresponse()
        assert response.status == 200
        for line in iter(response.fp.readline, b""):  # until the stream has sent its first event
            if b"endpoint" in line:
                break
        else:  # the stream ended first: fail instead of reading the end of the stream for ever
            pytest.fail(f"SSE-strömmen tog slut före endpoint-händelsen:\n{log.read_text('utf-8')}")
        returncode = stop(process, sig)
    finally:
        conn.close()
        if process.poll() is None:
            process.kill()
            process.wait()
    stderr = log.read_text("utf-8")
    assert returncode == 0, stderr
    assert "Traceback" not in stderr and "Exception in ASGI application" not in stderr, stderr


def test_port_in_use_exits_3(tmp_path):
    log = tmp_path / "stderr.log"
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        process = start(busy.getsockname()[1], log)
        try:
            returncode = process.wait(timeout=TIMEOUT)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            raise
    stderr = log.read_text("utf-8")
    assert returncode == 3, stderr  # service managers restart after a delay (docs/DRIFT.md)
    assert "Traceback" not in stderr
