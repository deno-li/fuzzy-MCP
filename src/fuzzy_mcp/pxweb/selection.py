# SPDX-FileCopyrightText: 2026 Deniz Özer
#
# SPDX-License-Identifier: MIT

"""PxWeb selections shared by PxWebApi 2 (SCB) and PxWeb API v1 (FoHM).

A selection is ``{variable_code: [value_code_or_expression, ...]}``. The
expression language is the PxWebApi 2 one:

* ``*`` and ``?`` wildcards (``"*"``, ``"202*"``, ``"??"``)
* ``TOP(n)`` / ``TOP(n,offset)``  – for a time variable the *latest* n
  periods, otherwise the first n values in metadata order
* ``BOTTOM(n)`` / ``BOTTOM(n,offset)`` – the opposite end
* ``RANGE(from,to)``, ``FROM(code)``, ``TO(code)`` – inclusive spans in
  metadata order

Expressions are expanded locally against table metadata so that unknown
codes are caught early and the number of cells can be checked against
the API's limit before calling it.
"""

import re
from collections.abc import Iterable, Mapping, Sequence
from fnmatch import fnmatchcase

from pydantic import BaseModel, Field

from ..errors import InvalidInputError, TooLargeError

_FUNC = re.compile(r"^\s*(top|bottom|range|from|to)\s*\((.*)\)\s*$", re.IGNORECASE)


class PxValue(BaseModel):
    code: str
    label: str


class PxCodelistRef(BaseModel):
    id: str
    label: str | None = None
    type: str | None = Field(default=None, description="Aggregation eller Valueset")


class PxVariable(BaseModel):
    code: str
    label: str
    is_time: bool = False
    is_contents: bool = False
    is_geo: bool = False
    elimination: bool = False
    values: list[PxValue]
    codelists: list[PxCodelistRef] = Field(default_factory=list)
    opaque: bool = Field(
        default=False,
        description="Metadata saknar värdelista (för många värden); koder skickas vidare utan lokal kontroll",
    )

    def codes(self) -> list[str]:
        return [v.code for v in self.values]


class ResolvedSelection(BaseModel):
    """Concrete value codes per variable after expanding expressions."""

    values: dict[str, list[str]] = Field(description="Variabelkod → valda värdekoder")
    eliminated: list[str] = Field(
        default_factory=list, description="Variabler som utelämnats och elimineras (summeras/total)"
    )
    defaulted: dict[str, list[str]] = Field(
        default_factory=dict, description="Variabler som saknades i urvalet och fick ett standardval"
    )
    cells: int = Field(description="Beräknat antal dataceller (-1 om okänt)")
    unvalidated: list[str] = Field(
        default_factory=list,
        description="Variabler utan värdelista i metadata – koderna skickades vidare okontrollerade",
    )
    value_counts: dict[str, int] = Field(default_factory=dict, description="Antal valda värden per variabel")
    truncated_lists: list[str] = Field(
        default_factory=list, description="Variabler vars kodlista kortats i svaret (se value_counts)"
    )

    def compacted(self, limit: int = 50) -> "ResolvedSelection":
        """Copy for tool output: long code lists are shortened (the request used all of them)."""
        long = [code for code, codes in self.values.items() if len(codes) > limit]
        if not long:
            return self
        return self.model_copy(
            update={
                "values": {k: (v[:limit] if k in long else v) for k, v in self.values.items()},
                "defaulted": {k: (v[:limit] if k in long else v) for k, v in self.defaulted.items()},
                "truncated_lists": long,
            }
        )


def is_expression(token: str) -> bool:
    return "*" in token or "?" in token or bool(_FUNC.match(token))


_POSITIVE = re.compile(r"^[1-9][0-9]*$")
_NON_NEGATIVE = re.compile(r"^(0|[1-9][0-9]*)$")


def _parse_int(raw: str, expression: str, *, positive: bool) -> int:
    """Integers as the PxWebApi 2 grammar accepts them: no spaces, no signs,
    no leading zeros; the count must be at least 1."""
    text = raw.strip()
    if not (_POSITIVE if positive else _NON_NEGATIVE).match(text):
        need = "ett heltal ≥ 1" if positive else "ett heltal ≥ 0"
        raise InvalidInputError(f"Ogiltigt tal {text!r} i uttrycket {expression!r} ({need}, utan inledande nollor)")
    return int(text)


