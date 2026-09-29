# Windows packaging

Builds the Wayfinder Aura **double-click Windows installer** — the Windows
equivalent of the Linux AppImage and macOS `.app`. Windows-only: nothing here
changes or consumes the Linux/macOS build (platform-development contract).

## Build

From the repo root, in the app's virtualenv:

```powershell
python packaging/windows/build.py
```

That runs these steps:

1. **App icon** (`make_icon.py`) → `assets/icon.ico`, re-rendered from the
   shared app-icon art every build.
2. **whisper.cpp** → the pinned official CPU build (`build/windows-whisper`,
   SHA-256 checked) and a **Vulkan build of the same tag** compiled from source
   (`build/windows-whisper-vulkan`; whisper.cpp publishes none for Windows x64),
   with the release job's own flags plus `GGML_VULKAN=ON`. Dynamically loaded
   backends: on a PC without a Vulkan driver the GPU backend is just skipped.
3. **PyInstaller** (`wayfinder-aura-windows.spec`) → a onedir bundle at
   `dist/Wayfinder Aura/Wayfinder Aura.exe` (the app + all dependencies; the
   overlay runs as `Wayfinder Aura.exe --overlay-subprocess`), with both
   whisper builds under `_internal/whisper` and `_internal/whisper-vulkan`.
4. **Signing** (optional, see below), then **Inno Setup** (`installer.iss`) →
   `dist/installer/WayfinderAura-Setup-<version>.exe`.

Prerequisites:

- `python -m pip install -r requirements-windows.txt -e ".[dev]"` in the app
  venv, including PyInstaller and the pinned prebuilt CPU llama engine.
- [Inno Setup 6](https://jrsoftware.org/isinfo.php) for the installer step
  (`winget install JRSoftware.InnoSetup`). If it's absent, step 3 still produces
  the runnable bundle.
- For the GPU build: Visual Studio 2022 (Build Tools, C++), CMake and the
  [Vulkan SDK](https://vulkan.lunarg.com/sdk/home#windows) (`VULKAN_SDK`, or
  installed under `C:\VulkanSDK`). A dev build without the SDK is CPU-only.

Release builds use `python packaging/windows/build.py --require-installer`.
This requires Inno Setup, the Vulkan SDK and `llama_cpp` in the build
interpreter; the Windows spec bundles the engine's modules, data, and native DLLs.

The bundle leaves out what the Windows app never loads (checked against each
file's imports; see `_UNUSED_PREFIXES` in the spec): Qt's software OpenGL,
translations, PDF/SVG/network/D-Bus pieces and unused image/platform plugins,
the whisper release's SDL2/llama/parakeet DLLs, llama's `.lib` files, other
architectures' PortAudio builds, and **SciPy** (~72 MB): the recorder's
resampling and rumble filter run on `core/audio_dsp.py`, NumPy twins checked
against SciPy by `tests/test_audio_dsp.py`.

## Signing (Authenticode)

`build.py` signs the app, both whisper builds' executables, `Setup.exe` and the
uninstaller when the build machine has a code-signing identity:

- `WAYFINDER_SIGN_CERT_SHA1`: thumbprint of an OV/EV code-signing certificate
  in the Windows certificate store, or
- `WAYFINDER_SIGN_DLIB` + `WAYFINDER_SIGN_METADATA`: Azure Trusted Signing
  (`Azure.CodeSigning.Dlib.dll` and its `metadata.json`).

`WAYFINDER_SIGN_TIMESTAMP_URL` overrides the timestamp server and `SIGNTOOL`
the `signtool.exe` path (default: the newest Windows SDK's).
`WAYFINDER_REQUIRE_SIGNING=1` fails a build that has no identity, for release
jobs once one exists. Without one the build is unsigned, as before.

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
- **User data is preserved** on uninstall: config and the license live under
  `%APPDATA%\wayfinder-aura`, speech and cleanup models under
  `%LOCALAPPDATA%\wayfinder-aura\whisper-models` and `\llm-models`, not the
  install dir. Speech models downloaded before 2026-09 in `%USERPROFILE%\whisper.cpp\models`
  are still found.

The bundle ships the pinned prebuilt whisper.cpp CPU build (`whisper-cli.exe`,
`whisper-server.exe` and their DLLs, SHA-256 checked by `build.py`) under
`_internal/whisper/`, its Vulkan build under `_internal/whisper-vulkan/` (used
only in Ultra's GPU mode, with the CPU build as its crash fallback), plus the
llama-cpp-python cleanup engine. Speech and cleanup models stay
user-downloadable: on first launch the app shows a setup prompt that opens the
model download.

## Not yet done (follow-ups)

- **A code-signing identity** (an OV/EV certificate or Azure Trusted Signing)
  for the signing step above, then SmartScreen reputation. Until one exists the
  installer is unsigned and SmartScreen warns on first download.
- A GPU (Vulkan) cleanup engine: llama-cpp-python publishes CPU and CUDA wheels
  only, so cleanup stays on the CPU (about 1 s for a dictation with Gemma 3 1B).
