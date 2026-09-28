"""Where a hover tooltip goes, and how wide it wraps.

Long setting hints (Audio Processing is ~900 characters) used to wrap at 260px,
flip below their icon when there was no room above, and run off the bottom of
the screen - on macOS behind the Dock. Pure functions so the placement is
testable without a display.
"""

from __future__ import annotations

NARROW_WRAP = 260
WIDE_WRAP = 420
# Past this many characters a narrow column gets taller than most screens.
WIDE_WRAP_AFTER_CHARS = 180


def tooltip_wraplength(text: str) -> int:
    """Short hints stay a compact column; long ones get a wider, shorter block."""
    return WIDE_WRAP if len(text or "") > WIDE_WRAP_AFTER_CHARS else NARROW_WRAP


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(value, high)) if high >= low else low


def tooltip_position(
    anchor: tuple[int, int, int, int],
    size: tuple[int, int],
    area: tuple[int, int, int, int],
    *,
    gap: int = 6,
    margin: int = 10,
) -> tuple[int, int]:
    """Top-left (x, y) for a tooltip of ``size`` next to ``anchor``.

    ``anchor`` and ``area`` are (x, y, w, h) in screen coordinates; ``area`` is
    the usable screen (on macOS the visible frame, without menu bar and Dock).
    Preference: centred above, then centred below, then beside the anchor on
    the roomier side. The result always lies inside ``area`` when it fits.
    """
    ax, ay, aw, ah = anchor
    tw, th = size
    left, top, aw_, ah_ = area
    right, bottom = left + aw_, top + ah_

    centre_x = ax + (aw - tw) // 2
    x = _clamp(centre_x, left + margin, right - margin - tw)

    above = ay - gap - th
    if above >= top + margin:
        return x, above
    below = ay + ah + gap
    if below + th <= bottom - margin:
        return x, below

    # Neither fits vertically: go beside the anchor, vertically centred on it
    # and pulled inside the area.
    y = _clamp(ay + (ah - th) // 2, top + margin, bottom - margin - th)
    room_right = right - (ax + aw + gap)
    room_left = (ax - gap) - left
    if room_right >= tw + margin or room_right >= room_left:
        side_x = ax + aw + gap
    else:
        side_x = ax - gap - tw
    return _clamp(side_x, left + margin, right - margin - tw), y


def anchor_in_area(anchor: tuple[int, int, int, int], area: tuple[int, int, int, int]) -> bool:
    """False when the anchor sits on another monitor than ``area`` describes."""
    ax, ay, aw, ah = anchor
    left, top, w, h = area
    cx, cy = ax + aw // 2, ay + ah // 2
    return left <= cx < left + w and top <= cy < top + h
