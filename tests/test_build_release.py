# SPDX-FileCopyrightText: 2026 Deniz Özer
#
# SPDX-License-Identifier: MIT

import gzip
import importlib.util
import io
import os
import subprocess
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
            build_release.build_image(tmp_path, "0.2.1", "3.12", None, tmp_path / "requirements.lock")
        error_text = str(caught.value)
        assert caught.value.cmd == commands[0]
        assert not list(tmp_path.iterdir())
    else:
        assert build_release.build_image(tmp_path, "0.2.1", "3.12", None, tmp_path / "requirements.lock") == (
            image_id,
            28,
        )
        with gzip.open(tmp_path / "fuzzy-mcp-0.2.1-image.tar.gz", "rb") as archive:
            assert archive.read() == b"exported image"

    assert len(commands) == 1
    command = commands[0]
    build_args = [command[index + 1] for index, value in enumerate(command[:-1]) if value == "--build-arg"]
    assert all(name in build_args for name in proxies)
    assert "PYTHON_VERSION=3.12" in build_args
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
