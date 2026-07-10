"""Pure geometry helpers for positioning the always-on overlay.

Kept free of any Qt import so the math is fast to unit-test and lives in exactly one
place. The Qt-facing code in ``overlay.py`` reads screen rectangles and the widget size,
then defers the actual coordinate math to here.
"""

from __future__ import annotations


def parse_anchor(anchor: str) -> tuple[str, str]:
    """Split an anchor like 'bottom-center' into (vertical, horizontal).

    vertical ∈ {top, bottom}; horizontal ∈ {left, center, right}. Anything unrecognized
    falls back to ('bottom', 'center') — the historical default.
    """
    parts = (anchor or "").lower().split("-")
    vertical = parts[0] if parts and parts[0] in ("top", "bottom") else "bottom"
    horizontal = parts[1] if len(parts) > 1 and parts[1] in ("left", "center", "right") else "center"
    return vertical, horizontal


def anchor_x(avail_x: int, avail_w: int, widget_w: int, horizontal: str, margin: int = 12) -> int:
    """X (left edge) for the horizontal anchor within the usable area.

    left/right sit ``margin`` px from the usable edge; center is centered. Clamped so the
    whole widget stays on the usable area.
    """
    if horizontal == "left":
        x = avail_x + margin
    elif horizontal == "right":
        x = avail_x + avail_w - widget_w - margin
    else:  # center
        x = avail_x + (avail_w - widget_w) // 2
    return max(avail_x, min(x, avail_x + avail_w - widget_w))


def clamp_overlay_y(
    avail_y: int,
    avail_h: int,
    full_y: int,
    full_h: int,
    widget_h: int,
    offset: int,
    gap: int = 12,
    min_taskbar: int = 48,
    vertical: str = "bottom",
    visual_inset: int = 0,
) -> int:
    """Return the Y for the overlay's *top* edge.

    Rest position (offset=0, bottom anchor): the widget's *visual* bottom (excluding
    transparent glow below the pill when ``visual_inset`` is set) sits ``gap`` pixels
    above the usable work-area bottom (above the taskbar).

    Offset semantics (both anchors):
      - positive → move down the screen (toward / into the panel zone)
      - negative → move up

    Historical bug: the lower clamp was ``usable_bottom - widget_h``, so a positive
    offset could only travel through the ``gap`` (~12px). The settings slider goes to
    +200, so anything past ~gap looked "stuck" with a permanent gap above the panel.
    The lower clamp is now the **physical screen bottom**, so the slider can pull the
    overlay down over the reserved panel strip when the user asks for it.

    Args:
        avail_y, avail_h: ``QScreen.availableGeometry()`` (excludes reserved panels).
        full_y, full_h: ``QScreen.geometry()`` (full screen).
        widget_h: full widget height including glow margins.
        offset: user fine-tune (negative = higher, positive = lower).
        gap: rest gap between the visual bottom and the usable bottom (default 12).
        min_taskbar: heuristic panel height when availableGeometry ≈ full geometry.
        vertical: ``top`` or ``bottom`` anchor.
        visual_inset: bottom glow (or similar) that may hang past the visible pill.
            Rest/clamp treat the content bottom as ``y + widget_h - visual_inset`` so
            transparent glow does not invent a permanent empty gap above the panel.
    """
    screen_bottom = full_y + full_h
    avail_bottom = avail_y + avail_h
    inset = max(0, int(visual_inset))

    if avail_bottom >= screen_bottom - 10:
        # availableGeometry didn't reserve the panel — fall back to the heuristic.
        usable_bottom = screen_bottom - min_taskbar
    else:
        usable_bottom = avail_bottom

    # Base position from the anchored edge, then apply the user's fine-tune offset
    # (positive = lower on screen for both anchors).
    if vertical == "top":
        y = avail_y + gap + offset
    else:  # bottom
        # Content bottom at rest = usable_bottom - gap; top of widget is content height
        # above that (widget_h - inset of that height is content+top glow).
        y = usable_bottom - gap - (widget_h - inset) + offset

    # Keep the whole widget on the physical screen (never fully off the top or bottom).
    # Deliberately allow y large enough that the widget covers the panel strip — that is
    # what the positive half of the overlay-position slider is for.
    upper_bound = avail_y
    lower_bound = screen_bottom - widget_h
    if lower_bound < upper_bound:
        # Tiny screen / huge widget — pin to the top of the usable area.
        return upper_bound
    return max(upper_bound, min(y, lower_bound))
