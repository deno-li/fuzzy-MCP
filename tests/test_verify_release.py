# SPDX-FileCopyrightText: 2026 Deniz Özer
#
# SPDX-License-Identifier: MIT

import hashlib
import importlib.util
import io
import tarfile
import zipfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "verify_release.py"
if not SCRIPT.exists():
    pytest.skip("scripts/verify_release.py finns bara i repot", allow_module_level=True)
spec = importlib.util.spec_from_file_location("verify_release", SCRIPT)
verify_release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verify_release)


def make_archive(tmp_path, platform="linux", *, omit=(), extra=None, tamper=None, unsafe=None):
    suffix, python = ("zip", "3.14") if platform == "windows" else ("tar.gz", "3.12")
    name = f"fuzzy-mcp-0.2.1-{platform}-py{python}"
    files = {path: b"packaged content" for path in verify_release.REQUIRED_FILES if path not in omit}
    files.update({"dist/fuzzy_mcp-0.2.1-py3-none-any.whl": b"project wheel", "wheelhouse/dependency.whl": b"wheel"})
    files["SHA256SUMS"] = "".join(
        f"{hashlib.sha256(content).hexdigest()} *{path}\n" for path, content in sorted(files.items())
    ).encode()
    if extra:
        files[extra] = b"unexpected file"
    if tamper:
        files[tamper] = b"changed after checksum generation"
    members = [(f"{name}/{path}", content) for path, content in files.items()]
    if unsafe:
        members.append((unsafe, b"outside package"))
    archive = tmp_path / f"{name}.{suffix}"
    if platform == "windows":
        with zipfile.ZipFile(archive, "w") as package:
            for path, content in members:
                package.writestr(path, content)
    else:
        with tarfile.open(archive, "w:gz") as package:
            for path, content in members:
                info = tarfile.TarInfo(path)
                info.size = len(content)
                package.addfile(info, io.BytesIO(content))
    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_name(archive.name + ".sha256").write_text(f"{checksum} *{archive.name}\n", encoding="utf-8")
    return archive


@pytest.mark.parametrize("platform", ["linux", "windows"])
def test_final_archive_is_verified_and_unpacked(tmp_path, platform):
    archive = make_archive(tmp_path, platform)
    destination = verify_release.verify_archive(archive, tmp_path / "unpacked")
    assert (destination / "docs/KALLKONTROLL.md").read_bytes() == b"packaged content"
    assert len(list((destination / "wheelhouse").glob("*.whl"))) == 1


def test_outer_checksum_failure_prevents_extraction(tmp_path, monkeypatch):
    archive = make_archive(tmp_path)
    archive.write_bytes(b"corrupted archive")
    monkeypatch.setattr(verify_release, "unpack", lambda *args: pytest.fail("must not extract an unverified archive"))
    with pytest.raises(ValueError, match="Arkivets kontrollsumma"):
        verify_release.verify_archive(archive, tmp_path / "unpacked")
    assert not (tmp_path / "unpacked").exists()


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"tamper": "README.md"}, "Fel kontrollsumma: README.md"),
        ({"extra": ".unexpected"}, "Paketets fillista avviker"),
        ({"omit": ["docs/KALLKONTROLL.md"]}, "saknar obligatoriska filer: docs/KALLKONTROLL.md"),
    ],
)
def test_internal_integrity_failure_leaves_no_package(tmp_path, options, message):
    archive = make_archive(tmp_path, **options)
    with pytest.raises(ValueError, match=message):
        verify_release.verify_archive(archive, tmp_path / "unpacked")
    assert not list((tmp_path / "unpacked").iterdir())


@pytest.mark.parametrize("platform", ["linux", "windows"])
@pytest.mark.parametrize("unsafe", ["../escape", "/absolute", "other-package/README.md"])
def test_members_outside_the_package_are_rejected(tmp_path, platform, unsafe):
    archive = make_archive(tmp_path, platform, unsafe=unsafe)
    with pytest.raises(ValueError, match=r"Otillåten sökväg|utanför paketet"):
        verify_release.verify_archive(archive, tmp_path / "unpacked")
    assert not (tmp_path / "escape").exists()


@pytest.mark.parametrize("platform", ["linux", "windows"])
@pytest.mark.parametrize("path", ["D:/escape", ".. /escape", "folder./escape", "NUL.txt", "COM1/data"])
def test_windows_unsafe_components_are_rejected_on_every_platform(tmp_path, platform, path):
    root = f"fuzzy-mcp-0.2.1-{platform}-py{'3.14' if platform == 'windows' else '3.12'}"
    archive = make_archive(tmp_path, platform, unsafe=f"{root}/{path}")
    with pytest.raises(ValueError, match="Otillåten sökväg"):
        verify_release.verify_archive(archive, tmp_path / "unpacked")
    assert not list((tmp_path / "unpacked").iterdir())


def test_archive_symlink_is_rejected(tmp_path):
    archive = make_archive(tmp_path)
    root_name = archive.name.removesuffix(".tar.gz")
    with tarfile.open(archive, "r:gz") as source:
        members = [(entry, source.extractfile(entry).read()) for entry in source]
    with tarfile.open(archive, "w:gz") as output:
        for entry, content in members:
            output.addfile(entry, io.BytesIO(content))
        link = tarfile.TarInfo(f"{root_name}/linked")
        link.type = tarfile.SYMTYPE
        link.linkname = "../outside"
        output.addfile(link)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_name(archive.name + ".sha256").write_text(f"{digest} *{archive.name}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Otillåten arkivpost"):
        verify_release.verify_archive(archive, tmp_path / "unpacked")


def test_existing_package_directory_is_preserved(tmp_path):
    archive = make_archive(tmp_path)
    target = tmp_path / "unpacked" / archive.name.removesuffix(".tar.gz")
    target.mkdir(parents=True)
    (target / "user-file").write_text("preserve", encoding="utf-8")
    with pytest.raises(ValueError, match="Katalogen finns redan"):
        verify_release.verify_archive(archive, tmp_path / "unpacked")
    assert (target / "user-file").read_text() == "preserve"


def test_cli_stdout_is_the_verified_package_path(tmp_path, capsys):
    archive = make_archive(tmp_path)
    assert verify_release.main(["--archive", str(archive), "--out-dir", str(tmp_path / "unpacked")]) == 0
    result = capsys.readouterr()
    assert Path(result.out.strip()).is_dir()
    assert "SHA-256" in result.err
