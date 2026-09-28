"""Native window chrome for the main Aura window on macOS.

Two layers, both AppKit-only:

* A unified title bar: transparent, no title text, dark appearance, so the
  traffic lights sit directly on the app surface.
* Frosted dark glass: an ``NSVisualEffectView`` (behind-window blur) placed
  under Tk's content view, with a deep-ink tint on top so the surface stays
  dark and text stays readable. Tk paints ``systemTransparent`` wherever the
  glass should show (window margins and the gaps between cards).

Set ``WAYFINDER_MACOS_GLASS=0`` to keep the unified title bar on an opaque
surface.
"""

from __future__ import annotations

import functools
import os
import sys

TRANSPARENT = "systemTransparent"

# NSVisualEffectView constants (AppKit enums, stable since 10.14).
_MATERIAL_UNDER_WINDOW_BACKGROUND = 21
_MATERIAL_HUD_WINDOW = 13
_BLENDING_BEHIND_WINDOW = 0
_STATE_ACTIVE = 1
_AUTORESIZE_WIDTH_HEIGHT = 2 | 16
_ORDER_BELOW = -1
_TITLE_HIDDEN = 1

# Deep ink (#0A0D13) over the blur: dark first, glass second. Overridable for
# tuning with WAYFINDER_MACOS_GLASS_TINT (alpha 0..1).
_GLASS_TINT = (10 / 255, 13 / 255, 19 / 255, float(os.environ.get("WAYFINDER_MACOS_GLASS_TINT", "0.78")))
# Opaque fallback surface (matches COLORS["bg_base"] #0D1117).
_SOLID_BG = (13 / 255, 17 / 255, 23 / 255, 1.0)


# Deeper ink panes on the glass, each with a lifted blue-grey rim so its edge
# reads against any backdrop (light pages behind the window, or a dark desktop).
GLASS_PALETTE = {
    "bg_base": "#0B0F15",       # content pane + underlays (deepest)
    "bg_surface": "#121720",    # sidebar pane
    "bg_card": "#151B25",       # hero + cards (Metal hero bg follows)
    "border_rim": "#26303F",    # glass edge on every pane
}

# Pages inside the rounded content pane stay square and opaque (Aqua would
# otherwise flash white while mapping them), inset so the corners show.
CONTENT_PANE_INSET = 6

_applied: set[int] = set()


def place_in_content_pane(widget, inset: int | None = None) -> None:
    """Fill the content pane, inset so its rounded corners stay visible.

    Uses Tk's placer directly: CustomTkinter's place() rejects width/height
    (it wants them scaled in the constructor), and the inset is a few raw px.
    """
    import tkinter

    if inset is None:
        inset = CONTENT_PANE_INSET if glass_enabled() else 0
    tkinter.Place.place_configure(
        widget,
        relx=0, rely=0, relwidth=1, relheight=1,
        x=inset, y=inset, width=-2 * inset, height=-2 * inset,
    )


def apply_glass_palette(*color_maps) -> bool:
    """Swap in the glass pane colours (macOS glass only; call before building UI)."""
    if not glass_enabled():
        return False
    for colors in color_maps:
        colors.update(GLASS_PALETTE)
    return True


@functools.lru_cache(maxsize=1)
def _reduce_transparency() -> bool:
    """System Settings → Accessibility → Display → Reduce transparency.

    Read once per launch (the palette is chosen before the UI is built)."""
    try:
        from AppKit import NSWorkspace

        return bool(NSWorkspace.sharedWorkspace().accessibilityDisplayShouldReduceTransparency())
    except Exception:
        return False


def glass_enabled() -> bool:
    # Honour Reduce Transparency: the solid dark palette instead of glass.
    return (
        sys.platform == "darwin"
        and os.environ.get("WAYFINDER_MACOS_GLASS", "1") != "0"
        and not _reduce_transparency()
    )


def prepare_tk_root(root) -> bool:
    """Make the Tk root paint nothing where glass should show. Call before mapping."""
    if not glass_enabled():
        return False
    try:
        root.wm_attributes("-transparent", True)
        return True
    except Exception:
        return False


def _find_window(title: str):
    from AppKit import NSApp

    for window in NSApp.windows():
        try:
            if str(window.title()) == title:
                return window
        except Exception:
            continue
    return None


def apply_window_chrome(title: str) -> bool:
    """Unify the title bar and install the glass backdrop. Idempotent."""
    if sys.platform != "darwin":
        return False
    try:
        from AppKit import NSAppearance, NSColor, NSView, NSVisualEffectView
    except Exception:
        return False

    window = _find_window(title)
    if window is None:
        return False
    key = int(window.windowNumber())
    if key in _applied:
        return True

    window.setTitlebarAppearsTransparent_(True)
    window.setTitleVisibility_(_TITLE_HIDDEN)
    window.setAppearance_(NSAppearance.appearanceNamed_("NSAppearanceNameDarkAqua"))

    if not glass_enabled():
        window.setBackgroundColor_(NSColor.colorWithSRGBRed_green_blue_alpha_(*_SOLID_BG))
        _applied.add(key)
        return True

    content = window.contentView()
    frame_view = content.superview() if content is not None else None
    if frame_view is None:
        return False

    window.setOpaque_(False)
    window.setBackgroundColor_(NSColor.clearColor())

    glass = NSVisualEffectView.alloc().initWithFrame_(frame_view.bounds())
    glass.setMaterial_(int(os.environ.get("WAYFINDER_MACOS_GLASS_MATERIAL", _MATERIAL_HUD_WINDOW)))
    glass.setBlendingMode_(_BLENDING_BEHIND_WINDOW)
    glass.setState_(_STATE_ACTIVE)
    glass.setAutoresizingMask_(_AUTORESIZE_WIDTH_HEIGHT)

    tint = NSView.alloc().initWithFrame_(glass.bounds())
    tint.setWantsLayer_(True)
    # Quartz bridges CGColorRef; NSColor.CGColor() alone yields a raw pointer.
    from Quartz import CGColorCreateSRGB

    tint.layer().setBackgroundColor_(CGColorCreateSRGB(*_GLASS_TINT))
    tint.setAutoresizingMask_(_AUTORESIZE_WIDTH_HEIGHT)
    glass.addSubview_(tint)

    frame_view.addSubview_positioned_relativeTo_(glass, _ORDER_BELOW, content)
    _applied.add(key)
    return True
