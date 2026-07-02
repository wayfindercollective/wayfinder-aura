"""Tests for the tinted line-icon system (Phase 4 of the premium polish pass).

The pure-PIL layer (`tint_icon`, ICON_DIR resolution, REGISTRY, the cache key
logic) is exercised headlessly. `get_icon` needs customtkinter; it imports
fine without a display on the host, so it is tested behind an importorskip
guard.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from PIL import Image

from wayfinder.ui import icons
from wayfinder.ui.icons import (
    ICON_DIR,
    REGISTRY,
    STYLE_ICONS,
    tint_icon,
)

REPO = Path(__file__).resolve().parent.parent
MAIN = REPO / "wayfinder_main.py"


# ---------------------------------------------------------------------------
# assets / registry integrity
# ---------------------------------------------------------------------------

def test_icon_dir_resolves_to_real_directory():
    assert ICON_DIR.is_dir(), f"ICON_DIR does not exist: {ICON_DIR}"


def test_every_registered_icon_has_a_png():
    missing = [n for n in REGISTRY if not (ICON_DIR / f"{n}.png").exists()]
    assert not missing, f"registered icons with no PNG in {ICON_DIR}: {missing}"


def test_style_icons_point_at_registered_names():
    for style, name in STYLE_ICONS.items():
        assert name in REGISTRY, f"STYLE_ICONS[{style!r}] = {name!r} not in REGISTRY"


def test_source_pngs_are_white_on_transparent_96px():
    for name in REGISTRY:
        im = Image.open(ICON_DIR / f"{name}.png").convert("RGBA")
        assert im.size == (96, 96), f"{name}: expected 96x96, got {im.size}"
        alphas = im.getchannel("A").getdata()
        assert min(alphas) == 0, f"{name}: no transparent pixels"
        assert max(alphas) > 0, f"{name}: fully transparent (no stroke)"


# ---------------------------------------------------------------------------
# tint_icon (pure PIL)
# ---------------------------------------------------------------------------

def test_tint_icon_renders_every_registered_icon():
    for name in REGISTRY:
        im = tint_icon(name, "#A78BFA", 32)
        assert im.mode == "RGBA"
        assert im.size == (32, 32)


def test_tint_icon_output_size_matches_request_including_4x():
    # get_icon renders its source at 4x; tint_icon must honor that request.
    im = tint_icon("mic", "#FFFFFF", 16 * 4)
    assert im.size == (64, 64)


def test_tint_applies_requested_color_and_preserves_alpha():
    name = "mic"
    color = "#A78BFA"  # (167, 139, 250)
    src = Image.open(ICON_DIR / f"{name}.png").convert("RGBA").resize((64, 64), Image.LANCZOS)
    tinted = tint_icon(name, color, 64)

    # Alpha channel preserved exactly (the glyph coverage is unchanged).
    assert list(tinted.getchannel("A").getdata()) == list(src.getchannel("A").getdata())

    # A fully-opaque stroke pixel carries the requested RGB.
    alpha = tinted.getchannel("A")
    max_a = max(alpha.getdata())
    for (r, g, b, a) in tinted.getdata():
        if a == max_a:
            assert (r, g, b) == (0xA7, 0x8B, 0xFA)
            break
    else:  # pragma: no cover
        pytest.fail("no opaque stroke pixel found")


def test_tint_icon_missing_name_raises_keyerror_with_name():
    with pytest.raises(KeyError) as exc:
        tint_icon("definitely-not-an-icon", "#FFFFFF", 16)
    assert "definitely-not-an-icon" in str(exc.value)


def test_short_hex_and_case_insensitive():
    a = tint_icon("check", "#fff", 16)
    b = tint_icon("check", "#FFFFFF", 16)
    assert list(a.getdata()) == list(b.getdata())


# ---------------------------------------------------------------------------
# get_icon (needs customtkinter; imports fine headlessly on the host)
# ---------------------------------------------------------------------------

def test_get_icon_caches_by_name_size_color():
    pytest.importorskip("customtkinter")
    icons._CACHE.clear()
    a = icons.get_icon("mic", 16, "#A78BFA")
    b = icons.get_icon("mic", 16, "#A78BFA")
    assert a is b, "identical (name,size,color) must return the cached CTkImage"

    c = icons.get_icon("mic", 16, "#FFFFFF")
    assert c is not a, "different color must not hit the same cache entry"

    d = icons.get_icon("mic", 20, "#A78BFA")
    assert d is not a, "different size must not hit the same cache entry"

    assert ("mic", 16, "#A78BFA") in icons._CACHE


def test_get_icon_logical_size_matches_request():
    pytest.importorskip("customtkinter")
    icons._CACHE.clear()
    img = icons.get_icon("mic", 18, "#FFFFFF")
    # CTkImage stores the logical (display) size; source is rendered at 4x.
    assert tuple(img._size) == (18, 18)


def test_get_icon_missing_name_raises_keyerror():
    pytest.importorskip("customtkinter")
    with pytest.raises(KeyError):
        icons.get_icon("definitely-not-an-icon", 16, "#FFFFFF")


# ---------------------------------------------------------------------------
# manifest: every icon name referenced in the app exists on disk
# ---------------------------------------------------------------------------

def test_all_referenced_icon_names_exist_on_disk():
    """Scan wayfinder_main.py for get_icon("name", ...) calls and STYLE_ICONS
    targets; assert every referenced icon has a PNG."""
    src = MAIN.read_text(encoding="utf-8")
    referenced = set(re.findall(r"get_icon\(\s*['\"]([a-z0-9-]+)['\"]", src))
    referenced |= set(STYLE_ICONS.values())  # rendered indirectly via STYLE_ICONS[tone_id]
    assert referenced, "expected at least one get_icon(...) reference in wayfinder_main.py"
    missing = sorted(n for n in referenced if not (ICON_DIR / f"{n}.png").exists())
    assert not missing, f"code references icons with no PNG: {missing}"
