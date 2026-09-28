"""The macOS hero ribbon on Windows: the Metal shader, ported to NumPy.

The Mac draws its hero waves natively. ``macos_hero_metal.py`` holds the
compute shader that shades every pixel from its distance to the wave
polylines (anti-aliased cores, Gaussian glow, edge fade), and
``hero_renderer.m`` hands the calm ribbon to Core Animation. Neither exists
on Windows, and Pillow's aliased polylines (the Linux reference and the Aqua
fallback in ``hero_render``) render with visible facets there.

This module evaluates the same shader, line for line, on NumPy arrays: the
same wave and highlight curves, soft limiter, point counts, segment-distance
coverage, Gaussian glow, stroke order and edge fade. The only difference is
where it runs (CPU, vectorised). Returns a ``PIL.Image`` (RGB) so the Tk
PhotoImage path is unchanged. Windows uses it; it is importable anywhere.
"""

from __future__ import annotations

import math

import numpy as np
from PIL import Image

_FREQS = (0.07, 0.11, 0.16, 0.22)
_PHASES = (0.0, 1.0, 2.2, 0.7)
_ALPHAS = (0.15, 0.25, 0.40, 0.55)
_THICKNESS = (6.0, 5.0, 4.0, 3.0)

_GRID_CACHE: dict = {}
_LUT_PER_PX = 8          # distance resolution of the glow/coverage tables
_LUT_SIZE = 32 * _LUT_PER_PX   # 32 px: every glow has faded out well before


def _grid(w: int, h: int):
    key = (w, h)
    grid = _GRID_CACHE.get(key)
    if grid is None:
        px = np.arange(w, dtype=np.float32)[None, :]            # gid.x as the point
        py = np.arange(h, dtype=np.float32)[:, None]            # gid.y
        fade_zone = max(24.0, w * 0.06)
        edge = np.clip(np.minimum(px, w - 1.0 - px) / fade_zone, 0.0, 1.0).astype(np.float32)
        band = np.ones((h, 1), dtype=np.float32)
        band[:3] = 0.0                                          # no glow in the guard rows
        band[max(0, h - 2):] = 0.0
        grid = (px, py, edge, band)
        if len(_GRID_CACHE) > 8:
            _GRID_CACHE.clear()
        _GRID_CACHE[key] = grid
    return grid


def _soft_limit(dy, height, stroke_scale):
    max_stroke = round(6.0 * stroke_scale)
    glow_extra = round(2.0 * stroke_scale)
    max_stroke_radius = (max_stroke + glow_extra * 2.0) * 0.5
    a_max = max(4.0, height * 0.5 - max_stroke_radius - 3.0)
    knee = a_max * 0.7
    soft_range = a_max - knee
    magnitude = np.abs(dy)
    limited = knee + soft_range * np.tanh((magnitude - knee) / soft_range)
    return np.where(magnitude <= knee, dy, np.copysign(limited, dy))


def _wave_y(x, width, height, t, amp, freq, phase, stroke_scale):
    u = 640.0 * x / max(width - 1.0, 1.0)
    f = freq * 0.32
    dy = amp * np.sin(f * u + t + phase)
    dy += amp * 0.4 * np.sin(f * 2.3 * u + t * 1.6 + phase)
    dy += amp * 0.2 * np.sin(f * 3.7 * u + t * 2.0 + phase * 0.5)
    return np.clip(height * 0.5 + _soft_limit(dy, height, stroke_scale), 1.0, height - 1.0)


def _highlight_y(x, width, height, t, amp, stroke_scale):
    u = 640.0 * x / max(width - 1.0, 1.0)
    dy = amp * np.sin(0.13 * 0.32 * u + t * 1.4)
    dy += amp * 0.5 * np.sin(0.26 * 0.32 * u + t * 2.0 + 0.8)
    return np.clip(height * 0.5 + _soft_limit(dy, height, stroke_scale), 1.0, height - 1.0)


