"""Hover hints stay on screen: above, below, or beside their icon."""

from wayfinder.ui.tooltip_geometry import (
    NARROW_WRAP,
    WIDE_WRAP,
    anchor_in_area,
    tooltip_position,
    tooltip_wraplength,
)

AREA = (0, 25, 1512, 870)  # a MacBook visible frame: menu bar above, Dock below


def _inside(pos, size, area=AREA, margin=10):
    (x, y), (w, h) = pos, size
    left, top, aw, ah = area
    return left + margin <= x and x + w <= left + aw - margin and top + margin <= y and y + h <= top + ah - margin


def test_short_hint_sits_centred_above_its_icon():
    anchor = (700, 500, 16, 16)
    x, y = tooltip_position(anchor, (200, 60), AREA)
    assert y == 500 - 6 - 60
    assert x == 700 + (16 - 200) // 2


def test_no_room_above_goes_below():
    anchor = (700, 60, 16, 16)
    x, y = tooltip_position(anchor, (200, 60), AREA)
    assert y == 60 + 16 + 6


def test_tall_hint_near_the_middle_goes_beside_and_stays_on_screen():
    # The Audio Processing case: too tall for above or below the icon.
    anchor = (350, 300, 16, 16)
    size = (440, 640)
    pos = tooltip_position(anchor, size, AREA)
    assert _inside(pos, size)
    assert pos[0] >= 350 + 16 + 6  # to the right of the icon, not covering it


def test_beside_prefers_the_roomier_side():
    anchor = (1400, 400, 16, 16)
    size = (440, 700)
    x, _ = tooltip_position(anchor, size, AREA)
    assert x + 440 <= 1400 - 6 + 1  # left of the icon


def test_never_runs_under_the_dock():
    for ay in range(40, 880, 40):
        pos = tooltip_position((600, ay, 16, 16), (300, 420), AREA)
        assert pos[1] + 420 <= 25 + 870 - 10


def test_horizontal_clamp_at_screen_edges():
    x, _ = tooltip_position((2, 500, 16, 16), (260, 40), AREA)
    assert x == 10
    x, _ = tooltip_position((1500, 500, 16, 16), (260, 40), AREA)
    assert x == 1512 - 10 - 260


def test_long_hints_wrap_wider():
    assert tooltip_wraplength("Size of the status pill.") == NARROW_WRAP
    assert tooltip_wraplength("x" * 400) == WIDE_WRAP
    assert tooltip_wraplength("") == NARROW_WRAP


def test_anchor_on_another_monitor_is_detected():
    assert anchor_in_area((700, 500, 16, 16), AREA)
    assert not anchor_in_area((2000, 500, 16, 16), AREA)


def test_every_live_setting_hint_is_plain_text():
    """Rule 11: no emoji as UI chrome in the hints users hover."""
    import unicodedata
    import wayfinder_main

    for key, text in wayfinder_main.SETTING_TOOLTIPS.items():
        symbols = [c for c in text if unicodedata.category(c) == "So"]
        assert not symbols, f"{key}: {symbols}"
