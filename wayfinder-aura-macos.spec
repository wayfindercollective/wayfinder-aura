# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec file for Wayfinder Aura — macOS .app bundle

Build with: pyinstaller wayfinder-aura-macos.spec --clean --noconfirm
"""

import sys
import os
from pathlib import Path
from datetime import datetime

block_cipher = None

PROJECT_ROOT = Path(SPECPATH)
SRC_DIR = PROJECT_ROOT / 'src'

VERSION = '1.0.0'
BUILD_DATE = datetime.now().strftime('%Y-%m-%d')

a = Analysis(
    ['main.py'],
    pathex=[str(PROJECT_ROOT), str(SRC_DIR)],
    binaries=[],
    datas=[
        # Include assets
        ('assets/icon.png', 'assets'),
        ('assets/icon.icns', 'assets'),
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
        'wayfinder.hotkeys',
        'wayfinder.hotkeys.pynput_listener',
        'wayfinder.hotkeys.types',
        'wayfinder.utils',
        'wayfinder.utils.gpu',
        'wayfinder.utils.gpu_simple',
        'wayfinder.utils.platform',
        'wayfinder.utils.lazy_imports',
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

# Remove duplicate entries
def remove_duplicate_entries(entries):
    seen = set()
    unique = []
    for entry in entries:
        key = entry[1] if isinstance(entry, tuple) else str(entry)
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
    target_arch='arm64',
    codesign_identity=None,
    entitlements_file=None,
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
    bundle_identifier='io.github.wayfindercollective.wayfinder-aura',
    info_plist={
        'CFBundleName': 'Wayfinder Aura',
        'CFBundleDisplayName': 'Wayfinder Aura',
        'CFBundleVersion': VERSION,
        'CFBundleShortVersionString': VERSION,
        'CFBundleExecutable': 'Wayfinder Aura',
        'CFBundleIdentifier': 'io.github.wayfindercollective.wayfinder-aura',
        'CFBundlePackageType': 'APPL',
        'CFBundleSignature': '????',
        'NSPrincipalClass': 'NSApplication',
        'NSHighResolutionCapable': True,
        'LSMinimumSystemVersion': '12.0',
        'LSBackgroundOnly': False,
        # Privacy permission descriptions (required by macOS)
        'NSMicrophoneUsageDescription':
            'Wayfinder Aura needs microphone access for voice dictation.',
        'NSAppleEventsUsageDescription':
            'Wayfinder Aura needs to send keystrokes to paste transcribed text.',
        # Allow the app to work in the background (tray mode)
        'LSUIElement': False,
    },
)

print(f"\n{'='*60}")
print(f"Wayfinder Aura macOS Build")
print(f"Version: {VERSION}")
print(f"Build Date: {BUILD_DATE}")
print(f"Output: dist/Wayfinder Aura.app")
print(f"{'='*60}\n")
