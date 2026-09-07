# -*- mode: python ; coding: utf-8 -*-
# ruff: noqa: F821
"""
PyInstaller spec for Wayfinder Aura — Windows onedir bundle.

Build from the repo root with:
    pyinstaller packaging/windows/wayfinder-aura-windows.spec --clean --noconfirm

Produces dist/Wayfinder Aura/Wayfinder Aura.exe (+ its dependencies). The
Inno Setup script in this folder wraps that into a double-click installer.

Windows-only packaging (platform-development contract): it must not change or
consume the Linux AppImage/Flatpak or macOS .app specs.
"""

import re
from pathlib import Path

block_cipher = None

# This spec lives in packaging/windows/, so the repo root is two levels up.
PROJECT_ROOT = Path(SPECPATH).parent.parent
SRC_DIR = PROJECT_ROOT / "src"

VERSION = re.search(
    r'^version = "([^"]+)"', (PROJECT_ROOT / "pyproject.toml").read_text(), re.MULTILINE
).group(1)

a = Analysis(
    [str(PROJECT_ROOT / "main.py")],
    pathex=[str(PROJECT_ROOT), str(SRC_DIR)],
    binaries=[],
    datas=[
        (str(PROJECT_ROOT / "assets" / "icon.png"), "assets"),
        (str(PROJECT_ROOT / "assets" / "icon.ico"), "assets"),
        (str(PROJECT_ROOT / "assets" / "icons"), "assets/icons"),
        # The package source is needed because the overlay subprocess imports it.
        (str(SRC_DIR / "wayfinder"), "wayfinder"),
        (str(SRC_DIR / "wayfinder" / "ui" / "overlay.py"), "."),
        (str(PROJECT_ROOT / "wayfinder_main.py"), "."),
    ],
    hiddenimports=[
        "wayfinder", "wayfinder.config", "wayfinder.state", "wayfinder.app",
        "wayfinder.license", "wayfinder.core", "wayfinder.core.recorder",
        "wayfinder.core.transcriber", "wayfinder.core.injector",
        "wayfinder.core.injector_windows",   # native Windows text injection
        "wayfinder.core.postprocessor", "wayfinder.core.voice_profile",
        "wayfinder.core.setup", "wayfinder.core.model_updates",
        "wayfinder.ui", "wayfinder.ui.theme", "wayfinder.ui.components",
        "wayfinder.ui.overlay",
        "wayfinder.hotkeys", "wayfinder.hotkeys.pynput_listener",
        "wayfinder.hotkeys.types",
        "wayfinder.utils", "wayfinder.utils.gpu", "wayfinder.utils.gpu_simple",
        "wayfinder.utils.platform", "wayfinder.utils.lazy_imports",
        # CustomTkinter UI + imaging
        "customtkinter", "PIL", "PIL._tkinter_finder", "PIL.Image",
        "PIL.ImageDraw", "PIL.ImageFilter",
        # Audio
        "sounddevice", "numpy", "numpy.core._multiarray_umath",
        "scipy", "scipy.signal", "scipy.io", "scipy.io.wavfile",
        # Windows hotkeys + tray (the _win32 backends)
        "pynput", "pynput.keyboard", "pynput.keyboard._win32",
        "pynput.mouse", "pynput.mouse._win32",
        "pystray", "pystray._win32",
        # PyQt6 overlay
        "PyQt6", "PyQt6.QtCore", "PyQt6.QtGui", "PyQt6.QtWidgets",
        # HTTP + JSON (license activation, model downloads, whisper-server)
        "requests", "urllib3", "json",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Linux-only
        "evdev", "dbus", "gi", "gi.repository",
        # macOS-only injection
        "pyautogui", "objc", "AppKit", "Foundation", "Cocoa",
        # Dev/test tooling
        "pytest", "pytest_cov", "_pytest", "pytest_timeout", "mypy", "ruff",
        # Heavy unused packages
        "matplotlib", "pandas", "sklearn", "tensorflow", "torch",
        "IPython", "notebook", "jupyter",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)


def _dedupe(entries):
    seen, out = set(), []
    for entry in entries:
        key = entry[1] if isinstance(entry, tuple) else str(entry)
        if key not in seen:
            seen.add(key)
            out.append(entry)
    return out


a.binaries = _dedupe(a.binaries)
a.datas = _dedupe(a.datas)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,   # onedir: dependencies collected alongside
    name="Wayfinder Aura",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,             # strip is unreliable on Windows
    upx=False,
    console=False,           # windowed GUI (no console window)
    disable_windowed_traceback=False,
    icon=str(PROJECT_ROOT / "assets" / "icon.ico"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name="Wayfinder Aura",
)

print(f"\n{'='*60}\n  Wayfinder Aura Windows build — v{VERSION}\n"
      f"  Output: dist/Wayfinder Aura/Wayfinder Aura.exe\n{'='*60}\n")
