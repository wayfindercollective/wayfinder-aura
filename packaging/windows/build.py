#!/usr/bin/env python3
"""Build the Wayfinder Aura Windows installer, end to end.

    python packaging/windows/build.py

Step 1 runs PyInstaller (packaging/windows/wayfinder-aura-windows.spec) to produce
the onedir bundle at dist/Wayfinder Aura/. Step 2 runs Inno Setup (ISCC) on
installer.iss to wrap that into dist/installer/WayfinderAura-Setup-<version>.exe.

Windows-only; it never touches the Linux AppImage/Flatpak or macOS .app builds.
Prerequisites: the app's venv with `pyinstaller` installed, and (for the
installer step) Inno Setup 6. If ISCC isn't found, the bundle is still produced.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent


def _find_iscc() -> Path | None:
    candidates = [
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Inno Setup 6" / "ISCC.exe",
        Path(r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe"),
        Path(r"C:\Program Files\Inno Setup 6\ISCC.exe"),
    ]
    for c in candidates:
        if c.exists():
            return c
    found = shutil.which("ISCC")
    return Path(found) if found else None


def main() -> int:
    if sys.platform != "win32":
        print("The Windows installer build runs on Windows only.")
        return 1

    # The .exe icon: generated from the shared PNG if not already present.
    ico = ROOT / "assets" / "icon.ico"
    if not ico.exists():
        from PIL import Image
        Image.open(ROOT / "assets" / "icon.png").convert("RGBA").save(
            ico, sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
        )
        print(f"generated {ico}")

    print("== PyInstaller: building the onedir bundle ==")
    subprocess.run(
        [sys.executable, "-m", "PyInstaller",
         str(HERE / "wayfinder-aura-windows.spec"), "--clean", "--noconfirm"],
        cwd=str(ROOT), check=True,
    )

    iscc = _find_iscc()
    if iscc is None:
        print("\nInno Setup (ISCC.exe) not found — the app bundle is ready at "
              "dist/Wayfinder Aura/. Install Inno Setup 6 to build the installer, "
              "or run: winget install JRSoftware.InnoSetup")
        return 0

    print("== Inno Setup: building the installer ==")
    subprocess.run([str(iscc), str(HERE / "installer.iss")], cwd=str(ROOT), check=True)
    print("\nDone → dist/installer/WayfinderAura-Setup-*.exe")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
