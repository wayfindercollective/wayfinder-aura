"""Artwork and Finder layout for the macOS installer disk image.

The window is 660x400pt: the app on the left, the Applications alias on the
right, a glowing guide arrow between them, on the app's dark navy surface.
The background ships at 1x and 2x in one TIFF so Retina Finder windows stay
sharp.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

WINDOW_SIZE = (660, 400)
ICON_SIZE = 128
APP_POSITION = (170, 208)
APPLICATIONS_POSITION = (490, 208)

BG_TOP = (22, 29, 42)
BG_BOTTOM = (8, 10, 15)
ACCENT = (91, 143, 212)
ACCENT_LIGHT = (140, 186, 255)
TEXT = (236, 240, 247)
TEXT_MUTED = (136, 146, 164)

_SYSTEM_FONT = "/System/Library/Fonts/SFNS.ttf"


def _font(size: int, weight: str = "Regular") -> ImageFont.FreeTypeFont:
    try:
        font = ImageFont.truetype(_SYSTEM_FONT, size)
        try:
            font.set_variation_by_name(weight)
        except Exception:
            pass
        return font
    except OSError:
        return ImageFont.load_default(size)


def _gradient(size, top, bottom) -> Image.Image:
    w, h = size
    column = Image.new("RGB", (1, h))
    for y in range(h):
        t = y / max(1, h - 1)
        column.putpixel((0, y), tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)))
    return column.resize((w, h))


def _centered_text(draw, y, text, font, fill, width):
    left, _top, right, _bottom = draw.textbbox((0, 0), text, font=font)
    draw.text(((width - (right - left)) / 2, y), text, font=font, fill=fill)


def render_background(scale: int) -> Image.Image:
    w, h = WINDOW_SIZE[0] * scale, WINDOW_SIZE[1] * scale
    img = _gradient((w, h), BG_TOP, BG_BOTTOM).convert("RGBA")

    # Soft brand glow behind the icon row.
    glow = Image.new("L", (w, h), 0)
    gd = ImageDraw.Draw(glow)
    cy = APP_POSITION[1] * scale
    gd.ellipse((w * 0.18, cy - 110 * scale, w * 0.82, cy + 120 * scale), fill=70)
    glow = glow.filter(ImageFilter.GaussianBlur(70 * scale))
    img = Image.composite(Image.new("RGBA", (w, h), (*ACCENT, 255)), img, glow)

    # Guide arrow: a glowing stroke with a chevron head, app -> Applications.
    x0 = (APP_POSITION[0] + ICON_SIZE / 2 + 22) * scale
    x1 = (APPLICATIONS_POSITION[0] - ICON_SIZE / 2 - 22) * scale
    y = APP_POSITION[1] * scale
    head = 12 * scale
    width = max(2, int(3 * scale))
    shape = [(x0, y), (x1, y)]
    chevron = [(x1 - head, y - head), (x1, y), (x1 - head, y + head)]

    halo = Image.new("L", (w, h), 0)
    hd = ImageDraw.Draw(halo)
    hd.line(shape, fill=140, width=width * 4)
    hd.line(chevron, fill=140, width=width * 4, joint="curve")
    halo = halo.filter(ImageFilter.GaussianBlur(6 * scale))
    img = Image.composite(Image.new("RGBA", (w, h), (*ACCENT, 255)), img, halo)

    stroke = Image.new("L", (w, h), 0)
    sd = ImageDraw.Draw(stroke)
    # Fade the shaft in from the app side so it reads as motion.
    steps = 48
    for i in range(steps):
        a = x0 + (x1 - x0) * i / steps
        b = x0 + (x1 - x0) * (i + 1) / steps
        sd.line([(a, y), (b, y)], fill=int(60 + 195 * (i + 1) / steps), width=width)
    sd.line(chevron, fill=255, width=width, joint="curve")
    img = Image.composite(Image.new("RGBA", (w, h), (*ACCENT_LIGHT, 255)), img, stroke)

    draw = ImageDraw.Draw(img)
    _centered_text(draw, 44 * scale, "Install Wayfinder Aura", _font(22 * scale, "Semibold"), TEXT, w)
    _centered_text(
        draw, 76 * scale, "Drag the app into Applications", _font(13 * scale), TEXT_MUTED, w
    )
    _centered_text(
        draw,
        (WINDOW_SIZE[1] - 40) * scale,
        "Local voice dictation  ·  runs entirely on this Mac",
        _font(11 * scale),
        (*TEXT_MUTED, 255),
        w,
    )

    return img.convert("RGB")


def write_background(directory: Path) -> Path:
    """Write a 1x+2x multi-representation TIFF for the Finder window."""
    directory.mkdir(parents=True, exist_ok=True)
    one = directory / "dmg-background.png"
    two = directory / "dmg-background@2x.png"
    render_background(1).save(one, dpi=(72, 72))
    render_background(2).save(two, dpi=(144, 144))
    tiff = directory / "dmg-background.tiff"
    subprocess.run(
        ["tiffutil", "-cathidpicheck", str(one), str(two), "-out", str(tiff)],
        check=True,
        capture_output=True,
    )
    return tiff


def dmgbuild_settings(app_path: Path, background: Path, volume_icon: Path) -> dict:
    """Settings for dmgbuild.build_dmg (Finder layout written without Finder)."""
    return {
        "format": "UDZO",
        "filesystem": "HFS+",
        "files": [str(app_path)],
        "symlinks": {"Applications": "/Applications"},
        "icon": str(volume_icon),
        "badge_icon": None,
        "background": str(background),
        "window_rect": ((200, 140), WINDOW_SIZE),
        "default_view": "icon-view",
        "show_status_bar": False,
        "show_tab_view": False,
        "show_toolbar": False,
        "show_pathbar": False,
        "show_sidebar": False,
        "show_icon_preview": False,
        "icon_size": ICON_SIZE,
        "text_size": 13,
        "icon_locations": {
            app_path.name: APP_POSITION,
            "Applications": APPLICATIONS_POSITION,
        },
    }


if __name__ == "__main__":  # preview: python packaging/macos/dmg_assets.py
    out = Path(tempfile.gettempdir()) / "wayfinder-dmg-preview"
    print(write_background(out))
