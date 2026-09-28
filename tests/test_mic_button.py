"""The hero mic button renderer, its canvas size, and the Linux default zoom."""
from __future__ import annotations

from types import SimpleNamespace

import wayfinder_main as wm
from wayfinder.ui.mic_button import render_mic_button

CARD, INK, LIGHT = "#151B25", "#0B0F15", "#E8E8E8"
BLUE, ROSE = "#5B8FD4", "#E8707F"


def _px(img, x, y):
    return img.getpixel((x, y))


def _dist(a, b):
    return sum((a[i] - b[i]) ** 2 for i in range(3)) ** 0.5


def test_renders_every_state_at_the_requested_size():
    for phys in (80, 156):
        for kw in ({}, {"hover": True}, {"active": True}, {"pulse": 0.8}, {"pulse": 1.0}):
            img = render_mic_button(phys, BLUE, card=CARD, ink=INK, light=LIGHT, **kw)
            assert img.size == (phys, phys) and img.mode == "RGB"


def test_idle_disc_is_dark_glass_with_a_light_glyph():
    img = render_mic_button(160, BLUE, card=CARD, ink=INK, light=LIGHT)
    corner = _px(img, 2, 2)
    assert _dist(corner, (0x15, 0x1B, 0x25)) < 12        # the card shows at the corners
    assert _dist(_px(img, 80, 72), (0xE8, 0xE8, 0xE8)) < 40  # glyph capsule, light
    assert _dist(_px(img, 60, 80), (0xE8, 0xE8, 0xE8)) > 120  # beside it: dark disc


def test_recording_fills_the_disc_with_the_state_colour_and_a_stop_square():
    img = render_mic_button(160, ROSE, card=CARD, ink=INK, light=LIGHT, pulse=0.9)
    assert _dist(_px(img, 80, 80), (0x0B, 0x0F, 0x15)) < 20  # stop square: ink
    ring = _px(img, 80, 80 + 36)                                 # disc below the square
    assert ring[0] > ring[2] + 40                                # rose, not dark


def test_hover_glows_brighter_than_rest():
    rest = render_mic_button(160, BLUE, card=CARD, ink=INK, light=LIGHT)
    hover = render_mic_button(160, BLUE, card=CARD, ink=INK, light=LIGHT, hover=True)
    # Just outside the disc (radius 24 of 80 units -> 48 px at 160) the halo shows.
    assert sum(_px(hover, 80, 80 - 52)) > sum(_px(rest, 80, 80 - 52))


def test_mic_canvas_follows_zoom_on_linux_only():
    assert wm._mic_canvas_px(2.0, "linux") == 160
    assert wm._mic_canvas_px(1.0, "linux") == 80
    assert wm._mic_canvas_px(9.0, "linux") == 200     # clamped like the UI scale
    assert wm._mic_canvas_px(2.0, "darwin") == wm._windows_px(80)


def test_linux_default_zoom_matches_the_desktop_scale(monkeypatch):
    import wayfinder.utils.tk_dpi as tk_dpi

    app = SimpleNamespace(winfo_screenheight=lambda: 2160)
    monkeypatch.setattr(wm, "_IS_LINUX", True)
    for dpi, scale in ((192.0, 2.0), (144.0, 1.5), (120.0, 1.25), (96.0, 1.0)):
        monkeypatch.setattr(tk_dpi, "read_xft_dpi", lambda d=dpi: d)
        assert wm.WayfinderApp._get_recommended_scale(app) == scale
    # No Xft.dpi: the resolution table (4K -> 200%).
    monkeypatch.setattr(tk_dpi, "read_xft_dpi", lambda: None)
    assert wm.WayfinderApp._get_recommended_scale(app) == 2.0