def validate_wildcard(token: str) -> None:
    """PxWebApi 2 accepts '*' only first and/or last (at most two), '?' as a
    one-character mask, and never both kinds in one item."""
    if "*" in token and "?" in token:
        raise InvalidInputError(f"Uttrycket {token!r} blandar '*' och '?' – använd bara den ena")
    if "*" in token:
        inner = token[1:] if token.startswith("*") else token
        inner = inner[:-1] if inner.endswith("*") else inner
        if "*" in inner:
            raise InvalidInputError(f"'*' får bara stå först och/eller sist i uttrycket, inte i {token!r}")


def expand_token(token: str, variable: PxVariable) -> list[str]:
    """Expand one value code or expression to concrete codes (metadata order).

    Like PxWebApi 2, a literal value code always wins over an expression, so
    codes such as ``100+`` are safe. Matching is case-insensitive.
    """
    token = token.strip()
    if variable.opaque:
        # No value list to check against: pass the code through. PxWeb v1 then only
        # understands '*'-prefix/suffix patterns ('all') and TOP(n) ('top').
        if "?" in token:
            raise InvalidInputError(
                f"'?' stöds inte för variabeln {variable.code!r} som saknar värdelista – använd '*' först eller sist"
            )
        if "*" in token:
            validate_wildcard(token)
            if token != "*" and not (token.startswith("*") or token.endswith("*")):
                raise InvalidInputError(f"'*' måste stå först eller sist i {token!r}")
        match = _FUNC.match(token)
        if match:
            args = [a.strip() for a in match.group(2).split(",")]
            if match.group(1).lower() != "top" or len(args) != 1:
                raise InvalidInputError(
                    f"{token!r} stöds inte för variabeln {variable.code!r} som saknar värdelista – bara TOP(n), "
                    "koder och '*'-mönster"
                )
            _parse_int(args[0], token, positive=True)
        return [token]
    codes = variable.codes()
    if token in codes:
        return [token]
    # Case-insensitive fallback for codes such as "TotSa" vs "totsa".
    lowered = {c.lower(): c for c in codes}
    if token.lower() in lowered:
        return [lowered[token.lower()]]
    match = _FUNC.match(token)
    if match:
        func = match.group(1).lower()
        args = [a.strip() for a in match.group(2).split(",")] if match.group(2).strip() else []
        if func in ("top", "bottom"):
            if not 1 <= len(args) <= 2:
                raise InvalidInputError(f"{func.upper()} tar 1–2 argument: {token!r}")
            n = _parse_int(args[0], token, positive=True)
            offset = _parse_int(args[1], token, positive=False) if len(args) == 2 else 0
            # Time values are listed oldest first, so "top" means newest.
            from_end = (func == "top") == variable.is_time
            if from_end:
                end = len(codes) - offset
                return codes[max(0, end - n) : max(0, end)]
            return codes[offset : offset + n]
        if func == "range":
            if len(args) != 2:
                raise InvalidInputError(f"RANGE tar två argument: {token!r}")
            start, stop = (_index_of(a, variable, token) for a in args)
            if start > stop:
                start, stop = stop, start
            return codes[start : stop + 1]
        if func == "from":
            if len(args) != 1:
                raise InvalidInputError(f"FROM tar ett argument: {token!r}")
            return codes[_index_of(args[0], variable, token) :]
        if len(args) != 1:
            raise InvalidInputError(f"TO tar ett argument: {token!r}")
        return codes[: _index_of(args[0], variable, token) + 1]
    if "*" in token or "?" in token:
        validate_wildcard(token)
        pattern = token.lower().replace("[", "[[]")
        return [c for c in codes if fnmatchcase(c.lower(), pattern)]
    by_label = [v.code for v in variable.values if v.label.lower() == token.lower()]
    if len(by_label) == 1:
        return by_label
    raise InvalidInputError(
        f"Okänd värdekod {token!r} för variabeln {variable.code!r}. Exempel på giltiga koder: {_preview(variable)}"
    )


def _index_of(code: str, variable: PxVariable, expression: str) -> int:
    # The server compares RANGE/FROM/TO bounds case-insensitively.
    codes = variable.codes()
    if code in codes:
        return codes.index(code)
    lowered = [c.lower() for c in codes]
    if code.lower() in lowered:
        return lowered.index(code.lower())
    raise InvalidInputError(f"Koden {code!r} i {expression!r} finns inte i variabeln {variable.code!r}")


def _preview(variable: PxVariable, limit: int = 8) -> str:
    shown = [f"{v.code} ({v.label})" if v.label != v.code else v.code for v in variable.values[:limit]]
    more = len(variable.values) - limit
    return ", ".join(shown) + (f" … (+{more} till)" if more > 0 else "")


