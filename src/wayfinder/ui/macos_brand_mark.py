"""The header brand mark on macOS, drawn by Core Animation at Retina resolution.

Aqua Tk draws PhotoImages at one pixel per point and refuses to draw them on
the transparent glass, so the Tk header logo was a soft 1x bitmap on an opaque
chip. Here the same mark (arrow + stardust trail) is rendered for the window's
backing scale and handed to a CALayer over a transparent Tk placeholder: crisp
on Retina, sits directly on the glass, static contents (no timers, no redraws).
"""

from __future__ import annotations

import math
import random
import sys

# Logical footprint in points: trail space on the left, the arrow on the right.
MARK_WIDTH = 58
MARK_HEIGHT = 34
_ARROW_HEIGHT = 21.0   # points, the arrow's own height (tip to tail point)
_SUPERSAMPLE = 4


def _rgb(hex_color: str) -> tuple[int, int, int]:
    value = hex_color.lstrip("#")
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def _mix(a, b, t):
    return tuple(int(round(x + (y - x) * t)) for x, y in zip(a, b))


def render_brand_mark(icon_path, *, scale: float = 2.0, is_ultra: bool = False,
                      accent: str = "#4682DC", gold: str = "#E5AC2A"):
    """The mark as an RGBA image of MARK_WIDTH x MARK_HEIGHT points at ``scale``.

    Pure PIL, deterministic (seeded), no Tk/AppKit - usable in tests.
    """
    import numpy as np
    from PIL import Image, ImageDraw, ImageFilter

    scale = max(1.0, float(scale))
    out_w = int(round(MARK_WIDTH * scale))
    out_h = int(round(MARK_HEIGHT * scale))
    ss = scale * _SUPERSAMPLE
    W, H = int(round(MARK_WIDTH * ss)), int(round(MARK_HEIGHT * ss))

    # --- the arrow: brand silhouette, cropped tight and filled with a soft
    # diagonal gradient (lighter at the tip, deeper at the tail).
    icon = Image.open(icon_path).convert("RGBA")
    alpha = icon.split()[3]
    bbox = alpha.getbbox() or (0, 0, icon.width, icon.height)
    alpha = alpha.crop(bbox)
    ah = int(round(_ARROW_HEIGHT * ss))
    aw = int(round(alpha.width * ah / alpha.height))
    alpha = alpha.resize((aw, ah), Image.LANCZOS)
    ax = W - aw - int(round(5 * ss))
    ay = int(round(2.5 * ss))  # high, so the trail has room to fall away

    base = _rgb(accent)
    tip_col = np.array(_mix(base, (255, 255, 255), 0.26), dtype=np.float32)
    tail_col = np.array(_mix(base, (0, 0, 0), 0.14), dtype=np.float32)
    yy, xx = np.mgrid[0:ah, 0:aw].astype(np.float32)
    t = (((aw - 1 - xx) / max(aw - 1, 1)) + (yy / max(ah - 1, 1))) / 2.0
    rgb = tip_col * (1.0 - t[..., None]) + tail_col * t[..., None]
    arrow = Image.fromarray(np.clip(rgb, 0, 255).astype(np.uint8), "RGB").convert("RGBA")
    arrow.putalpha(alpha)

    canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))

    # --- halo: a soft glow of the silhouette (gold for Ultra).
    glow_col = _rgb(gold) if is_ultra else base
    glow_alpha = alpha.point(lambda v: int(v * (0.85 if is_ultra else 0.55)))
    glow = Image.new("RGBA", (W, H), glow_col + (0,))
    glow_mask = Image.new("L", (W, H), 0)
    glow_mask.paste(glow_alpha, (ax, ay))
    glow_mask = glow_mask.filter(ImageFilter.GaussianBlur((3.2 if is_ultra else 2.4) * ss))
    glow.putalpha(glow_mask)
    canvas.alpha_composite(glow)

    # --- stardust trail: a faint tapered streak plus round glowing motes that
    # shrink and fade away from the tail, with two crisp four-point glints.
    tail_x = ax + aw * 0.40
    tail_y = ay + ah * 0.80
    end_x = 3 * ss
    end_y = H - 2.5 * ss

    def along(t, wobble=0.0):
        # Gentle curve from the tail notch down-left into open space.
        x = tail_x + (end_x - tail_x) * t
        y = tail_y + (end_y - tail_y) * (t ** 1.15) + wobble
        return x, y

    streak = Image.new("L", (W, H), 0)
    sd = ImageDraw.Draw(streak)
    segs = 40
    for i in range(segs):
        t0, t1 = i / segs, (i + 1) / segs
        width = max(1, int(round((1.0 - t0) ** 1.6 * 2.2 * ss)))
        a = int(120 * (1.0 - t0) ** 2)
        sd.line([along(t0), along(t1)], fill=a, width=width)
    streak = streak.filter(ImageFilter.GaussianBlur(0.9 * ss))
    streak_rgba = Image.new("RGBA", (W, H), base + (0,))
    streak_rgba.putalpha(streak)
    canvas.alpha_composite(streak_rgba)

    rng = random.Random(11)
    motes = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    md = ImageDraw.Draw(motes)
    white = (255, 255, 255)
    n = 10
    for i in range(n):
        t = min(0.98, 0.20 + 0.78 * (i / (n - 1)) ** 0.85 + rng.uniform(-0.04, 0.04))
        jitter = rng.uniform(-1.0, 1.0) * (1.2 + 6.0 * t) * ss
        x, y = along(t, wobble=jitter)
        fade = (1.0 - t) ** 1.2
        radius = (0.45 + 0.85 * fade) * ss * rng.uniform(0.8, 1.2)
        col = _mix(base, white, 0.35 + 0.45 * rng.random())
        a = int(255 * (0.18 + 0.72 * fade))
        md.ellipse([x - radius, y - radius, x + radius, y + radius], fill=col + (a,))
    halo = motes.filter(ImageFilter.GaussianBlur(1.1 * ss))
    canvas.alpha_composite(halo)
    canvas.alpha_composite(motes)

    glints = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gl = ImageDraw.Draw(glints)
    for t, size, a, wob in ((0.30, 3.4, 240, -2.6), (0.66, 2.3, 150, 2.8)):
        x, y = along(t, wobble=wob * ss)
        s, w = size * ss, 0.42 * ss
        col = _mix(base, white, 0.8) + (a,)
        # Tapered four-point star: two thin diamonds, brighter core.
        gl.polygon([(x - s, y), (x, y - w), (x + s, y), (x, y + w)], fill=col)
        gl.polygon([(x, y - s), (x - w, y), (x, y + s), (x + w, y)], fill=col)
        c = 0.75 * ss
        gl.ellipse([x - c, y - c, x + c, y + c], fill=(255, 255, 255, a))
    canvas.alpha_composite(glints.filter(ImageFilter.GaussianBlur(0.9 * ss)))
    canvas.alpha_composite(glints)

    # --- the arrow on top.
    canvas.alpha_composite(arrow, (ax, ay))
    return canvas.resize((out_w, out_h), Image.LANCZOS)


