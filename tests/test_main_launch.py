"""Tests for source launcher health checks."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import main


def _write_pyvenv_cfg(path, version: str) -> None:
    venv = path / "venv-gpu"
    venv.mkdir()
    (venv / "pyvenv.cfg").write_text(
        f"home = /usr/bin\ninclude-system-site-packages = true\nversion = {version}\n",
        encoding="utf-8",
    )


def test_stale_pyvenv_metadata_warns_when_smoke_imports_pass(tmp_path, monkeypatch, capsys):
    _write_pyvenv_cfg(tmp_path, "3.14.3")
    monkeypatch.setattr(main.sys, "version_info", SimpleNamespace(major=3, minor=13, micro=14))

    main._check_venv_health(tmp_path / "venv-gpu", smoke_imports=("sys",))

    output = capsys.readouterr().out
    assert "pyvenv.cfg says Python 3.14.3" in output
    assert "smoke imports passed, continuing" in output


def test_stale_pyvenv_metadata_exits_when_smoke_imports_fail(tmp_path, monkeypatch, capsys):
    _write_pyvenv_cfg(tmp_path, "3.14.3")
    monkeypatch.setattr(main.sys, "version_info", SimpleNamespace(major=3, minor=13, micro=14))

    with pytest.raises(SystemExit) as excinfo:
        main._check_venv_health(tmp_path / "venv-gpu", smoke_imports=("definitely_missing_wayfinder_dep",))

    output = capsys.readouterr().out
    assert excinfo.value.code == 1
    assert "VENV MISMATCH" in output
    assert "Failed smoke imports:" in output
    assert "definitely_missing_wayfinder_dep" in output


def test_tkinter_smoke_failure_prints_system_package_hint(tmp_path, monkeypatch, capsys):
    _write_pyvenv_cfg(tmp_path, "3.14.3")
    monkeypatch.setattr(main.sys, "version_info", SimpleNamespace(major=3, minor=13, micro=14))
    monkeypatch.setattr(
        main,
        "_missing_venv_smoke_imports",
        lambda _modules: ["customtkinter (No module named 'tkinter')"],
    )

    with pytest.raises(SystemExit) as excinfo:
        main._check_venv_health(tmp_path / "venv-gpu")

    output = capsys.readouterr().out
    assert excinfo.value.code == 1
    assert "If tkinter is missing" in output
    assert "sudo dnf install python3-tkinter" in output
    assert "sudo apt install python3-tk" in output
