# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec file for Wayfinder Voice
Build with: pyinstaller wayfinder-voice.spec
"""

import sys
import os
from pathlib import Path

block_cipher = None

# Get the project root
PROJECT_ROOT = Path(SPECPATH)
SRC_DIR = PROJECT_ROOT / 'src'

a = Analysis(
    ['main.py'],
    pathex=[str(PROJECT_ROOT), str(SRC_DIR)],
    binaries=[],
    datas=[
        # Include assets
        ('assets/icon.png', 'assets'),
        # Include the wayfinder package
        ('src/wayfinder', 'wayfinder'),
    ],
    hiddenimports=[
        # Wayfinder package modules
        'wayfinder',
        'wayfinder.config',
        'wayfinder.state',
        'wayfinder.app',
        'wayfinder.core',
        'wayfinder.core.recorder',
        'wayfinder.core.transcriber',
        'wayfinder.core.injector',
        'wayfinder.core.postprocessor',
        'wayfinder.core.ollama_manager',
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
        # CustomTkinter and dependencies
        'customtkinter',
        'PIL',
        'PIL._tkinter_finder',
        # Audio recording
        'sounddevice',
        'numpy',
        'numpy.core._multiarray_umath',
        # System integration
        'evdev',
        'pystray',
        # D-Bus for Wayland
        'dbus',
        'gi',
        'gi.repository.GLib',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Exclude problematic scipy if not needed
        'scipy',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
    module_collection_mode={
        'numpy': 'pyz+py',  # Collect numpy differently
    },
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='wayfinder-voice',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,  # No console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
