# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec file for Wayfinder Aura

Build with: pyinstaller wayfinder-aura.spec
Build optimized: pyinstaller wayfinder-aura.spec --clean --noconfirm
"""

import sys
import os
import importlib.util
import importlib.metadata
from pathlib import Path
from datetime import datetime

block_cipher = None

# Get the project root
PROJECT_ROOT = Path(SPECPATH)
SRC_DIR = PROJECT_ROOT / 'src'

# Version info - update this for releases
VERSION = '1.1.0'
VERSION_TUPLE = (1, 1, 0, 0)  # (major, minor, patch, build)
BUILD_DATE = datetime.now().strftime('%Y-%m-%d')

def _module_available(name):
    return importlib.util.find_spec(name) is not None

def _distribution_available(name):
    try:
        importlib.metadata.version(name)
        return True
    except importlib.metadata.PackageNotFoundError:
        return False

OPTIONAL_HIDDENIMPORTS = []
OPTIONAL_EXCLUDES = []


def _python_standalone_tk_binaries():
    """Return Tcl/Tk libraries shipped beside a portable Python runtime.

    Astral's uv-managed Python builds use Tcl/Tk 9 and keep their matching
    libraries in ``sys.base_prefix/lib``.  On Linux, PyInstaller's Tk hook can
    find the Tcl library through the system loader while missing uv's combined
    ``libtcl9tk9.0.so`` library.  The resulting executable contains Tk's data
    files and _tkinter extension but dies at import time.

    System Python builds (including the Ubuntu release builder) normally have
    no Tcl/Tk libraries directly in this directory, so their standard
    PyInstaller discovery remains unchanged.
    """
    if not sys.platform.startswith('linux'):
        return []

    runtime_lib_dir = Path(sys.base_prefix) / 'lib'
    if not runtime_lib_dir.is_dir():
        return []

    libraries = {}
    for pattern in ('libtcl*.so*', 'libtk*.so*'):
        for library in runtime_lib_dir.glob(pattern):
            if library.is_file():
                libraries[library.name] = library
    return [(str(library), '.') for library in libraries.values()]


PYTHON_STANDALONE_TK_BINARIES = _python_standalone_tk_binaries()

# dbus-python and PyGObject are optional runtime integrations. PyInstaller's
# gi hook requires PyGObject package metadata; some system Python installs expose
# a partial `gi` module without that metadata, which makes the build fail.
HAS_GLIB_DBUS = (
    _module_available('dbus')
    and _module_available('gi')
    and _distribution_available('pygobject')
)
if HAS_GLIB_DBUS:
    OPTIONAL_HIDDENIMPORTS += [
        'dbus',
        'dbus.mainloop.glib',
        'gi',
        'gi.repository.GLib',
    ]
else:
    OPTIONAL_EXCLUDES += ['dbus', 'gi']

# Linux uses the Qt StatusNotifier tray. Keep pystray for non-Linux builds only;
# on Linux its AppIndicator backend can import a partial gi module and break
# PyInstaller in otherwise valid build environments.
if sys.platform.startswith('linux'):
    OPTIONAL_EXCLUDES.append('pystray')
elif _module_available('pystray'):
    OPTIONAL_HIDDENIMPORTS.append('pystray')

a = Analysis(
    ['main.py'],
    pathex=[str(PROJECT_ROOT), str(SRC_DIR)],
    binaries=PYTHON_STANDALONE_TK_BINARIES,
    datas=[
        # Include assets
        ('assets/icon.png', 'assets'),
        ('assets/icons', 'assets/icons'),
        ('assets/fonts', 'assets/fonts'),
        # Include the wayfinder package
        ('src/wayfinder', 'wayfinder'),
    ],
    hiddenimports=[
        # Wayfinder package modules
        'wayfinder',
        'wayfinder.config',
        'wayfinder.state',
        'wayfinder.app',
        'wayfinder.license',
        'wayfinder.core',
        'wayfinder.core.recorder',
        'wayfinder.core.transcriber',
        'wayfinder.core.injector',
        'wayfinder.core.postprocessor',
        'wayfinder.core.voice_profile',
        'wayfinder.ui',
        'wayfinder.ui.theme',
        'wayfinder.ui.components',
        'wayfinder.ui.overlay',
        'wayfinder.hotkeys',
        'wayfinder.hotkeys.evdev',
        'wayfinder.hotkeys.socket',
        'wayfinder.hotkeys.dbus',
        'wayfinder.utils',
        'wayfinder.utils.gpu',
        'wayfinder.utils.platform',
        'wayfinder.utils.tk_renderer',
        # CustomTkinter and dependencies
        'customtkinter',
        'PIL',
        'PIL._tkinter_finder',
        # Audio recording and processing
        'sounddevice',
        'numpy',
        'numpy.core._multiarray_umath',
        # SciPy for audio preprocessing (important for quality)
        'scipy',
        'scipy.signal',
        'scipy.io',
        'scipy.io.wavfile',
        # System integration
        'evdev',
        # PyQt6 for overlay
        'PyQt6',
        'PyQt6.QtCore',
        'PyQt6.QtGui',
        'PyQt6.QtWidgets',
        # In-process KWin scripting for overlay placement on KDE Wayland —
        # host qdbus is unusable from the bundle (env/libstdc++ mismatch)
        'PyQt6.QtDBus',
        # HTTP clients
        'requests',
        'httpx',
    ] + OPTIONAL_HIDDENIMPORTS,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Exclude test frameworks from build
        'pytest',
        'pytest_cov',
        '_pytest',
        # Exclude development tools
        'mypy',
        'ruff',
        # Exclude unused heavy packages
        'matplotlib',
        'pandas',
        'sklearn',
        'tensorflow',
        'torch',
        'IPython',
        'notebook',
        'jupyter',
    ] + OPTIONAL_EXCLUDES,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
    module_collection_mode={
        'numpy': 'pyz+py',  # Collect numpy differently for better compatibility
    },
)

# Remove duplicate binaries/datas that can cause issues
def remove_duplicate_entries(entries, name_attr='name'):
    """Remove duplicate entries based on destination path."""
    seen = set()
    unique = []
    for entry in entries:
        key = entry[1] if isinstance(entry, tuple) else getattr(entry, name_attr, str(entry))
        if key not in seen:
            seen.add(key)
            unique.append(entry)
    return unique


a.binaries = remove_duplicate_entries(a.binaries)
a.datas = remove_duplicate_entries(a.datas)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='wayfinder-aura',
    debug=False,
    bootloader_ignore_signals=False,
    # NEVER strip: jammy's binutils `strip` (2.38, the CI release builder)
    # corrupts numpy's 64KB-aligned libscipy_openblas64 — the stripped .so has
    # non-page-aligned LOAD segments and EVERY launch dies with "ELF load
    # command address/offset not page-aligned" (v1.1.3 first release attempt).
    # Newer binutils (24.04+) strip it fine, which is why local builds worked.
    # Wheel libs ship pre-stripped anyway; the size win was negligible.
    strip=False,
    upx=True,    # Use UPX compression
    upx_exclude=[
        # Some libraries don't work well with UPX
        'libpython*.so*',
        'libQt*.so*',
    ],
    runtime_tmpdir=None,
    console=False,  # No console window - GUI application
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # Icon for Linux (requires python-xlib)
    icon='assets/icon.png',
)

# Print build info
print(f"\n{'='*60}")
print(f"Wayfinder Aura Build")
print(f"Version: {VERSION}")
print(f"Build Date: {BUILD_DATE}")
print(f"Output: dist/wayfinder-aura")
print(f"{'='*60}\n")
