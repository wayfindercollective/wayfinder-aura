"""Header brand mark rendered for Core Animation on macOS (pure PIL parts)."""
from __future__ import annotations

from pathlib import Path

import pytest

from wayfinder.ui.macos_brand_mark import (
    MARK_HEIGHT, MARK_WIDTH, MacOSBrandMark, render_brand_mark,
)

ICON = Path(__file__).resolve().parents[1] / "assets" / "icon.png"


@pytest.mark.parametrize("scale", [1.0, 2.0, 3.0])
def test_mark_is_rendered_at_the_backing_scale(scale):
    image = render_brand_mark(ICON, scale=scale)
    assert image.mode == "RGBA"
    assert image.size == (round(MARK_WIDTH * scale), round(MARK_HEIGHT * scale))


def test_mark_is_transparent_around_the_arrow_so_it_sits_on_the_glass():
    image = render_brand_mark(ICON, scale=2.0)
    alpha = image.getchannel("A")
    assert alpha.getpixel((0, 0)) == 0
    assert alpha.getpixel((image.width - 1, image.height - 1)) == 0
    assert max(alpha.getdata()) == 255  # the arrow itself is solid


def test_mark_is_deterministic():
    a = render_brand_mark(ICON, scale=2.0).tobytes()
    b = render_brand_mark(ICON, scale=2.0).tobytes()
    assert a == b


def test_ultra_mark_has_a_gold_halo():
    free = render_brand_mark(ICON, scale=2.0, is_ultra=False)
    ultra = render_brand_mark(ICON, scale=2.0, is_ultra=True)

    def warmth(image):
        r, g, b, a = (sum(ch.getdata()) for ch in image.split())
        return (r - b) / max(a, 1)

    assert warmth(ultra) > warmth(free)


def test_native_layer_is_never_created_off_macos(monkeypatch):
    import wayfinder.ui.macos_brand_mark as mod

    monkeypatch.setattr(mod.sys, "platform", "linux")
    assert MacOSBrandMark.try_create(object(), object(), ICON, is_ultra=False,
                                     accent="#4682DC", gold="#E5AC2A") is None
