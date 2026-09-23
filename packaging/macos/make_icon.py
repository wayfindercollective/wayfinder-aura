#!/usr/bin/env python3
"""Render the macOS app icon (assets/icon.icns) from the brand arrow.

macOS 26+ places any icon that does not fill the standard squircle on a light
plate ("icon jail"). This draws a full-bleed dark squircle on Apple's 1024pt
grid (824pt body, 100pt margin) with a soft drop shadow, a faint glass rim,
a brand-blue glow and the Wayfinder arrow, then packs every iconset size.

macOS-only packaging: Linux keeps assets/icon.png.

    venv-mac/bin/python packaging/macos/make_icon.py
"""

from __future__ import annotations

import math
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ICNS_PATH = PROJECT_ROOT / "assets" / "icon.icns"

# Brand arrow in 64-unit design space (mirrors ui/tray_icon.py _ARROW).
ARROW = [(55, 3), (43, 61), (38, 44), (29, 47), (28, 38), (9, 41)]

CANVAS = 1024
BODY = 824
MARGIN = (CANVAS - BODY) // 2
SS = 4  # supersampling factor

BG_TOP = (30, 39, 56)
BG_BOTTOM = (9, 12, 18)
GLOW = (70, 130, 220)
ARROW_LIGHT = (132, 182, 255)
ARROW_DARK = (58, 112, 214)


def squircle(size: int, inset: float = 0.0, exponent: float = 5.0, steps: int = 720):
    """Superellipse points approximating Apple's continuous-corner icon shape."""
    half = size / 2.0
    radius = half - inset
    points = []
    for i in range(steps):
        theta = 2 * math.pi * i / steps
        c, s = math.cos(theta), math.sin(theta)
        x = radius * math.copysign(abs(c) ** (2 / exponent), c)
        y = radius * math.copysign(abs(s) ** (2 / exponent), s)
        points.append((half + x, half + y))
    return points


def body_mask(size: int, inset: float = 0.0) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).polygon(squircle(size, inset), fill=255)
    return mask


def vertical_gradient(size: int, top, bottom) -> Image.Image:
    column = Image.new("RGB", (1, size))
    for y in range(size):
        t = y / (size - 1)
        column.putpixel((0, y), tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)))
    return column.resize((size, size))


def diagonal_gradient(size: int, start, end) -> Image.Image:
    """Top-right (start) to bottom-left (end), matching the arrow's direction."""
    small = 64
    img = Image.new("RGB", (small, small))
    for y in range(small):
        for x in range(small):
            t = ((small - 1 - x) + y) / (2 * (small - 1))
            img.putpixel((x, y), tuple(int(start[i] + (end[i] - start[i]) * t) for i in range(3)))
    return img.resize((size, size), Image.BICUBIC)


def arrow_polygon(size: int):
    """Arrow scaled to ~58% of the body height, optically centred."""
    xs = [p[0] for p in ARROW]
    ys = [p[1] for p in ARROW]
    scale = (0.58 * size) / (max(ys) - min(ys))
    cx = (min(xs) + max(xs)) / 2
    cy = (min(ys) + max(ys)) / 2
    ox = size / 2 - 0.02 * size
    oy = size / 2 + 0.015 * size
    return [((x - cx) * scale + ox, (y - cy) * scale + oy) for x, y in ARROW]


def render_body(size: int) -> Image.Image:
    """The squircle artwork (RGBA, size x size) before the drop shadow."""
    mask = body_mask(size)
    art = vertical_gradient(size, BG_TOP, BG_BOTTOM).convert("RGBA")

    # Brand glow behind the arrow.
    glow = Image.new("L", (size, size), 0)
    gd = ImageDraw.Draw(glow)
    r = size * 0.34
    gd.ellipse((size / 2 - r, size / 2 - r * 0.9, size / 2 + r, size / 2 + r * 1.1), fill=110)
    glow = glow.filter(ImageFilter.GaussianBlur(size * 0.12))
    art = Image.composite(Image.new("RGBA", (size, size), (*GLOW, 255)), art, glow)

    # Arrow: soft blue halo, then the gradient-filled glyph.
    poly = arrow_polygon(size)
    halo = Image.new("L", (size, size), 0)
    ImageDraw.Draw(halo).polygon(poly, fill=150)
    halo = halo.filter(ImageFilter.GaussianBlur(size * 0.03))
    art = Image.composite(Image.new("RGBA", (size, size), (*ARROW_LIGHT, 255)), art, halo)
    glyph = Image.new("L", (size, size), 0)
    ImageDraw.Draw(glyph).polygon(poly, fill=255)
    art = Image.composite(diagonal_gradient(size, ARROW_LIGHT, ARROW_DARK).convert("RGBA"), art, glyph)

    # Glass rim: a thin inner edge, bright at the top, fading out by mid-height.
    rim_width = max(2, int(size * 0.006))
    rim = ImageChops.subtract(mask, body_mask(size, inset=rim_width))
    fade = Image.linear_gradient("L").resize((size, size)).transpose(Image.FLIP_TOP_BOTTOM)
    fade = fade.point(lambda v: max(0, int((v - 128) * 2 * 0.38)))
    rim = ImageChops.multiply(rim, fade)
    art = Image.composite(Image.new("RGBA", (size, size), (255, 255, 255, 255)), art, rim)

    art.putalpha(mask)
    return art


def render_icon() -> Image.Image:
    size = CANVAS * SS
    body = render_body(BODY * SS)
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))

    shadow_alpha = body.getchannel("A").point(lambda v: int(v * 0.45))
    shadow = Image.new("RGBA", body.size, (0, 0, 0, 255))
    shadow.putalpha(shadow_alpha)
    shadow_layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    shadow_layer.paste(shadow, (MARGIN * SS, MARGIN * SS + 12 * SS))
    shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(14 * SS))

    canvas.alpha_composite(shadow_layer)
    canvas.alpha_composite(body, (MARGIN * SS, MARGIN * SS))
    return canvas.resize((CANVAS, CANVAS), Image.LANCZOS)


def write_icns(master: Image.Image, destination: Path) -> None:
    if shutil.which("iconutil") is None:
        raise SystemExit("iconutil is required (macOS)")
    with tempfile.TemporaryDirectory() as tmp:
        iconset = Path(tmp) / "AppIcon.iconset"
        iconset.mkdir()
        for points in (16, 32, 128, 256, 512):
            for scale in (1, 2):
                pixels = points * scale
                name = f"icon_{points}x{points}{'@2x' if scale == 2 else ''}.png"
                master.resize((pixels, pixels), Image.LANCZOS).save(iconset / name)
        subprocess.run(["iconutil", "-c", "icns", "-o", str(destination), str(iconset)], check=True)


def main() -> int:
    master = render_icon()
    preview = PROJECT_ROOT / "build" / "macos-icon-1024.png"
    preview.parent.mkdir(parents=True, exist_ok=True)
    master.save(preview)
    write_icns(master, ICNS_PATH)
    print(f"Wrote {ICNS_PATH} (preview: {preview})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
