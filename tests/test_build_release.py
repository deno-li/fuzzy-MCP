# SPDX-FileCopyrightText: 2026 Deniz Özer
#
# SPDX-License-Identifier: MIT

import fnmatch
import gzip
import importlib.util
import io
import os
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "build_release.py"
if not SCRIPT.exists():
    pytest.skip("scripts/build_release.py finns bara i repot", allow_module_level=True)
spec = importlib.util.spec_from_file_location("build_release", SCRIPT)
build_release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build_release)


@pytest.mark.parametrize("build_fails", [False, True])
def test_image_build_inherits_proxies_without_exposing_values(tmp_path, monkeypatch, capsys, build_fails):
    proxies = {
        "HTTPS_PROXY": "http://synthetic-user:synthetic-password@proxy.example:8080",
        "HTTP_PROXY": "http://another-user:another-password@proxy.example:8080",
        "NO_PROXY": "private.internal.example,127.0.0.1",
    }
    for name, value in proxies.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(build_release.shutil, "which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr(build_release, "source_commit", lambda: ("a" * 40, False))
    commands = []

    def docker_build(cmd, **kwargs):
        commands.append(cmd)
        assert cmd[:2] == ["docker", "build"]
        assert kwargs["check"] is True
        # No replacement environment: Docker can resolve each --build-arg NAME from the original environment.
        assert "env" not in kwargs
        assert {name: os.environ[name] for name in proxies} == proxies
        if build_fails:
            raise subprocess.CalledProcessError(1, cmd)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(build_release.subprocess, "run", docker_build)
    monkeypatch.setattr(build_release, "image_matches_lock", lambda *_: 28)
    monkeypatch.setattr(
        build_release.subprocess,
        "Popen",
        lambda *_args, **_kwargs: SimpleNamespace(stdout=io.BytesIO(b"exported image"), wait=lambda: 0),
    )
    image_id = "sha256:" + "b" * 64
    monkeypatch.setattr(build_release, "_output", lambda _: image_id)

    error_text = ""
    if build_fails:
        with pytest.raises(subprocess.CalledProcessError) as caught:
            build_release.build_image(tmp_path, "0.2.1", None, tmp_path / "requirements.lock")
        error_text = str(caught.value)
        assert caught.value.cmd == commands[0]
        assert not list(tmp_path.iterdir())
    else:
        assert build_release.build_image(tmp_path, "0.2.1", None, tmp_path / "requirements.lock") == (
            image_id,
            28,
        )
        with gzip.open(tmp_path / "fuzzy-mcp-0.2.1-image.tar.gz", "rb") as archive:
            assert archive.read() == b"exported image"

    assert len(commands) == 1
    command = commands[0]
    build_args = [command[index + 1] for index, value in enumerate(command[:-1]) if value == "--build-arg"]
    assert all(name in build_args for name in proxies)
    # The Python version is fixed by the Dockerfile's digest-pinned base image, not a build argument.
    assert not any(arg.startswith("PYTHON_VERSION") for arg in build_args)
    assert "VERSION=0.2.1" in build_args
    captured = capsys.readouterr()
    sensitive_values = [
        *proxies.values(),
        "synthetic-user",
        "synthetic-password",
        "another-user",
        "another-password",
        "private.internal.example",
    ]
    for value in sensitive_values:
        assert value not in " ".join(command)
        assert value not in captured.out + captured.err + error_text


def test_dockerfile_pins_one_base_image_with_the_linux_python():
    base, python = build_release.base_image()
    assert base.startswith(f"python:{python}-slim@sha256:")
    # Dependabot reads only literal FROM lines; both stages use the reference that BUILDINFO.txt names.
    lines = build_release.DOCKERFILE.read_text(encoding="utf-8").splitlines()
    assert [line.split()[1] for line in lines if line.startswith("FROM ")] == [base, base]
    assert [line for line in lines if re.match(r"\s*FROM\s", line, re.IGNORECASE)] == [
        line for line in lines if line.startswith("FROM ")
    ]
    # The image gets the Linux package's Python: another minor version must change both deliberately.
    assert python == build_release.DEFAULT_PYTHON["linux"]


@pytest.mark.parametrize(
    "dockerfile",
    [
        "FROM python:3.12-slim AS build\nFROM python:3.12-slim\n",
        f"FROM python:3.12-slim@sha256:{'a' * 64} AS build\nFROM python:3.12-slim@sha256:{'b' * 64}\n",
        f"ARG BASE=python:3.12-slim@sha256:{'a' * 64}\nFROM ${{BASE}} AS build\nFROM ${{BASE}}\n",
    ],
    ids=["no-digest", "two-bases", "arg-template"],
)
def test_base_image_must_be_one_literal_pinned_python_image(tmp_path, monkeypatch, dockerfile):
    path = tmp_path / "Dockerfile"
    path.write_text(dockerfile, encoding="utf-8")
    monkeypatch.setattr(build_release, "DOCKERFILE", path)
    with pytest.raises(SystemExit, match="låst till en digest"):
        build_release.base_image()


def test_base_image_reads_from_lines_as_buildkit_does(tmp_path, monkeypatch):
    """BuildKit also takes an indented FROM, in any case, with flags before the image: an indented stage without a
    digest is refused and named, and --platform=… is not taken for the image (#44)."""
    digest = "a" * 64
    path = tmp_path / "Dockerfile"
    monkeypatch.setattr(build_release, "DOCKERFILE", path)
    path.write_text(f"FROM python:3.12-slim@sha256:{digest} AS build\n  FROM python:3.13-slim\n", encoding="utf-8")
    with pytest.raises(SystemExit, match=r"låst till en digest \(@sha256:…\): FROM python:3\.13-slim$"):
        build_release.base_image()
    path.write_text(
        f"FROM --platform=$BUILDPLATFORM python:3.12-slim@sha256:{digest} AS build\n"
        f"from python:3.12-slim@sha256:{digest}\n",
        encoding="utf-8",
    )
    assert build_release.base_image() == (f"python:3.12-slim@sha256:{digest}", "3.12")


def test_build_tools_live_under_the_output_directory(monkeypatch, tmp_path):
    """Not in the system's temp directory, which hardened hosts mount noexec, so that the tools can run (#44)."""
    seen: list[Path] = []

    def tools(directory: Path) -> Path:
        seen.append(directory)
        return directory / "bin"

    monkeypatch.setattr(sys, "argv", ["build_release.py", "--platform", "windows", "--out", str(tmp_path / "ut")])
    monkeypatch.setattr(build_release.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(build_release, "build_tools", tools)
    monkeypatch.setattr(build_release, "build", lambda args, tools, image_base: 0)
    assert build_release.main() == 0
    assert seen[0].parent == tmp_path / "ut" and seen[0].name.startswith(".build-tools-")
    assert not seen[0].exists()  # removed after the build


def test_an_os_error_is_one_line(monkeypatch, tmp_path, capsys):
    """An OSError (e.g. a tool that may not run) ends the build with one line, not a traceback (#44)."""
    import runpy

    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "uv").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    (fake / "uv").chmod(0o755)
    (tmp_path / "fil").write_text("", encoding="utf-8")
    monkeypatch.setenv("PATH", f"{fake}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setattr(sys, "argv", ["build_release.py", "--platform", "linux", "--out", str(tmp_path / "fil" / "ut")])
    with pytest.raises(SystemExit) as caught:
        runpy.run_path(str(SCRIPT), run_name="__main__")
    assert caught.value.code == 1
    err = capsys.readouterr().err
    assert "Steget misslyckades:" in err and "Traceback" not in err and len(err.strip().splitlines()) == 1


def test_image_needs_the_base_image_python(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["build_release.py", "--platform", "linux", "--python", "3.11", "--with-image"])
    monkeypatch.setattr(build_release, "build_tools", lambda _: pytest.fail("ska stoppa före bygget"))
    with pytest.raises(SystemExit) as caught:
        build_release.main()
    assert caught.value.code == 2
    assert "--with-image kräver --python 3.12" in capsys.readouterr().err


def test_buildinfo_names_the_base_image(monkeypatch):
    monkeypatch.setattr(build_release, "source_commit", lambda: ("a" * 40, False))
    monkeypatch.setattr(build_release, "_output", lambda _: "uv 0.11.32")
    args = SimpleNamespace(
        platform="linux", python="3.12", out="release", skip_audit=False, with_image=True, ca_file=None
    )
    base = f"python:3.12-slim@sha256:{'c' * 64}"
    lines = build_release.build_info("0.2.3", args, "uv", f"sha256:{'b' * 64}", 28, base).splitlines()
    # release_notes.py copies the lines that start with "Imagens" to the release text.
    assert f"Imagens bas: {base}" in lines


def test_build_tools_are_locked_with_hashes():
    text = build_release.BUILD_REQUIREMENTS.read_text(encoding="utf-8").replace("\\\n", " ")
    names = []
    for line in text.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        match = re.fullmatch(r"([a-z0-9-]+)==(\S+)( ; [^-]+)?((?: +--hash=sha256:[0-9a-f]{64})+)", line.strip())
        assert match, f"utan exakt version och SHA-256: {line}"
        names.append(match[1])
    assert len(names) == len(set(names))
    assert {"uv", "hatchling", "pip", "cyclonedx-bom", "pip-audit"} <= set(names)


def test_build_tools_are_installed_with_hash_checks(tmp_path, monkeypatch):
    commands = []
    monkeypatch.setattr(build_release, "run", lambda cmd, **_: commands.append(cmd))
    tools = build_release.build_tools(tmp_path / "tools")
    assert tools.parent == tmp_path / "tools"
    assert commands[0][:2] == ["uv", "venv"]
    install = commands[1]
    assert install[:3] == ["uv", "pip", "install"]
    assert "--require-hashes" in install
    assert install[install.index("-r") + 1] == str(build_release.BUILD_REQUIREMENTS)


def test_build_tools_reach_the_docker_build_context():
    """.dockerignore leaves out scripts/ but not the lock file that the Dockerfile's build stage mounts."""
    lock = build_release.BUILD_REQUIREMENTS.relative_to(build_release.ROOT).as_posix()
    assert f"source={lock}," in build_release.DOCKERFILE.read_text(encoding="utf-8")
    included = True
    for line in (build_release.ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        pattern = line.strip().removeprefix("!")
        if fnmatch.fnmatch(lock, pattern) or lock.startswith(pattern.rstrip("/") + "/"):
            included = line.startswith("!")
    assert included
