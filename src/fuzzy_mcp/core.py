# SPDX-FileCopyrightText: 2026 Deniz Özer
#
# SPDX-License-Identifier: MIT

"""Shared plumbing for the source modules: service container, error
translation for MCP tools and small helpers."""

import functools
import inspect
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, ParamSpec, TypeVar

import httpx2
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import BaseModel

from .config import Settings
from .errors import FuzzyMcpError, InvalidInputError
from .http import HttpClient, RateLimit

P = ParamSpec("P")
R = TypeVar("R")

# Read-only, open-world: every tool only reads public data from external APIs.
READ_ONLY_OPEN = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)
# Read-only, closed-world: tools answering from bundled reference data.
READ_ONLY_LOCAL = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)


@dataclass
class Services:
    """Lazily created shared services handed to every source module."""

    settings: Settings
    transport: httpx2.AsyncBaseTransport | None = None
    rate_limits: Mapping[str, RateLimit] = field(default_factory=dict)
    _http: HttpClient | None = None
    _users: int = 0

    @property
    def http(self) -> HttpClient:
        if self._http is None:
            self._http = HttpClient(self.settings, transport=self.transport, rate_limits=self.rate_limits)
        return self._http

    def acquire(self) -> None:
        """Register one running session (the SDK enters the lifespan once per SSE connection)."""
        self._users += 1

    async def release(self) -> None:
        """End one session; the shared HTTP client is closed only when the last one ends."""
        self._users = max(0, self._users - 1)
        if self._users == 0:
            await self.aclose()

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None


def inline_schema_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """Resolve local ``$ref``s (``#/$defs/...``) and drop ``$defs``.

    Pydantic puts nested models in ``$defs``; several LLM providers reject ``$ref`` in tool parameters, and MCP
    clients such as Eneo pass the input schema on unchanged. Validation is unaffected (it uses the Python types).
    """
    defs = schema.get("$defs") or {}

    def resolve(node: Any, seen: tuple[str, ...]) -> Any:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                name = ref.removeprefix("#/$defs/")
                if name in seen or name not in defs:  # recursive or unknown: leave as it is
                    return node
                merged = {**defs[name], **{k: v for k, v in node.items() if k != "$ref"}}
                return resolve(merged, (*seen, name))
            return {k: resolve(v, seen) for k, v in node.items() if k != "$defs"}
        if isinstance(node, list):
            return [resolve(item, seen) for item in node]
        return node

    resolved = resolve(schema, ())
    if "$ref" in json.dumps(resolved):  # recursion left a reference: keep the definitions
        resolved["$defs"] = defs
    return resolved


def personal_data(services: Services, requested: bool) -> bool:
    """Gate for the opt-in personal-data parameters (``include_personal_data``, ``include_contacts``).

    With ``FUZZY_MCP_PERSONAL_DATA=off`` a request for personal data is refused with a clear message instead of
    being ignored silently, so the assistant can tell the user why.
    """
    if requested and not services.settings.allow_personal_data:
        raise InvalidInputError(
            "Personuppgifter (t.ex. namn på rektor eller kontaktpersoner) är avstängda i den här installationen "
            "(FUZZY_MCP_PERSONAL_DATA=off). Ställ frågan utan den parametern."
        )
    return requested


