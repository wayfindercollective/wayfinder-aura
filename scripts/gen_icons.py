#!/usr/bin/env python3
"""
gen_icons.py — dev-only regeneration of Wayfinder Aura's line-icon set.

This script is NEVER imported by the app. It documents the provenance of the
white PNGs under assets/icons/ and regenerates them from upstream Lucide SVGs.

Provenance
----------
- Icon source: Lucide (https://lucide.dev), lucide-static package.
  URL pattern: https://unpkg.com/lucide-static@<version>/icons/<name>.svg
  (mirror: https://raw.githubusercontent.com/lucide-icons/lucide/main/icons/<name>.svg)
- Pinned version: lucide-static v1.23.0 (as fetched 2026-07-01).
- License: Lucide is ISC-licensed (https://github.com/lucide-icons/lucide/blob/main/LICENSE).
  ISC permits redistribution of the rasterized assets with attribution.

Rasterization
-------------
- Lucide ships 24x24 viewBox SVGs with stroke="currentColor" and stroke-width=2.
- We substitute currentColor -> #FFFFFF (white strokes) so the app can re-tint at
  runtime via PIL (see src/wayfinder/ui/icons.py :: tint_icon), preserving alpha.
- Rasterized at 96x96 (= 4x of the 24px logical box) for HiDPI, matching the
  _ultra_glow_logo 4x pattern. Output: white-on-transparent PNG.
- Rasterizer: rsvg-convert (librsvg). Fallbacks tried in order: rsvg-convert,
  ImageMagick `magick`/`convert`, cairosvg (pip install --user cairosvg).

Usage
-----
    python3 scripts/gen_icons.py            # download + rasterize all icons
    python3 scripts/gen_icons.py --offline  # rasterize from cached SVGs only

Add a new icon by appending its Lucide name to ICONS below and re-running.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

LUCIDE_VERSION = "1.23.0"
SVG_URL = "https://unpkg.com/lucide-static@{ver}/icons/{name}.svg"

# Logical viewBox is 24px; render at 4x for HiDPI.
LOGICAL_SIZE = 24
SCALE = 4
OUTPUT_SIZE = LOGICAL_SIZE * SCALE  # 96px

REPO_ROOT = Path(__file__).resolve().parents[1]
ICON_DIR = REPO_ROOT / "assets" / "icons"

# The app's icon set (Lucide names). Keep in sync with the REGISTRY in
# src/wayfinder/ui/icons.py and tests/test_icons.py.
ICONS = [
    "audio-waveform",   # sidebar: Dictate
    "settings-2",       # sidebar: Settings
    "pen-line",         # sidebar: Style
    "history",          # sidebar: History
    "mic",              # tone: minimal
    "briefcase",        # tone: professional
    "message-circle",   # tone: casual
    "terminal",         # tone: dev
    "sparkles",         # tone: personal
    "download",         # model download affordance
    "check",            # generic confirm
    "rotate-ccw",       # header rescue
    "inbox",            # empty states (Phase 8)
    "timer",            # benchmark (optional)
    "x",                # close affordance
]


def fetch_svg(name: str, cache: Path) -> str:
    """Return SVG source for `name`, downloading (and caching) if needed."""
    cached = cache / f"{name}.svg"
    if cached.exists():
        return cached.read_text(encoding="utf-8")
    url = SVG_URL.format(ver=LUCIDE_VERSION, name=name)
    with urllib.request.urlopen(url, timeout=30) as resp:  # noqa: S310 (trusted host)
        src = resp.read().decode("utf-8")
    cache.mkdir(parents=True, exist_ok=True)
    cached.write_text(src, encoding="utf-8")
    return src


def whiten(svg: str) -> str:
    """Replace currentColor strokes/fills with explicit white."""
    return svg.replace("currentColor", "#FFFFFF")


def rasterize(svg_path: Path, out_path: Path) -> None:
    """Rasterize a white SVG to a 96x96 transparent PNG."""
    if shutil.which("rsvg-convert"):
        subprocess.run(
            ["rsvg-convert", "-w", str(OUTPUT_SIZE), "-h", str(OUTPUT_SIZE),
             "-o", str(out_path), str(svg_path)],
            check=True,
        )
        return
    if shutil.which("magick") or shutil.which("convert"):
        tool = "magick" if shutil.which("magick") else "convert"
        subprocess.run(
            [tool, "-background", "none", "-density", "384",
             str(svg_path), "-resize", f"{OUTPUT_SIZE}x{OUTPUT_SIZE}", str(out_path)],
            check=True,
        )
        return
    try:
        import cairosvg  # type: ignore
    except ImportError as exc:  # pragma: no cover - dev tooling path
        raise SystemExit(
            "No SVG rasterizer found. Install one of: rsvg-convert (librsvg), "
            "ImageMagick, or `pip install --user cairosvg`."
        ) from exc
    cairosvg.svg2png(
        url=str(svg_path), write_to=str(out_path),
        output_width=OUTPUT_SIZE, output_height=OUTPUT_SIZE,
    )


def main() -> int:
    offline = "--offline" in sys.argv
    cache = REPO_ROOT / "scripts" / ".lucide_cache"
    ICON_DIR.mkdir(parents=True, exist_ok=True)
    for name in ICONS:
        if offline:
            svg = (cache / f"{name}.svg").read_text(encoding="utf-8")
        else:
            svg = fetch_svg(name, cache)
        white_svg = cache / f"{name}.white.svg"
        cache.mkdir(parents=True, exist_ok=True)
        white_svg.write_text(whiten(svg), encoding="utf-8")
        out = ICON_DIR / f"{name}.png"
        rasterize(white_svg, out)
        print(f"  {name:<16} -> {out.relative_to(REPO_ROOT)}")
    print(f"Generated {len(ICONS)} icons into {ICON_DIR.relative_to(REPO_ROOT)}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
