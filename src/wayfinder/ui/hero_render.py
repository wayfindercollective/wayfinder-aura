#!/usr/bin/env python3
"""Pure-PIL liquid-ribbon waveform renderer for the main-window hero.

NO Tk import — this module is headless-importable and unit-tested standalone
(``tests/test_hero_render.py``). ``wayfinder_main._draw_hero_waveform`` calls
``render_hero_wave`` and blits the returned RGB image into its single persistent
canvas image item.

The visual language is ported from the PyQt6 overlay's ``LiquidWaveRenderer``
(layered translucent sine waves, glow-under-core, edge fade) and tuned in
``scripts/design/hero_mock.py`` — those constants (``FREQ_SCALE``, the idle
breath ``0.26 + 0.09*sin``, the amplitude/brightness morph curves) are
USER-APPROVED and reproduced here verbatim.

Cost model (see plan): per frame = 10 opaque ``draw.line`` polylines (sampled
every 6px, NO ``joint="curve"`` — Codex benchmarked that at ~8.3ms/frame) + one
``Image.composite`` through a baked edge-fade L mask + one 1px top-highlight
line. Alpha is pre-blended over the opaque bg into solid RGBs (no per-segment
RGBA compositing, no per-frame blur/supersample). All per-(w,h,color,bg)
invariants are memoized in a bounded module-level cache. Target <=1.5ms/frame.
"""
from __future__ import annotations

import math
import random

from PIL import Image, ImageDraw

