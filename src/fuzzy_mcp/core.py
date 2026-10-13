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


def preserve_output(fn: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
    """Mark a tool's answer as indivisible: reject it if it exceeds the configured output cap.

    Query recipes contain executable code and selections whose meaning must not change. The server reads this
    marker before applying its output wrapper; functools.wraps preserves it through other tool decorators.
    """
    fn.__dict__["_fuzzy_preserve_output"] = True
    return fn


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
# Lists that explain, reproduce or index the answer rather than being it: never shortened, because a cut would change
# what the answer means without any field saying so. Long strings inside them may still be shortened; when they alone
# do not fit, the caller reports an error.
PROTECTED_KEYS = POSITIONAL_KEYS | {
    TRUNCATION_KEY,
    "notes",
    "notices",
    "sections",
    "filters",
    "selection",
    "publisher_uris",
    "media_types",
    "requested",
    "dimensions",
    "value_map",
    "path",
    "available_school_forms",
    "available_surveys",
    "forbehall",
    "kopplingsnycklar",
}
# Every entry is needed to read the rest (PxWeb metadata variables): the list keeps its length, the lists inside its
# entries may shrink.
KEEP_LENGTH_KEYS = frozenset({"variables"})
# Opt-in verbatim payloads (include_raw): left out whole, before anything else is shortened.
DROP_FIRST_KEYS = ("raw",)
# A tool's pointers to the next page of the answer it built; wrong once the server has shortened that page.
CONTINUATION_KEYS = ("next_offset", "next_page", "next_skip_matches")
CUT_MARKER = " … [kortad]"
MIN_STRING = 200


def client_size(text: str) -> int:
    """Size of a text answer after a client JSON-encodes it (as Eneo does: UTF-8 kept, quotes escaped)."""
    return len(json.dumps(text, ensure_ascii=False)) + ENVELOPE_CHARS


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


# --- Paging contracts: how the output cap moves a tool's own paging fields after shortening its list -------------


@dataclass(frozen=True)
class Cut:
    """What the output cap updates in a tool's answer when it shortens the list at ``items`` (a dotted path of object
    keys, e.g. ``"datasets"`` or ``"school_units.items"``). ``truncated``, ``<list>_truncated`` and ``returned`` are
    updated for every tool; a contract adds what only the tool's paging defines, read and written in the object that
    holds the list. ``stale_notes`` are prefixes of the tool's own notes that describe the uncut list."""

    items: str
    stale_notes: tuple[str, ...] = ()
    when: tuple[str, Any] | None = None  # applies only when this top-level field has this value
    hint: str | None = None  # appended to the note's message when the contract applied

    def resume(
        self, root: dict[str, Any], holder: dict[str, Any], kept: int, original: list[Any]
    ) -> dict[str, Any] | None:
        """``{"anrop": arguments}`` for the call that continues after the ``kept`` first items, or ``{"koder":
        codes}`` naming the items left out; None when neither exists."""
        return None


@dataclass(frozen=True)
class OffsetCut(Cut):
    """Item-granular offset paging: the next call starts at ``offset + kept``. Exact when every row of the page is
    shown; when the tool dropped unusable rows before the cut it repeats a few rows but never skips one."""

    next_field: str | None = "next_offset"  # None: the tool has no such field (fortsatt is the pointer)
    tool: str | None = None  # continue in another tool (skolverket_school_unit_changes)
    call: Callable[[dict[str, Any], dict[str, Any], int], dict[str, Any]] | None = None

    def resume(self, root, holder, kept, original):
        position = int(holder.get("offset") or 0) + kept
        if self.next_field:
            holder[self.next_field] = position
        entry: dict[str, Any] = {"anrop": self.call(root, holder, position) if self.call else {"offset": position}}
        if self.tool:
            entry["verktyg"] = self.tool
        return entry


def page_continuation(first_unseen: int, kept: int) -> tuple[int, int]:
    """(page, page size) for continuing at item ``first_unseen`` (0-based) of a page-numbered list after ``kept``
    items of a page were shown. No page number starts in the middle of a page, so the page size changes: the size
    between ``ceil(kept / 2)`` and ``kept`` that repeats the fewest already shown items (none on the first page) is
    chosen. The page starts at or before ``first_unseen``, so nothing is skipped, and after the shown items' start, so
    paging always progresses."""
    size = min(range(kept, (kept + 1) // 2 - 1, -1), key=lambda s: (first_unseen % s, -s))
    return first_unseen // size, size


@dataclass(frozen=True)
class PageCut(Cut):
    """Page-numbered paging (page/page size): ``next_page`` would skip the rest of the shortened page, so it is
    removed and ``fortsatt`` gives a page and page size that start at or before the first item not shown."""

    page_argument: str = "page"
    size_argument: str = "page_size"
    first_page: int = 0  # SCB counts pages from 1, Skolverket's Planerad utbildning from 0
    hint: str | None = " Behåll sidstorleken från fortsatt när du bläddrar vidare (total_pages gäller den gamla)."

    def resume(self, root, holder, kept, original):
        first_unseen = (int(holder["page"]) - self.first_page) * int(holder["page_size"]) + kept
        page, size = page_continuation(first_unseen, kept)
        holder.pop("next_page", None)
        return {"anrop": {self.page_argument: page + self.first_page, self.size_argument: size}}


@dataclass(frozen=True)
class CursorCut(Cut):
    """Susa-navet's start_page/skip_matches cursor, as a backstop: the tools fit their rows themselves (they know on
    which page every match was found). Counting every page as full puts the cursor at or before the first row not
    shown: exact without client filters, never past a row with them (pages then hold fewer matches)."""

    hint: str | None = " Behåll limit, page_size och filtren oförändrade."

    def resume(self, root, holder, kept, original):
        size = max(1, int(holder.get("page_size") or 1))
        skipped = min(int(holder.get("skip_matches") or 0), size)  # skipping past a page equals skipping the page
        page, skip = divmod(int(holder.get("start_page") or 0) * size + skipped + kept, size)
        holder["next_page"] = page  # also 0: the cursor is set, not the end of the list
        holder["complete_scan"] = False
        if skip:
            holder["next_skip_matches"] = skip
        else:
            holder.pop("next_skip_matches", None)
        holder.pop("hint", None)  # its resume clause and counts describe the uncut answer
        return {"anrop": {"start_page": page, "skip_matches": skip}}


@dataclass(frozen=True)
class CodesCut(Cut):
    """Items named by a code: ``argument`` asks for the left-out items again; without it the codes are listed."""

    code: str = "code"
    argument: str | None = None

    def resume(self, root, holder, kept, original):
        codes = [item.get(self.code) if isinstance(item, dict) else None for item in original[kept:]]
        if not codes or any(code is None for code in codes):
            return None
        return {"anrop": {self.argument: codes}} if self.argument else {"koder": codes}


def cut_contract(*cuts: Cut) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
    """Declare how the output cap updates a tool's paging fields when it shortens the tool's list. Read by the server
    like :func:`preserve_output`; functools.wraps carries the marker through the other tool decorators."""

    def mark(fn: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
        fn.__dict__["_fuzzy_cuts"] = cuts
        return fn

    return mark


# --- fit_to_limit ---------------------------------------------------------------------------------------------------


@dataclass
class _Candidate:
    path: str
    items: list[Any]
    holder: dict[str, Any]  # the object holding the list
    key: str
    ancestors: tuple[dict[str, Any], ...]  # every object from the root down to the holder


def _candidates(node: Any, path: str = "", ancestors: tuple[dict[str, Any], ...] = ()) -> list[_Candidate]:
    """Trimmable lists: records or values under a key. Lists directly inside lists (table rows, coordinate pairs),
    lists of numbers and anything under ``PROTECTED_KEYS`` are kept whole; ``KEEP_LENGTH_KEYS`` lists keep their
    length while the lists in their entries may shrink."""
    found: list[_Candidate] = []
    if isinstance(node, dict):
        inner = (*ancestors, node)
        for key, value in node.items():
            if key in PROTECTED_KEYS:
                continue
            sub = f"{path}.{key}" if path else str(key)
            if isinstance(value, list):
                numeric = all(isinstance(x, int | float) and not isinstance(x, bool) for x in value)
                if len(value) > 1 and not numeric and key not in KEEP_LENGTH_KEYS:
                    found.append(_Candidate(sub, value, node, key, inner))
                for item in value:
                    found += _candidates(item, f"{sub}[]", inner)
            else:
                found += _candidates(value, sub, inner)
    elif isinstance(node, list):  # a list inside a list is never trimmed itself, but its records may hold lists
        for item in node:
            found += _candidates(item, f"{path}[]", ancestors)
    return found


def _pick_list(data: dict[str, Any], prefer: frozenset[str] = frozenset()) -> _Candidate | None:
    """The trimmable list with the largest own weight (its size minus nested trimmable lists), so long inner value
    lists shrink before whole records are dropped. Lists a paging contract names (``prefer``) go first: shortening
    them leaves the answer resumable."""
    candidates = _candidates(data)
    preferred = [c for c in candidates if c.path in prefer]
    candidates = preferred or candidates
    sizes = {id(c.items): len(compact_json(c.items)) for c in candidates}
    best: tuple[int, _Candidate] | None = None
    for c in candidates:
        nested = sum(
            sizes[id(o.items)] for o in candidates if o.items is not c.items and o.path.startswith(c.path + "[")
        )
        own = sizes[id(c.items)] - nested
        if best is None or own > best[0]:
            best = (own, c)
    return best[1] if best else None


def _longest_string(node: Any) -> tuple[dict[str, Any] | list[Any], Any, str, str] | None:
    """The longest string that may be shortened: not a URL or a query (shortened, it would no longer work), nothing
    positional, and long enough that shortening it again makes it shorter."""
    best: tuple[int, Any, Any, str, str] | None = None
    stack: list[tuple[Any, str, str]] = [(node, "", "")]
    while stack:
        current, path, name = stack.pop()
        items = (
            current.items() if isinstance(current, dict) else enumerate(current) if isinstance(current, list) else ()
        )
        for key, value in items:
            sub = (f"{path}.{key}" if path else str(key)) if isinstance(current, dict) else f"{path}[]"
            label = str(key) if isinstance(current, dict) else name
            if isinstance(value, str):
                eligible = (
                    len(value) > MIN_STRING + len(CUT_MARKER)
                    and not any(part in label.lower() for part in ("url", "query", "href"))
                    and not value.startswith(("http://", "https://"))
                )
                if eligible and (best is None or len(value) > best[0]):
                    best = (len(value), current, key, value, sub)
            elif isinstance(value, dict | list) and not (
                isinstance(current, dict) and key in POSITIONAL_KEYS | {TRUNCATION_KEY}
            ):
                stack.append((value, sub, label))
    return (best[1], best[2], best[3], best[4]) if best else None


def _resolve(data: Any, dotted: str) -> tuple[dict[str, Any] | None, Any]:
    holder: Any = None
    node = data
    for part in dotted.split("."):
        if not isinstance(node, dict):
            return None, None
        holder, node = node, node.get(part)
    return holder, node


def _reachable_lists(node: Any) -> set[int]:
    seen: set[int] = set()
    stack = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, list):
            seen.add(id(current))
            stack.extend(current)
        elif isinstance(current, dict):
            stack.extend(current.values())
    return seen


def fit_to_limit(data: dict[str, Any], limit: int, cuts: tuple[Cut, ...] = ()) -> tuple[dict[str, Any] | None, bool]:
    """Shorten lists (and, if needed, long strings) until the answer fits ``limit`` as a client measures it.

    The answer stays truthful: the object holding a shortened list gets ``<list>_truncated=true`` and ``returned`` =
    the new length (when it has those fields), every object on the way to it gets ``truncated=true``, and the tool's
    ``cuts`` contracts move its paging fields so that a client following them never skips an item. Without a
    contract, paging fields beside a shortened list are removed (a dead end, never a gap). Returns ``(data, False)``
    when it already fits, ``(shortened copy, True)`` with a ``_kortat`` note, or ``(None, True)`` when even that
    cannot fit.
    """
    if limit <= 0 or client_size(compact_json(data)) <= limit:
        return data, False
    work = json.loads(json.dumps(data))  # deep copy; never mutate the caller's data
    own = work.pop(TRUNCATION_KEY, None)  # a tool's own note (ref_get_code_list) is kept, not replaced
    own = own if isinstance(own, dict) else {}
    active = tuple(c for c in cuts if c.when is None or work.get(c.when[0]) == c.when[1])
    contracted = frozenset(c.items for c in active)
    # id(list) -> (candidate, length before the first cut, whether the holder's ``returned`` counts this list)
    shortened: dict[int, tuple[_Candidate, int, bool]] = {}
    strings: dict[str, list[int]] = {}  # path -> [length before, length now]
    left_out: list[str] = []

    def finish() -> dict[str, Any]:
        """The answer as it would be sent: flags, contracts and the note for everything shortened so far."""
        reachable = _reachable_lists(work)
        current = [(c, before, counted) for c, before, counted in shortened.values() if id(c.items) in reachable]
        for c, _, counted in current:
            if isinstance(c.holder.get(f"{c.key}_truncated"), bool):
                c.holder[f"{c.key}_truncated"] = True
            if counted:
                c.holder["returned"] = len(c.items)
            for obj in c.ancestors:
                if isinstance(obj.get("truncated"), bool):
                    obj["truncated"] = True
            if c.path not in contracted:
                for key in CONTINUATION_KEYS:
                    c.holder.pop(key, None)
        resumes: list[dict[str, Any]] = []
        named: list[dict[str, Any]] = []
        hints: list[str] = []
        for cut in active:
            holder, items = _resolve(work, cut.items)
            _, original = _resolve(data, cut.items)
            if not isinstance(holder, dict) or not isinstance(items, list) or not isinstance(original, list):
                continue
            if len(items) >= len(original):
                continue
            notes = work.get("notes")
            if cut.stale_notes and isinstance(notes, list):
                work["notes"] = [n for n in notes if not (isinstance(n, str) and n.startswith(cut.stale_notes))]
            resumed = cut.resume(work, holder, len(items), original)
            if resumed:
                (resumes if "anrop" in resumed else named).append({"falt": cut.items, **resumed})
            if cut.hint:
                hints.append(cut.hint)
        totals: dict[str, dict[str, Any]] = {}
        for c, before, _ in current:
            entry = totals.setdefault(c.path, {"falt": c.path, "fore": 0, "efter": 0, "listor": 0})
            entry["fore"] += before
            entry["efter"] += len(c.items)
            entry["listor"] += 1
        kortade = [e for e in own.get("kortade", []) if isinstance(e, dict)]
        kortade += [
            {k: v for k, v in e.items() if k != "listor" or v > 1} for e in totals.values() if e["efter"] < e["fore"]
        ]
        kortade += [
            {"falt": path, "fore": lengths[0], "efter": lengths[1], "enhet": "tecken"}
            for path, lengths in strings.items()
        ]
        message = " ".join(
            part
            for part in (
                str(own.get("meddelande") or ""),
                f"Svaret kortades för att rymmas i klientens gräns ({limit} tecken).",
                "Fälten i kortade är inte fullständiga." if kortade else "",
                "raw utelämnades helt (anropa utan include_raw om du inte behöver det)." if left_out else "",
                "Hämta resten utan luckor med fortsatt: samma verktyg med övriga argument oförändrade och värdena i "
                "anrop, eller verktyget i verktyg med exakt argumenten i anrop."
                if resumes
                else "Posterna i utelamnade saknas i svaret."
                if named
                else "Begränsa urvalet för att få allt.",
            )
            if part
        ) + "".join(hints)
        note: dict[str, Any] = {"meddelande": message, "kortade": kortade}
        if left_out:
            note["utelamnade_falt"] = left_out
        if resumes:
            note["fortsatt"] = resumes
        if named:
            note["utelamnade"] = named
        return {**work, TRUNCATION_KEY: note}

    def fits() -> bool:
        return client_size(compact_json(finish())) <= limit

    for key in DROP_FIRST_KEYS:
        if key in work and not fits():
            del work[key]
            left_out.append(key)
    for _ in range(10_000):
        if fits():
            return finish(), True
        picked = _pick_list(work, contracted)
        if picked is None:
            break
        returned = picked.holder.get("returned")
        counted = isinstance(returned, int) and not isinstance(returned, bool) and returned == len(picked.items)
        shortened.setdefault(id(picked.items), (picked, len(picked.items), counted))
        lst = picked.items
        original = len(lst)
        items = list(lst)
        lst[:] = items[:1]
        if not fits():
            # This list alone cannot make the answer fit: halve it and let the others shrink too.
            lst[:] = items[: max(1, original // 2)]
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
    for _ in range(1_000):
        if fits():
            return finish(), True
        found = _longest_string(work)
        if found is None:
            break
        container, key, value, path = found
        shorter = value.removesuffix(CUT_MARKER)
        container[key] = shorter[: max(MIN_STRING, len(shorter) // 2)] + CUT_MARKER
        lengths = strings.setdefault(path, [len(value), 0])
        lengths[1] = len(container[key])
    return (finish(), True) if fits() else (None, True)


def fit_count(build: Callable[[int], BaseModel | Mapping[str, Any]], count: int, limit: int) -> int:
    """The largest n <= count whose answer ``build(n)`` fits ``limit`` as the server's output cap measures it (at
    least 1: the cap is the backstop). For tools that know more about their items than the generic cap does."""
    if limit <= 0 or count <= 1:
        return count

    def fits(n: int) -> bool:
        return client_size(compact_json(dump(build(n)))) <= limit

    if fits(count):
        return count
    low, high = 1, count - 1
    while low < high:
        mid = (low + high + 1) // 2
        if fits(mid):
            low = mid
        else:
            high = mid - 1
    return low


def too_large_result(size: int, limit: int, *, preserve_output: bool = False) -> CallToolResult:
    """Error answer when even a shortened answer cannot fit the client's limit."""
    reason = (
        "Svaret innehåller en återanvändbar fråga och får inte kortas eftersom urval eller körbar kod då kan ändras. "
        if preserve_output
        else "Svaret ryms inte ens efter kortning. "
    )
    return CallToolResult(
        content=[
            TextContent(
                type="text",
                text=f"Svaret är för stort ({size} tecken) för klientens gräns ({limit} tecken). "
                + reason
                + "Begränsa urvalet (färre värden, perioder eller rader) och försök igen.",
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
