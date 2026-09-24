"""The macOS pill stays inside the visible frame, clear of the Dock."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("PyQt6")

from wayfinder.ui import overlay  # noqa: E402


class _Rect:
    def __init__(self, x, y, w, h):
        self._r = (x, y, w, h)

    def x(self):
        return self._r[0]

    def y(self):
        return self._r[1]

    def width(self):
        return self._r[2]

    def height(self):
        return self._r[3]


def _position(monkeypatch, platform, *, dock_band=90, widget=(220, 80), glow=16):
    # 1920x1080 screen; Qt's available area (NSScreen.visibleFrame on macOS)
    # stops above a Dock band of ``dock_band`` points spanning the full width.
    screen = SimpleNamespace(
        geometry=lambda: _Rect(0, 0, 1920, 1080),
        availableGeometry=lambda: _Rect(0, 25, 1920, 1080 - 25 - dock_band),
    )
    monkeypatch.setattr(overlay.sys, "platform", platform)
    fake = SimpleNamespace(
        _target_screen=lambda: screen,
        _anchor="bottom-right",
        _vertical_offset=0,
        TASKBAR_GAP=overlay.GlassmorphicOverlay.TASKBAR_GAP,
        glow_margin=glow,
    )
    return overlay.GlassmorphicOverlay._calculate_position(fake, *widget)


@pytest.mark.parametrize("dock_band", [64, 90, 128])
def test_mac_pill_rests_above_the_dock_band(monkeypatch, dock_band):
    x, y = _position(monkeypatch, "darwin", dock_band=dock_band)
    visible_pill_bottom = y + 80 - 16  # widget height minus its bottom glow
    assert visible_pill_bottom <= 1080 - dock_band


def test_mac_and_linux_place_the_pill_identically(monkeypatch):
    assert _position(monkeypatch, "darwin") == _position(monkeypatch, "linux")
