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

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

block_cipher = None

# This spec lives in packaging/windows/, so the repo root is two levels up.
PROJECT_ROOT = Path(SPECPATH).parent.parent
SRC_DIR = PROJECT_ROOT / "src"

VERSION = re.search(
    r'^version = "([^"]+)"', (PROJECT_ROOT / "pyproject.toml").read_text(), re.MULTILINE
).group(1)

# whisper.cpp binaries staged by build.py. Added as data (copied verbatim, no
# dependency analysis) into their own folder: their ggml*.dll names collide with
# llama_cpp's, and each exe loads the DLLs beside it.
def _whisper_files(folder):
    """The exes and DLLs whisper-cli/whisper-server load. The release zip also
    carries SDL2 (the stream example), llama and parakeet: neither exe imports
    them and ggml only loads ggml-*.dll backends, so they stay out."""
    unused = {"sdl2.dll", "llama.dll", "parakeet.dll"}
    return sorted(
        path for path in (PROJECT_ROOT / "build" / folder).glob("*")
        if path.suffix in (".exe", ".dll") and path.name.lower() not in unused
    )


whisper_datas = [(str(path), "whisper") for path in _whisper_files("windows-whisper")] + [
    # The Vulkan (GPU) build of the same whisper.cpp, used only for Ultra's GPU
    # acceleration; absent from dev builds without the Vulkan SDK.
    (str(path), "whisper-vulkan") for path in _whisper_files("windows-whisper-vulkan")
]

llama_binaries, llama_datas, llama_hiddenimports = [], [], []
try:
    import llama_cpp  # noqa: F401
except ImportError:
    pass
else:
    llama_binaries = collect_dynamic_libs("llama_cpp")
    llama_datas = collect_data_files("llama_cpp")
    llama_hiddenimports = collect_submodules("llama_cpp")

a = Analysis(
    [str(PROJECT_ROOT / "main.py")],
    pathex=[str(PROJECT_ROOT), str(SRC_DIR)],
    binaries=llama_binaries,
    datas=[
        (str(PROJECT_ROOT / "assets" / "icon.png"), "assets"),
        (str(PROJECT_ROOT / "assets" / "icon.ico"), "assets"),
        (str(PROJECT_ROOT / "assets" / "icons"), "assets/icons"),
        # The package source is needed because the overlay subprocess imports it.
        (str(SRC_DIR / "wayfinder"), "wayfinder"),
        (str(SRC_DIR / "wayfinder" / "ui" / "overlay.py"), "."),
        (str(PROJECT_ROOT / "wayfinder_main.py"), "."),
    ] + llama_datas + whisper_datas,
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
        # Audio. No SciPy (~72 MB): the recorder's resampling and rumble filter run
        # on core.audio_dsp's NumPy twins, checked against SciPy by the tests.
        "sounddevice", "numpy", "numpy.core._multiarray_umath",
        "wayfinder.core.audio_dsp",
        # Windows hotkeys + tray (the _win32 backends)
        "pynput", "pynput.keyboard", "pynput.keyboard._win32",
        "pynput.mouse", "pynput.mouse._win32",
        "pystray", "pystray._win32",
        # PyQt6 overlay
        "PyQt6", "PyQt6.QtCore", "PyQt6.QtGui", "PyQt6.QtWidgets",
        # HTTP + JSON (license activation, model downloads, whisper-server)
        "requests", "urllib3", "json",
    ] + llama_hiddenimports,
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
        # Replaced by core.audio_dsp on Windows (see hiddenimports).
        "scipy",
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

# Files the Windows app never loads (~55 MB), each checked against its imports.
# The pill draws with QPainter (no OpenGL: opengl32sw is Qt's software GL), its
# icons are PNG and icon.ico (qico stays), there is no QTranslator, QtNetwork
# only served QtPdf and the touch plugin, QtDBus only KWin on Linux.
_UNUSED_PREFIXES = (
    'PyQt6/Qt6/bin/opengl32sw.dll', 'PyQt6/Qt6/translations',
    'PyQt6/Qt6/bin/Qt6Pdf.dll', 'PyQt6/Qt6/bin/Qt6Network.dll', 'PyQt6/Qt6/bin/Qt6Svg.dll',
    'PyQt6/Qt6/bin/Qt6DBus.dll', 'PyQt6/QtDBus',
    'PyQt6/Qt6/plugins/generic', 'PyQt6/Qt6/plugins/iconengines',
    'PyQt6/Qt6/plugins/platforms/qminimal', 'PyQt6/Qt6/plugins/platforms/qoffscreen',
    *(f'PyQt6/Qt6/plugins/imageformats/q{fmt}'
      for fmt in ('gif', 'icns', 'jpeg', 'pdf', 'svg', 'tga', 'tiff', 'wbmp', 'webp')),
    # sounddevice loads libportaudio64bit(-asio).dll in an x64 bundle.
    '_sounddevice_data/portaudio-binaries/libportaudio.dylib',
    '_sounddevice_data/portaudio-binaries/libportaudio32bit',
    '_sounddevice_data/portaudio-binaries/libportaudioarm64',
    # AVIF is never opened (PIL imports _avif lazily, behind try/except).
    'PIL/_avif',
)


def _keep(entry):
    dest = entry[0].replace('\\', '/')
    if any(dest.startswith(prefix) for prefix in _UNUSED_PREFIXES):
        return False
    if '/__pycache__/' in f'/{dest}':  # stale bytecode from the source datas copy
        return False
    # MSVC import libraries are link-time only.
    return not (dest.startswith('llama_cpp/lib/') and dest.endswith('.lib'))


a.binaries = [entry for entry in a.binaries if _keep(entry)]
a.datas = [entry for entry in a.datas if _keep(entry)]

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
