"""Headless tests for the pure-PIL hero liquid-wave renderer.

No Tk — imports ``wayfinder.ui.hero_render`` directly (PYTHONPATH=src). Covers
mode/size, determinism, edge-fade polarity, morph amplitude spread, NaN/inf
robustness, colour plumbing, cache memoization, and a per-frame perf guard.
"""
import math
import time

import pytest

from wayfinder.ui import hero_render as hr

W, H = 640, 64
ROSE = (0xE8, 0x70, 0x7F)
INDIGO = (0x7B, 0x8B, 0xD9)
BG = hr.BG_CARD


def _dist(a, b):
    return math.sqrt(sum((a[i] - b[i]) ** 2 for i in range(3)))


def _col(img, x):
    """Mean colour of column x below the 1px top highlight (rows 1..h-1)."""
    px = img.load()
    w, h = img.size
    n = h - 1
    r = g = b = 0
    for y in range(1, h):
        pr, pg, pb = px[x, y]
        r += pr; g += pg; b += pb
    return (r / n, g / n, b / n)


def test_mode_and_size():
    img = hr.render_hero_wave(W, H, 1.0, 0.5, 1.0, ROSE)
    assert img.mode == "RGB"
    assert img.size == (W, H)


def test_determinism():
    caches = hr.get_hero_caches(W, H, ROSE, BG)
    a = hr.render_hero_wave(W, H, 2.5, 0.4, 1.0, ROSE, caches=caches)
    b = hr.render_hero_wave(W, H, 2.5, 0.4, 1.0, ROSE, caches=caches)
    assert a.tobytes() == b.tobytes()


def test_edge_columns_are_background_except_top_row():
    """Edge columns fade to the flat bg everywhere except the intentional 1px
    top highlight (row 0)."""
    img = hr.render_hero_wave(W, H, 1.3, 0.9, 1.0, ROSE)
    px = img.load()
    for x in (0, W - 1):
        for y in range(1, H):  # skip row 0 (top highlight)
            assert px[x, y] == BG, f"edge col {x} row {y} = {px[x, y]} != bg"


def test_top_row_is_highlight_not_bg():
    img = hr.render_hero_wave(W, H, 1.3, 0.9, 1.0, ROSE)
    px = img.load()
    # 6% toward white from bg; distinct from bg, same across the whole row.
    assert px[0, 0] != BG
    assert px[W // 2, 0] == px[0, 0]


def test_center_differs_from_background():
    """The ribbon paints meaningfully non-bg colour down the center column."""
    img = hr.render_hero_wave(W, H, 1.3, 0.9, 1.0, ROSE)
    assert _dist(_col(img, W // 2), BG) > 20.0


def test_morph_increases_vertical_spread():
    """morph=1 (active) spreads the ribbon further from center than morph=0
    (idle), measured on rows below the top highlight via non-bg extent."""
    def spread(morph):
        img = hr.render_hero_wave(W, H, 1.3, 0.6, morph, ROSE)
        px = img.load()
        ys = []
        for y in range(1, H):  # below top highlight
            if _dist(px[W // 2, y], BG) > 20.0:
                ys.append(y)
        return (max(ys) - min(ys)) if ys else 0

    assert spread(1.0) >= spread(0.0)


def test_no_hard_clipping_at_max_level():
    """At full energy (level=1, morph=1) the soft limiter keeps the ribbon off
    the strip bounds: no non-bg pixel in the top/bottom 2 rows (excluding the
    intentional row-0 highlight), across several animation phases."""
    for t in (0.0, 1.3, 2.9, 4.7, 7.1):
        img = hr.render_hero_wave(W, H, t, 1.0, 1.0, ROSE)
        px = img.load()
        for y in (1, 2, H - 2, H - 1):
            for x in range(W):
                assert px[x, y] == BG, (
                    f"t={t}: ink at ({x},{y}) = {px[x, y]} — ribbon touches the "
                    "strip bounds (hard clipping)"
                )


def test_soft_limit_preserves_idle_shape():
    """The limiter's knee sits above the idle ribbon's max displacement, so the
    approved idle look is untouched at low energy — and idle stays well clear of
    the bounds too."""
    # At morph=0 the max centerline displacement (~1.6 * 0.35 * 0.42h ≈ 15px at
    # h=64) is below the knee (~16.8px), so soft_limit is identity there.
    img = hr.render_hero_wave(W, H, 3.7, 0.0, 0.0, ROSE)
    px = img.load()
    # sanity: the idle ribbon still paints (limiter didn't crush it)
    assert _dist(_col(img, W // 2), BG) > 5.0
    # and it never reaches the guard rows either
    for y in (1, 2, H - 2, H - 1):
        for x in range(W):
            assert px[x, y] == BG


def test_nan_inf_do_not_crash():
    for bad in (float("nan"), float("inf"), float("-inf")):
        img = hr.render_hero_wave(W, H, bad, bad, bad, ROSE)
        assert img.size == (W, H)
        # produced a valid image, no exception
        assert img.mode == "RGB"


def test_color_plumbing_rose_vs_indigo():
    """The ribbon colour tracks the state colour argument."""
    rose_img = hr.render_hero_wave(W, H, 1.3, 0.9, 1.0, ROSE)
    indigo_img = hr.render_hero_wave(W, H, 1.3, 0.9, 1.0, INDIGO)
    rc = _col(rose_img, W // 2)
    ic = _col(indigo_img, W // 2)
    # center column clearly differs between the two colours
    assert _dist(rc, ic) > 15.0
    # rose is redder-than-blue; indigo is bluer-than-red
    assert rc[0] - rc[2] > ic[0] - ic[2]


def test_cache_memoization_and_mask_size():
    c1 = hr.get_hero_caches(W, H, ROSE, BG)
    c2 = hr.get_hero_caches(W, H, ROSE, BG)
    assert c1 is c2  # same object returned (memoized)
    assert c1["mask"].size == (W, H)
    assert c1["mask"].mode == "L"
    assert c1["bg_flat"].size == (W, H)


def test_perf_under_budget():
    """100 renders at 640x64 must average < 5ms/frame (generous CI margin over
    the ~1.5ms target — catches a joint='curve'-class regression)."""
    caches = hr.get_hero_caches(W, H, ROSE, BG)
    # warmup
    for _ in range(10):
        hr.render_hero_wave(W, H, 1.0, 0.5, 1.0, ROSE, caches=caches)
    n = 100
    t0 = time.perf_counter()
    for i in range(n):
        hr.render_hero_wave(W, H, i * 0.05, 0.5, 1.0, ROSE, caches=caches)
    mean_ms = (time.perf_counter() - t0) / n * 1000.0
    assert mean_ms < 5.0, f"hero_render mean {mean_ms:.3f} ms/frame >= 5ms budget"
