# SPDX-FileCopyrightText: 2026 Deniz Özer
#
# SPDX-License-Identifier: MIT

"""Build a release package for IT operations (one target platform per package).

    python scripts/build_release.py --platform windows            # Windows x64, Python 3.14 (standard för Windows)
    python scripts/build_release.py --platform linux              # Linux x86_64, Python 3.12 (standard för Linux)
    python scripts/build_release.py --platform linux --python 3.11  # t.ex. Debian 12
    python scripts/build_release.py --skip-audit                   # offline-ish build without pip-audit
    python scripts/build_release.py --platform linux --with-image  # also a container image (docker save)

Creates release/fuzzy-mcp-<version>-<platform>-py<python>/ and a .zip (Windows) or .tar.gz (Linux) next to it:

    dist/                 wheel + source distribution
    requirements.lock     every dependency pinned with SHA-256 hashes, resolved for the target platform
    wheelhouse/           the dependency wheels for offline installation (--no-index)
    sbom.cdx.json         CycloneDX SBOM of the locked dependencies
    audit.txt             pip-audit result (known vulnerabilities) unless --skip-audit
    smoke_test.py, eneo_check.py
    docs/                 DRIFT.md, ENEO.md and eneo/ (assistant instruction, compose file, tool profiles)
    README.md, CHANGELOG.md, SECURITY.md, … and LICENSE/LICENSES/ – same paths as in the repository, so links work
    fuzzy-mcp-<version>-image.tar.gz   with --with-image: `docker load -i …`
    BUILDINFO.txt         git commit, platform, build time, command, tool versions and image id (traceability)
    SHA256SUMS            checksums of every file above

Needs only uv (https://docs.astral.sh/uv/) and internet access to PyPI; pip, cyclonedx-py and pip-audit run
through uvx in pinned versions (PIP, CYCLONEDX, PIP_AUDIT below).
The lock is resolved for the TARGET platform: on Windows the MCP SDK also needs pywin32, which a lock
resolved on Linux would miss.
"""

