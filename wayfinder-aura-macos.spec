# -*- mode: python ; coding: utf-8 -*-
# ruff: noqa: F821
"""
PyInstaller spec file for Wayfinder Aura — macOS .app bundle

Build with: pyinstaller wayfinder-aura-macos.spec --clean --noconfirm
"""

import os
import platform
import sys
from pathlib import Path
from datetime import datetime

block_cipher = None

PROJECT_ROOT = Path(SPECPATH)
SRC_DIR = PROJECT_ROOT / 'src'

# Derived from pyproject.toml (single source; the literal here drifted).
import re as _re
_pyproject = (PROJECT_ROOT / 'pyproject.toml').read_text()
VERSION = _re.search(r'^version = "([^"]+)"', _pyproject, _re.MULTILINE).group(1)
BUILD_DATE = datetime.now().strftime('%Y-%m-%d')
TARGET_ARCH = os.environ.get('AURA_MACOS_ARCH', platform.machine()).lower()
if TARGET_ARCH not in {'arm64', 'x86_64'}:
    raise ValueError(
        f"Unsupported macOS architecture {TARGET_ARCH!r}; expected arm64 or x86_64"
    )

# PyInstaller uses ad-hoc signing when this is unset. Release builds set a
# Developer ID Application identity through the environment; keeping secrets
# out of the spec makes the same file safe for local and CI builds.
CODESIGN_IDENTITY = os.environ.get('MACOS_CODESIGN_IDENTITY') or None
ENTITLEMENTS_FILE = str(PROJECT_ROOT / 'packaging' / 'macos' / 'entitlements.plist')

# The macOS build helper places pinned, static whisper.cpp executables here.
# A direct PyInstaller invocation remains useful for UI-only development, while
# packaging/macos/build.py requires and verifies the complete runtime.
NATIVE_BIN_DIR = PROJECT_ROOT / 'build' / 'macos-native' / 'bin'
macos_binaries = []
for binary_name in (
    'whisper-cli', 'whisper-server',
    'llama-simple', 'llama-server',
):
    binary_path = NATIVE_BIN_DIR / binary_name
    if binary_path.is_file():
        macos_binaries.append((str(binary_path), 'bin'))
hero_renderer = PROJECT_ROOT / 'build' / 'macos-native' / 'lib' / 'libwayfinder_hero.dylib'
if hero_renderer.is_file():
    macos_binaries.append((str(hero_renderer), 'lib'))