def find_variable(variables: Sequence[PxVariable], code: str) -> PxVariable:
    for variable in variables:
        if variable.code == code:
            return variable
    for variable in variables:
        if variable.code.lower() == code.lower() or variable.label.lower() == code.lower():
            return variable
    valid = ", ".join(f"{v.code} ({v.label})" for v in variables)
    raise InvalidInputError(f"Okänd variabel {code!r}. Tabellens variabler: {valid}")


def _dedupe(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


MAX_DEFAULT_VALUES = 100


def resolve_selection(
    variables: Sequence[PxVariable],
    selection: Mapping[str, Sequence[str]] | None,
    *,
    fill_defaults: bool = True,
    max_cells: int | None = None,
) -> ResolvedSelection:
    """Expand ``selection`` against metadata.

    Variables missing from the selection are eliminated when the table
    allows it. Otherwise, when ``fill_defaults`` is set, a time variable
    gets its latest period and any other variable all of its values (which
    is also what PxWeb returns for an omitted mandatory variable) – but only
    up to ``MAX_DEFAULT_VALUES`` values; larger variables must be chosen.
    Variables without a value list in the metadata (``opaque``) are passed
    through unchecked and must be selected explicitly.
    """
    selection = selection or {}
    resolved: dict[str, list[str]] = {}
    unvalidated: list[str] = []
    for raw_code, tokens in selection.items():
        variable = find_variable(variables, raw_code)
        if isinstance(tokens, str):
            tokens = [tokens]
        if not tokens:
            raise InvalidInputError(f"Tomt urval för variabeln {variable.code!r}")
        expanded = _dedupe(code for token in tokens for code in expand_token(str(token), variable))
        if not expanded:
            raise InvalidInputError(
                f"Urvalet {list(tokens)!r} matchar inga värden i {variable.code!r}. Exempel: {_preview(variable)}"
            )
        resolved[variable.code] = expanded
        if variable.opaque:
            kinds = {"top" if _FUNC.match(t) else "pattern" if "*" in t else "code" for t in expanded}
            if len(kinds) > 1 or ("top" in kinds and len(expanded) > 1):
                raise InvalidInputError(
                    f"Variabeln {variable.code!r} saknar värdelista: ange antingen koder, '*'-mönster eller ett TOP(n) "
                    "– inte en blandning (PxWeb v1 tillåter ett filter per variabel)"
                )
            unvalidated.append(variable.code)

    eliminated: list[str] = []
    defaulted: dict[str, list[str]] = {}
    for variable in variables:
        if variable.code in resolved:
            continue
        if variable.elimination:
            eliminated.append(variable.code)
            continue
        if variable.opaque:
            raise InvalidInputError(
                f"Variabeln {variable.code!r} ({variable.label}) måste väljas. Metadata saknar värdelista "
                "(för många värden), så ange koder direkt – eller '*' för alla."
            )
        if not fill_defaults:
            raise InvalidInputError(f"Variabeln {variable.code!r} ({variable.label}) måste väljas")
        if not variable.is_time and len(variable.values) > MAX_DEFAULT_VALUES:
            raise InvalidInputError(
                f"Variabeln {variable.code!r} ({variable.label}) har {len(variable.values)} värden och måste väljas "
                f"(eller ange '*' för alla). Exempel: {_preview(variable)}"
            )
        chosen = variable.codes()[-1:] if variable.is_time else variable.codes()
        resolved[variable.code] = chosen
        defaulted[variable.code] = chosen

    exact = all(not (code in unvalidated and any(is_expression(t) for t in codes)) for code, codes in resolved.items())
    cells = 1
    for codes in resolved.values():
        cells *= max(1, len(codes))
    if not exact:
        cells = -1
    elif max_cells is not None and cells > max_cells:
        sizes = ", ".join(f"{code}={len(codes)}" for code, codes in resolved.items())
        raise TooLargeError(
            f"Urvalet ger {cells:,} celler vilket överskrider gränsen {max_cells:,}. "
            f"Antal värden per variabel: {sizes}. Begränsa t.ex. tid med TOP(n) eller välj färre regioner."
        )
    ordered = {v.code: resolved[v.code] for v in variables if v.code in resolved}
    return ResolvedSelection(
        values=ordered,
        eliminated=eliminated,
        defaulted=defaulted,
        cells=cells,
        unvalidated=unvalidated,
        value_counts={k: len(v) for k, v in ordered.items()},
    )
