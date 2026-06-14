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
) -> int:
    """Return the Y for the overlay's *top* edge.

    The overlay sits at the bottom of the usable screen area: its bottom edge rests
    ``gap`` pixels above the taskbar, then the user's vertical ``offset`` is applied, then
    the result is clamped so the *whole* widget stays on the usable area (it can never run
    off the top of the screen, nor disappear behind the taskbar).

    Args:
        avail_y, avail_h: top and height of ``QScreen.availableGeometry()`` (excludes
            panels/taskbars that reserve space).
        full_y, full_h: top and height of ``QScreen.geometry()`` (the whole screen).
        widget_h: the overlay widget's full height (glow margins included). The clamp
            subtracts this so the entire widget stays visible — i.e. it is size-aware.
        offset: the user's ``overlay_vertical_offset`` (negative = higher, positive = lower).
        gap: resting gap between the widget's bottom and the taskbar (default 12px).
        min_taskbar: heuristic taskbar height used ONLY in the dock/overlay case below.

    Why ``min_taskbar`` exists: normally ``availableGeometry()`` already excludes the
    taskbar, so ``usable_bottom == avail_bottom`` is the real usable edge. But some panels
    (auto-hide docks, certain KDE/Wayland setups) do *not* reserve space, so
    ``availableGeometry()`` spans (nearly) the full screen. There is no reliable cross-DE
    API for such a panel's height, so in that case only we fall back to assuming
    ``min_taskbar`` pixels at the bottom. This is a heuristic, not the true edge.
    """
    screen_bottom = full_y + full_h
    avail_bottom = avail_y + avail_h

    if avail_bottom >= screen_bottom - 10:
        # availableGeometry didn't reserve the panel — fall back to the heuristic.
        usable_bottom = screen_bottom - min_taskbar
    else:
        usable_bottom = avail_bottom

    # Base position from the anchored edge, then apply the user's fine-tune offset (positive =
    # lower on screen for both anchors).
    if vertical == "top":
        y = avail_y + gap + offset
    else:  # bottom
        y = usable_bottom - gap - widget_h + offset

    # Clamp so the WHOLE widget stays on the usable area: never off the top, never below the
    # taskbar edge. (KWin also clamps native X11 positioning to the work area, so the overlay
    # rests above the taskbar — placing it OVER the taskbar would need KWin frameGeometry
    # scripting, which we deliberately don't use on X11 to avoid the reposition-freeze.)
    lower_bound = usable_bottom - widget_h
    return max(avail_y, min(y, lower_bound))
