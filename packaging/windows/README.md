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

- `python -m pip install -r requirements-windows.txt -e ".[dev]"` in the app
  venv, including PyInstaller and the pinned prebuilt CPU llama engine.
- [Inno Setup 6](https://jrsoftware.org/isinfo.php) for the installer step
  (`winget install JRSoftware.InnoSetup`). If it's absent, step 1 still produces
  the runnable bundle.

Release builds use `python packaging/windows/build.py --require-installer`.
This requires both Inno Setup and `llama_cpp` in the build interpreter; the
Windows spec bundles the engine's modules, data, and native DLLs.

## CI candidate and releases

The **Windows Candidate** workflow (`.github/workflows/windows-build.yml`) runs
the Windows suite, the platform smoke check, and this build on `windows-latest`,
then silently installs it, runs the packaged import, TLS, and LLM engine
self-tests, and uninstalls it. Each process has a timeout and must exit with
code zero; uninstall must remove the app executable. Only then does CI upload
the unsigned installer as the `wayfinder-aura-windows-x64`
artifact (kept 7 days). Run it from **Actions → Windows Candidate → Run
workflow**; it also runs on every PR targeting `main`.

The Release workflow reuses this build after quality and release-readiness
checks, but the installer is **not attached to public releases**: Windows stays
internal until testing is complete and the owner signs off (2026-09-21). Team
members install from the workflow artifact. Dispatch Release with
`artifacts: windows` for a fully gated candidate. The re-attach change is parked
on `release/windows-public-pending-signoff`. Manual Windows acceptance on the exact installer remains required
by [the platform contract](../../docs/PLATFORM-DEVELOPMENT.md).

## Packaged LLM engine self-test

Run the installed `Wayfinder Aura.exe --llm-engine-self-test` to import
`llama_cpp` and call `llama_print_system_info()`, proving the native DLLs load.
For manual inference acceptance, set `WAYFINDER_LLM_SELFTEST_MODEL` to an
already-downloaded GGUF model path. The same probe then loads it with a
512-token context and requests up to eight tokens, requiring non-empty text.
CI leaves this variable unset so it needs no model download.

The probe exits before Tk or single-instance locking, with exit code 0 and
`LLM_ENGINE_SELF_TEST_OK`, or exit code 1 and `LLM_ENGINE_SELF_TEST_FAILED`.
The packaged app is windowed, so use `Start-Process -PassThru` and
`WaitForExit(timeout)` to check its exit code; terminate it on timeout.

## What the installer does

- **Per-user install** to `%LOCALAPPDATA%\Programs\Wayfinder Aura` — no admin/UAC.
- Start Menu shortcut (desktop shortcut optional).
- Registered uninstaller that removes the whole install tree.
- **User data is preserved** on uninstall: models, config, and the license live
  under `%APPDATA%`/`%LOCALAPPDATA%\wayfinder-aura`, not the install dir.

The bundle ships the pinned prebuilt whisper.cpp CPU build (`whisper-cli.exe`,
`whisper-server.exe` and their DLLs, SHA-256 checked by `build.py`) under
`_internal/whisper/`, plus the llama-cpp-python cleanup engine. Speech and
cleanup models stay user-downloadable: on first launch the app shows a setup
prompt that opens the model download.

## Not yet done (follow-ups)

- **Code signing** (Authenticode) + SmartScreen reputation — the installer is
  currently unsigned, so SmartScreen will warn on first download.
- Bundling a GPU (CUDA/Vulkan) whisper build for Ultra GPU acceleration.
