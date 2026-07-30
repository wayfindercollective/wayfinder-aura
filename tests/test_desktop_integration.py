"""AppImage desktop self-integration: the downloaded file must feel installed."""

import os
from pathlib import Path

import pytest

from wayfinder.utils.desktop_integration import APP_ID, integrate_appimage


@pytest.fixture
def appimage_run(monkeypatch, tmp_path):
    """Simulate an AppImage runtime: $APPIMAGE file, $APPDIR with icon, XDG home."""
    appimage = tmp_path / "Wayfinder_Aura-x86_64.AppImage"
    appimage.write_bytes(b"\x7fELF fake")
    appdir = tmp_path / "AppDir"
    appdir.mkdir()
    (appdir / f"{APP_ID}.png").write_bytes(b"\x89PNG fake")
    data = tmp_path / "xdg-data"
    monkeypatch.setenv("APPIMAGE", str(appimage))
    monkeypatch.setenv("APPDIR", str(appdir))
    monkeypatch.setenv("XDG_DATA_HOME", str(data))
    return appimage, appdir, data


def test_creates_desktop_entry_and_icon(appimage_run):
    appimage, _appdir, data = appimage_run
    assert integrate_appimage(log=lambda m: None) is True

    desktop = data / "applications" / f"{APP_ID}.desktop"
    icon = data / "icons" / "hicolor" / "256x256" / "apps" / f"{APP_ID}.png"
    assert desktop.exists() and icon.exists()
    content = desktop.read_text()
    assert f'Exec="{appimage}"' in content
    assert f"Icon={APP_ID}" in content
    assert "Categories=Utility;Accessibility;" in content


def test_idempotent_when_current(appimage_run):
    _appimage, _appdir, data = appimage_run
    integrate_appimage(log=lambda m: None)
    desktop = data / "applications" / f"{APP_ID}.desktop"
    before = desktop.stat().st_mtime_ns
    assert integrate_appimage(log=lambda m: None) is True
    assert desktop.stat().st_mtime_ns == before  # untouched second time


def test_repoints_after_appimage_moves(appimage_run, monkeypatch, tmp_path):
    appimage, _appdir, data = appimage_run
    integrate_appimage(log=lambda m: None)
    moved = tmp_path / "Downloads" ; moved.mkdir()
    new_path = moved / appimage.name
    appimage.rename(new_path)
    monkeypatch.setenv("APPIMAGE", str(new_path))

    assert integrate_appimage(log=lambda m: None) is True
    content = (data / "applications" / f"{APP_ID}.desktop").read_text()
    assert f'Exec="{new_path}"' in content


def test_noop_outside_appimage(monkeypatch, tmp_path):
    monkeypatch.delenv("APPIMAGE", raising=False)
    monkeypatch.delenv("APPDIR", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert integrate_appimage(log=lambda m: None) is False
    assert not (tmp_path / "xdg").exists()
