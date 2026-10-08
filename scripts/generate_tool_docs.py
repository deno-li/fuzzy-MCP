# SPDX-FileCopyrightText: 2026 Deniz Özer
#
# SPDX-License-Identifier: MIT

"""Regenerate the tool, resource and prompt tables in README.md and the Eneo tool profiles from the server.

Usage: python scripts/generate_tool_docs.py [--check]
"""

import json
import sys
from dataclasses import replace
from pathlib import Path

import anyio
from mcp import Client

from fuzzy_mcp.config import ALL_SOURCES, Settings
from fuzzy_mcp.server import build_server

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
PROFILES_DOC = ROOT / "docs" / "eneo" / "verktygsprofiler.md"
START = "<!-- verktyg:start -->"
END = "<!-- verktyg:end -->"

# Tool profiles for Eneo assistants (docs/ENEO.md section 5): one profile per assistant keeps the tool
# definitions the model receives on every turn small. Values are FUZZY_MCP_SOURCES.
PROFILES = [
    ("Statistik", "scb.statistik,skolverket.statistik,fohm,reference"),
    ("Skolor och skolenheter", "skolverket.skolenhetsregistret,skolverket.planerad,reference"),
    ("Läroplaner, ämnen och kurser", "skolverket.syllabus,reference"),
    ("Vuxen- och högre utbildning", "skolverket.susa,skolverket.planerad,reference"),
    ("Geodata och dataset", "scb.geodata,dataportal,reference"),
]

GROUPS = [
    ("fuzzy_", "Översikt"),
    ("ref_", "Referensdata (lokalt, utan nätverk)"),
    ("scb_", "SCB – Statistikdatabasen (PxWebApi 2)"),
    ("fohm_", "Folkhälsomyndigheten – Folkhälsodata (PxWeb)"),
    ("skolverket_stat_", "Skolverket – Statistikdatabasen (PxWeb)"),
    ("skolverket_pe_", "Skolverket – Planerad utbildning (v4)"),
    ("skolverket_susa_", "Skolverket – Susa-navet (EMIL 3)"),
    ("skolverket_", "Skolverket – Skolenhetsregistret (v2) och Läroplan/Syllabus (v1)"),
    ("dataportal_", "Sveriges dataportal"),
]


def first_sentence(text: str | None) -> str:
    line = " ".join((text or "").split())
    for end in (". ", ": "):
        if end in line:
            line = line.split(end, 1)[0] + "."
            break
    return line.replace("|", "\\|")


async def build_markdown() -> str:
    async with Client(build_server(Settings(), rate_limits={})) as client:
        tools = (await client.list_tools()).tools
        resources = (await client.list_resources()).resources
        templates = (await client.list_resource_templates()).resource_templates
        prompts = (await client.list_prompts()).prompts

    lines: list[str] = [
        f"Servern har **{len(tools)} verktyg**, {len(resources) + len(templates)} resurser "
        f"och {len(prompts)} promptar.",
        "",
    ]
    seen: set[str] = set()
    for prefix, title in GROUPS:
        group = [t for t in tools if t.name.startswith(prefix) and t.name not in seen]
        if not group:
            continue
        lines += [f"#### {title}", "", "| Verktyg | Beskrivning |", "| --- | --- |"]
        for tool in group:
            seen.add(tool.name)
            lines.append(f"| `{tool.name}` | {first_sentence(tool.description)} |")
        lines.append("")
    lines += ["#### Resurser", "", "| URI | Innehåll |", "| --- | --- |"]
    for resource in resources:
        lines.append(f"| `{resource.uri}` | {first_sentence(resource.description)} |")
    for template in templates:
        lines.append(f"| `{template.uri_template}` | {first_sentence(template.description)} |")
    lines += ["", "#### Promptar", "", "| Prompt | Beskrivning |", "| --- | --- |"]
    for prompt in prompts:
        lines.append(f"| `{prompt.name}` | {first_sentence(prompt.description)} |")
    return "\n".join(lines)


async def profile_tools(sources: str) -> list[dict]:
    """What Eneo passes to the model per tool: name, description and input schema (no output schema)."""
    settings = replace(Settings(), enabled_sources=frozenset(s.strip() for s in sources.split(",")))
    async with Client(build_server(settings, rate_limits={})) as client:
        tools = (await client.list_tools()).tools
    return [{"name": t.name, "description": t.description or "", "parameters": t.input_schema} for t in tools]


def _number(value: int) -> str:
    return f"{value:,}".replace(",", " ")


async def build_profiles() -> str:
    # REUSE-IgnoreStart (the header of the generated file, not this file's licence)
    lines = [
        "<!--",
        "SPDX-FileCopyrightText: 2026 Deniz Özer",
        "",
        "SPDX-License-Identifier: CC0-1.0",
        "-->",
        # REUSE-IgnoreEnd
        "",
        "# Verktygsprofiler för Eneo",
        "",
        "Genereras från servern med `python scripts/generate_tool_docs.py` – ändra inte för hand.",
        "",
        "Eneo skickar alla aktiverade verktygsdefinitioner till modellen i varje tur. Aktivera därför en profil",
        "per assistent: antingen genom att bara slå på profilens verktyg i assistenten, eller med en egen",
        "fuzzy-mcp-instans per profil (`FUZZY_MCP_SOURCES`). Katalogen är namn, beskrivning och indataschema",
        "per verktyg (det Eneo skickar till modellen). Token är en grov uppskattning (byte / 4); den faktiska",
        "kostnaden beror på modellen.",
        "",
        "| Profil | `FUZZY_MCP_SOURCES` | Verktyg | Katalog | Cirka token per tur |",
        "| --- | --- | --- | --- | --- |",
    ]
    details: list[str] = []
    for name, sources in [*PROFILES, ("Allt", ",".join(ALL_SOURCES))]:
        tools = await profile_tools(sources)
        size = len(json.dumps(tools, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        shown = "alla" if name == "Allt" else f"`{sources}`"
        lines.append(
            f"| {name} | {shown} | {len(tools)} | {_number(size)} byte | {_number(round(size / 2000) * 500)} |"
        )
        if name != "Allt":
            details += [f"## {name}", "", f"`FUZZY_MCP_SOURCES={sources}`", ""]
            details += [f"- `{t['name']}`" for t in sorted(tools, key=lambda t: t["name"])]
            details.append("")
    return "\n".join([*lines, "", *details]).rstrip() + "\n"


def main() -> int:
    check = "--check" in sys.argv
    text = README.read_text(encoding="utf-8")
    if START not in text or END not in text:
        print("README.md saknar markörerna för verktygstabellen", file=sys.stderr)
        return 2
    generated = anyio.run(build_markdown)
    head, rest = text.split(START, 1)
    _, tail = rest.split(END, 1)
    updated = f"{head}{START}\n{generated}\n{END}{tail}"
    profiles = anyio.run(build_profiles)
    current_profiles = PROFILES_DOC.read_text(encoding="utf-8") if PROFILES_DOC.exists() else ""
    if check:
        stale = [
            p.name
            for p, new, old in ((README, updated, text), (PROFILES_DOC, profiles, current_profiles))
            if new != old
        ]
        if stale:
            print(f"{', '.join(stale)} är inte uppdaterad – kör python scripts/generate_tool_docs.py", file=sys.stderr)
            return 1
        return 0
    README.write_text(updated, encoding="utf-8")
    PROFILES_DOC.write_text(profiles, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