class MacOSBrandMark:
    """A static CALayer showing the mark over one Tk placeholder widget."""

    def __init__(self, owner, anchor, ns_window, layer):
        self.owner = owner
        self.anchor = anchor
        self.ns_window = ns_window
        self.layer = layer
        self._keep = None

    @classmethod
    def try_create(cls, owner, anchor, icon_path, *, is_ultra, accent, gold):
        """Attach to the mapped app window; ``None`` when unavailable."""
        if sys.platform != "darwin":
            return None
        try:
            from AppKit import NSApplication
            from Quartz import CALayer, kCAGravityResize

            owner.update_idletasks()
            if anchor.winfo_width() <= 1 or anchor.winfo_height() <= 1:
                return None
            title = str(owner.title())
            ns_window = next(
                (w for w in NSApplication.sharedApplication().windows()
                 if str(w.title() or "") == title),
                None,
            )
            if ns_window is None:
                return None
            parent = ns_window.contentView().layer()
            if parent is None:
                return None
            backing = float(ns_window.backingScaleFactor() or 2.0)
            image = render_brand_mark(icon_path, scale=backing, is_ultra=is_ultra,
                                      accent=accent, gold=gold)
            layer = CALayer.layer()
            layer.setName_("WayfinderBrandMark")
            layer.setContentsGravity_(kCAGravityResize)
            layer.setContentsScale_(backing)
            layer.setZPosition_(1000.0)
            mark = cls(owner, anchor, ns_window, layer)
            if not mark._set_contents(image):
                return None
            parent.addSublayer_(layer)
            if not mark.update_geometry():
                mark.close()
                return None
            return mark
        except Exception:
            return None

    def _set_contents(self, image) -> bool:
        try:
            from Foundation import NSData
            from Quartz import (
                CGColorSpaceCreateWithName, CGDataProviderCreateWithCFData,
                CGImageCreate, kCGBitmapByteOrder32Big,
                kCGImageAlphaPremultipliedLast, kCGColorSpaceSRGB,
                kCGRenderingIntentDefault,
            )
            from PIL import Image, ImageChops

            rgba = image.convert("RGBA")
            # Core Animation wants premultiplied alpha.
            r, g, b, a = rgba.split()
            rgba = Image.merge("RGBA", (ImageChops.multiply(r, a), ImageChops.multiply(g, a),
                                        ImageChops.multiply(b, a), a))
            raw = rgba.tobytes("raw", "RGBA")
            data = NSData.dataWithBytes_length_(raw, len(raw))
            contents = CGImageCreate(
                rgba.width, rgba.height, 8, 32, rgba.width * 4,
                CGColorSpaceCreateWithName(kCGColorSpaceSRGB),
                kCGImageAlphaPremultipliedLast | kCGBitmapByteOrder32Big,
                CGDataProviderCreateWithCFData(data), None, False,
                kCGRenderingIntentDefault,
            )
            if contents is None:
                return False
            self.layer.setContents_(contents)
            self._keep = (data, contents)
            return True
        except Exception:
            return False

    def update_geometry(self) -> bool:
        """Align with the placeholder, in content-view points (origin bottom-left)."""
        try:
            from Quartz import CATransaction, CGRectMake

            if not self.anchor.winfo_exists() or not self.anchor.winfo_ismapped():
                self.set_hidden(True)
                return True
            width = MARK_WIDTH
            height = MARK_HEIGHT
            content_height = float(self.ns_window.contentView().bounds().size.height)
            aw, ah = self.anchor.winfo_width(), self.anchor.winfo_height()
            x = float(self.anchor.winfo_rootx() - self.owner.winfo_rootx()) + (aw - width) / 2.0
            top = float(self.anchor.winfo_rooty() - self.owner.winfo_rooty()) + (ah - height) / 2.0
            y = content_height - top - height
            CATransaction.begin()
            CATransaction.setDisableActions_(True)
            self.layer.setFrame_(CGRectMake(math.floor(x), math.floor(y), width, height))
            self.layer.setHidden_(False)
            CATransaction.commit()
            return True
        except Exception:
            return False

    def set_hidden(self, hidden: bool) -> None:
        try:
            from Quartz import CATransaction

            CATransaction.begin()
            CATransaction.setDisableActions_(True)
            self.layer.setHidden_(bool(hidden))
            CATransaction.commit()
        except Exception:
            pass

    def close(self) -> None:
        try:
            self.layer.removeFromSuperlayer()
        except Exception:
            pass
        self._keep = None
