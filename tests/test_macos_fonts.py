"""macOS bundled-font registration."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from wayfinder.utils.macos_fonts import register_bundled_macos_fonts


@pytest.mark.skipif(sys.platform != "darwin", reason="CoreText is macOS-only")
def test_bundled_dejavu_is_visible_to_aqua_tk():
    root_dir = Path(__file__).parents[1]
    registered = register_bundled_macos_fonts(root_dir)
    assert any(path.name == "DejaVuSans.ttf" for path in registered)

    import tkinter as tk
    import tkinter.font as tkfont

    root = tk.Tk()
    root.withdraw()
    try:
        font = tkfont.Font(root=root, family="DejaVu Sans", size=13)
        assert font.actual("family") == "DejaVu Sans"
    finally:
        root.destroy()
