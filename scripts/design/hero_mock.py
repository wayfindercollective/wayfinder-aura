#!/usr/bin/env python3
"""
Hero design-mock renderer (Step 0 of the hero refactor).

Pure-PIL prototype of the future ``src/wayfinder/ui/hero_render.py`` plus the
quiet-minimal mic button and the cosmic sparkle logo signature. NO Tk. Runs on
the flatpak-host python (PIL + numpy live there):

    flatpak-spawn --host sh -c 'cd <repo> && python3 scripts/design/hero_mock.py'

Flags:
    --bench   time 200 renders @640x64 and print mean ms/frame
    --out DIR write PNGs + gallery index.html to DIR (default: ./hero_mocks)

Everything is deterministic (fixed t samples, seeded sparkle RNG). This file is
a design artifact — it does not import from or modify the running app.
"""
from __future__ import annotations

import argparse
import math
import os
import random
import sys
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

# --- make wayfinder.ui.icons importable (pure-PIL tint_icon) -----------------
_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "src"))
from wayfinder.ui.icons import tint_icon  # noqa: E402

# --- palette (mirror of wayfinder_main.COLORS, only what we need) ------------
BG_BASE = (0x0D, 0x11, 0x17)      # #0D1117 deep ink page
BG_CARD = (0x1E, 0x1E, 0x1F)      # #1E1E1F bento tile
BORDER_RIM = (0x1E, 0x1B, 0x26)   # #1E1B26 10% violet rim
ACCENT = (0xA7, 0x8B, 0xFA)       # #A78BFA soft violet
STATE_READY = (0x7B, 0x8B, 0xD9)  # #7B8BD9 indigo
STATE_REC = (0xE8, 0x70, 0x7F)    # #E8707F rose
STATE_PROC = (0xE5, 0xAC, 0x2A)   # #E5AC2A gold
STATE_TYPE = (0x5D, 0xD4, 0xA8)   # #5DD4A8 mint
TEXT_PRIMARY = (0xE8, 0xE8, 0xE8)
TEXT_SECONDARY = (0x8B, 0x8B, 0x8F)
TEXT_MUTED = (0x5C, 0x5C, 0x60)
WHITE = (255, 255, 255)


# ============================================================================
#  1. LIQUID-RIBBON WAVEFORM RENDERER
# ============================================================================
# Wave layers ported from overlay.LiquidWaveRenderer wave_configs:
#   (frequency, phase_offset, base_alpha, thickness)
WAVE_CONFIGS = [
    (0.07, 0.0, 0.15, 6),   # slow background wave
    (0.11, 1.0, 0.25, 5),   # medium
    (0.16, 2.2, 0.40, 4),   # faster
    (0.22, 0.7, 0.55, 3),   # quick
]
# The overlay freqs were tuned for its narrow strip; the hero strip is ~600px
# wide, so the same per-pixel freqs pack ~20 cycles across and read as spaghetti.
# Scale them down for a few broad, overlapping "liquid" waves instead.
FREQ_SCALE = 0.32
_SAMPLE = 6  # px between polyline points


def _clampf(v, lo, hi):
    return lo if v < lo else hi if v > hi else v


def get_hero_caches(w, h, color_rgb, bg_rgb):
    """Memoized per-(w,h,color,bg) invariants: edge-fade L mask, flat bg image,
    and per-layer/​highlight colour *deltas* (color-bg)*base_alpha so a frame can
    do rgb = bg + delta*brightness (linear in brightness — lets morph scale
    layer brightness without re-blending)."""
    key = (w, h, color_rgb, bg_rgb)
    cache = _HERO_CACHE.get(key)
    if cache is not None:
        return cache

    br, bg_, bb = bg_rgb
    cr, cg, cb = color_rgb

    def delta(alpha):
        return ((cr - br) * alpha, (cg - bg_) * alpha, (cb - bb) * alpha)

    layer_deltas = []
    for _freq, _phase, base_alpha, _thick in WAVE_CONFIGS:
        layer_deltas.append({
            "glow": delta(base_alpha * 0.3),
            "core": delta(base_alpha),
        })
    highlight_deltas = {"glow": delta(0.4), "core": delta(1.0)}

    # Edge-fade L mask: 255 in the middle, ramp to 0 across a generous zone.
    fade_zone = max(24, w * 0.06)
    mask = Image.new("L", (w, h), 0)
    mdraw = ImageDraw.Draw(mask)
    for x in range(w):
        d = min(x, w - 1 - x)
        val = 255 if d >= fade_zone else int(255 * (d / fade_zone))
        mdraw.line([(x, 0), (x, h - 1)], fill=val)

    bg_flat = Image.new("RGB", (w, h), bg_rgb)

    cache = {
        "layer_deltas": layer_deltas,
        "highlight_deltas": highlight_deltas,
        "mask": mask,
        "bg_flat": bg_flat,
    }
    _HERO_CACHE[key] = cache
    return cache


