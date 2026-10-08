# SPDX-FileCopyrightText: 2026 Deniz Özer
#
# SPDX-License-Identifier: MIT

"""Static token authentication for the HTTP transports.

MCP clients such as Eneo connect with a shared secret: either
``Authorization: Bearer <token>`` or the token in an API-key header
(default ``X-API-Key``). Tokens are read from a file (one per line, so a new
token can be added before the old one is removed) or from an environment
variable. ``/healthz`` stays open for load balancers and container health
checks; every other path needs a valid token.

This is service-to-service authentication, not user identity: the server
never sees or stores who asked the question.
"""

import hmac
import json
import os
import sys
from collections.abc import Awaitable, Callable, Iterable, MutableMapping
from pathlib import Path
from typing import Any

from .config import ENV_PREFIX

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

MIN_TOKEN_LENGTH = 32
DEFAULT_API_KEY_HEADER = "X-API-Key"
OPEN_PATHS = frozenset({"/healthz"})


class AuthConfigError(ValueError):
    """Invalid token configuration (reported at start-up)."""


def load_tokens(token_file: str | None, token: str | None) -> list[str]:
    """Tokens from a file (one per line, ``#`` comments allowed) and/or a single value."""
    tokens: list[str] = []
    if token_file:
        path = Path(token_file)
        try:
            # utf-8-sig accepts a BOM (Notepad); UTF-16 (PowerShell 5.1 `>`) is reported below.
            text = sys.stdin.read() if str(path) == "-" else path.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError as exc:
            raise AuthConfigError(
                f"Tokenfilen {path} är inte UTF-8. Spara den som UTF-8, t.ex. i PowerShell: "
                "Set-Content -Encoding ascii -Path <fil> -Value <token>"
            ) from exc
        except OSError as exc:
            raise AuthConfigError(f"Kan inte läsa tokenfilen {path}: {exc.strerror or exc}") from exc
        tokens += [line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith("#")]
        if not tokens:
            raise AuthConfigError(f"Tokenfilen {path} innehåller ingen token")
    if token:
        tokens.append(token.strip())
    for value in tokens:
        if len(value) < MIN_TOKEN_LENGTH:
            raise AuthConfigError(
                f"En token är kortare än {MIN_TOKEN_LENGTH} tecken. Skapa en ny, t.ex. med "
                '`python -c "import secrets; print(secrets.token_urlsafe(32))"`.'
            )
        if any(ch.isspace() for ch in value) or not value.isprintable():
            raise AuthConfigError("En token innehåller blanksteg eller kontrolltecken")
    return list(dict.fromkeys(tokens))


def tokens_from_env(environ: MutableMapping[str, str] | None = None) -> list[str]:
    env = os.environ if environ is None else environ
    return load_tokens(env.get(f"{ENV_PREFIX}AUTH_TOKEN_FILE") or None, env.get(f"{ENV_PREFIX}AUTH_TOKEN") or None)


class TokenAuthMiddleware:
    """Pure ASGI middleware (works with streaming responses) that checks a static token."""

    def __init__(
        self,
        app: ASGIApp,
        tokens: Iterable[str],
        *,
        api_key_header: str = DEFAULT_API_KEY_HEADER,
        open_paths: frozenset[str] = OPEN_PATHS,
    ) -> None:
        self.app = app
        self.tokens = [t.encode("utf-8") for t in tokens]
        if not self.tokens:
            raise AuthConfigError("Ingen token angiven")
        self.api_key_header = api_key_header.lower().encode("latin-1")
        self.open_paths = open_paths

    def _presented(self, scope: Scope) -> bytes | None:
        for name, value in scope.get("headers") or []:
            if name == b"authorization":
                scheme, _, credentials = value.partition(b" ")
                if scheme.lower() == b"bearer" and credentials.strip():
                    return credentials.strip()
            elif name == self.api_key_header and value.strip():
                return value.strip()
        return None

    def _valid(self, presented: bytes | None) -> bool:
        if presented is None:
            return False
        # Compare against every token without stopping early (constant time per token).
        results = [hmac.compare_digest(presented, token) for token in self.tokens]
        return any(results)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        kind = scope["type"]
        if kind == "lifespan" or (kind == "http" and scope.get("path") in self.open_paths):
            await self.app(scope, receive, send)
            return
        if self._valid(self._presented(scope)):
            await self.app(scope, receive, send)
            return
        if kind == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        if kind != "http":
            return
        body = json.dumps({"error": "unauthorized", "detail": "Ogiltig eller saknad token"}).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                    (b"www-authenticate", b'Bearer realm="fuzzy-mcp"'),
                    (b"cache-control", b"no-store"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
