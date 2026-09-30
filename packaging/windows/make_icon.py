#!/usr/bin/env python3
"""Render the Windows app icon (assets/icon.ico) from the macOS app icon art.

The same dark squircle, glow, glass rim and gradient brand arrow as
packaging/macos/make_icon.py (one source of truth for the artwork), without the
Mac's 100pt grid margin and drop shadow: on Windows those leave a small, muddy
tile in the title bar and taskbar. One .ico feeds the .exe, the installer, the
uninstall entry and the window/taskbar icon.

    python packaging/windows/make_icon.py

Windows-only packaging; packaging/windows/build.py runs it on every build.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from PIL import Image

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
ICO_PATH = ROOT / "assets" / "icon.ico"

# Every size Windows asks for across 100-300% scaling (title bar, taskbar,
# Alt+Tab, Start, Explorer views).
SIZES = (16, 20, 24, 32, 40, 48, 64, 96, 128, 256)
MASTER = 1024
SUPERSAMPLE = 4
MARGIN = 1 / 32  # a hairline of air so the corners never touch the tile edge


def _mac_artwork():
    """packaging/macos/make_icon.py, loaded by path: it shares this module's name."""
    spec = importlib.util.spec_from_file_location(
        "wayfinder_macos_make_icon", ROOT / "packaging" / "macos" / "make_icon.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def render_master() -> Image.Image:
    mac = _mac_artwork()
    body = mac.render_body(MASTER * SUPERSAMPLE).resize((MASTER, MASTER), Image.LANCZOS)
    inset = round(MASTER * MARGIN)
    canvas = Image.new("RGBA", (MASTER, MASTER), (0, 0, 0, 0))
    canvas.alpha_composite(body.resize((MASTER - 2 * inset,) * 2, Image.LANCZOS), (inset, inset))
    return canvas


def write_ico(master: Image.Image, destination: Path = ICO_PATH) -> None:
    frames = [master.resize((s, s), Image.LANCZOS) for s in SIZES]
    frames[-1].save(destination, format="ICO", sizes=[(s, s) for s in SIZES],
                    append_images=frames[:-1])


def main() -> int:
    write_ico(render_master())
    print(f"Wrote {ICO_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
