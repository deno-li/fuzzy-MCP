# SPDX-FileCopyrightText: 2026 Deniz Özer
#
# SPDX-License-Identifier: MIT

import os

import httpx2
import pytest

from fuzzy_mcp.auth import AuthConfigError, TokenAuthMiddleware, load_tokens
from fuzzy_mcp.config import Settings
from fuzzy_mcp.server import build_server

pytestmark = pytest.mark.anyio

TOKEN = "a" * 20 + "B" * 20
OTHER = "z" * 40


def test_load_tokens_from_file_and_env(tmp_path):
    path = tmp_path / "tokens.txt"
    path.write_text(f"# rotation: ny först\n{TOKEN}\n\n{OTHER}\n", encoding="utf-8")
    assert load_tokens(str(path), None) == [TOKEN, OTHER]
    assert load_tokens(None, f" {TOKEN} ") == [TOKEN]
    assert load_tokens(None, None) == []
    with pytest.raises(AuthConfigError, match="kortare än 32"):
        load_tokens(None, "kort")
    with pytest.raises(AuthConfigError, match="ingen token"):
        (tmp_path / "empty.txt").write_text("# bara kommentar\n", encoding="utf-8")
        load_tokens(str(tmp_path / "empty.txt"), None)
    with pytest.raises(AuthConfigError, match="Kan inte läsa"):
        load_tokens(str(tmp_path / "saknas.txt"), None)


async def _client(app):
    return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://127.0.0.1:8000")


async def test_middleware_requires_token_except_healthz():
    server = build_server(Settings(enabled_sources=frozenset({"reference"})), rate_limits={})
    inner = server.streamable_http_app()
    app = TokenAuthMiddleware(inner, [TOKEN, OTHER])
    ping = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    accept = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    async with inner.router.lifespan_context(inner), await _client(app) as http:
        assert (await http.get("/healthz")).status_code == 200
        missing = await http.post("/mcp", json=ping, headers=accept)
        assert missing.status_code == 401 and missing.headers["www-authenticate"].startswith("Bearer")
        wrong = await http.post("/mcp", json=ping, headers={**accept, "Authorization": f"Bearer {'x' * 40}"})
        assert wrong.status_code == 401
        basic = await http.post("/mcp", json=ping, headers={**accept, "Authorization": f"Basic {TOKEN}"})
        assert basic.status_code == 401
        # A valid token reaches the MCP app (which then asks for a session, i.e. not 401).
        bearer = await http.post("/mcp", json=ping, headers={**accept, "Authorization": f"Bearer {OTHER}"})
        api_key = await http.post("/mcp", json=ping, headers={**accept, "X-API-Key": TOKEN})
        assert bearer.status_code != 401 and api_key.status_code != 401


def test_middleware_needs_tokens():
    with pytest.raises(AuthConfigError):
        TokenAuthMiddleware(lambda *a: None, [])


def test_personal_data_switch_from_env(monkeypatch):
    monkeypatch.setenv("FUZZY_MCP_PERSONAL_DATA", "off")
    assert Settings.from_env().allow_personal_data is False
    monkeypatch.setenv("FUZZY_MCP_PERSONAL_DATA", "ja")
    with pytest.raises(ValueError, match="opt-in"):
        Settings.from_env()
    monkeypatch.delenv("FUZZY_MCP_PERSONAL_DATA")
    assert Settings.from_env().allow_personal_data is True


async def test_options_and_websocket_need_a_token():
    server = build_server(Settings(enabled_sources=frozenset({"reference"})), rate_limits={})
    inner = server.streamable_http_app()
    app = TokenAuthMiddleware(inner, [TOKEN])
    async with inner.router.lifespan_context(inner), await _client(app) as http:
        assert (await http.options("/mcp")).status_code == 401
    sent = []

    async def receive():
        return {"type": "websocket.connect"}

    async def send(message):
        sent.append(message)

    await app({"type": "websocket", "path": "/mcp", "headers": []}, receive, send)
    assert sent == [{"type": "websocket.close", "code": 1008}]


def test_token_file_encodings(tmp_path):
    bom = tmp_path / "bom.txt"
    bom.write_bytes(b"\xef\xbb\xbf" + TOKEN.encode())
    assert load_tokens(str(bom), None) == [TOKEN]
    utf16 = tmp_path / "utf16.txt"
    utf16.write_text(TOKEN, encoding="utf-16")
    with pytest.raises(AuthConfigError, match="inte UTF-8"):
        load_tokens(str(utf16), None)


