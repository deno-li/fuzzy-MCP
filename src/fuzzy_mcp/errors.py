# SPDX-FileCopyrightText: 2026 Deniz Özer
#
# SPDX-License-Identifier: MIT

"""Error types shared by all source modules."""

from typing import Any


class FuzzyMcpError(Exception):
    """Base class for errors raised by this package."""


class UpstreamError(FuzzyMcpError):
    """An upstream API answered with an error or could not be reached."""

    def __init__(
        self,
        source: str,
        message: str,
        *,
        status: int | None = None,
        url: str | None = None,
        detail: Any = None,
    ) -> None:
        super().__init__(message)
        self.source = source
        self.message = message
        self.status = status
        self.url = url
        self.detail = detail

    def __str__(self) -> str:
        parts = [f"[{self.source}] {self.message}"]
        if self.status is not None:
            parts.append(f"HTTP {self.status}")
        if self.url:
            parts.append(self.url)
        text = " | ".join(parts)
        if self.detail:
            text += f" | detalj: {self.detail}"
        return text


class InvalidInputError(FuzzyMcpError):
    """The caller's arguments cannot produce a valid upstream request."""


class TooLargeError(InvalidInputError):
    """The requested selection exceeds an upstream size limit."""