# --- palette defaults (mirror of wayfinder_main.COLORS, only what we need) ----
BG_CARD = (0x1E, 0x1E, 0x1F)      # #1E1E1F bento tile (hero canvas bg)
STATE_READY = (0x5B, 0x8F, 0xD4)  # #5B8FD4 soft brand blue
WHITE = (255, 255, 255)

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
_SAMPLE = 6  # px between polyline points at the reference width
# The approved mock was designed at 640px: waves are computed in that reference
# space and stretched to the real strip width. This keeps BOTH the approved
# proportions (same bump count at any width — per-pixel frequencies would pack
# 3.4x the cycles into a 2180px window) AND a constant point count (~109), so
# render cost does not scale with window width (live-measured: per-pixel
# sampling at 2180px cost ~2.4ms/frame = +5% idle CPU at 30fps).
_REF_W = 640.0
_N_POINTS_MIN = int(_REF_W // _SAMPLE) + 3  # 109 samples (the mock's density)
_N_POINTS_MAX = 240  # cap: wider windows never exceed this (cost ceiling)


def _n_points(w):
    """Sample count: mock density at <=640px, then ~9px on-screen segments up
    to a hard cap — enough to keep high-curvature recording peaks smooth at
    2160+px without letting cost scale unbounded with window width."""
    return max(_N_POINTS_MIN, min(_N_POINTS_MAX, round(w / 9)))

# Bounded per-(w,h,color,bg) invariant cache: edge mask, flat bg, layer/hilite
# colour deltas. ~16 live entries in practice (a handful of state colours across
# one or two canvas widths).
_HERO_CACHE: dict = {}
_HERO_CACHE_MAX = 16


def _clampf(v, lo, hi):
    return lo if v < lo else hi if v > hi else v


def get_hero_caches(w, h, color_rgb, bg_rgb=BG_CARD):
    """Memoized per-(w,h,color,bg) invariants.

    Returns a dict with:
      - ``layer_deltas``: per-layer (color-bg)*alpha *deltas* for glow+core, so a
        frame does ``rgb = bg + delta*brightness`` (linear in brightness — lets
        morph scale layer brightness without re-blending).
      - ``highlight_deltas``: same for the bright center highlight wave.
      - ``mask``: baked edge-fade L mask (255 center -> 0 at edges).
      - ``bg_flat``: an opaque flat bg image reused as the composite backdrop.
    """
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
    if len(_HERO_CACHE) >= _HERO_CACHE_MAX:
        _HERO_CACHE.clear()
    _HERO_CACHE[key] = cache
    return cache


def render_hero_wave(w, h, t, level, morph, state_color_rgb, bg_rgb=BG_CARD, *,
                     caches=None, stardust=False):
    """Liquid-ribbon waveform onto a w x h RGB strip.

    Args:
        w, h: strip pixel size.
        t: animation time (advanced delta-based by the caller).
        level: audio level in [0,1].
        morph: idle<->active morph in [0,1]. 0 = calm dim breath (idle),
            1 = energetic bright (active). Scales amplitude reactivity + layer
            brightness + voice-boost gain.
        state_color_rgb: (r,g,b) ribbon colour for the current app state.
        bg_rgb: opaque canvas background the alpha is pre-blended over.
        caches: optional ``get_hero_caches`` result (else computed/looked up).
        stardust: draw a few drifting sparkle points (placement B; unused by the
            approved header-only design, kept for completeness).

    Returns a ``PIL.Image`` in mode ``RGB``, size ``(w, h)``.
    """
    # NaN/inf guards — one bad value would poison every frame.
    if t != t or t in (float("inf"), float("-inf")):
        t = 0.0
    if level != level or level in (float("inf"), float("-inf")):
        level = 0.0
    if morph != morph or morph in (float("inf"), float("-inf")):
        morph = 0.0
    level = _clampf(level, 0.0, 1.0)
    morph = _clampf(morph, 0.0, 1.0)

    if caches is None:
        caches = get_hero_caches(w, h, state_color_rgb, bg_rgb)

    center_y = h / 2.0
    max_amp = h * 0.42
    top, bottom = 1.0, h - 1.0

    # Amplitude: calm gentle breath at idle (ribbon sits ~1/3 height), then morph
    # + voice open it up while recording. The energy cap is 0.80 (not 1.0): at
    # full drive the summed sines would push the soft limiter below deep into
    # saturation and the peaks plateaued — capping the drive keeps the limiter
    # in its gentle range so high-level peaks stay rounded and flowing. The
    # energetic feel comes from motion + brightness, not from slamming bounds.
    # (Idle is untouched: breath tops out at 0.35, far below the cap.)
    breath = 0.26 + 0.09 * (0.5 + 0.5 * math.sin(t * 0.8))
    amp = max_amp * min(0.80, breath + 0.20 * morph + (level ** 0.6) * 0.62 * morph)
    fs = FREQ_SCALE

    # Soft amplitude limiter: at high level the summed sines (worst case 1.6*amp
    # centerline) used to slam the strip bounds and flatten — the ribbon read as
    # "confined". Budget the usable half-height for the thickest stroke (glow
    # width thick+4 = 10 -> radius 5) plus a 3px air margin, pass anything above
    # a knee through tanh so peaks asymptotically approach (never touch) the
    # bounds. Below the knee it's identity — the approved idle look (max
    # displacement ~15px at h=64) is untouched.
    max_stroke_rad = (max(cfg[3] for cfg in WAVE_CONFIGS) + 4) / 2.0
    a_max = max(4.0, h / 2.0 - max_stroke_rad - 3.0)
    knee = a_max * 0.7
    soft_range = a_max - knee

    def soft_limit(dy):
        ad = dy if dy >= 0.0 else -dy
        if ad <= knee:
            return dy
        lim = knee + soft_range * math.tanh((ad - knee) / soft_range)
        return lim if dy >= 0.0 else -lim

    # Layer brightness: dim & calm at idle, full & bright active.
    brightness = 0.55 + 0.45 * morph
    hi_brightness = 0.40 + 0.60 * morph

    br, bg_, bb = bg_rgb
    layer_deltas = caches["layer_deltas"]

    img = caches["bg_flat"].copy()
    draw = ImageDraw.Draw(img)

    # Fixed point count in mock-reference space: x is the on-image pixel, u is
    # the wave-space coordinate the sines are evaluated at.
    # Idle (low morph) is low-amplitude — segments can't facet — and runs at
    # 30fps, so it keeps the cheap mock density; the extra points only get paid
    # for by the energetic 15fps active ribbon where curvature is visible.
    n = _N_POINTS_MIN if morph < 0.3 else _n_points(w)
    xus = [(w * i / (n - 1), _REF_W * i / (n - 1)) for i in range(n)]

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
        for x, u in xus:
            dy = amp * math.sin(f * u + t + phase)
            dy += (amp * 0.4) * math.sin(f * 2.3 * u + t * 1.6 + phase)
            dy += (amp * 0.2) * math.sin(f * 3.7 * u + t * 2.1 + phase * 0.5)
            pts.append((x, _clampf(center_y + soft_limit(dy), top, bottom)))
        draw.line(pts, fill=blend(deltas["glow"], brightness), width=thick + 4)
        draw.line(pts, fill=blend(deltas["core"], brightness), width=thick)

    # --- bright center highlight wave ---------------------------------------
    hdelt = caches["highlight_deltas"]
    hpts = []
    for x, u in xus:
        dy = amp * math.sin(0.13 * fs * u + t * 1.4)
        dy += (amp * 0.5) * math.sin(0.26 * fs * u + t * 2.0 + 0.8)
        hpts.append((x, _clampf(center_y + soft_limit(dy), top, bottom)))
    draw.line(hpts, fill=blend(hdelt["glow"], hi_brightness), width=6)
    draw.line(hpts, fill=blend(hdelt["core"], hi_brightness * 0.95), width=2)

    # --- optional drifting stardust (placement B) ---------------------------
    if stardust:
        _draw_wave_stardust(draw, w, h, t, state_color_rgb, bg_rgb)

    # --- edge fade: composite wave over flat bg through baked mask -----------
    final = Image.composite(img, caches["bg_flat"], caches["mask"])

    # --- 1px inner top-edge highlight (bg +6% toward white), post-composite --
    # Intentionally NOT background (kept from hero-depth commit ae3da97) — the
    # edge-fade test asserts every column EXCEPT row 0.
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
        band = by
        y = int((band * 0.28 + (0.0 if i % 2 else 0.72) * 1.0) * h)
        y = int(_clampf(y, 2, h - 3))
        twinkle = 0.5 + 0.5 * math.sin(t * 1.3 + i * 2.1)
        alpha = 0.4 * twinkle
        pal = [color, WHITE, STATE_READY][i % 3]
        col = tuple(int(bg[j] + (pal[j] - bg[j]) * alpha) for j in range(3))
        sz = 1 if i % 3 else 2
        draw.ellipse([x - sz, y - sz, x + sz, y + sz], fill=col)
