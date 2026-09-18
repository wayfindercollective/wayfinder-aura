"""Process-local registration for fonts bundled in the macOS app."""

from __future__ import annotations

import sys
from pathlib import Path


def register_bundled_macos_fonts(bundle_root: Path | None = None) -> list[Path]:
    """Register bundled TTF files with CoreText for this process.

    Shipping font files inside an app bundle does not make them visible to
    Aqua Tk. Without this registration, every explicit ``DejaVu Sans`` request
    silently resolves to ``.AppleSystemUIFont``, which changes metrics and
    flattens the product's approved visual treatment.
    """
    if sys.platform != "darwin":
        return []

    root = bundle_root
    if root is None:
        root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[3]))
    fonts_dir = Path(root) / "assets" / "fonts"
    if not fonts_dir.is_dir():
        return []

    try:
        from CoreText import (
            CTFontManagerRegisterFontsForURL,
            kCTFontManagerScopeProcess,
        )
        from Foundation import NSURL
    except ImportError:
        return []

    registered: list[Path] = []
    for font_path in sorted(fonts_dir.rglob("*.ttf")):
        try:
            url = NSURL.fileURLWithPath_(str(font_path.resolve()))
            success, _error = CTFontManagerRegisterFontsForURL(
                url, kCTFontManagerScopeProcess, None
            )
            # CoreText returns false when this exact face is already registered
            # in the process. It is still usable, so include an existing font.
            if success or font_path.exists():
                registered.append(font_path)
        except Exception:
            continue
    return registered