def test_http_always_requires_a_token(monkeypatch, capsys):
    from fuzzy_mcp import __main__ as cli

    started = []
    monkeypatch.setattr(cli, "serve_http", lambda *a: started.append(a))
    monkeypatch.delenv("FUZZY_MCP_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("FUZZY_MCP_AUTH_TOKEN_FILE", raising=False)
    monkeypatch.delenv("FUZZY_MCP_ALLOW_UNAUTHENTICATED", raising=False)
    # Public interface, loopback, and loopback behind a proxy under a public name: all need a token.
    for host, extra in (("0.0.0.0", []), ("127.0.0.1", []), ("127.0.0.1", ["--allowed-hosts", "mcp.kommun.se"])):
        with pytest.raises(SystemExit) as info:
            cli.main(["--transport", "streamable-http", "--host", host, "--sources", "reference", *extra])
        assert info.value.code == 2 and "kräver en token" in capsys.readouterr().err and not started
    for host in ("0.0.0.0", "127.0.0.1"):
        cli.main(
            ["--transport", "streamable-http", "--host", host, "--sources", "reference", "--allow-unauthenticated"]
        )
    assert len(started) == 2
    monkeypatch.setenv("FUZZY_MCP_AUTH_TOKEN", TOKEN)
    cli.main(["--transport", "sse", "--host", "127.0.0.1", "--sources", "reference"])
    assert len(started) == 3


def test_invalid_transport_from_environment_is_rejected(monkeypatch, capsys):
    from fuzzy_mcp import __main__ as cli

    started = []
    monkeypatch.setattr(cli, "serve_http", lambda *a: started.append(a))
    monkeypatch.setenv("FUZZY_MCP_TRANSPORT", "streamable_http")
    with pytest.raises(SystemExit) as info:
        cli.main(["--sources", "reference"])
    assert info.value.code == 2 and "FUZZY_MCP_TRANSPORT" in capsys.readouterr().err and not started


def test_access_log_goes_to_stderr():
    from fuzzy_mcp.__main__ import uvicorn_log_config

    config = uvicorn_log_config()
    streams = {name: h.get("stream") for name, h in config["handlers"].items()}
    assert streams["access"] == "ext://sys.stderr"
    assert "ext://sys.stdout" not in streams.values()


def test_boolean_environment_variables_are_strict(monkeypatch, capsys):
    from fuzzy_mcp import __main__ as cli

    started = []
    monkeypatch.setattr(cli, "serve_http", lambda *a: started.append(a))
    monkeypatch.delenv("FUZZY_MCP_AUTH_TOKEN_FILE", raising=False)
    monkeypatch.delenv("FUZZY_MCP_ALLOW_UNAUTHENTICATED", raising=False)
    monkeypatch.setenv("FUZZY_MCP_AUTH_TOKEN", TOKEN)
    # A typo must not silently mean "false" (e.g. stateful behind a load balancer without sticky sessions).
    monkeypatch.setenv("FUZZY_MCP_STATELESS", "maybe")
    with pytest.raises(SystemExit) as info:
        cli.main(["--transport", "streamable-http", "--sources", "reference"])
    assert info.value.code == 2 and "FUZZY_MCP_STATELESS" in capsys.readouterr().err and not started
    monkeypatch.setenv("FUZZY_MCP_STATELESS", "On")
    cli.main(["--transport", "streamable-http", "--sources", "reference"])
    assert started[-1][1].stateless is True
    monkeypatch.setenv("FUZZY_MCP_STATELESS", "nej")
    cli.main(["--transport", "streamable-http", "--sources", "reference", "--stateless"])
    assert started[-1][1].stateless is True  # the command line wins over the environment
    monkeypatch.delenv("FUZZY_MCP_AUTH_TOKEN")
    monkeypatch.setenv("FUZZY_MCP_ALLOW_UNAUTHENTICATED", "nej")
    with pytest.raises(SystemExit) as info:
        cli.main(["--transport", "streamable-http", "--sources", "reference"])
    assert info.value.code == 2 and "kräver en token" in capsys.readouterr().err and len(started) == 2


def test_access_log_drops_query_string():
    import logging

    from uvicorn.logging import AccessFormatter

    from fuzzy_mcp.__main__ import LogWithoutQuery, uvicorn_log_config

    handlers = uvicorn_log_config()["handlers"]
    assert all("without_query" in handler["filters"] for handler in handlers.values())
    formatter = AccessFormatter("%(client_addr)s - %(request_line)s %(status_code)s", use_colors=False)
    # Origin form, absolute form (as uvicorn logs it) and a target without a leading slash.
    for target, shown in (
        (f"/mcp?token={TOKEN}", "/mcp?…"),
        (f"http%3A//h%3A8000/mcp?token={TOKEN}", "http%3A//h%3A8000/mcp?…"),
        (f"mcp?token={TOKEN}", "mcp?…"),
    ):
        record = logging.LogRecord(
            "uvicorn.access",
            logging.INFO,
            __file__,
            1,
            '%s - "%s %s HTTP/%s" %d',
            ("127.0.0.1:5000", "POST", target, "1.1", 401),
            None,
        )
        assert LogWithoutQuery().filter(record)
        line = formatter.format(record)
        assert TOKEN not in line
        assert f"POST {shown} HTTP/1.1 401" in line


def test_invalid_port_is_a_configuration_error(monkeypatch, capsys):
    from fuzzy_mcp import __main__ as cli

    started = []
    monkeypatch.setattr(cli, "serve_http", lambda *a: started.append(a))
    monkeypatch.delenv("FUZZY_MCP_AUTH_TOKEN_FILE", raising=False)
    monkeypatch.setenv("FUZZY_MCP_AUTH_TOKEN", TOKEN)
    for value in ("80a", "0", "70000", "-1", "８０"):
        monkeypatch.setenv("FUZZY_MCP_PORT", value)
        with pytest.raises(SystemExit) as info:
            cli.main(["--transport", "streamable-http", "--sources", "reference"])
        assert info.value.code == 2 and "Konfigurationsfel: FUZZY_MCP_PORT" in capsys.readouterr().err
    with pytest.raises(SystemExit) as info:
        cli.main(["--transport", "streamable-http", "--sources", "reference", "--port", "0"])
    assert info.value.code == 2 and "Konfigurationsfel: --port" in capsys.readouterr().err and not started
    monkeypatch.setenv("FUZZY_MCP_PORT", " 8123 ")
    cli.main(["--transport", "streamable-http", "--sources", "reference"])
    assert started[-1][1].port == 8123


def test_websocket_log_line_drops_query_string():
    import logging

    from fuzzy_mcp.__main__ import LogWithoutQuery

    record = logging.LogRecord(
        "uvicorn.error",
        logging.INFO,
        __file__,
        1,
        '%s - "WebSocket %s" %s',
        ("1.2.3.4:5", f"/mcp?t={TOKEN}", 403),
        None,
    )
    assert LogWithoutQuery().filter(record) and TOKEN not in record.getMessage()
    plain = logging.LogRecord("uvicorn.error", logging.INFO, __file__, 1, "Started server process [%d]", (42,), None)
    assert LogWithoutQuery().filter(plain) and plain.getMessage() == "Started server process [42]"


def test_http_server_runs_without_websockets(monkeypatch):
    import uvicorn

    from fuzzy_mcp import __main__ as cli

    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: calls.append(kw))
    monkeypatch.delenv("FUZZY_MCP_AUTH_TOKEN_FILE", raising=False)
    monkeypatch.setenv("FUZZY_MCP_AUTH_TOKEN", TOKEN)
    cli.main(["--transport", "streamable-http", "--sources", "reference"])
    assert calls and calls[0]["ws"] == "none"  # WebSocket upgrades would be logged with the query string


