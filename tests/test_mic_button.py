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


def test_ready_chip_has_a_light_line_glyph_on_a_dark_chip():
    img = render_mic_button(160, BLUE, card=CARD, ink=INK, light=LIGHT)
    assert _dist(_px(img, 2, 2), (0x15, 0x1B, 0x25)) < 12       # the card at the corners
    assert _dist(_px(img, 76, 72), (0xE8, 0xE8, 0xE8)) < 60     # capsule outline: light
    assert _dist(_px(img, 80, 72), (0xE8, 0xE8, 0xE8)) > 150    # inside the line glyph: chip


def test_recording_is_a_solid_state_disc_with_a_white_stop_square():
    img = render_mic_button(160, ROSE, card=CARD, ink=INK, light=LIGHT, pulse=0.9)
    assert _dist(_px(img, 80, 80), (255, 255, 255)) < 20        # stop square
    below = _px(img, 80, 80 + 36)                                # disc below the square
    assert _dist(below, (0xE8, 0x70, 0x7F)) < 20                 # solid rose, no gradient


def test_hover_colours_the_rim():
    rest = render_mic_button(160, BLUE, card=CARD, ink=INK, light=LIGHT)
    hover = render_mic_button(160, BLUE, card=CARD, ink=INK, light=LIGHT, hover=True)
    # The rim at the top of the disc (radius 24 of 80 units -> 48 px at 160).
    r_rest, r_hover = _px(rest, 80, 33), _px(hover, 80, 33)
    assert r_hover[2] - r_hover[0] > r_rest[2] - r_rest[0] + 30  # blue, not neutral


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


def test_linux_brand_mark_is_rendered_for_the_zoom(monkeypatch):
    from wayfinder.ui.macos_brand_mark import MARK_HEIGHT, MARK_WIDTH

    app = SimpleNamespace(ui_scale=1.95)
    image, size = wm.WayfinderApp._linux_brand_mark_image(app, False)
    assert size == (MARK_WIDTH, MARK_HEIGHT)
    # Drawn at the real pixel size CTkImage will show, not stretched from 1x.
    assert image.size == (round(MARK_WIDTH * 1.95), round(MARK_HEIGHT * 1.95))
