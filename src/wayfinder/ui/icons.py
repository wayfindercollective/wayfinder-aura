"""
Tinted line-icon system for Wayfinder Aura.

White-on-transparent Lucide PNGs (96px = 4x of a 24px logical box, see
assets/icons/ and scripts/gen_icons.py) are loaded, re-tinted to any palette
color at runtime via pure PIL (alpha preserved), and wrapped in a cached
CTkImage for CustomTkinter widgets.

Design intent: single-tint minimal line icons drawn from the app palette — no
multicolor, no decoration. Mirrors the proven ``_ultra_glow_logo`` 4x HiDPI
pattern (render large, downscale into a CTkImage).

This module is intentionally headless-importable: customtkinter is imported
lazily inside ``get_icon`` so the pure-PIL layer (``tint_icon``) and the icon
registry can be exercised in tests without a display.
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image

# --- ICON_DIR resolution -----------------------------------------------------
# Mirror the ICON_PATH logic in wayfinder_main.py (~:87-99): assets/ ships next
# to the app both in a source checkout and in the Flatpak install tree
# (${FLATPAK_DEST}/lib/wayfinder-aura/assets/icons/). From this module
# (src/wayfinder/ui/icons.py) the repo/app root is parents[3]; both layouts
# keep assets/ there, so the same relative walk resolves in each.
_MODULE_DIR = Path(__file__).resolve().parent


def _resolve_icon_dir() -> Path:
    """Find assets/icons for both a source checkout and the Flatpak install."""
    candidates = [
        # src/wayfinder/ui/icons.py -> repo/app root (parents[3]) / assets/icons
        _MODULE_DIR.parents[2] / "assets" / "icons",
        # Explicit Flatpak install path, in case the package is imported from a
        # non-standard location.
        Path("/app/lib/wayfinder-aura/assets/icons"),
    ]
    for cand in candidates:
        if cand.is_dir():
            return cand
    # Fall back to the primary candidate so error messages point somewhere real.
    return candidates[0]


ICON_DIR = _resolve_icon_dir()

# Registered icon names (Lucide). Kept in sync with scripts/gen_icons.py ICONS
# and tests/test_icons.py. A name here must have a matching PNG in ICON_DIR.
REGISTRY = [
    "audio-waveform",
    "settings-2",
    "pen-line",
    "history",
    "mic",
    "briefcase",
    "message-circle",
    "terminal",
    "sparkles",
    "download",
    "check",
    "rotate-ccw",
    "inbox",
    "timer",
    "x",
    "gamepad-2",
]

# style_id -> icon name, for the Style-tab tone cards (parallel to STYLE_LABELS
# in wayfinder_main.py, which stays text-only for logs).
STYLE_ICONS = {
    "minimal": "mic",
    "professional": "briefcase",
    "casual": "message-circle",
    "dev": "terminal",
    "personal": "sparkles",
}


def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if len(h) != 6:
        raise ValueError(f"invalid hex color: {hex_color!r}")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _icon_path(name: str) -> Path:
    path = ICON_DIR / f"{name}.png"
    if not path.exists():
        raise KeyError(
            f"icon {name!r} not found at {path} — add it to assets/icons/ "
            f"(regenerate via scripts/gen_icons.py) and REGISTRY in icons.py"
        )
    return path


def tint_icon(name: str, hex_color: str, size_px: int) -> Image.Image:
    """Load the white PNG for `name`, recolor its RGB to `hex_color` while
    preserving the original alpha channel exactly, and return it resized to a
    square `size_px`. Pure PIL — no Tk/CTk dependency."""
    src = Image.open(_icon_path(name)).convert("RGBA")
    if src.size != (size_px, size_px):
        src = src.resize((size_px, size_px), Image.LANCZOS)
    r, g, b = _hex_to_rgb(hex_color)
    alpha = src.getchannel("A")
    # Solid color plate stamped through the glyph's alpha — the white source is
    # discarded, only its coverage (alpha) shapes the tint.
    tinted = Image.new("RGBA", src.size, (r, g, b, 0))
    tinted.putalpha(alpha)
    return tinted


# (name, size, color) -> CTkImage
_CACHE: dict[tuple[str, int, str], object] = {}


def get_icon(name: str, size: int = 16, color: str = "#8B8B8F"):
    """Return a cached CTkImage for `name` tinted `color`, sized `size` logical
    px. Rendered at 4x (size*4 source) for HiDPI crispness, matching the
    _ultra_glow_logo pattern. customtkinter is imported lazily so this module
    stays headless-importable."""
    key = (name, size, color)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached

    import customtkinter as ctk  # lazy: keeps module importable without a display

    hi_res = tint_icon(name, color, size * 4)
    image = ctk.CTkImage(light_image=hi_res, dark_image=hi_res, size=(size, size))
    _CACHE[key] = image
    return image
