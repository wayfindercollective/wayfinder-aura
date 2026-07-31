"""Pixel-exact Tk font rendering (utils/tk_dpi).

Empirical contract (validated live on Tk 8.6.12 and 8.6.14 against a 192-dpi
KDE session): with `tk scaling` set to Xft.dpi/72, negative (pixel) font
sizes render pixel-exact on every Tk we ship or run against. Without it, the
bundled 8.6.12 inflates every font by Xft.dpi/96 while widget dimensions
stay put — the 1.1.4 "weirdly large text" field report.
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from wayfinder.utils import tk_dpi


def _fake_run(stdout: str, returncode: int = 0):
    def run(*args, **kwargs):
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")
    return run


def test_read_xft_dpi_parses_xrdb(monkeypatch):
    monkeypatch.setattr(
        subprocess, "run",
        _fake_run("Xft.antialias:\t1\nXft.dpi:\t192\nXft.hinting:\t1\n"),
    )
    assert tk_dpi.read_xft_dpi() == 192.0


def test_read_xft_dpi_fractional(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _fake_run("Xft.dpi:\t120.5\n"))
    assert tk_dpi.read_xft_dpi() == 120.5


def test_read_xft_dpi_absent(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _fake_run("Xft.antialias:\t1\n"))
    assert tk_dpi.read_xft_dpi() is None


def test_read_xft_dpi_rejects_absurd_values(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _fake_run("Xft.dpi:\t9999\n"))
    assert tk_dpi.read_xft_dpi() is None


def test_read_xft_dpi_no_xrdb(monkeypatch):
    def run(*args, **kwargs):
        raise FileNotFoundError("xrdb")
    monkeypatch.setattr(subprocess, "run", run)
    assert tk_dpi.read_xft_dpi() is None


def test_read_xft_dpi_ignores_lookalike_keys(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _fake_run("MyXft.dpi:\t288\n"))
    assert tk_dpi.read_xft_dpi() is None


class _FakeRoot:
    def __init__(self, fail: bool = False):
        self.calls = []
        self._fail = fail
        self.tk = self

    def call(self, *args):
        if self._fail:
            raise RuntimeError("no display")
        self.calls.append(args)


def test_normalize_sets_scaling_from_xft_dpi(monkeypatch):
    monkeypatch.setattr(tk_dpi, "read_xft_dpi", lambda: 192.0)
    root = _FakeRoot()
    assert tk_dpi.normalize_tk_font_dpi(root) == 192.0
    assert root.calls == [("tk", "scaling", 192.0 / 72.0)]


def test_normalize_defaults_to_96(monkeypatch):
    """96 is applied, not skipped: pixel-exactness is the calibration baseline."""
    monkeypatch.setattr(tk_dpi, "read_xft_dpi", lambda: None)
    root = _FakeRoot()
    assert tk_dpi.normalize_tk_font_dpi(root) == 96.0
    assert root.calls == [("tk", "scaling", 96.0 / 72.0)]


def test_normalize_survives_tk_failure(monkeypatch):
    monkeypatch.setattr(tk_dpi, "read_xft_dpi", lambda: 144.0)
    root = _FakeRoot(fail=True)
    assert tk_dpi.normalize_tk_font_dpi(root) == 96.0
