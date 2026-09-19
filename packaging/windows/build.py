#!/usr/bin/env python3
"""Build the Wayfinder Aura Windows installer, end to end.

    python packaging/windows/build.py

Step 1 runs PyInstaller (packaging/windows/wayfinder-aura-windows.spec) to produce
the onedir bundle at dist/Wayfinder Aura/. Step 2 runs Inno Setup (ISCC) on
installer.iss to wrap that into dist/installer/WayfinderAura-Setup-<version>.exe.

Windows-only; it never touches the Linux AppImage/Flatpak or macOS .app builds.
Prerequisites: the app's venv with `pyinstaller` installed, and (for the
installer step) Inno Setup 6. If ISCC isn't found, the bundle is still produced
unless --require-installer is passed (CI uses it so a missing ISCC fails loudly).
"""
from __future__ import annotations

import importlib.util
import os
import re
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


def _project_version() -> str:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if match is None:
        raise SystemExit("could not read version from pyproject.toml")
    return match.group(1)


# The pinned prebuilt whisper.cpp CPU build (same one the setup flow downloads,
# see WHISPER_WINDOWS_URL in src/wayfinder/core/setup.py). Bundled so a fresh
# install transcribes without a first-run download, like the AppImage and .app.
WHISPER_ZIP_SHA256 = "c2a4b60edb11f7e11a9191ffb50929535527d4d91c9903dbe3e554583bbbc63d"
WHISPER_STAGE = ROOT / "build" / "windows-whisper"
WHISPER_FILES = ("whisper-cli.exe", "whisper-server.exe")


def _whisper_zip_url() -> str:
    text = (ROOT / "src" / "wayfinder" / "core" / "setup.py").read_text(encoding="utf-8")
    build = re.search(r'^WHISPER_WINDOWS_BUILD = "([^"]+)"', text, re.MULTILINE)
    if build is None:
        raise SystemExit("could not read WHISPER_WINDOWS_BUILD from setup.py")
    return ("https://github.com/ggml-org/whisper.cpp/releases/download/"
            f"{build.group(1)}/whisper-bin-x64.zip")


def _stage_whisper() -> None:
    """Download, verify, and extract whisper-cli/whisper-server + DLLs for the spec."""
    if all((WHISPER_STAGE / name).exists() for name in WHISPER_FILES):
        return
    import hashlib
    import io
    import urllib.request
    import zipfile

    url = _whisper_zip_url()
    print(f"== Fetching pinned whisper.cpp: {url} ==")
    with urllib.request.urlopen(url, timeout=180) as resp:
        payload = resp.read()
    digest = hashlib.sha256(payload).hexdigest()
    if digest != WHISPER_ZIP_SHA256:
        raise SystemExit(f"whisper.cpp zip digest mismatch: {digest} != {WHISPER_ZIP_SHA256}")
    WHISPER_STAGE.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        for member in zf.namelist():
            name = os.path.basename(member)
            if name in WHISPER_FILES or name.endswith(".dll"):
                (WHISPER_STAGE / name).write_bytes(zf.read(member))


def main() -> int:
    require_installer = "--require-installer" in sys.argv[1:]
    if sys.platform != "win32":
        print("The Windows installer build runs on Windows only.")
        return 1

    if require_installer and importlib.util.find_spec("llama_cpp") is None:
        print("llama_cpp is required for release builds (--require-installer). "
              "Install it with: python -m pip install -r requirements-windows.txt")
        return 1

    # The .exe icon: generated from the shared PNG if not already present.
    ico = ROOT / "assets" / "icon.ico"
    if not ico.exists():
        from PIL import Image
        Image.open(ROOT / "assets" / "icon.png").convert("RGBA").save(
            ico, sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
        )
        print(f"generated {ico}")

    _stage_whisper()

    print("== PyInstaller: building the onedir bundle ==")
    subprocess.run(
        [sys.executable, "-m", "PyInstaller",
         str(HERE / "wayfinder-aura-windows.spec"), "--clean", "--noconfirm"],
        cwd=str(ROOT), check=True,
    )

    iscc = _find_iscc()
    if iscc is None:
        if require_installer:
            print("Inno Setup (ISCC.exe) not found and --require-installer was passed.")
            return 1
        print("\nInno Setup (ISCC.exe) not found — the app bundle is ready at "
              "dist/Wayfinder Aura/. Install Inno Setup 6 to build the installer, "
              "or run: winget install JRSoftware.InnoSetup")
        return 0

    print("== Inno Setup: building the installer ==")
    version = _project_version()
    subprocess.run(
        [str(iscc), f"/DMyAppVersion={version}", str(HERE / "installer.iss")],
        cwd=str(ROOT), check=True,
    )
    print("\nDone → dist/installer/WayfinderAura-Setup-*.exe")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