_HERO_CACHE: dict = {}


def render_hero_wave(w, h, t, level, morph, state_color_rgb, bg_rgb=BG_CARD,
                     caches=None, stardust=False):
    """Liquid-ribbon waveform onto a w×h RGB strip.

    morph in [0,1]: 0 = calm dim breath (idle), 1 = energetic bright (active).
    Scales amplitude reactivity + layer brightness. level in [0,1] audio.
    """
    # NaN/inf guards — one bad value would poison every frame.
    if t != t or t in (float("inf"), float("-inf")):
        t = 0.0
    if level != level or level in (float("inf"), float("-inf")):
        level = 0.0
    level = _clampf(level, 0.0, 1.0)
    morph = _clampf(morph, 0.0, 1.0)

    if caches is None:
        caches = get_hero_caches(w, h, state_color_rgb, bg_rgb)

    center_y = h / 2.0
    max_amp = h * 0.42
    top, bottom = 1.0, h - 1.0

    # Amplitude: calm gentle breath at idle (ribbon sits ~1/3 height), then morph
    # + voice open it up to full while recording.
    breath = 0.26 + 0.09 * (0.5 + 0.5 * math.sin(t * 0.8))
    amp = max_amp * min(1.0, breath + 0.20 * morph + (level ** 0.6) * 0.62 * morph)
    fs = FREQ_SCALE

    # Layer brightness: dim & calm at idle, full & bright active.
    brightness = 0.55 + 0.45 * morph
    hi_brightness = 0.40 + 0.60 * morph

    br, bg_, bb = bg_rgb
    layer_deltas = caches["layer_deltas"]

    img = caches["bg_flat"].copy()
    draw = ImageDraw.Draw(img)

    xs = list(range(0, w + 1, _SAMPLE))
    if xs[-1] != w:
        xs.append(w)

    def blend(delta, k):
        dr, dg, db = delta
        return (
            int(_clampf(br + dr * k, 0, 255)),
            int(_clampf(bg_ + dg * k, 0, 255)),
            int(_clampf(bb + db * k, 0, 255)),
        )

    # --- 4 stacked layers: glow (wide, dim) UNDER core (narrow) --------------
    for (freq, phase, _a, thick), deltas in zip(WAVE_CONFIGS, layer_deltas):
        f = freq * fs
        pts = []
        for x in xs:
            y = center_y + amp * math.sin(f * x + t + phase)
            y += (amp * 0.4) * math.sin(f * 2.3 * x + t * 1.6 + phase)
            y += (amp * 0.2) * math.sin(f * 3.7 * x + t * 2.1 + phase * 0.5)
            pts.append((x, _clampf(y, top, bottom)))
        draw.line(pts, fill=blend(deltas["glow"], brightness), width=thick + 4)
        draw.line(pts, fill=blend(deltas["core"], brightness), width=thick)

    # --- bright center highlight wave ---------------------------------------
    hdelt = caches["highlight_deltas"]
    hpts = []
    for x in xs:
        y = center_y + amp * math.sin(0.13 * fs * x + t * 1.4)
        y += (amp * 0.5) * math.sin(0.26 * fs * x + t * 2.0 + 0.8)
        hpts.append((x, _clampf(y, top, bottom)))
    draw.line(hpts, fill=blend(hdelt["glow"], hi_brightness), width=6)
    draw.line(hpts, fill=blend(hdelt["core"], hi_brightness * 0.95), width=2)

    # --- optional drifting stardust (placement B) ---------------------------
    if stardust:
        _draw_wave_stardust(draw, w, h, t, state_color_rgb, bg_rgb)

    # --- edge fade: composite wave over flat bg through baked mask -----------
    final = Image.composite(img, caches["bg_flat"], caches["mask"])

    # --- 1px inner top-edge highlight (bg_card +6% toward white), post-comp --
    fdraw = ImageDraw.Draw(final)
    hi = tuple(int(c + (255 - c) * 0.06) for c in bg_rgb)
    fdraw.line([(0, 0), (w - 1, 0)], fill=hi)

    return final