def _polyline_distance_fast(px, py, width, count, ys):
    """The same distance for these gentle curves, at a fraction of the cost:
    vertical distance to the polyline at the pixel's column, scaled by the
    local segment's slope (the perpendicular distance to that segment)."""
    step = width / float(count - 1)
    xs = px[0]
    seg = np.clip((xs / max(step, 0.001)).astype(np.int64), 0, count - 2)
    y0 = ys[seg]
    slope = (ys[seg + 1] - y0) / np.float32(step)
    y_at = y0 + slope * (xs - np.float32(step) * seg.astype(np.float32))
    inv = np.float32(1.0) / np.sqrt(np.float32(1.0) + slope * slope)
    return np.abs(py - y_at[None, :]) * inv[None, :]


def _polyline_distance(px, py, width, count, ys):
    """Per-pixel distance to the polyline through (step*i, ys[i]): the shader's
    wave_distance (nearest of the three segments around the pixel's column)."""
    step = width / float(count - 1)
    center = np.floor(px[0] / max(step, 0.001)).astype(np.int64)
    best = None
    for offset in (-1, 0, 1):
        index = np.clip(center + offset, 0, count - 2)
        ax = (step * index).astype(np.float32)[None, :]
        bx = (step * (index + 1)).astype(np.float32)[None, :]
        ay = ys[index][None, :]
        by = ys[index + 1][None, :]
        abx, aby = bx - ax, by - ay
        denom = np.maximum(abx * abx + aby * aby, 0.0001)
        along = np.clip(((px - ax) * abx + (py - ay) * aby) / denom, 0.0, 1.0)
        dx = px - (ax + along * abx)
        dyy = py - (ay + along * aby)
        dist = np.sqrt(dx * dx + dyy * dyy)
        best = dist if best is None else np.minimum(best, dist)
    return best


def _coverage(distance, width):
    lo = max(0.0, width * 0.5 - 0.5)
    hi = width * 0.5 + 0.5
    x = np.clip((distance - lo) / (hi - lo), 0.0, 1.0)
    return 1.0 - x * x * (3.0 - 2.0 * x)                      # 1 - smoothstep


