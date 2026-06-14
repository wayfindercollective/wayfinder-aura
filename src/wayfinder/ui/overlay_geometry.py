"""Pure geometry helpers for positioning the always-on overlay.

Kept free of any Qt import so the math is fast to unit-test and lives in exactly one
place. The Qt-facing code in ``overlay.py`` reads screen rectangles and the widget size,
then defers the actual coordinate math to here.
"""

from __future__ import annotations


def clamp_overlay_y(
    avail_y: int,
    avail_h: int,
    full_y: int,
    full_h: int,
    widget_h: int,
    offset: int,
    gap: int = 12,
    min_taskbar: int = 48,
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

    # Base = gap above the usable bottom, then apply the user's offset.
    y = usable_bottom - gap - widget_h + offset

    # Clamp: never above the top of the screen; the lower bound lets the user push the overlay
    # all the way down so its BOTTOM edge can touch the bottom of the SCREEN (over the taskbar)
    # — requested behavior. Default offsets still rest gap-above the usable bottom; only a large
    # positive offset reaches the very bottom. (Was clamped to usable_bottom, i.e. the taskbar
    # top, which is what stopped the slider ~12px above the taskbar.)
    lower_bound = screen_bottom - widget_h
    return max(avail_y, min(y, lower_bound))