def tool_errors(fn: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
    """Translate package errors into :class:`ToolError` so the message reaches
    the client (MCPServer masks other exception types)."""

    if not inspect.iscoroutinefunction(fn):
        raise TypeError("tool_errors kräver en async-funktion")

    @functools.wraps(fn)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return await fn(*args, **kwargs)
        except ToolError:
            raise
        except FuzzyMcpError as exc:
            raise ToolError(str(exc)) from exc
        except httpx2.HTTPError as exc:
            # Safety net: the HTTP client maps request errors to UpstreamError, but any
            # httpx error that slips through must still reach the client with a message.
            raise ToolError(f"Anropet till källan misslyckades: {type(exc).__name__}: {exc}") from exc

    return wrapper


def dump(model: BaseModel | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(model, BaseModel):
        return model.model_dump(mode="json", by_alias=True, exclude_none=True)
    return compact(dict(model))


# Optional cap on the size of a tool answer as a client sees it (FUZZY_MCP_MAX_OUTPUT_CHARS, 0 = off). Eneo, for
# example, cuts a tool result at 32 768 characters of its JSON-encoded form, i.e. in the middle of our JSON.
TRUNCATION_KEY = "_kortat"
ENVELOPE_CHARS = 100  # the client's wrapper around our text, e.g. {"content": [{"type": "text", "text": ...}]}
# Lists whose positions carry meaning, or query recipes that must stay complete: never shortened.
POSITIONAL_KEYS = frozenset({"coordinates", "bbox", "columns", "post_body", "selection_tokens"})
MIN_STRING = 200


def client_size(text: str) -> int:
    """Size of a text answer after a client JSON-encodes it (as Eneo does: UTF-8 kept, quotes escaped)."""
    return len(json.dumps(text, ensure_ascii=False)) + ENVELOPE_CHARS


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _candidates(node: Any, path: str, in_list: bool) -> list[tuple[str, list[Any]]]:
    """Trimmable lists: records or values under a key. Lists directly inside lists (table rows, coordinate
    pairs), lists of numbers and anything under ``POSITIONAL_KEYS`` are kept whole."""
    found: list[tuple[str, list[Any]]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key not in POSITIONAL_KEYS:
                found += _candidates(value, f"{path}.{key}" if path else str(key), False)
    elif isinstance(node, list):
        numeric = all(isinstance(x, int | float) and not isinstance(x, bool) for x in node)
        if len(node) > 1 and not in_list and not numeric:
            found.append((path, node))
        for item in node:
            found += _candidates(item, f"{path}[]", True)
    return found


def _pick_list(data: dict[str, Any]) -> tuple[str, list[Any]] | None:
    """The trimmable list with the largest own weight (its size minus nested trimmable lists), so long inner
    value lists shrink before whole records are dropped."""
    candidates = _candidates(data, "", False)
    sizes = {id(lst): len(compact_json(lst)) for _, lst in candidates}
    best: tuple[int, str, list[Any]] | None = None
    for path, lst in candidates:
        nested = sum(sizes[id(other)] for p, other in candidates if other is not lst and p.startswith(path + "["))
        own = sizes[id(lst)] - nested
        if best is None or own > best[0]:
            best = (own, path, lst)
    return (best[1], best[2]) if best else None


def _longest_string(node: Any) -> tuple[dict[str, Any] | list[Any], Any, str] | None:
    best: tuple[int, Any, Any, str] | None = None
    stack: list[Any] = [node]
    while stack:
        current = stack.pop()
        items = (
            current.items() if isinstance(current, dict) else enumerate(current) if isinstance(current, list) else ()
        )
        for key, value in items:
            if isinstance(value, str) and len(value) > MIN_STRING and (best is None or len(value) > best[0]):
                best = (len(value), current, key, value)
            elif isinstance(value, dict | list):
                stack.append(value)
    return (best[1], best[2], best[3]) if best else None


def fit_to_limit(data: dict[str, Any], limit: int) -> tuple[dict[str, Any] | None, bool]:
    """Shorten lists (and, if needed, long strings) until the answer fits ``limit`` as a client measures it.

    Returns ``(data, False)`` when it already fits, ``(shortened copy, True)`` with a ``_kortat`` note naming
    every shortened field, or ``(None, True)`` when even that cannot fit (the caller then reports an error).
    """
    if limit <= 0 or client_size(compact_json(data)) <= limit:
        return data, False
    work = json.loads(json.dumps(data))  # deep copy; never mutate the caller's data
    cut_by_field: dict[str, dict[str, Any]] = {}
    note: dict[str, Any] = {
        "meddelande": f"Svaret kortades för att rymmas i klientens gräns ({limit} tecken). "
        "Begränsa urvalet eller bläddra vidare.",
        "kortade": [],
    }

    def record(field: str, before: int, after: int) -> None:
        # One entry per field (also when several lists share a path), so the note stays small.
        entry = cut_by_field.setdefault(field, {"falt": field, "fore": before, "efter": after})
        entry["fore"] = max(entry["fore"], before)
        entry["efter"] = min(entry["efter"], after)
        note["kortade"] = list(cut_by_field.values())

    def fits() -> bool:
        return client_size(compact_json({**work, TRUNCATION_KEY: note})) <= limit

    for _ in range(10_000):
        if fits():
            return {**work, TRUNCATION_KEY: note}, True
        picked = _pick_list(work)
        if picked is None:
            break
        path, lst = picked
        original = len(lst)
        items = list(lst)
        lst[:] = items[:1]
        if not fits():
            # This list alone cannot make the answer fit: halve it and let the others shrink too.
            lst[:] = items[: max(1, original // 2)]
            record(path, original, len(lst))
            continue
        low, high = 1, original - 1  # longest prefix that fits
        while low < high:
            mid = (low + high + 1) // 2
            lst[:] = items[:mid]
            if fits():
                low = mid
            else:
                high = mid - 1
        lst[:] = items[:low]
        record(path, original, low)
    for _ in range(1_000):
        if fits():
            return {**work, TRUNCATION_KEY: note}, True
        found = _longest_string(work)
        if found is None:
            break
        container, key, value = found
        container[key] = value[: max(MIN_STRING, len(value) // 2)] + " … [kortad]"
        record("text", len(value), len(container[key]))
    return ({**work, TRUNCATION_KEY: note}, True) if fits() else (None, True)


def too_large_result(size: int, limit: int) -> CallToolResult:
    """Error answer when even a shortened answer cannot fit the client's limit."""
    return CallToolResult(
        content=[
            TextContent(
                type="text",
                text=f"Svaret är för stort ({size} tecken) för klientens gräns ({limit} tecken) även efter kortning. "
                "Begränsa urvalet (färre värden, perioder eller rader) och försök igen.",
            )
        ],
        is_error=True,
    )


def structured(
    model: BaseModel | Mapping[str, Any], summary: str | None = None, *, drop_empty: bool = True
) -> CallToolResult:
    """Tool result with compact JSON text (no indentation, UTF-8) plus the same
    data as structured content. Annotate the tool as
    ``Annotated[CallToolResult, Model]`` to keep the output schema.

    MCPServer's default rendering indents JSON, which roughly doubles the
    token cost of tabular results; hosts such as Claude Code cap tool output
    at ~25k tokens. Everything is sent in ONE text block: some clients (Eneo)
    join blocks without a separator and never read structured content. The
    size cap (FUZZY_MCP_MAX_OUTPUT_CHARS) is applied per server in build_server.
    """
    data = dump(model) if drop_empty or isinstance(model, BaseModel) else dict(model)
    text = compact_json(data)
    if summary:
        text = f"{summary}\n{text}"
    return CallToolResult(content=[TextContent(type="text", text=text)], structured_content=data)


def compact(value: Any) -> Any:
    """Recursively drop ``None`` values and empty containers from JSON data,
    which keeps tool output small for the model."""
    if isinstance(value, dict):
        cleaned = {k: compact(v) for k, v in value.items()}
        return {k: v for k, v in cleaned.items() if v is not None and v != {} and v != []}
    if isinstance(value, list):
        return [compact(v) for v in value]
    return value


def clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))
