# Windows packaging

Builds the Wayfinder Aura **double-click Windows installer** — the Windows
equivalent of the Linux AppImage and macOS `.app`. Windows-only: nothing here
changes or consumes the Linux/macOS build (platform-development contract).

## Build

From the repo root, in the app's virtualenv:

```powershell
python packaging/windows/build.py
```

That runs two steps:

1. **PyInstaller** (`wayfinder-aura-windows.spec`) → a onedir bundle at
   `dist/Wayfinder Aura/Wayfinder Aura.exe` (the app + all dependencies; the
   overlay runs as `Wayfinder Aura.exe --overlay-subprocess`).
2. **Inno Setup** (`installer.iss`) → `dist/installer/WayfinderAura-Setup-<version>.exe`.

Prerequisites:

- `pip install pyinstaller` in the app venv (it's in the `dev` extra).
- [Inno Setup 6](https://jrsoftware.org/isinfo.php) for the installer step
  (`winget install JRSoftware.InnoSetup`). If it's absent, step 1 still produces
  the runnable bundle.

## What the installer does

- **Per-user install** to `%LOCALAPPDATA%\Programs\Wayfinder Aura` — no admin/UAC.
- Start Menu shortcut (desktop shortcut optional).
- Registered uninstaller that removes the whole install tree.
- **User data is preserved** on uninstall: models, config, and the license live
  under `%APPDATA%`/`%LOCALAPPDATA%\wayfinder-aura`, not the install dir.

On first launch the app's setup provisions the whisper engine (prebuilt binary
download) and speech model; see `requirements-windows.txt` for the local-cleanup
engine.

## Not yet done (follow-ups)

- **Code signing** (Authenticode) + SmartScreen reputation — the installer is
  currently unsigned, so SmartScreen will warn on first download.
- Bundling a GPU (CUDA/Vulkan) whisper build for Ultra GPU acceleration.