a = Analysis(
    ['main.py'],
    pathex=[str(PROJECT_ROOT), str(SRC_DIR)],
    binaries=macos_binaries,
    datas=[
        # Include assets
        ('assets/icon.png', 'assets'),
        ('assets/icon.icns', 'assets'),
        ('assets/icons', 'assets/icons'),
        ('assets/fonts', 'assets/fonts'),
        # Include the wayfinder package source (needed for overlay subprocess)
        ('src/wayfinder', 'wayfinder'),
        # Include overlay.py at top level of Resources for subprocess access
        ('src/wayfinder/ui/overlay.py', '.'),
        # Include the legacy wayfinder_main.py (imported by main.py)
        ('wayfinder_main.py', '.'),
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
        'wayfinder.core.setup',
        'wayfinder.core.model_updates',
        'wayfinder.ui',
        'wayfinder.ui.theme',
        'wayfinder.ui.components',
        'wayfinder.ui.overlay',
        'wayfinder.ui.macos_overlay_metal',
        'wayfinder.hotkeys',
        'wayfinder.hotkeys.pynput_listener',
        'wayfinder.hotkeys.types',
        'wayfinder.utils',
        'wayfinder.utils.gpu',
        'wayfinder.utils.gpu_simple',
        'wayfinder.utils.platform',
        'wayfinder.utils.lazy_imports',
        'wayfinder.utils.macos_lifecycle',
        # CustomTkinter and dependencies
        'customtkinter',
        'PIL',
        'PIL._tkinter_finder',
        'PIL.Image',
        'PIL.ImageDraw',
        'PIL.ImageFilter',
        # Audio recording and processing
        'sounddevice',
        'numpy',
        'numpy.core._multiarray_umath',
        'scipy',
        'scipy.signal',
        'scipy.io',
        'scipy.io.wavfile',
        # macOS hotkey and text injection
        'pynput',
        'pynput.keyboard',
        'pynput.keyboard._darwin',
        'pyautogui',
        # System tray
        'pystray',
        'pystray._darwin',
        # PyQt6 for overlay
        'PyQt6',
        'PyQt6.QtCore',
        'PyQt6.QtGui',
        'PyQt6.QtWidgets',
        # PyObjC (used by overlay for NSFloatingWindowLevel)
        'objc',
        'AppKit',
        'Foundation',
        'Cocoa',
        'Metal',
        # HTTP clients
        'requests',
        'urllib3',
        # JSON (used by whisper-server backend)
        'json',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Linux-only modules
        'evdev',
        'dbus',
        'gi',
        'gi.repository',
        'gi.repository.GLib',
        # Test frameworks
        'pytest',
        'pytest_cov',
        '_pytest',
        'pytest_timeout',
        # Development tools
        'mypy',
        'ruff',
        # Unused heavy packages
        'matplotlib',
        'pandas',
        'sklearn',
        'tensorflow',
        'torch',
        'IPython',
        'notebook',
        'jupyter',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

# Qt pieces the Mac build never loads (~11 MB). QtGui links QtDBus, so that
# framework stays; the overlay only needs cocoa + macstyle, and its icons are
# PNG (built into QtGui). PDF/SVG image plugins pull in QtPdf/QtSvg.
_UNUSED_QT = (
    'PyQt6/Qt6/lib/QtPdf.framework', 'PyQt6/Qt6/lib/QtNetwork.framework',
    'PyQt6/Qt6/lib/QtSvg.framework',
    'PyQt6/Qt6/plugins/iconengines', 'PyQt6/Qt6/plugins/generic',
    'PyQt6/Qt6/plugins/platforms/libqminimal', 'PyQt6/Qt6/plugins/platforms/libqoffscreen',
    'PyQt6/Qt6/plugins/imageformats/libqpdf', 'PyQt6/Qt6/plugins/imageformats/libqsvg',
    'PyQt6/Qt6/plugins/imageformats/libqtga', 'PyQt6/Qt6/plugins/imageformats/libqwbmp',
    'PyQt6/Qt6/plugins/imageformats/libqwebp', 'PyQt6/Qt6/plugins/imageformats/libqtiff',
    'PyQt6/Qt6/plugins/imageformats/libqgif', 'PyQt6/Qt6/plugins/imageformats/libqmacheif',
    'PyQt6/Qt6/plugins/imageformats/libqmacjp2',
    'PyQt6/QtNetwork', 'PyQt6/QtPdf', 'PyQt6/QtSvg',
    # PyInstaller's top-level framework shortcuts (Frameworks/QtNetwork ->
    # PyQt6/...): left behind they are dangling links the validator rejects.
    'QtNetwork', 'QtPdf', 'QtSvg',
)


def _keep(entry):
    dest = entry[0].replace('\\', '/')
    return not any(dest.startswith(prefix) for prefix in _UNUSED_QT)


a.binaries = [entry for entry in a.binaries if _keep(entry)]
a.datas = [entry for entry in a.datas if _keep(entry)]

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,  # Collect binaries separately for .app bundle
    name='Wayfinder Aura',
    debug=False,
    bootloader_ignore_signals=False,
    strip=True,
    upx=False,  # UPX not reliable on macOS ARM
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=TARGET_ARCH,
    codesign_identity=CODESIGN_IDENTITY,
    entitlements_file=ENTITLEMENTS_FILE,
    icon='assets/icon.icns',
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=True,
    upx=False,
    name='Wayfinder Aura',
)

app = BUNDLE(
    coll,
    name='Wayfinder Aura.app',
    icon='assets/icon.icns',
    bundle_identifier='io.wayfindercollective.WayfinderAura',
    info_plist={
        'CFBundleName': 'Wayfinder Aura',
        'CFBundleDisplayName': 'Wayfinder Aura',
        'CFBundleVersion': VERSION,
        'CFBundleShortVersionString': VERSION,
        'CFBundleExecutable': 'Wayfinder Aura',
        'CFBundleIdentifier': 'io.wayfindercollective.WayfinderAura',
        'CFBundlePackageType': 'APPL',
        'CFBundleSignature': '????',
        'LSApplicationCategoryType': 'public.app-category.productivity',
        'NSPrincipalClass': 'NSApplication',
        'NSHighResolutionCapable': True,
        # NumPy/SciPy wheels in the locked Mac release stack target macOS 14.
        # Never advertise an older OS than bundled native extensions can load.
        'LSMinimumSystemVersion': '14.0',
        'LSBackgroundOnly': False,
        # Privacy permission descriptions (required by macOS)
        'NSMicrophoneUsageDescription':
            'Wayfinder Aura uses the microphone only while you dictate, to turn your speech into text.',
        # Allow the app to work in the background (tray mode)
        'LSUIElement': False,
        # A hotkey-driven background utility: App Nap throttles the Tk timers
        # that turn a hotkey press into recording (late start, clipped words).
        'LSAppNapIsDisabled': True,
        'NSHumanReadableCopyright': f'© {datetime.now().year} Wayfinder Collective',
    },
)

print(f"\n{'='*60}")
print(f"Wayfinder Aura macOS Build")
print(f"Version: {VERSION}")
print(f"Build Date: {BUILD_DATE}")
print(f"Architecture: {TARGET_ARCH}")
print(f"Signing: {CODESIGN_IDENTITY or 'ad-hoc'}")
print(f"Bundled native runtime payloads: {len(macos_binaries)}")
print(f"Output: dist/Wayfinder Aura.app")
print(f"{'='*60}\n")
