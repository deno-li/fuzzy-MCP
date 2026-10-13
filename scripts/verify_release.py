# SPDX-FileCopyrightText: 2026 Deniz Özer
#
# SPDX-License-Identifier: MIT

"""Verify and unpack a final release archive before installation.

    python scripts/verify_release.py --archive release/fuzzy-mcp-<version>-linux-py3.12.tar.gz --out-dir unpacked

Checks the adjacent .sha256 before extraction, then every packaged file against
SHA256SUMS. Prints only the verified package directory to stdout for CI callers.
Existing package directories are never replaced.
"""

import argparse
import hashlib
import re
import shutil
import stat
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

REQUIRED_FILES = (
    "BUILDINFO.txt",
    "audit.txt",
    "requirements.lock",
    "sbom.cdx.json",
    "README.md",
    "docs/DRIFT.md",
    "docs/ENEO.md",
    "docs/KALLKONTROLL.md",
    "smoke_test.py",
    "eneo_check.py",
)


def sha256(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def relative_path(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    parts = name.split("/")
    invalid = any(
        not part
        or part.endswith((".", " "))
        or any(character in '<>:"\\|?*' or ord(character) < 32 for character in part)
        or re.fullmatch(
            r"CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|COM[1-9¹²³]|LPT[1-9¹²³]", part.split(".")[0].rstrip().upper()
        )
        for part in parts
    )
    if not name or path.is_absolute() or invalid:
        raise ValueError(f"Otillåten sökväg: {name!r}")
    return path


def checksum_entries(path: Path) -> dict[str, str]:
    entries = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split(maxsplit=1)
        if len(parts) != 2 or not re.fullmatch(r"[0-9a-f]{64}", parts[0]):
            raise ValueError(f"Ogiltig kontrollsumma i {path.name}")
        name = parts[1].removeprefix("*")
        relative_path(name)
        if name in entries:
            raise ValueError(f"Duplicerad kontrollsumma för {name}")
        entries[name] = parts[0]
    if not entries:
        raise ValueError(f"{path.name} är tom")
    return entries


def verify_tree(root: Path) -> int:
    entries = checksum_entries(root / "SHA256SUMS")
    actual = {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}
    actual.discard("SHA256SUMS")
    if actual != entries.keys():
        missing = sorted(entries.keys() - actual)
        extra = sorted(actual - entries.keys())
        raise ValueError(f"Paketets fillista avviker: saknas={missing}, extra={extra}")
    for name, expected in entries.items():
        if sha256(root / name) != expected:
            raise ValueError(f"Fel kontrollsumma: {name}")
    missing = [name for name in REQUIRED_FILES if name not in entries]
    if missing:
        raise ValueError(f"Paketet saknar obligatoriska filer: {', '.join(missing)}")
    if not any((root / "dist").glob("*.whl")) or not any((root / "wheelhouse").glob("*.whl")):
        raise ValueError("Paketet saknar installationshjul i dist eller wheelhouse")
    return len(entries)


def unpack(archive: Path, destination: Path, root_name: str) -> None:
    """Accept only regular files/directories within the named package root."""
    seen = set()

    def target(name: str) -> Path:
        path = relative_path(name.rstrip("/"))
        if not path.parts or path.parts[0] != root_name:
            raise ValueError(f"Arkivpost ligger utanför paketet: {name!r}")
        if path in seen:
            raise ValueError(f"Duplicerad arkivpost: {name!r}")
        seen.add(path)
        output = destination.joinpath(*path.parts)
        if not output.resolve().is_relative_to(destination.resolve()):
            raise ValueError(f"Arkivpost ligger utanför paketet: {name!r}")
        return output

    if archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as source:
            for member in source.infolist():
                path = target(member.filename)
                mode = stat.S_IFMT(member.external_attr >> 16)
                if member.is_dir():
                    path.mkdir(parents=True, exist_ok=True)
                elif mode in (0, stat.S_IFREG):
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with source.open(member) as content, path.open("xb") as output:
                        shutil.copyfileobj(content, output)
                else:
                    raise ValueError(f"Otillåten arkivpost: {member.filename!r}")
    else:
        with tarfile.open(archive, "r:gz") as source:
            for member in source:
                path = target(member.name)
                if member.isdir():
                    path.mkdir(parents=True, exist_ok=True)
                elif member.isfile():
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with source.extractfile(member) as content, path.open("xb") as output:
                        shutil.copyfileobj(content, output)
                else:
                    raise ValueError(f"Otillåten arkivpost: {member.name!r}")


def verify_archive(archive: Path, out_dir: Path) -> Path:
    match = re.fullmatch(r"(fuzzy-mcp-\d+\.\d+\.\d+-(?:linux|windows)-py\d+\.\d+)\.(?:tar\.gz|zip)", archive.name)
    if not match:
        raise ValueError(f"Oväntat arkivnamn: {archive.name}")
    root_name = match[1]
    checksums = checksum_entries(archive.with_name(archive.name + ".sha256"))
    if set(checksums) != {archive.name} or sha256(archive) != checksums[archive.name]:
        raise ValueError(f"Arkivets kontrollsumma stämmer inte: {archive.name}")
    out_dir = out_dir.resolve()
    destination = out_dir / root_name
    if destination.exists():
        raise ValueError(f"Katalogen finns redan: {destination}")
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".verify-release-", dir=out_dir) as temporary:
        temporary = Path(temporary)
        unpack(archive, temporary, root_name)
        root = temporary / root_name
        count = verify_tree(root)
        root.rename(destination)
    print(f"OK {archive.name}: arkivets SHA-256 och {count} paketfiler verifierade", file=sys.stderr)
    return destination


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0], allow_abbrev=False)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        print(verify_archive(args.archive, args.out_dir))
    except (OSError, ValueError, tarfile.TarError, zipfile.BadZipFile) as exc:
        print(f"Paketkontrollen misslyckades: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
