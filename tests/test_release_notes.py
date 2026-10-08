# SPDX-FileCopyrightText: 2026 Deniz Özer
#
# SPDX-License-Identifier: MIT

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "release_notes.py"
if not SCRIPT.exists():  # the sdist ships tests/ but not scripts/
    pytest.skip("scripts/release_notes.py finns bara i repot", allow_module_level=True)
spec = importlib.util.spec_from_file_location("release_notes", SCRIPT)
release_notes = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release_notes)

COMMIT = "0123456789abcdef0123456789abcdef01234567"
CHANGELOG = """# Changelog

## [Unreleased]

## [0.2.0] - 2026-11-01

### Tillagt

- Något nytt.

## [0.1.0] - 2026-10-07

- Första utgåvan.
"""


def make_release(tmp_path: Path, version: str = "0.2.0", commit: str = COMMIT) -> Path:
    release = tmp_path / "release"
    for platform, suffix, python in (("windows", "zip", "3.14"), ("linux", "tar.gz", "3.12")):
        name = f"fuzzy-mcp-{version}-{platform}-py{python}"
        package = release / name
        package.mkdir(parents=True)
        info = [f"fuzzy-mcp {version}", f"Commit: {commit}"]
        if platform == "linux":
            info += [
                f"Container-image: fuzzy-mcp:{version} sha256:{'b' * 64}",
                "Imagens Python-beroenden: samma (28 st.)",
            ]
        (package / "BUILDINFO.txt").write_text("\n".join(info) + "\n", encoding="utf-8")
        (package / "audit.txt").write_text("WARNING: något\nNo known vulnerabilities found\n", encoding="utf-8")
        archive = release / f"{name}.{suffix}"
        archive.write_bytes(b"paket")
        digest = ("a" if platform == "windows" else "c") * 64
        (release / f"{archive.name}.sha256").write_text(f"{digest} *{archive.name}\n", encoding="utf-8")
    return release


def test_notes_have_changelog_packages_checksums_and_traceability(tmp_path):
    text = release_notes.notes("0.2.0", CHANGELOG, make_release(tmp_path), COMMIT, "https://example.invalid/run/1")
    assert "- Något nytt." in text and "Första utgåvan" not in text
    assert "| `fuzzy-mcp-0.2.0-windows-py3.14.zip` | Windows Server x64 med Python 3.14 |" in text
    assert "| `fuzzy-mcp-0.2.0-linux-py3.12.tar.gz` | Linux x86_64 med Python 3.12" in text
    assert f"{'a' * 64}  fuzzy-mcp-0.2.0-windows-py3.14.zip" in text
    assert f"{'c' * 64}  fuzzy-mcp-0.2.0-linux-py3.12.tar.gz" in text
    assert f"Commit: `{COMMIT}`" in text and "Container-image: fuzzy-mcp:0.2.0" in text
    assert "pip-audit – No known vulnerabilities found" in text and "https://example.invalid/run/1" in text


def test_missing_changelog_section_stops(tmp_path):
    with pytest.raises(SystemExit, match=r"saknar ett avsnitt för \[0.3.0\]"):
        release_notes.notes("0.3.0", CHANGELOG, make_release(tmp_path, "0.3.0"), COMMIT)


def test_package_from_another_or_dirty_commit_stops(tmp_path):
    with pytest.raises(SystemExit, match="anger inte commit"):
        release_notes.notes("0.2.0", CHANGELOG, make_release(tmp_path, commit="f" * 40), COMMIT)
    dirty = make_release(tmp_path / "dirty", commit=f"{COMMIT} (med ändringar som inte är incheckade)")
    with pytest.raises(SystemExit, match="anger inte commit"):
        release_notes.notes("0.2.0", CHANGELOG, dirty, COMMIT)


def test_missing_package_or_bad_checksum_file_stops(tmp_path):
    release = make_release(tmp_path)
    (release / "fuzzy-mcp-0.2.0-linux-py3.12.tar.gz.sha256").write_text("inte en kontrollsumma\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="Oväntat innehåll"):
        release_notes.notes("0.2.0", CHANGELOG, release, COMMIT)
    (release / "fuzzy-mcp-0.2.0-windows-py3.14.zip").unlink()
    with pytest.raises(SystemExit, match="Hittade 0 windows-paket"):
        release_notes.notes("0.2.0", CHANGELOG, release, COMMIT)