def test_http_settings_do_not_stop_stdio(monkeypatch):
    import fuzzy_mcp.server as server_module
    from fuzzy_mcp import __main__ as cli

    served = []
    monkeypatch.setattr(server_module, "build_server", lambda settings: type("S", (), {"run": served.append})())
    monkeypatch.delenv("FUZZY_MCP_TRANSPORT", raising=False)
    monkeypatch.setenv("FUZZY_MCP_PORT", "abc")
    monkeypatch.setenv("FUZZY_MCP_STATELESS", "kanske")
    cli.main(["--sources", "reference"])  # stdio: HTTP-only settings do not apply
    assert served == ["stdio"]


def _pem_certificate(path):
    import datetime

    x509 = pytest.importorskip("cryptography.x509")
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(x509.oid.NameOID.COMMON_NAME, "fuzzy-mcp test CA")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return cert.public_bytes(serialization.Encoding.DER)


def test_unusable_ca_settings_are_configuration_errors(monkeypatch, capsys, tmp_path):
    import fuzzy_mcp.server as server_module
    from fuzzy_mcp import __main__ as cli

    served = []
    monkeypatch.setattr(server_module, "build_server", lambda settings: type("S", (), {"run": served.append})())
    monkeypatch.delenv("FUZZY_MCP_TRANSPORT", raising=False)
    pem, der = tmp_path / "ca.pem", tmp_path / "ca.cer"
    der.write_bytes(_pem_certificate(pem))
    for name, value in (
        ("SSL_CERT_FILE", tmp_path / "saknas.pem"),
        ("SSL_CERT_FILE", der),
        ("SSL_CERT_FILE", tmp_path),
    ):
        monkeypatch.setenv(name, str(value))
        with pytest.raises(SystemExit) as info:
            cli.main(["--sources", "reference"])
        assert info.value.code == 2 and f"Konfigurationsfel: {name}" in capsys.readouterr().err
    # A usable PEM bundle starts the server; SSL_CERT_DIR is then not used (as in the HTTP client), so not checked.
    monkeypatch.setenv("SSL_CERT_FILE", str(pem))
    monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "saknas"))
    cli.main(["--sources", "reference"])
    assert served == ["stdio"]
    monkeypatch.delenv("SSL_CERT_FILE")
    with pytest.raises(SystemExit) as info:
        cli.main(["--sources", "reference"])
    assert info.value.code == 2 and "Konfigurationsfel: SSL_CERT_DIR" in capsys.readouterr().err
    # A list of directories is accepted as long as one of them exists (as OpenSSL does).
    monkeypatch.setenv("SSL_CERT_DIR", os.pathsep.join([str(tmp_path / "saknas"), str(tmp_path)]))
    cli.main(["--sources", "reference"])
    assert served == ["stdio", "stdio"]
