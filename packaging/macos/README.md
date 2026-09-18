# macOS packaging

The current macOS artifact supports **Apple Silicon on macOS 14 or newer**. It
is an architecture-specific PyInstaller `.app` containing
the Python UI and pinned Metal-enabled whisper.cpp and llama.cpp executables.
Whisper uses its resident server; local cleanup uses the deterministic
`llama-simple` executable (the optional llama.cpp web server and general CLI
are not packaged).
Speech and cleanup models remain user-downloadable so the installer does not
carry hundreds of megabytes of weights.

## Local candidate

Use the Python.org Python 3.12 framework build. The macOS UI is intentionally
pinned to the 3.12 line: 3.11 ships an older Aqua Tk, while 3.13+ has produced
visibly different/flat CustomTkinter rendering in project testing. The build
helper refuses any other Python minor so a local package cannot silently ship a
different UI stack.

```bash
python3.12 -m venv venv-mac
venv-mac/bin/python -m pip install --upgrade pip
venv-mac/bin/python -m pip install -c packaging/macos/constraints.txt -e '.[dev]'
venv-mac/bin/python packaging/macos/build.py
```

The current release stack requires Tk 8.6.16 or newer within the 8.6 line.
The official Python 3.12.10 universal2 installer satisfies that requirement.

The floating status waveform and in-app hero are rendered by the bundled native
Metal helper; Qt/Tk retain the approved chrome, typography, and controls.

The native runtime build also requires Xcode Command Line Tools and CMake
(`xcode-select --install` and `brew install cmake`).

The build helper pins and compiles whisper.cpp, creates
`dist/Wayfinder Aura.app`, validates its code signature and packaged runtime,
runs the bundle self-tests, and creates an architecture-labelled DMG. Local
builds use an ad-hoc signature and are suitable for development/QA only.

Use `--skip-native-build` to reuse the pinned native build, or `--no-dmg` when
iterating on the app bundle.

## Developer ID signing and notarization

Import the Developer ID Application certificate into the login keychain, then
set the identity by its full `security find-identity -v -p codesigning` name:

```bash
export MACOS_CODESIGN_IDENTITY='Developer ID Application: Example Company (TEAMID)'
xcrun notarytool store-credentials wayfinder-aura-notary \
  --apple-id you@example.com --team-id TEAMID --password APP_SPECIFIC_PASSWORD
export MACOS_NOTARY_PROFILE=wayfinder-aura-notary
venv-mac/bin/python packaging/macos/build.py
```

The helper passes the hardened-runtime entitlements to PyInstaller, submits the
DMG with `notarytool`, staples both the app and DMG, and runs Gatekeeper
assessment. Never commit certificate files, Apple credentials, or keychain
passwords.

## Manual release QA

The automated checks prove bundle structure and imports, not desktop behavior.
Before distributing, follow the macOS checklist in
`docs/PLATFORM-DEVELOPMENT.md` on a clean Mac, including microphone denial and
re-enable, Accessibility/Input Monitoring, global hotkeys, Unicode paste and
clipboard restoration, overlay/menu-bar behavior, sleep, and multiple apps.

For a clean install, drag **Wayfinder Aura.app** into **Applications** before
launching it. macOS privacy approvals should be granted only to that stable copy.