def _draw_wave_stardust(draw, w, h, t, color, bg):
    """6-10 tiny drifting sparkle points above/below the ribbon, alpha<=40%,
    deterministic from t. Drawn as pre-blended opaque dots (cheap, no RGBA)."""
    rng = random.Random(1234)
    n = 8
    for i in range(n):
        bx = rng.random()
        by = rng.random()
        drift = 0.04 * math.sin(t * 0.6 + i * 1.7)
        x = int((bx + drift) % 1.0 * (w - 4)) + 2
        # keep them toward the top/bottom bands, away from the busy center
        band = by
        y = int((band * 0.28 + (0.0 if i % 2 else 0.72) * 1.0) * h)
        y = _clampf(y, 2, h - 3)
        twinkle = 0.5 + 0.5 * math.sin(t * 1.3 + i * 2.1)
        alpha = 0.4 * twinkle
        pal = [color, WHITE, STATE_READY][i % 3]
        col = tuple(int(bg[j] + (pal[j] - bg[j]) * alpha) for j in range(3))
        sz = 1 if i % 3 else 2
        draw.ellipse([x - sz, y - sz, x + sz, y + sz], fill=col)


# ============================================================================
#  2. QUIET-MINIMAL MIC BUTTON
# ============================================================================
def render_mic_button(color_rgb, size=80, ss=4, *, state="idle", pulse=0.0,
                      bg_rgb=BG_CARD):
    """Quiet-minimal mic button. Renders at ``size*ss`` then LANCZOS to size*2
    for a crisp mock. states: idle | hover | stop-square | icon-swap | pulse |
    processing."""
    S = size * ss
    k = ss  # design px -> supersample px
    img = Image.new("RGBA", (S, S), bg_rgb + (255,))
    draw = ImageDraw.Draw(img)

    cx = cy = S / 2
    r = 24 * k  # radius in SS px
    stroke = 2 * k

    def brighten(c, amt):
        return tuple(int(v + (255 - v) * amt) for v in c)

    icon_px = int(30 * k)  # glyph box

    def paste_glyph(hex_color):
        glyph = tint_icon("mic", hex_color, icon_px)
        img.alpha_composite(glyph, (int(cx - icon_px / 2), int(cy - icon_px / 2)))

    def hexof(c):
        return "#%02X%02X%02X" % c

    if state in ("idle", "hover", "processing"):
        oc = color_rgb
        glyph_color = TEXT_SECONDARY
        if state == "hover":
            oc = brighten(color_rgb, 0.15)
            glyph_color = brighten(TEXT_SECONDARY, 0.30)
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=oc, width=stroke)
        paste_glyph(hexof(glyph_color))

    elif state == "stop-square":
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=color_rgb)
        sq = 9 * k
        rad = 3 * k
        draw.rounded_rectangle([cx - sq, cy - sq, cx + sq, cy + sq],
                               radius=rad, fill=BG_BASE)

    elif state == "icon-swap":
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=color_rgb)
        paste_glyph(hexof(BG_BASE))

    elif state == "pulse":
        # expanding low-alpha ring behind the filled circle
        ring = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        rdraw = ImageDraw.Draw(ring)
        rr = r + int((6 + 10 * pulse) * k)
        ring_alpha = int(90 * (1.0 - pulse))
        rdraw.ellipse([cx - rr, cy - rr, cx + rr, cy + rr],
                      outline=color_rgb + (ring_alpha,), width=2 * k)
        img.alpha_composite(ring)
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=color_rgb)
        sq = 9 * k
        rad = 3 * k
        draw.rounded_rectangle([cx - sq, cy - sq, cx + sq, cy + sq],
                               radius=rad, fill=BG_BASE)

    return img.resize((size * 2, size * 2), Image.LANCZOS)