def render_hero_wave_windows(w, h, t, level, morph, state_color_rgb, bg_rgb, *, stroke_scale=1.0):
    """The Mac hero ribbon for a w x h strip, as a ``PIL.Image`` in mode RGB."""
    def finite(value, default):
        value = float(value)
        return default if value != value or value in (float("inf"), float("-inf")) else value

    t = finite(t, 0.0)
    level = min(1.0, max(0.0, finite(level, 0.0)))
    morph = min(1.0, max(0.0, finite(morph, 0.0)))
    stroke_scale = min(2.5, max(0.7, finite(stroke_scale, 1.0)))
    w, h = max(2, int(w)), max(2, int(h))
    width, height = float(w), float(h)
    px, py, edge_fade, band = _grid(w, h)

    # Python floats (weak scalars) keep every array float32.
    max_amp = height * 0.42
    breath = 0.26 + 0.09 * (0.5 + 0.5 * math.sin(t * 0.8))
    amp = float(max_amp * min(0.80, breath + 0.20 * morph + (level ** 0.6) * 0.62 * morph))
    brightness = 0.55 + 0.45 * morph
    hi_brightness = 0.40 + 0.60 * morph
    count = 109 if morph < 0.3 else int(min(240, max(109, round(width / 9.0))))
    blur_radius = max(1.0, 2.0 * stroke_scale)
    glow_extra = max(1.0, round(2.0 * stroke_scale))
    two_sigma2 = 2.0 * blur_radius * blur_radius

    step = width / float(count - 1)
    vx = (step * np.arange(count)).astype(np.float32)
    lut_d = np.arange(_LUT_SIZE, dtype=np.float32) / _LUT_PER_PX   # distance of each LUT slot

    def glow_lut(peak, glow_width):
        outside = np.maximum(0.0, lut_d - glow_width * 0.5)
        return (peak * np.exp(-outside * outside / two_sigma2)).astype(np.float32)

    def core_lut(peak, core_width):
        # Stored as the background's remaining weight, 1 - alpha.
        return (1.0 - peak * _coverage(lut_d, core_width)).astype(np.float32)

    # Per strand: the curve, its shader glow and core as lookup tables of the
    # distance (1/8 px steps), so each pixel is two table reads, not exp and
    # smoothstep. Same values as the shader to within the 1/8 px quantisation.
    strands = []
    for freq, phase, alpha, thick in zip(_FREQS, _PHASES, _ALPHAS, _THICKNESS):
        ys = _wave_y(vx, width, height, t, amp, freq, phase, stroke_scale).astype(np.float32)
        core_width = max(1.0, round(thick * stroke_scale))
        strands.append((ys, glow_lut(alpha * 0.3 * brightness, core_width + glow_extra * 2.0),
                        core_lut(alpha * brightness, core_width)))
    hys = _highlight_y(vx, width, height, t, amp, stroke_scale).astype(np.float32)
    strands.append((hys, glow_lut(0.4 * hi_brightness, max(2.0, round(4.0 * stroke_scale))),
                    core_lut(0.95 * hi_brightness, max(1.0, round(2.0 * stroke_scale)))))

    # result = mix(result, colour, a) for the glow, then each core dim-to-bright,
    # then the highlight: track the background's remaining weight ("keep").
    # Preallocated buffers and in-place ops: the strip is re-rendered 30x/s.
    step32 = np.float32(step)
    xs = px[0]
    glow = np.zeros((h, w), dtype=np.float32)
    keep = np.ones((h, w), dtype=np.float32)
    dist = np.empty((h, w), dtype=np.float32)
    idx = np.empty((h, w), dtype=np.intp)
    for ys, g_lut, c_lut in strands:
        seg = np.clip((xs / step32).astype(np.intp), 0, count - 2)
        y0 = ys[seg]
        slope = (ys[seg + 1] - y0) / step32
        y_at = y0 + slope * (xs - step32 * seg.astype(np.float32))
        scale = np.float32(_LUT_PER_PX) / np.sqrt(np.float32(1.0) + slope * slope)
        # distance (in LUT slots) = |row - y(x)| / sqrt(1 + y'(x)^2)
        np.subtract(py, y_at[None, :], out=dist)
        np.abs(dist, out=dist)
        np.multiply(dist, scale[None, :], out=dist)
        np.minimum(dist, _LUT_SIZE - 1, out=dist)
        idx[...] = dist
        np.maximum(glow, g_lut[idx], out=glow)
        keep *= c_lut[idx]            # c_lut holds 1 - core alpha
    glow *= band
    # Edge fade applied once: identical to the shader wherever the fade is 1
    # (all but the outer 6% at each end), a smooth fade-out there.
    keep *= 1.0 - glow
    np.subtract(1.0, keep, out=keep)
    keep *= edge_fade
    np.subtract(1.0, keep, out=keep)

    # Every pixel is colour + (bg - colour) * keep: quantise keep to 256 levels
    # and let a 256-entry palette do the blend (one C pass, no float RGB).
    np.multiply(keep, 255.0, out=keep)
    np.add(keep, 0.5, out=keep)
    np.clip(keep, 0.0, 255.0, out=keep)
    level_img = Image.fromarray(keep.astype(np.uint8), "L")
    level_img.putpalette(_palette(tuple(state_color_rgb), tuple(bg_rgb)))
    return level_img.convert("RGB")


_PALETTES: dict = {}


def _palette(colour, bg) -> bytes:
    """256 RGB entries: level k -> colour + (bg - colour) * k / 255."""
    key = (colour, bg)
    pal = _PALETTES.get(key)
    if pal is None:
        k = np.arange(256, dtype=np.float32)[:, None] / 255.0
        c = np.asarray(colour, dtype=np.float32)
        b = np.asarray(bg, dtype=np.float32)
        pal = np.clip(c + (b - c) * k + 0.5, 0, 255).astype(np.uint8).tobytes()
        if len(_PALETTES) > 16:
            _PALETTES.clear()
        _PALETTES[key] = pal
    return pal
