# SPDX-FileCopyrightText: 2026 Deniz Özer
#
# SPDX-License-Identifier: MIT

"""Shared plumbing for the source modules: service container, error
translation for MCP tools and small helpers."""

import bisect
import functools
import heapq
import inspect
import json
import logging
import threading
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

logger = logging.getLogger("fuzzy_mcp")

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
            logger.info("HTTP-klienten mot myndigheterna är stängd")


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
        in_object = isinstance(current, dict)
        for key, value in items:
            label = str(key) if in_object else name
            if isinstance(value, str):
                if len(value) <= (best[0] if best else MIN_STRING + len(CUT_MARKER)):
                    continue  # too short, or no longer than the longest so far
                if any(part in label.lower() for part in ("url", "query", "href")) or value.startswith(
                    ("http://", "https://")
                ):
                    continue
            elif not isinstance(value, dict | list) or (in_object and key in POSITIONAL_KEYS | {TRUNCATION_KEY}):
                continue
            sub = (f"{path}.{key}" if path else str(key)) if in_object else f"{path}[]"
            if isinstance(value, str):
                best = (len(value), current, key, value, sub)
            else:
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


def _json_size(text: str) -> tuple[int, int]:
    """(length, client length) of a compact JSON text. A client JSON-encodes it escaping only quotes and backslashes
    (json.dumps has already escaped every control character), so both lengths add up over the parts of a JSON value
    and ``client_size(text) == client length + 2 + ENVELOPE_CHARS``."""
    return len(text), len(text) + text.count('"') + text.count("\\")