# ============================================================================
#  3. COSMIC SPARKLE LOGO SIGNATURE
# ============================================================================
def _draw_sparkle(draw, x, y, size, color, alpha, ss=4):
    """A 4-point star sparkle + soft core, drawn RGBA. width scaled to the
    supersample factor so it survives the LANCZOS downscale."""
    a = int(_clampf(alpha, 0, 1) * 255)
    col = color + (a,)
    lw = max(1, round(ss * 0.5))
    # cross spikes (long axes) + short diagonals for a 4-point-star read
    draw.line([(x - size, y), (x + size, y)], fill=col, width=lw)
    draw.line([(x, y - size), (x, y + size)], fill=col, width=lw)
    # bright core dot
    cr = max(1.0, ss * 0.5)
    core = color + (min(255, int(a * 1.25)),)
    draw.ellipse([x - cr, y - cr, x + cr, y + cr], fill=core)


def render_header_logo(intensity="subtle", logo_size=28, ss=4, strip_w=180,
                       strip_h=56):
    """Header logo with a cosmic stardust TRAIL behind/below-left, as if the
    arrow flew through space. intensity: whisper | subtle | visible | none."""
    S = ss
    W, H = strip_w * S, strip_h * S
    img = Image.new("RGBA", (W, H), BG_BASE + (255,))

    logo = Image.open(_REPO / "assets" / "icon.png").convert("RGBA")
    ls = logo_size * S
    logo = logo.resize((ls, ls), Image.LANCZOS)
    # Give the trail open space to the left; logo sits right-of-center.
    lx = int((strip_w - logo_size - 14) * S)
    ly = int((strip_h - logo_size) / 2 * S)
    # arrow tip is roughly upper-right of the icon; trail streams to lower-left.
    tip_x = lx + int(ls * 0.32)
    tip_y = ly + int(ls * 0.44)

    if intensity != "none":
        counts = {"whisper": 9, "subtle": 13, "visible": 16}
        base_alpha = {"whisper": 0.42, "subtle": 0.68, "visible": 0.95}[intensity]
        n = counts[intensity]
        star = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        sdraw = ImageDraw.Draw(star)
        rng = random.Random(7)
        palette = [ACCENT, WHITE, STATE_READY]
        for i in range(n):
            # spread down-left of the tip along a gentle arc into open space
            dist = (i + 1) / n
            spread = rng.uniform(-0.30, 0.30)
            sx = tip_x - dist * ls * 2.2 + spread * ls * 0.4
            sy = tip_y + dist * ls * 0.7 + spread * ls * 0.55
            fade = (1.0 - dist) ** 1.15
            a = base_alpha * (0.30 + 0.70 * fade) * rng.uniform(0.7, 1.0)
            sz = (1.4 + rng.random() * 2.8) * S
            col = palette[i % 3]
            _draw_sparkle(sdraw, sx, sy, sz, col, a, ss=S)
            # occasional motion streak toward the tip
            if i % 3 == 0:
                sa = int(a * 130)
                sdraw.line([(sx, sy), (sx + ls * 0.10, sy - ls * 0.05)],
                           fill=col + (sa,), width=max(1, round(S * 0.4)))
        # very gentle bloom so it reads as light, not confetti
        star = star.filter(ImageFilter.GaussianBlur(0.35 * S))
        img.alpha_composite(star)

    img.alpha_composite(logo, (lx, ly))
    return img.resize((strip_w * 2, strip_h * 2), Image.LANCZOS)


