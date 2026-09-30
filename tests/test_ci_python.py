"""Reject incompatible native Python before creating a job environment."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def preparation(monkeypatch):
    spec = importlib.util.spec_from_file_location("prepare_python", ROOT / "scripts/ci/prepare-python.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "sys", SimpleNamespace(
        platform="darwin", version_info=(3, 12, 10), executable="/provisioned/python3",
    ))
    monkeypatch.setattr(module.platform, "machine", lambda: "arm64")
    monkeypatch.setitem(sys.modules, "_tkinter", SimpleNamespace(TK_VERSION="8.6"))
    monkeypatch.setitem(sys.modules, "tkinter", SimpleNamespace(
        Tcl=lambda: SimpleNamespace(call=lambda *args: "8.6.16"),
    ))
    return module


@pytest.mark.parametrize("system,arch,version", [
    ("linux", "arm64", (3, 12, 10)),
    ("darwin", "x86_64", (3, 12, 10)),
    ("darwin", "arm64", (3, 12, 9)),
])
def test_incompatible_mac_python_fails_before_venv_or_runner_writes(preparation, monkeypatch, tmp_path, system, arch, version):
    preparation.sys.platform = system
    preparation.sys.version_info = version
    monkeypatch.setattr(preparation.platform, "machine", lambda: arch)
    monkeypatch.setenv("GITHUB_PATH", str(tmp_path / "path"))
    monkeypatch.setattr(preparation.subprocess, "run", lambda *a, **kw: pytest.fail("venv started"))
    with pytest.raises(SystemExit, match="Aura macOS CI requires"):
        preparation.main(["--macos"])
    assert not (tmp_path / "path").exists()


@pytest.mark.parametrize("tk,tcl", [("8.5", "8.6.16"), ("8.6", "8.6.15"), ("9.0", "9.0.0"), ("8.6", "unknown")])
def test_incompatible_tk_stack_is_rejected(preparation, monkeypatch, tk, tcl):
    monkeypatch.setitem(sys.modules, "_tkinter", SimpleNamespace(TK_VERSION=tk))
    monkeypatch.setitem(sys.modules, "tkinter", SimpleNamespace(
        Tcl=lambda: SimpleNamespace(call=lambda *args: tcl),
    ))
    with pytest.raises(SystemExit, match="Tk 8.6 bindings and Tcl 8.6.16"):
        preparation.verify_macos_python()


def test_missing_tk_is_actionable(preparation, monkeypatch):
    monkeypatch.setitem(sys.modules, "_tkinter", None)
    with pytest.raises(SystemExit, match="runner owner"):
        preparation.verify_macos_python()


def test_verified_mac_interpreter_creates_and_exports_its_own_venv(preparation, monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(preparation.subprocess, "run", lambda *args, **kwargs: calls.append((args, kwargs)))
    for name in ("GITHUB_ENV", "GITHUB_PATH", "GITHUB_STEP_SUMMARY"):
        monkeypatch.setenv(name, str(tmp_path / name))
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    preparation.main(["--macos"])
    venv = tmp_path / "aura-venv"
    assert calls == [((["/provisioned/python3", "-m", "venv", str(venv), "--clear"],), {"check": True})]
    scripts = venv / ("Scripts" if os.name == "nt" else "bin")
    assert (tmp_path / "GITHUB_PATH").read_text().strip() == str(scripts)
    assert (tmp_path / "GITHUB_ENV").read_text().strip() == f"VIRTUAL_ENV={venv}"


@pytest.mark.skipif(sys.platform == "win32", reason="Mac bootstrap uses a POSIX shell")
def test_mac_bootstrap_uses_registered_interpreter_even_when_cache_path_has_spaces(tmp_path):
    cache = tmp_path / "runner cache"
    interpreter = cache / "Python/3.12.10/arm64/bin/python3"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$CAPTURE_PATH"\n')
    interpreter.chmod(0o700)
    capture = tmp_path / "arguments"
    result = subprocess.run(["bash", "scripts/ci/prepare-macos-python.sh"], cwd=ROOT,
        env={**os.environ, "RUNNER_TOOL_CACHE": str(cache), "PYTHON_VERSION": "3.12.10", "CAPTURE_PATH": str(capture)},
        capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert capture.read_text().splitlines() == ["scripts/ci/prepare-python.py", "--macos"]


@pytest.mark.skipif(sys.platform == "win32", reason="Mac bootstrap uses a POSIX shell")
def test_mac_bootstrap_missing_cache_fails_without_using_path_python(tmp_path):
    result = subprocess.run(["bash", "scripts/ci/prepare-macos-python.sh"], cwd=ROOT,
        env={**os.environ, "RUNNER_TOOL_CACHE": str(tmp_path), "PYTHON_VERSION": "3.12.10"},
        capture_output=True, text=True)
    assert result.returncode == 1
    assert "Missing provisioned macOS Python" in result.stderr
