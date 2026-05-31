#!/usr/bin/env python3
"""Resize assets/icon.png into hicolor PNG sizes for the Flatpak export.

The source icon is 2048x2048, but flatpak-builder's export rejects icons larger than
512x512 (STEAMDECK-INSTALL-LOG: the first Flatpak build failed here). Run during the
wayfinder-aura module build, after python-deps has installed Pillow:

    PYTHONPATH="$(ls -d ${FLATPAK_DEST}/lib/python3*/site-packages | head -1)" \
        python3 flatpak/resize-icons.py ${FLATPAK_DEST}
"""
import os
import sys

from PIL import Image

dest = sys.argv[1] if len(sys.argv) > 1 else os.environ["FLATPAK_DEST"]
app_id = "io.github.user.WayfinderAura"
src = Image.open("assets/icon.png").convert("RGBA")

for size in (256, 128, 64, 48):
    d = os.path.join(dest, "share/icons/hicolor", f"{size}x{size}", "apps")
    os.makedirs(d, exist_ok=True)
    src.resize((size, size), Image.LANCZOS).save(os.path.join(d, f"{app_id}.png"))
    print(f"  wrote {size}x{size} icon")