# ============================================================================
#  4. CURRENT-LOOK BARS (for before/after comparison)
# ============================================================================
def render_current_bars(w, h, t, level, color_rgb, bg_rgb=BG_CARD):
    """Replicate today's flat 3px bars / 2px gap waveform (simplified)."""
    center_y = h // 2
    max_amp = (h // 2) - 2
    bw, gap = 3, 2
    num = w // (bw + gap)
    r, g, b = color_rgb
    br, bgc, bb = bg_rgb
    base_breath = 0.6 + 0.3 * (0.5 + 0.5 * math.sin(t * 0.3))
    voice = (level ** 0.4) * 2.0
    ampf = min(1.0, base_breath + voice)
    lut = []
    for i in range(10):
        a = 0.5 + 0.05 * i
        lut.append((int(r * a + br * (1 - a)), int(g * a + bgc * (1 - a)),
                    int(b * a + bb * (1 - a))))
    img = Image.new("RGB", (w, h), bg_rgb)
    draw = ImageDraw.Draw(img)
    for i in range(num):
        x = i * (bw + gap) + gap // 2
        ph = t + i * 0.2
        wv = (math.sin(ph) * 0.4 + math.sin(ph * 1.7 + 0.5) * 0.35
              + math.sin(ph * 0.6 + 1.0) * 0.25)
        aw = abs(wv)
        bh = max(8, (0.3 + aw * 0.7) * max_amp * ampf)
        col = lut[min(9, int(aw * 10))]
        draw.rectangle([x, int(center_y - bh), x + bw, int(center_y + bh)], fill=col)
    hi = tuple(int(c + (255 - c) * 0.06) for c in bg_rgb)
    draw.line([(0, 0), (w - 1, 0)], fill=hi)
    return img


# ============================================================================
#  FONTS
# ============================================================================
_FONT_CANDIDATES_BOLD = [
    "Inter-Bold.ttf", "Inter-SemiBold.ttf",
    "/home/bazzite/.fonts/Roboto-Medium.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]
_FONT_CANDIDATES_REG = [
    "Inter-Regular.ttf",
    "/home/bazzite/.fonts/Roboto-Regular.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]
FONT_USED = "default"


def _load_font(candidates, size):
    global FONT_USED
    for c in candidates:
        try:
            f = ImageFont.truetype(c, size)
            FONT_USED = os.path.basename(c)
            return f
        except Exception:
            continue
    return ImageFont.load_default()


# ============================================================================
#  5. COMPOSITE HERO CARDS
# ============================================================================
SC = 2  # supersample the whole card 2x for crisp text


def _rounded_card(page_w, page_h):
    img = Image.new("RGB", (page_w * SC, page_h * SC), BG_BASE)
    draw = ImageDraw.Draw(img)
    inset = 10 * SC
    rad = 24 * SC
    draw.rounded_rectangle(
        [inset, inset, page_w * SC - inset, page_h * SC - inset],
        radius=rad, fill=BG_CARD, outline=BORDER_RIM, width=1 * SC)
    return img, draw, inset


def render_hero_card(state_label, hotkey_text, color_rgb, *, morph, level,
                     mic_state, stardust=False, current=False,
                     page_w=660, page_h=230):
    img, draw, inset = _rounded_card(page_w, page_h)

    pad = 20 * SC
    wave_w = page_w * SC - 2 * inset - 2 * pad
    wave_h = 64 * SC
    wave_x = inset + pad
    wave_y = inset + 16 * SC

    t = 3.7  # fixed deterministic sample
    if current:
        wave = render_current_bars(wave_w, wave_h, t, level, color_rgb)
    else:
        wave = render_hero_wave(wave_w, wave_h, t, level, morph, color_rgb,
                                stardust=stardust)
    img.paste(wave, (wave_x, wave_y))

    # mic button, centered below the wave
    mic_logical = 72
    mic = render_mic_button(color_rgb, state=mic_state)  # size*2 px
    mic = mic.resize((mic_logical * SC, mic_logical * SC), Image.LANCZOS)
    mic_x = (page_w * SC - mic_logical * SC) // 2
    mic_y = wave_y + wave_h + 14 * SC
    img.paste(mic, (mic_x, mic_y), mic)

    # status text
    bold = _load_font(_FONT_CANDIDATES_BOLD, 15 * SC)
    reg = _load_font(_FONT_CANDIDATES_REG, 12 * SC)
    ty = mic_y + mic_logical * SC + 8 * SC
    _centered_text(draw, page_w * SC // 2, ty, state_label, bold, color_rgb)
    _centered_text(draw, page_w * SC // 2, ty + 22 * SC, hotkey_text, reg,
                   TEXT_SECONDARY)

    return img.resize((page_w, page_h), Image.LANCZOS)


def _centered_text(draw, cx, y, text, font, color):
    bbox = draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    draw.text((cx - tw / 2, y), text, font=font, fill=color)


# ============================================================================
#  STRIPS (mic variants, sparkle variants)
# ============================================================================
def _label(draw, x, y, text, font, color=TEXT_SECONDARY):
    draw.text((x, y), text, font=font, fill=color)


def render_mic_strip():
    variants = [
        ("idle", STATE_READY, "idle"),
        ("hover", STATE_READY, "hover"),
        ("stop-square", STATE_REC, "stop-square"),
        ("icon-swap", STATE_REC, "icon-swap"),
        ("pulse", STATE_REC, "pulse"),
        ("processing", STATE_PROC, "processing"),
    ]
    cell = 120
    labelh = 26
    W = cell * len(variants)
    H = cell + labelh
    img = Image.new("RGB", (W * SC, H * SC), BG_BASE)
    draw = ImageDraw.Draw(img)
    font = _load_font(_FONT_CANDIDATES_REG, 12 * SC)
    mic_logical = 80
    for i, (name, color, st) in enumerate(variants):
        mic = render_mic_button(color, state=st, pulse=0.5)
        mic = mic.resize((mic_logical * SC, mic_logical * SC), Image.LANCZOS)
        ox = i * cell * SC + (cell - mic_logical) // 2 * SC
        oy = 6 * SC
        img.paste(mic, (ox, oy), mic)
        bbox = draw.textbbox((0, 0), name, font=font)
        tw = bbox[2] - bbox[0]
        _label(draw, i * cell * SC + (cell * SC - tw) // 2,
               (cell + 4) * SC, name, font)
    return img.resize((W, H), Image.LANCZOS)


def render_sparkle_strip():
    variants = [
        ("whisper", "A · whisper"),
        ("subtle", "A · subtle"),
        ("visible", "A · visible"),
        ("none", "C · none"),
    ]
    sw, sh = 180, 56
    labelh = 24
    W = sw * len(variants)
    H = sh + labelh
    img = Image.new("RGB", (W * SC, H * SC), BG_BASE)
    draw = ImageDraw.Draw(img)
    font = _load_font(_FONT_CANDIDATES_REG, 12 * SC)
    for i, (inten, label) in enumerate(variants):
        strip = render_header_logo(intensity=inten, strip_w=sw, strip_h=sh)
        strip = strip.resize((sw * SC, sh * SC), Image.LANCZOS)
        img.paste(strip, (i * sw * SC, 0))
        _label(draw, i * sw * SC + 10 * SC, (sh + 4) * SC, label, font)
    return img.resize((W, H), Image.LANCZOS)


# ============================================================================
#  BENCH
# ============================================================================
def bench():
    w, h = 640, 64
    caches = get_hero_caches(w, h, STATE_REC, BG_CARD)
    # warmup
    for _ in range(20):
        render_hero_wave(w, h, 1.0, 0.5, 1.0, STATE_REC, caches=caches)
    n = 200
    t0 = time.perf_counter()
    for i in range(n):
        render_hero_wave(w, h, i * 0.05, 0.5, 1.0, STATE_REC, caches=caches)
    dt = time.perf_counter() - t0
    ms = dt / n * 1000
    print(f"bench: {ms:.3f} ms/frame  ({n} renders @ {w}x{h})")
    return ms


# ============================================================================
#  GALLERY
# ============================================================================
def _b64(img):
    import base64
    import io
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def build_gallery(out: Path, bench_ms):
    out.mkdir(parents=True, exist_ok=True)
    imgs = {}

    imgs["card_idle"] = render_hero_card(
        "Ready to Record", "Press F3 to toggle", STATE_READY,
        morph=0.0, level=0.0, mic_state="idle")
    imgs["card_rec_low"] = render_hero_card(
        "Listening...", "Press F3 to stop", STATE_REC,
        morph=1.0, level=0.25, mic_state="stop-square")
    imgs["card_rec_high"] = render_hero_card(
        "Listening...", "Press F3 to stop", STATE_REC,
        morph=1.0, level=0.8, mic_state="stop-square")
    imgs["card_proc"] = render_hero_card(
        "Processing...", "Cleaning up your words", STATE_PROC,
        morph=0.6, level=0.0, mic_state="processing")
    imgs["card_paste"] = render_hero_card(
        "Typing...", "Injecting text", STATE_TYPE,
        morph=0.5, level=0.0, mic_state="idle")
    imgs["card_idle_stardust"] = render_hero_card(
        "Ready to Record", "Press F3 to toggle", STATE_READY,
        morph=0.0, level=0.0, mic_state="idle", stardust=True)
    imgs["card_current"] = render_hero_card(
        "Ready to Record", "Press F3 to toggle", STATE_READY,
        morph=0.0, level=0.3, mic_state="idle", current=True)

    imgs["mic_strip"] = render_mic_strip()
    imgs["sparkle_strip"] = render_sparkle_strip()

    for name, im in imgs.items():
        im.save(out / f"{name}.png")

    def fig(key, cap):
        return (f'<figure><img src="{_b64(imgs[key])}" alt="{key}">'
                f'<figcaption>{cap}</figcaption></figure>')

    html = f"""<title>Wayfinder Hero Mocks</title>
<style>
  .gallery {{ font-family: system-ui, sans-serif; background:#0D1117;
    color:#E8E8E8; padding:32px; max-width:1000px; margin:0 auto; }}
  .gallery h1 {{ font-weight:700; font-size:26px; margin-bottom:4px; }}
  .gallery .sub {{ color:#8B8B8F; margin-bottom:28px; }}
  .gallery h2 {{ font-size:18px; margin:36px 0 6px; color:#A78BFA; }}
  .gallery p.note {{ color:#8B8B8F; font-size:14px; margin:0 0 16px; }}
  .gallery figure {{ margin:0 0 20px; }}
  .gallery img {{ max-width:100%; border-radius:10px; display:block; }}
  .gallery figcaption {{ color:#8B8B8F; font-size:13px; margin-top:6px; }}
  .bench {{ display:inline-block; background:#1E1E1F; border:1px solid #1E1B26;
    border-radius:8px; padding:6px 12px; color:#5DD4A8; font-family:monospace; }}
</style>
<div class="gallery">
<h1>Wayfinder Hero — "$100 premium" mocks</h1>
<div class="sub">Liquid-ribbon waveform · quiet-minimal mic · cosmic logo signature.
Pure-PIL prototypes of the future <code>hero_render.py</code>.</div>
<span class="bench">renderer bench: {bench_ms:.2f} ms/frame @640×64 (target ≤5)</span>

<h2>Waveform — before / after + states</h2>
<p class="note">The hybrid: one layered ribbon that breathes calmly at idle and
gets bright &amp; reactive while recording (single morph, no pop).</p>
{fig("card_current", "BEFORE — today's flat 3px bars (for comparison)")}
{fig("card_idle", "AFTER · IDLE — calm dim breath, indigo, morph 0")}
{fig("card_rec_low", "RECORDING · low level — rose, morph 1, level 0.25")}
{fig("card_rec_high", "RECORDING · high level — level 0.8, energetic")}
{fig("card_proc", "PROCESSING — gold")}
{fig("card_paste", "PASTING — mint")}

<h2>Cosmic sparkles — placement B (in the waveform)</h2>
<p class="note">Drifting stardust above/below the idle ribbon, alpha ≤ 40%,
deterministic.</p>
{fig("card_idle_stardust", "IDLE + waveform stardust (variant B)")}

<h2>Mic button variants</h2>
<p class="note">Quiet-minimal: thin outline + tinted Lucide mic at rest; fills
with state colour while recording. Pick stop-square vs icon-swap for the active
affordance.</p>
{fig("mic_strip", "idle · hover · stop-square · icon-swap · pulse · processing")}

<h2>Cosmic sparkles — placement A (header logo) 3 intensities / none</h2>
<p class="note">The arrow logo "in space" with a subtle stardust trail streaming
behind it. Whisper → subtle → visible, plus the no-sparkle control.</p>
{fig("sparkle_strip", "A·whisper · A·subtle · A·visible · C·none")}
</div>
"""
    (out / "index.html").write_text(html)
    return out / "index.html"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bench", action="store_true")
    ap.add_argument("--out", default=str(_REPO / "hero_mocks"))
    args = ap.parse_args()

    if args.bench:
        bench()
        return

    ms = bench()
    idx = build_gallery(Path(args.out), ms)
    print(f"font: {FONT_USED}")
    print(f"gallery: {idx}")


if __name__ == "__main__":
    main()