import argparse
import datetime
import gzip
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLATFORMS = {
    # uv --python-platform, pip download --platform tags
    "windows": ("x86_64-pc-windows-msvc", ["win_amd64"]),
    "linux": ("x86_64-manylinux_2_28", ["manylinux_2_28_x86_64", "manylinux_2_17_x86_64", "manylinux2014_x86_64"]),
}
# Copied with the same relative path as in the repository, so the links between the documents keep working.
PACKAGE_DOCS = [
    "README.md",
    "CHANGELOG.md",
    "SECURITY.md",
    "DEVELOPMENT.md",
    "CONTRIBUTING.md",
    "GOVERNANCE.md",
    "CODE_OF_CONDUCT.md",
    "publiccode.yml",
    "LICENSE",
    "LICENSES/MIT.txt",
    "LICENSES/CC0-1.0.txt",
    "REUSE.toml",
    "docs/DRIFT.md",
    "docs/ENEO.md",
    "docs/eneo/assistentinstruktion.md",
    "docs/eneo/docker-compose.fuzzy-mcp.yml",
    "docs/eneo/verktygsprofiler.md",
]
# Copied to the package root, where the documentation runs them from.
DEFAULT_PYTHON = {"windows": "3.14", "linux": "3.12"}
PACKAGE_SCRIPTS = ["scripts/smoke_test.py", "scripts/eneo_check.py"]
# Tools run through uvx, pinned so that a release build is repeatable (bump deliberately).
PIP = "pip==26.2.1"
CYCLONEDX = "cyclonedx-bom==7.5.0"
PIP_AUDIT = "pip-audit==2.10.1"


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    print("+", " ".join(cmd), flush=True)
    return subprocess.run(cmd, check=True, cwd=ROOT, **kwargs)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def build_image(out: Path, version: str, python: str, ca_file: str | None, lock: Path) -> tuple[str, int]:
    """docker build + docker save | gzip into the package (proxy settings are passed on from the environment).
    Returns the image id and the number of dependencies checked against the lock file."""
    if shutil.which("docker") is None:
        raise SystemExit("docker saknas – bygg utan --with-image")
    tag = f"fuzzy-mcp:{version}"
    # --no-cache-filter: resolve the image's dependencies now, in the same run as requirements.lock.
    cmd = ["docker", "build", "--no-cache-filter", "build", "-t", tag]
    cmd += ["--build-arg", f"PYTHON_VERSION={python}", "--build-arg", f"VERSION={version}"]
    commit, dirty = source_commit()
    if commit:
        cmd += ["--build-arg", f"VCS_REF={commit}-dirty" if dirty else f"VCS_REF={commit}"]
    for var in ("HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY"):
        if os.environ.get(var):
            cmd += ["--build-arg", f"{var}={os.environ[var]}"]
    if ca_file:
        cmd += ["--secret", f"id=ca,src={Path(ca_file).resolve()}"]
    run([*cmd, "."])
    checked = image_matches_lock(tag, lock)
    image = out / f"fuzzy-mcp-{version}-image.tar.gz"
    save = subprocess.Popen(["docker", "save", tag], stdout=subprocess.PIPE, cwd=ROOT)
    with gzip.open(image, "wb", compresslevel=6) as target:
        assert save.stdout is not None
        shutil.copyfileobj(save.stdout, target)
    if save.wait() != 0:
        raise subprocess.CalledProcessError(save.returncode, ["docker", "save", tag])
    print(f"Image: {image.name} ({image.stat().st_size // 1_000_000} MB)")
    return _output(["docker", "image", "inspect", "--format", "{{.Id}}", tag]), checked


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def image_matches_lock(tag: str, lock: Path) -> int:
    """The image resolves its dependencies itself; make sure it got exactly the versions in requirements.lock, so
    that the package's SBOM and audit also describe the image's Python packages."""
    listing = (
        "import importlib.metadata as m; "
        "print('\\n'.join(sorted(d.metadata['Name'] + '==' + d.version for d in m.distributions())))"
    )
    proc = subprocess.run(
        ["docker", "run", "--rm", "--network", "none", "--entrypoint", "python", tag, "-c", listing],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if proc.returncode or not proc.stdout.strip():
        raise SystemExit(
            f"Kunde inte lista imagens Python-paket (docker run, kod {proc.returncode}): {proc.stderr.strip()}"
        )
    image = {_normalize(n): v for n, _, v in (line.partition("==") for line in proc.stdout.splitlines()) if v}
    image.pop("fuzzy-mcp", None)
    lines = lock.read_text(encoding="utf-8").splitlines()
    locked = {
        _normalize(m[1]): m[2] for m in (re.match(r"([A-Za-z0-9._-]+)==([^\s;\\]+)", line) for line in lines) if m
    }
    if image != locked:
        names = sorted(image.keys() | locked.keys())
        differ = [f"{n}: image={image.get(n)} lås={locked.get(n)}" for n in names if image.get(n) != locked.get(n)]
        raise SystemExit("Imagens Python-beroenden skiljer sig från requirements.lock: " + "; ".join(differ))
    return len(locked)


def wheel_licence(wheel: Path) -> list[dict]:
    """The licence a wheel declares in its METADATA, in CycloneDX form (empty if it declares none)."""
    with zipfile.ZipFile(wheel) as zf:
        name = next(n for n in zf.namelist() if n.endswith(".dist-info/METADATA"))
        headers = zf.read(name).decode("utf-8", "replace").split("\n\n", 1)[0].splitlines()
    fields: dict[str, list[str]] = {}
    for line in headers:
        key, _, value = line.partition(": ")
        fields.setdefault(key, []).append(value.strip())
    if expression := fields.get("License-Expression", [""])[0]:
        return [{"expression": expression, "acknowledgement": "declared"}]
    names = [v for v in fields.get("License", []) if v and len(v) <= 80]
    names = names or [c.rsplit(" :: ", 1)[-1] for c in fields.get("Classifier", []) if c.startswith("License :: ")]
    return [{"license": {"name": n, "acknowledgement": "declared"}} for n in names[:1]]


def complete_sbom(sbom: Path, wheelhouse: Path) -> None:
    """cyclonedx-py reads only the lock file: add each component's declared licence from its wheel's METADATA, and
    the direct dependencies of fuzzy-mcp (from pyproject.toml) to the dependency graph (the rest is marked unknown)."""
    wheels = {}
    for wheel in wheelhouse.glob("*.whl"):
        name, version = wheel.name.split("-")[:2]
        wheels[(_normalize(name), version)] = wheel
    document = json.loads(sbom.read_text(encoding="utf-8"))
    components = document.get("components", [])
    for component in components:
        wheel = wheels.get((_normalize(component["name"]), component.get("version", "")))
        if wheel and (licences := wheel_licence(wheel)):
            component["licenses"] = licences
    requirements = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["dependencies"]
    direct = {_normalize(re.split(r"[\s<>=!~;\[]", requirement, maxsplit=1)[0]) for requirement in requirements}
    root = document["metadata"]["component"]["bom-ref"]
    depends_on = sorted(c["bom-ref"] for c in components if _normalize(c["name"]) in direct)
    # The lock file does not say what depends on what. cyclonedx-py still writes an empty entry per component, which
    # CycloneDX reads as "has no dependencies": keep only the root and mark the rest of the graph as unknown.
    document["dependencies"] = [{"ref": root, "dependsOn": depends_on}]
    document["compositions"] = [
        {"aggregate": "complete", "dependencies": [root]},
        {"aggregate": "unknown", "dependencies": sorted(c["bom-ref"] for c in components)},
    ]
    sbom.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _output(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def source_commit() -> tuple[str, bool]:
    """The checked-out commit and whether tracked files differ from it ("" if this is not a git checkout)."""
    commit = _output(["git", "rev-parse", "HEAD"])
    return commit, bool(commit and _output(["git", "status", "--porcelain", "--untracked-files=no"]))


def _command_line(args: argparse.Namespace) -> str:
    """The build command rebuilt from the parsed options, without local file paths (CA file, absolute --out)."""
    parts = ["python scripts/build_release.py", f"--platform {args.platform}", f"--python {args.python}"]
    if args.out != "release":
        parts.append("--out <katalog>" if Path(args.out).is_absolute() else f"--out {args.out}")
    parts += [flag for flag, on in (("--skip-audit", args.skip_audit), ("--with-image", args.with_image)) if on]
    if args.ca_file:
        parts.append("--ca-file <ca-bundle>")
    return " ".join(parts)


def build_info(version: str, args: argparse.Namespace, image_id: str = "", image_deps: int = 0) -> str:
    """Which source and tools the package was built from, so the recipient can trace it back to a commit."""
    commit, dirty = source_commit()
    commit = (
        commit + (" (med ändringar som inte är incheckade)" if dirty else "")
    ) or "okänd (inte en git-arbetskopia)"
    built = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return "\n".join(
        [
            f"fuzzy-mcp {version}",
            f"Plattform: {args.platform}, Python {args.python}",
            f"Commit: {commit}",
            f"Byggt: {built}",
            f"Kommando: {_command_line(args)}",
            f"Verktyg: {_output(['uv', '--version']) or 'uv okänd'}, Python {platform.python_version()}",
            *([f"Container-image: fuzzy-mcp:{version} {image_id}"] if image_id else []),
            *(
                [f"Imagens Python-beroenden: samma versioner som requirements.lock ({image_deps} st.)"]
                if image_id
                else []
            ),
            "",
        ]
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Bygg ett leveranspaket för IT-drift.", allow_abbrev=False)
    parser.add_argument("--platform", choices=sorted(PLATFORMS), default="windows")
    parser.add_argument(
        "--python",
        help="Python-version på målservern (standard: 3.14 för Windows, som får säkerhetsrättade installationsfiler; "
        "3.12 för Linux, t.ex. Ubuntu 24.04 och RHEL 9.4+)",
    )
    parser.add_argument("--out", default="release", help="Utkatalog (relativt repot)")
    parser.add_argument("--skip-audit", action="store_true", help="Hoppa över pip-audit")
    parser.add_argument("--with-image", action="store_true", help="Bygg även container-imagen (kräver docker)")
    parser.add_argument(
        "--ca-file",
        help="Komplett CA-bundle (publika rotcertifikat + organisationens CA) för container-bygget bakom en "
        "TLS-inspekterande proxy",
    )
    args = parser.parse_args()
    if args.with_image and args.platform != "linux":
        parser.error("--with-image kräver --platform linux (imagen är en Linux-container)")
    args.python = args.python or DEFAULT_PYTHON[args.platform]

    if shutil.which("uv") is None:
        print("uv saknas – installera från https://docs.astral.sh/uv/", file=sys.stderr)
        return 2
    version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    uv_platform, pip_platforms = PLATFORMS[args.platform]
    name = f"fuzzy-mcp-{version}-{args.platform}-py{args.python}"
    out = (ROOT / args.out / name).resolve()
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    run(["uv", "build", "--out-dir", str(out / "dist")])
    (out / "dist" / ".gitignore").unlink(missing_ok=True)
    lock = out / "requirements.lock"
    run(
        [
            "uv", "pip", "compile", "pyproject.toml", "--generate-hashes", "--no-header", "--quiet",
            "--python-version", args.python, "--python-platform", uv_platform, "-o", str(lock),
        ]
    )  # fmt: skip
    platform_args = [arg for tag in pip_platforms for arg in ("--platform", tag)]
    run(
        [
            "uvx", "--quiet", "--from", PIP, "pip", "download", "--quiet", "--no-deps", "--only-binary=:all:",
            "--implementation", "cp", "--python-version", args.python, *platform_args,
            "-r", str(lock), "-d", str(out / "wheelhouse"),
        ]
    )  # fmt: skip
    sbom = out / "sbom.cdx.json"
    run(
        [
            "uvx", "--quiet", "--from", CYCLONEDX, "cyclonedx-py", "requirements", str(lock),
            "--pyproject", "pyproject.toml", "--output-reproducible", "-o", str(sbom),
        ]
    )  # fmt: skip
    complete_sbom(sbom, out / "wheelhouse")
    if not args.skip_audit:
        audit = subprocess.run(
            ["uvx", "--quiet", "--from", PIP_AUDIT, "pip-audit", "--disable-pip", "--no-deps", "-r", str(lock)],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        (out / "audit.txt").write_text(audit.stdout + audit.stderr, encoding="utf-8")
        print(audit.stdout.strip())
        if audit.returncode != 0:
            print("pip-audit hittade sårbarheter eller misslyckades – se audit.txt", file=sys.stderr)
            return 1
    for doc in PACKAGE_DOCS:
        (out / doc).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / doc, out / doc)
    for script in PACKAGE_SCRIPTS:
        shutil.copy2(ROOT / script, out / Path(script).name)
    image_id, image_deps = build_image(out, version, args.python, args.ca_file, lock) if args.with_image else ("", 0)
    (out / "BUILDINFO.txt").write_text(build_info(version, args, image_id, image_deps), encoding="utf-8")

    files = sorted(p for p in out.rglob("*") if p.is_file() and p.name != "SHA256SUMS")
    (out / "SHA256SUMS").write_text(
        "".join(f"{sha256(p)} *{p.relative_to(out).as_posix()}\n" for p in files), encoding="utf-8"
    )
    wheels = len(list((out / "wheelhouse").glob("*.whl")))
    pinned = sum(1 for line in lock.read_text(encoding="utf-8").splitlines() if "==" in line)
    if wheels != pinned:
        print(f"wheelhouse har {wheels} hjul men låsfilen {pinned} paket", file=sys.stderr)
        return 1

    if args.platform == "windows":
        archive = out.parent / f"{name}.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in sorted(out.rglob("*")):
                zf.write(path, Path(name) / path.relative_to(out))
    else:
        archive = out.parent / f"{name}.tar.gz"
        with tarfile.open(archive, "w:gz") as tf:
            tf.add(out, arcname=name)
    (archive.parent / f"{archive.name}.sha256").write_text(f"{sha256(archive)} *{archive.name}\n", encoding="utf-8")
    print(f"\nKlart: {archive} ({pinned} beroenden, {wheels} hjul)")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except subprocess.CalledProcessError as exc:
        print(f"\nSteget misslyckades (avslutskod {exc.returncode}): {' '.join(map(str, exc.cmd))}", file=sys.stderr)
        sys.exit(1)
