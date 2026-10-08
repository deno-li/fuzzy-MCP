# SPDX-FileCopyrightText: 2026 Deniz Özer
#
# SPDX-License-Identifier: MIT

"""Release notes for a GitHub release, from CHANGELOG.md and the packages built by build_release.py.

    python scripts/release_notes.py --version 0.1.1 --changelog CHANGELOG.md --release-dir release \\
        --commit <sha> [--run-url <url>] > RELEASE_NOTES.md

The text holds the CHANGELOG section for the version, which package is for which server, the SHA-256 checksums
(docs/DRIFT.md section 2 points operators here as the separately published checksum) and the traceability lines
from BUILDINFO.txt and audit.txt. Stops with an error if the version has no CHANGELOG section or a package is missing.
"""

import argparse
import re
import sys
from pathlib import Path

PACKAGES = (
    ("windows", "zip", "Windows Server x64 med Python {python}"),
    ("linux", "tar.gz", "Linux x86_64 med Python {python}, eller som container bredvid Eneo (med färdig image)"),
)


def changelog_section(changelog: str, version: str) -> str:
    """The body of '## [version] - date' up to the next '## [' heading."""
    match = re.search(rf"^## \[{re.escape(version)}\][^\n]*\n(.*?)(?=^## \[|\Z)", changelog, re.M | re.S)
    if not match or not match.group(1).strip():
        raise SystemExit(f"CHANGELOG.md saknar ett avsnitt för [{version}]")
    return match.group(1).strip()


def package(release_dir: Path, version: str, platform: str, suffix: str) -> tuple[Path, str]:
    """The one archive for the platform and its Python version (from the name, e.g. py3.14)."""
    archives = sorted(release_dir.glob(f"fuzzy-mcp-{version}-{platform}-py*.{suffix}"))
    if len(archives) != 1:
        raise SystemExit(f"Hittade {len(archives)} {platform}-paket för {version} i {release_dir} – väntade exakt ett")
    python = archives[0].name.removesuffix(f".{suffix}").rsplit("-py", 1)[1]
    return archives[0], python


def checksum_line(archive: Path) -> str:
    """'<sha256>  <name>' from the archive's .sha256 file (written by build_release.py as '<sha256> *<name>')."""
    digest, _, name = archive.with_name(archive.name + ".sha256").read_text(encoding="utf-8").strip().partition(" ")
    if name.lstrip("*") != archive.name or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise SystemExit(f"Oväntat innehåll i {archive.name}.sha256")
    return f"{digest}  {archive.name}"


def buildinfo_lines(package_dir: Path, *prefixes: str) -> list[str]:
    lines = (package_dir / "BUILDINFO.txt").read_text(encoding="utf-8").splitlines()
    return [line for line in lines if line.startswith(prefixes)]


def audit_result(package_dir: Path) -> str:
    lines = [line for line in (package_dir / "audit.txt").read_text(encoding="utf-8").splitlines() if line.strip()]
    return lines[-1] if lines else "ingen utdata"


def notes(version: str, changelog: str, release_dir: Path, commit: str, run_url: str = "") -> str:
    section = changelog_section(changelog, version)
    rows, sums, trace = [], [], [f"- Commit: `{commit}` (båda paketen, utan ändringar som inte är incheckade)"]
    for platform, suffix, target in PACKAGES:
        archive, python = package(release_dir, version, platform, suffix)
        package_dir = release_dir / archive.name.removesuffix(f".{suffix}")
        # The release must describe exactly what was built: same commit, clean tree.
        if buildinfo_lines(package_dir, "Commit:") != [f"Commit: {commit}"]:
            raise SystemExit(f"{package_dir.name}/BUILDINFO.txt anger inte commit {commit} (utan ändringar)")
        rows.append(f"| `{archive.name}` | {target.format(python=python)} |")
        sums.append(checksum_line(archive))
        trace += [f"- {platform}: {line}" for line in buildinfo_lines(package_dir, "Container-image:", "Imagens")]
        trace.append(f"- {platform}: pip-audit – {audit_result(package_dir)}")
    if run_url:
        trace.append(f"- Byggd och kontrollerad av GitHub Actions: {run_url}")
    return "\n".join(
        [
            f"fuzzy-mcp {version} – MCP-server för svensk öppen data (SCB, Skolverket, Folkhälsomyndigheten, "
            "Sveriges dataportal).",
            "",
            section,
            "",
            "## Paket",
            "",
            "| Fil | För |",
            "| --- | --- |",
            *rows,
            "",
            "Installation, drift och säkerhet: `docs/DRIFT.md` i paketet. Eneo: `docs/ENEO.md`.",
            "",
            "## Kontrollsummor (SHA-256)",
            "",
            "Jämför med `.sha256`-filen innan du packar upp (DRIFT.md avsnitt 2).",
            "",
            "```",
            *sums,
            "```",
            "",
            "## Spårbarhet",
            "",
            *trace,
            "",
        ]
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0], allow_abbrev=False)
    parser.add_argument("--version", required=True)
    parser.add_argument("--changelog", required=True, type=Path)
    parser.add_argument("--release-dir", required=True, type=Path)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--run-url", default="")
    args = parser.parse_args(argv)
    text = notes(args.version, args.changelog.read_text(encoding="utf-8"), args.release_dir, args.commit, args.run_url)
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
