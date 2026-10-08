# SPDX-FileCopyrightText: 2026 Deniz Özer
#
# SPDX-License-Identifier: MIT

"""Runtime configuration, read from environment variables.

Every base URL can be overridden so that the server can be pointed at a
mirror, a test double or a new API version without code changes (the
DIGG REST API profile puts the major version in the path, so a version
bump is a base-URL change).
"""

import os
from dataclasses import dataclass, field

ENV_PREFIX = "FUZZY_MCP_"

ALL_SOURCES: tuple[str, ...] = ("scb", "fohm", "skolverket", "dataportal", "reference")
# SCB and Skolverket have several APIs; each can be enabled on its own (the parent enables all).
SUB_SOURCES: tuple[str, ...] = (
    "scb.statistik",
    "scb.geodata",
    "skolverket.skolenhetsregistret",
    "skolverket.syllabus",
    "skolverket.planerad",
    "skolverket.susa",
    "skolverket.statistik",
)

DEFAULT_BASE_URLS: dict[str, str] = {
    # SCB Statistikdatabasen, PxWebApi 2.0 (production since 2025).
    "scb": "https://statistikdatabasen.scb.se/api/v2",
    # SCB:s öppna geodata (GeoServer, WFS 1.1.0 at /wfs).
    "scb_geodata": "https://geodata.scb.se/geoserver/stat",
    # Folkhälsomyndigheten Folkhälsodata (PxWeb API v1).
    "fohm": "https://fohm-app.folkhalsomyndigheten.se/Folkhalsodata/api/v1",
    # Skolverket open APIs.
    "skolenhetsregistret": "https://api.skolverket.se/skolenhetsregistret",
    "syllabus": "https://api.skolverket.se/syllabus",
    "planned_educations": "https://api.skolverket.se/planned-educations",
    "susa_navet": "https://api.skolverket.se/susa-navet",
    # Skolverkets statistikdatabas (PxWeb API v1; replaced SIRIS).
    "skolverket_statistik": "https://statistikdatabasen.skolverket.se/PxWeb/api/v1",
    # Sveriges dataportal (EntryScape/EntryStore behind dataportal.se).
    "dataportal": "https://admin.dataportal.se/store",
}


# PxWeb installations that still run API v1 but can be switched to PxWebApi 2 by configuration
# (FUZZY_MCP_<KEY>_API_VERSION=v2 together with FUZZY_MCP_<KEY>_BASE_URL pointing at the v2 API).
DEFAULT_API_VERSIONS: dict[str, str] = {"fohm": "v1", "skolverket_statistik": "v1"}
API_VERSIONS = ("v1", "v2")


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(ENV_PREFIX + name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{ENV_PREFIX}{name} måste vara ett tal, fick {raw!r}") from exc


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{ENV_PREFIX}{name} måste vara ett heltal, fick {raw!r}") from exc


@dataclass(frozen=True)
class Settings:
    base_urls: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_BASE_URLS))
    api_versions: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_API_VERSIONS))
    enabled_sources: frozenset[str] = frozenset(ALL_SOURCES)
    timeout_seconds: float = 30.0
    max_retries: int = 3
    cache_ttl_seconds: float = 3600.0
    cache_max_entries: int = 512
    cache_max_bytes: int = 64_000_000
    max_rows: int = 1000
    max_output_chars: int = 0  # 0 = no cap; e.g. 30000 for Eneo (see docs/ENEO.md)
    user_agent: str = "fuzzy-mcp/0.1 (+https://github.com/deno-li/fuzzy-MCP)"
    default_language: str = "sv"
    # "opt-in" (default): tools return names of individuals only when asked explicitly; False = never.
    allow_personal_data: bool = True

    def base_url(self, key: str) -> str:
        return self.base_urls[key].rstrip("/")

    def api_version(self, key: str) -> str:
        """PxWeb API generation for a configurable installation ("v1" or "v2")."""
        return self.api_versions.get(key, "v1")

    def source_enabled(self, source: str) -> bool:
        """True if the source, its parent ("skolverket" for "skolverket.syllabus") or,
        for a parent, any of its sub-sources is enabled."""
        if source in self.enabled_sources:
            return True
        parent = source.split(".", 1)[0]
        if parent != source and parent in self.enabled_sources:
            return True
        return any(s.startswith(source + ".") for s in self.enabled_sources)

    @classmethod
    def from_env(cls) -> "Settings":
        base_urls = dict(DEFAULT_BASE_URLS)
        for key in DEFAULT_BASE_URLS:
            override = _env(f"{key.upper()}_BASE_URL")
            if override:
                base_urls[key] = override

        api_versions = dict(DEFAULT_API_VERSIONS)
        for key in DEFAULT_API_VERSIONS:
            version = (_env(f"{key.upper()}_API_VERSION") or api_versions[key]).lower()
            if version not in API_VERSIONS:
                raise ValueError(f"{ENV_PREFIX}{key.upper()}_API_VERSION måste vara 'v1' eller 'v2', fick {version!r}")
            if version == "v2" and base_urls[key] == DEFAULT_BASE_URLS[key]:
                raise ValueError(
                    f"{ENV_PREFIX}{key.upper()}_API_VERSION=v2 kräver att {ENV_PREFIX}{key.upper()}_BASE_URL pekar på "
                    "PxWebApi 2-adressen (t.ex. https://<värd>/api/v2); standardadressen är API v1"
                )
            api_versions[key] = version

        sources_raw = _env("SOURCES")
        if sources_raw:
            requested = {s.strip().lower() for s in sources_raw.split(",") if s.strip()}
            unknown = requested - set(ALL_SOURCES) - set(SUB_SOURCES)
            if unknown:
                raise ValueError(
                    f"Okända källor i --sources/{ENV_PREFIX}SOURCES: {sorted(unknown)}. "
                    f"Giltiga: {[*ALL_SOURCES, *SUB_SOURCES]}"
                )
            enabled = frozenset(requested)
        else:
            enabled = frozenset(ALL_SOURCES)

        personal_data = (_env("PERSONAL_DATA", "opt-in") or "opt-in").lower()
        if personal_data not in ("opt-in", "off"):
            raise ValueError(f"{ENV_PREFIX}PERSONAL_DATA måste vara 'opt-in' eller 'off', fick {personal_data!r}")

        language = (_env("LANGUAGE", "sv") or "sv").lower()
        if language not in ("sv", "en"):
            raise ValueError(f"{ENV_PREFIX}LANGUAGE måste vara 'sv' eller 'en', fick {language!r}")

        return cls(
            base_urls=base_urls,
            api_versions=api_versions,
            enabled_sources=enabled,
            timeout_seconds=_env_float("HTTP_TIMEOUT", 30.0),
            max_retries=_env_int("MAX_RETRIES", 3),
            cache_ttl_seconds=_env_float("CACHE_TTL", 3600.0),
            cache_max_entries=_env_int("CACHE_MAX_ENTRIES", 512),
            cache_max_bytes=_env_int("CACHE_MAX_MB", 64) * 1_000_000,
            max_rows=_env_int("MAX_ROWS", 1000),
            max_output_chars=_env_int("MAX_OUTPUT_CHARS", 0),
            user_agent=_env("USER_AGENT", cls.user_agent) or cls.user_agent,
            default_language=language,
            allow_personal_data=personal_data == "opt-in",
        )