class _Sizes:
    """Exact sizes of the answer fit_to_limit shortens, kept per object and list: a step measures again only what it
    changed and the objects above it, not the whole answer. Every change to the answer is reported with touch()."""

    def __init__(self, root: dict[str, Any]) -> None:
        self.root = root
        # id(node) -> (node, its parent, its key or index there); holding the node keeps every id unique
        self.parents: dict[int, tuple[Any, Any, Any]] = {}
        self.branches: set[int] = set()  # objects and lists holding objects or lists
        self.split: set[int] = set()  # changed below: measured part by part, reusing the sizes of unchanged parts
        self.memo: dict[int, list[Any]] = {}  # id(node) -> [node, length, client length]
        # id(node) -> {key: (child, child's length, child's client length)}: measured children that have changed since
        # node was measured; node's size is corrected by their difference instead of measuring all its parts again
        self.pending: dict[int, dict[Any, tuple[Any, int, int]]] = {}
        self.keys: dict[str, tuple[int, int]] = {}
        # id() of every object and list whose size may have changed since the caller last cleared the set: the node a
        # change is reported for and everything holding it (a change stops early only where an earlier one, not yet
        # measured, already went on up)
        self.changed: set[int] = set()
        self.adopt(root, None, None)

    def adopt(self, node: Any, parent: Any, key: Any) -> None:
        """Register ``node`` (new, or the whole answer) and everything in it."""
        if parent is not None:
            self.parents[id(node)] = (node, parent, key)
            self.touch(node)
        stack = [node]
        while stack:
            current = stack.pop()
            for index, child in current.items() if isinstance(current, dict) else enumerate(current):
                if isinstance(child, dict | list):
                    self.parents[id(child)] = (child, current, index)
                    self.branches.add(id(current))
                    stack.append(child)

    def touch(self, node: Any) -> None:
        """``node`` has changed: measure it again, and correct everything holding it by the difference."""
        self.changed.add(id(node))
        known = self.memo.pop(id(node), None)
        if known is None and id(node) in self.split:
            return  # not measured since it last changed: everything holding it already knows
        self.pending.pop(id(node), None)
        self.split.add(id(node))
        self._propagate(node, known)

    def set(self, container: dict[str, Any] | list[Any], key: Any, value: Any) -> None:
        """``container[key] = value`` where both the old and the new value are strings or numbers: the size known for
        the container is corrected by their difference instead of measuring the container again."""
        self.changed.add(id(container))
        known = self.memo.get(id(container))
        if known is None:
            container[key] = value
            self.touch(container)
            return
        length, client = self.measure(container[key])
        container[key] = value
        new_length, new_client = self.measure(value)
        self._propagate(container, known)
        known[1] += new_length - length
        known[2] += new_client - client

    def _propagate(self, node: Any, known: list[Any] | None) -> None:
        """Tell everything holding ``node`` that it changed; ``known`` is its size before the change, if measured."""
        while (entry := self.parents.get(id(node))) is not None:
            _, parent, key = entry
            self.changed.add(id(parent))
            if known is not None and id(parent) in self.memo:
                waiting = self.pending.setdefault(id(parent), {})
                if key in waiting:  # already changing, and so is everything above
                    return
                waiting[key] = (node, known[1], known[2])
                known = self.memo[id(parent)]
            elif id(parent) in self.split and id(parent) not in self.memo:
                return  # to be measured again already
            else:  # its size is part of the parent's, which must be measured again
                known = self.memo.pop(id(parent), None)
                self.pending.pop(id(parent), None)
                self.split.add(id(parent))
            node = parent

    def attached(self, node: Any) -> bool:
        """Whether ``node`` is still in the answer, i.e. not in an item a shorter list has left out."""
        while node is not self.root:
            entry = self.parents.get(id(node))
            if entry is None:
                return False
            _, parent, key = entry
            if isinstance(parent, list):
                if key >= len(parent) or parent[key] is not node:
                    return False
            elif parent.get(key) is not node:
                return False
            node = parent
        return True

    def key(self, key: str) -> tuple[int, int]:
        known = self.keys.get(key)
        if known is None:
            known = self.keys[key] = _json_size(compact_json(key))
        return known

    def measure(self, node: Any) -> tuple[int, int]:
        """(length, client length) of ``node`` as compact JSON."""
        known = self.memo.get(id(node))  # only objects and lists are kept (and their ids are unique)
        if known is not None:
            if id(node) in self.pending:
                for child, length, client in self.pending.pop(id(node)).values():
                    new_length, new_client = self.measure(child)
                    known[1] += new_length - length
                    known[2] += new_client - client
            return known[1], known[2]
        if not isinstance(node, dict | list):
            return _json_size(compact_json(node))
        if id(node) in self.split and id(node) in self.branches:
            length = client = max(len(node) + 1, 2)  # brackets and commas
            if isinstance(node, dict):
                for key, value in node.items():
                    key_length, key_client = self.key(key)
                    value_length, value_client = self.measure(value)
                    length += key_length + 1 + value_length
                    client += key_client + 1 + value_client
            else:
                for value in node:
                    value_length, value_client = self.measure(value)
                    length += value_length
                    client += value_client
        else:  # unchanged below, or holding only strings and numbers: one serialization
            length, client = _json_size(compact_json(node))
        self.memo[id(node)] = [node, length, client]
        return length, client

    def answer(self, note: dict[str, Any]) -> int:
        """``client_size(compact_json({**root, TRUNCATION_KEY: note}))``, serializing only the note."""
        return (
            self.measure(self.root)[1]
            + (1 if self.root else 0)
            + self.key(TRUNCATION_KEY)[1]
            + 1
            + _json_size(compact_json(note))[1]
            + 2
            + ENVELOPE_CHARS
        )


class ShorteningCancelled(Exception):
    """fit_to_limit stopped because its ``cancel`` event was set: the call it shortened for has given up."""


def _check(cancel: threading.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise ShorteningCancelled


class _Shortening:
    """One run of fit_to_limit on a copy of the answer.

    Each probe ("does the answer fit with the lists cut like this?") is measured exactly, but only what changed since
    the previous probe is measured again (see _Sizes), and a probe updates the list it cut, not every list cut so far.
    A pick compares the largest list of each path, not every list (see pick).
    With ``exact`` every probe and pick looks at the whole answer instead (serializes it, finds the trimmable lists),
    as fit_to_limit did before: the fallback should the incremental sizes ever disagree. Both take the same steps."""

    def __init__(
        self,
        data: dict[str, Any],
        limit: int,
        cuts: tuple[Cut, ...],
        exact: bool,
        cancel: threading.Event | None = None,
    ) -> None:
        self.data = data
        self.limit = limit
        self.exact = exact
        self.cancel = cancel
        self.work: dict[str, Any] = json.loads(json.dumps(data))  # deep copy; never mutate the caller's data
        own = self.work.pop(TRUNCATION_KEY, None)  # a tool's own note (ref_get_code_list) is kept, not replaced
        self.own: dict[str, Any] = own if isinstance(own, dict) else {}
        self.active = tuple(c for c in cuts if c.when is None or self.work.get(c.when[0]) == c.when[1])
        self.contracted = frozenset(c.items for c in self.active)
        self.sizes = _Sizes(self.work)
        # id(list) -> (candidate, length before the first cut, whether the holder's ``returned`` counts this list)
        self.shortened: dict[int, tuple[_Candidate, int, bool]] = {}
        self.strings: dict[str, list[int]] = {}  # path -> [length before, length now]
        self.left_out: list[str] = []
        self.recut: dict[int, list[Any]] = {}  # lists cut since the last probe
        self.counted: dict[int, list[_Candidate]] = {}  # id(holder) -> the cut lists its ``returned`` counts, in order
        # The lists that can still be shortened, in the order _candidates finds them. Cuts only shorten lists, so lists
        # only leave: one cut to its floor, and those in what a cut or a contract leaves out.
        self.live: list[_Candidate] = []
        self.floor: dict[int, int] = {}  # id(list) -> length at or below which it is no longer trimmable
        self.inside: dict[int, list[_Candidate]] = {}  # id(list) -> the trimmable lists inside it
        # id(list) -> (index of the item holding it, list) for the trimmable lists inside it, by index: a list leaves
        # the answer for good once the list it is inside is shorter than its index at the start of a pick
        self.within: dict[int, list[tuple[int, _Candidate]]] = {}
        # Without exact, pick() keeps per path a heap of (-size, order in _candidates, id) of its lists in live, and a
        # heap of (-own weight, order, path) of the largest list of every path. Both heaps keep entries left behind by a
        # change (a list that changed size or left, a path whose largest list or nested lists changed); a pick drops
        # them when they come to the top.
        self.by_id: dict[int, _Candidate] = {}
        self.order: dict[int, int] = {}
        self.alive: set[int] = set()  # id() of the lists in live
        self.size: dict[int, int] = {}  # id(list) -> its length as compact JSON, as of the last pick
        self.heaps: dict[str, list[tuple[int, int, int]]] = {}
        self.totals: dict[str, int] = {}  # path -> the sum of the sizes of its lists in live
        self.counts: dict[str, int] = {}  # path -> number of its lists in live
        self.inner: dict[str, int] = {}  # path -> the sum of totals at the paths nested in it
        self.outer: dict[str, list[str]] = {}  # path -> the paths it is nested in
        self.ranking: list[tuple[int, int, str]] = []
        self.weight: dict[str, tuple[int, int]] = {}  # path -> (own weight, order) of its largest list in live
        self.stale: set[str] = set()  # paths whose weight may have changed since the last pick
        self.rounds = 0  # picks at most: each pick halves a list or makes the answer fit
        self.emptied: set[int] = set()  # lists holding trimmable lists, cut since the last pick
        self.recheck = False  # whether any list in live may have been left out
        self.presence: dict[int, bool] = {}  # id(list) -> whether it is in the answer, as at the start of the probe
        self.moved = False  # whether presence is out of date (as from the next probe)
        self.reachable: set[int] = set()  # with exact: id() of every list in the answer at the start of the probe
        self.last: _Candidate | None = None  # the list picked last
        self.nested: dict[str, list[str]] = {}  # path -> the paths of trimmable lists inside lists at that path
        self.beside: list[_Candidate] = []  # lists without a contract, in the object a contract updates
        # what the contracts add to the note in the current probe
        self.resumes: list[dict[str, Any]] = []
        self.named: list[dict[str, Any]] = []
        self.hints: list[str] = []

    def run(self) -> tuple[dict[str, Any], int]:
        """Shorten until the answer fits or nothing more can be shortened; the answer and its measured size."""
        for key in DROP_FIRST_KEYS:
            if key in self.work and not self.fits():
                del self.work[key]
                self.sizes.touch(self.work)
                self.left_out.append(key)
        self.check()  # before finding and measuring every list
        self.index()
        for _ in range(self.rounds):
            self.check()
            if self.fits():
                return self.finish()
            picked = self.last = self.pick()
            if picked is None:
                break
            returned = picked.holder.get("returned")
            counted = isinstance(returned, int) and not isinstance(returned, bool) and returned == len(picked.items)
            if id(picked.items) not in self.shortened:
                self.shortened[id(picked.items)] = (picked, len(picked.items), counted)
                if counted:
                    self.counted.setdefault(id(picked.holder), []).append(picked)
            lst = picked.items
            original = len(lst)
            items = list(lst)
            self.cut(lst, items[:1])
            if not self.fits():
                # This list alone cannot make the answer fit: halve it and let the others shrink too.
                self.cut(lst, items[: max(1, original // 2)])
                continue
            low, high = 1, original - 1  # longest prefix that fits
            while low < high:
                mid = (low + high + 1) // 2
                self.cut(lst, items[:mid])
                if self.fits():
                    low = mid
                else:
                    high = mid - 1
            self.cut(lst, items[:low])
        for _ in range(1_000):
            self.check()
            if self.fits():
                return self.finish()
            found = _longest_string(self.work)
            if found is None:
                break
            container, key, value, path = found
            shorter = value.removesuffix(CUT_MARKER)
            self.sizes.set(container, key, shorter[: max(MIN_STRING, len(shorter) // 2)] + CUT_MARKER)
            lengths = self.strings.setdefault(path, [len(value), 0])
            lengths[1] = len(container[key])
        return self.finish()

    def index(self) -> None:
        """Find the trimmable lists once; afterwards a list only stops being trimmable (see live)."""
        self.live = _candidates(self.work)
        ids = {id(c.items) for c in self.live}
        for c in self.live:
            numbers = next(i for i, x in enumerate(c.items) if not isinstance(x, int | float) or isinstance(x, bool))
            self.floor[id(c.items)] = max(1, numbers)  # a prefix of numbers only is not trimmable
            entry = self.sizes.parents.get(id(c.items))
            while entry is not None:
                if id(entry[1]) in ids:
                    self.inside.setdefault(id(entry[1]), []).append(c)
                    self.within.setdefault(id(entry[1]), []).append((entry[2], c))
                entry = self.sizes.parents.get(id(entry[1]))
        for entries in self.within.values():
            entries.sort(key=lambda entry: entry[0])
        # A pick halves a list (its length loses a binary digit) or cuts it so that the answer fits.
        self.rounds = sum(len(c.items).bit_length() for c in self.live) + 2
        paths = sorted({c.path for c in self.live})
        for path in paths:
            start = end = bisect.bisect_left(paths, path + "[")
            while end < len(paths) and paths[end].startswith(path + "["):
                end += 1
            self.nested[path] = paths[start:end]
        if not self.exact:
            for order, c in enumerate(self.live):
                key = id(c.items)
                self.by_id[key], self.order[key] = c, order
                self.alive.add(key)
                self.size[key] = self.sizes.measure(c.items)[0]
                self.heaps.setdefault(c.path, []).append((-self.size[key], order, key))
                self.totals[c.path] = self.totals.get(c.path, 0) + self.size[key]
                self.counts[c.path] = self.counts.get(c.path, 0) + 1
            for heap in self.heaps.values():
                heapq.heapify(heap)
            for path, inner in self.nested.items():
                self.inner[path] = sum(self.totals[p] for p in inner)
                for p in inner:
                    self.outer.setdefault(p, []).append(path)
            self.stale = set(paths)
            self.sizes.changed.clear()
        holders = [_resolve(self.work, cut.items)[0] for cut in self.active]
        self.beside = [c for c in self.live if c.path not in self.contracted and any(c.holder is h for h in holders)]

    def check(self) -> None:
        _check(self.cancel)

    def pick(self) -> _Candidate | None:
        """The trimmable list with the largest own weight (its size minus nested trimmable lists), so long inner value
        lists shrink before whole records are dropped. Lists a paging contract names go first: shortening them leaves
        the answer resumable. The first of equals in _candidates order.

        The nested trimmable lists are those at the paths inside the list's path, in the whole answer, so their size is
        the same for every list at a path: only the largest list of each path can have the largest own weight. Without
        exact, a pick therefore measures only the lists that changed, and ranks the largest list of each path."""
        if self.exact:
            return self.pick_exact()
        if self.recheck:
            for key in [key for key in self.alive if not self.sizes.attached(self.by_id[key].items)]:
                self.leave(key)
            self.recheck = False
        for key in self.emptied:
            within, length = self.within[key], len(self.by_id[key].items)
            while within and within[-1][0] >= length:
                self.leave(id(within.pop()[1].items))
        self.emptied.clear()
        if self.last is not None and len(self.last.items) <= self.floor[id(self.last.items)]:
            self.leave(id(self.last.items))
        for key in self.sizes.changed & self.alive:
            size = self.sizes.measure(self.by_id[key].items)[0]
            if size != self.size[key]:
                self.add(self.by_id[key].path, size - self.size[key])
                self.size[key] = size
                heapq.heappush(self.heaps[self.by_id[key].path], (-size, self.order[key], key))
        self.sizes.changed.clear()
        for path in self.stale:
            if self.counts[path]:
                size, order, _ = self.largest(path)
                self.weight[path] = (size - self.inner[path], order)
                heapq.heappush(self.ranking, (self.inner[path] - size, order, path))
            else:
                self.weight.pop(path, None)
        self.stale.clear()
        if any(self.counts.get(path) for path in self.contracted):
            # Only the lists a contract names, and only their sizes in the nested lists (the contracts name few paths).
            best: tuple[int, int, str] | None = None  # (own weight, -order, path)
            for path in self.contracted:
                if self.counts.get(path):
                    size, order, _ = self.largest(path)
                    inner = sum(self.totals[p] for p in self.nested[path] if p in self.contracted)
                    if best is None or (size - inner, -order) > best[:2]:
                        best = (size - inner, -order, path)
            return self.by_id[self.largest(best[2])[2]] if best else None
        while self.ranking:
            weight, order, path = self.ranking[0]
            if self.weight.get(path) == (-weight, order):
                return self.by_id[self.largest(path)[2]]
            heapq.heappop(self.ranking)  # left behind
        return None

    def largest(self, path: str) -> tuple[int, int, int]:
        """(size, order, id) of the largest list in live at ``path``, the first of equals."""
        heap = self.heaps[path]
        while heap[0][2] not in self.alive or self.size[heap[0][2]] != -heap[0][0]:
            heapq.heappop(heap)  # left behind
        return -heap[0][0], heap[0][1], heap[0][2]

    def add(self, path: str, change: int) -> None:
        """The lists in live at ``path`` grew by ``change`` (negative: shrank) in all."""
        self.totals[path] += change
        self.stale.add(path)
        for outer in self.outer.get(path, ()):
            self.inner[outer] += change
            self.stale.add(outer)

    def leave(self, key: int) -> None:
        """The list ``key`` is no longer trimmable (cut to its floor, or no longer in the answer)."""
        if key in self.alive:
            self.alive.remove(key)
            path = self.by_id[key].path
            self.counts[path] -= 1
            self.add(path, -self.size[key])

    def pick_exact(self) -> _Candidate | None:
        """pick() looking at the whole answer: every list found and measured again."""
        self.live = _candidates(self.work)
        self.emptied.clear()
        if self.last is not None and len(self.last.items) <= self.floor[id(self.last.items)]:
            self.live = [c for c in self.live if c.items is not self.last.items]
        pool = [c for c in self.live if c.path in self.contracted] or self.live
        if not pool:
            return None
        sizes = [len(compact_json(c.items)) for c in pool]
        if any(self.nested.values()):
            totals: dict[str, int] = {}
            for c, size in zip(pool, sizes, strict=True):
                totals[c.path] = totals.get(c.path, 0) + size
            nested = {path: sum(totals.get(inner, 0) for inner in self.nested[path]) for path in totals}
            sizes = [size - nested[c.path] for c, size in zip(pool, sizes, strict=True)]
        return pool[sizes.index(max(sizes))]  # the first of equals

    def cut(self, lst: list[Any], items: list[Any]) -> None:
        lst[:] = items
        self.sizes.touch(lst)
        self.recut[id(lst)] = lst
        if id(lst) in self.inside:  # the lists in the items it left out (or put back) left (or are back)
            self.emptied.add(id(lst))
            for c in self.inside[id(lst)]:
                self.presence.pop(id(c.items), None)

    def dropped(self) -> None:
        """Lists may have left the answer: pick() checks every list again, and so does the next probe (a probe sees
        the answer as it was when the probe started, like the cuts before it)."""
        self.recheck = self.moved = True

    def present(self, lst: list[Any]) -> bool:
        known = self.presence.get(id(lst))
        if known is None:
            known = self.presence[id(lst)] = id(lst) in self.reachable if self.exact else self.sizes.attached(lst)
        return known

    def fits(self) -> bool:
        self.settle(full=self.exact)
        if self.exact:
            return client_size(compact_json({**self.work, TRUNCATION_KEY: self.note()})) <= self.limit
        if self.sizes.measure(self.work)[1] + 2 + ENVELOPE_CHARS > self.limit:  # too large even before the note
            return False
        return self.sizes.answer(self.note()) <= self.limit

    def finish(self) -> tuple[dict[str, Any], int]:
        """The answer as it would be sent, and its size as the probes measure it."""
        self.settle(full=True)
        note = self.note()
        answer = {**self.work, TRUNCATION_KEY: note}
        return answer, client_size(compact_json(answer)) if self.exact else self.sizes.answer(note)

    def mark(self, c: _Candidate) -> None:
        """Flag a cut list where it is: ``<list>_truncated`` beside it and ``truncated`` on every object above it (when
        they have those fields); without a contract its paging pointers go (a dead end, never a gap)."""
        for obj, flag in ((c.holder, f"{c.key}_truncated"), *((obj, "truncated") for obj in c.ancestors)):
            if obj.get(flag) is False:
                self.sizes.set(obj, flag, True)
        if c.path not in self.contracted:
            for key in CONTINUATION_KEYS:
                if key in c.holder:
                    if isinstance(c.holder.pop(key), dict | list):
                        self.dropped()
                    self.sizes.touch(c.holder)

    def count(self, holder: dict[str, Any], returned: int) -> None:
        if holder.get("returned") != returned:  # counted: the holder has an int returned
            self.sizes.set(holder, "returned", returned)

    def settle(self, full: bool) -> None:
        """Update the answer for everything cut so far: flags, ``returned`` and the contracts' paging fields.

        The updates of a list are the same at every probe once it is cut, so a probe updates only the lists cut since
        the previous one (``full`` updates every list). Exceptions: ``returned`` is the length of the last cut list it
        counts, and a contract writes its paging fields again at every probe, so the lists beside it remove theirs again
        first. Lists that a later cut leaves out, or puts back, need no update: their objects go and come with them."""
        if self.moved or self.exact:
            self.presence.clear()
            self.moved = False
            self.reachable = _reachable_lists(self.work) if self.exact else set()
        if full:
            for c, _, counted in self.shortened.values():
                if self.present(c.items):
                    self.mark(c)
                    if counted:
                        self.count(c.holder, len(c.items))
        else:
            for lst in self.recut.values():
                c, _, counted = self.shortened[id(lst)]
                if self.present(lst):
                    self.mark(c)
                    if counted:
                        self.count(c.holder, len(self.counted[id(c.holder)][-1].items))
            for c in self.beside:
                if id(c.items) in self.shortened and self.present(c.items):
                    self.mark(c)
        self.recut.clear()
        self.resumes, self.named, self.hints = [], [], []
        for cut in self.active:
            holder, items = _resolve(self.work, cut.items)
            _, original = _resolve(self.data, cut.items)
            if not isinstance(holder, dict) or not isinstance(items, list) or not isinstance(original, list):
                continue
            if len(items) >= len(original):
                continue
            notes = self.work.get("notes")
            if cut.stale_notes and isinstance(notes, list):
                kept = [n for n in notes if not (isinstance(n, str) and n.startswith(cut.stale_notes))]
                if len(kept) < len(notes):
                    self.work["notes"] = kept
                    self.sizes.adopt(kept, self.work, "notes")
            # A field that holds objects or lists, removed or replaced, may have held trimmable lists (CursorCut removes
            # hint and adds next_page and complete_scan, so the number of fields may stay the same).
            fields = [(key, value) for key, value in holder.items() if isinstance(value, dict | list)]
            resumed = cut.resume(self.work, holder, len(items), original)
            self.sizes.touch(holder)
            if any(holder.get(key) is not value for key, value in fields):
                self.dropped()
            if resumed:
                (self.resumes if "anrop" in resumed else self.named).append({"falt": cut.items, **resumed})
            if cut.hint:
                self.hints.append(cut.hint)

    def note(self) -> dict[str, Any]:
        """The ``_kortat`` note for everything shortened so far (after settle)."""
        totals: dict[str, dict[str, Any]] = {}
        for c, before, _ in self.shortened.values():
            if self.present(c.items):
                entry = totals.setdefault(c.path, {"falt": c.path, "fore": 0, "efter": 0, "listor": 0})
                entry["fore"] += before
                entry["efter"] += len(c.items)
                entry["listor"] += 1
        kortade = [e for e in self.own.get("kortade", []) if isinstance(e, dict)]
        kortade += [
            {k: v for k, v in e.items() if k != "listor" or v > 1} for e in totals.values() if e["efter"] < e["fore"]
        ]
        kortade += [
            {"falt": path, "fore": lengths[0], "efter": lengths[1], "enhet": "tecken"}
            for path, lengths in self.strings.items()
        ]
        message = " ".join(
            part
            for part in (
                str(self.own.get("meddelande") or ""),
                f"Svaret kortades för att rymmas i klientens gräns ({self.limit} tecken).",
                "Fälten i kortade är inte fullständiga." if kortade else "",
                "raw utelämnades helt (anropa utan include_raw om du inte behöver det)." if self.left_out else "",
                "Hämta resten utan luckor med fortsatt: samma verktyg med övriga argument oförändrade och värdena i "
                "anrop, eller verktyget i verktyg med exakt argumenten i anrop."
                if self.resumes
                else "Posterna i utelamnade saknas i svaret."
                if self.named
                else "Begränsa urvalet för att få allt."
                if kortade
                else "",
            )
            if part
        ) + "".join(self.hints)
        note: dict[str, Any] = {"meddelande": message, "kortade": kortade}
        if self.left_out:
            note["utelamnade_falt"] = self.left_out
        if self.resumes:
            note["fortsatt"] = self.resumes
        if self.named:
            note["utelamnade"] = self.named
        return note


def fit_to_limit(
    data: dict[str, Any], limit: int, cuts: tuple[Cut, ...] = (), *, cancel: threading.Event | None = None
) -> tuple[dict[str, Any] | None, bool]:
    """Shorten lists (and, if needed, long strings) until the answer fits ``limit`` as a client measures it.

    The answer stays truthful: the object holding a shortened list gets ``<list>_truncated=true`` and ``returned`` =
    the new length (when it has those fields), every object on the way to it gets ``truncated=true``, and the tool's
    ``cuts`` contracts move its paging fields so that a client following them never skips an item. Without a
    contract, paging fields beside a shortened list are removed (a dead end, never a gap). Returns ``(data, False)``
    when it already fits, ``(shortened copy, True)`` with a ``_kortat`` note, or ``(None, True)`` when even that
    cannot fit.

    Each step is measured incrementally and a pick looks only at the lists that changed (see _Shortening), so the
    time grows about linearly with the number of lists (measured in #41: 5 100 lists of records in about a second).
    The result is measured whole once more; should that ever disagree, every step is measured whole instead.

    ``cancel`` stops the work between two steps with :class:`ShorteningCancelled` once it is set: the server sets it
    when the call has given up (FUZZY_MCP_TOOL_TIMEOUT), so that an abandoned shortening does not go on in its thread.
    """
    if limit <= 0 or client_size(compact_json(data)) <= limit:
        return data, False
    _check(cancel)  # before the copy of the answer, too
    answer, size = _Shortening(data, limit, cuts, exact=False, cancel=cancel).run()
    measured = client_size(compact_json(answer))
    if size != measured:  # never expected: the incremental sizes are exact
        logger.warning(
            "Kortningen mätte svaret till %d tecken men det har %d; kortar om med full mätning", size, measured
        )
        _check(cancel)
        answer, size = _Shortening(data, limit, cuts, exact=True, cancel=cancel).run()
    return (answer, True) if size <= limit else (None, True)


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
