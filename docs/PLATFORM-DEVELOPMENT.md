# Platform Development Contract

Wayfinder Aura uses one repository for every desktop platform. Shared product
behavior belongs in shared code; operating-system behavior belongs behind a
small platform seam. Separate repositories would make fixes, configuration,
and tests drift, so they are not the development model for this project.

## Support status

| Platform | Status | Release promise |
|---|---|---|
| Linux | Production baseline | AppImage and Flatpak behavior must not regress |
| macOS | Active port | Source and bundle work may proceed behind macOS-specific branches |
| Windows | Planned | Import/path contracts are checked; no supported release yet |

Do not advertise macOS or Windows as generally available until their manual
release checklist has been completed on signed artifacts.

## Code ownership boundaries

| Area | Shared or platform-specific | Rule |
|---|---|---|
| Recording, transcription, post-processing, licensing, state, and config schema | Shared | Change once and test on every platform |
| `src/wayfinder/utils/platform.py` | Platform seam | Put detection, paths, and executable discovery here |
| `src/wayfinder/core/injector.py` | Shared interface, platform implementations | Keep `inject_text()` stable; isolate OS-specific input APIs |
| `src/wayfinder/hotkeys/` | Shared interface, platform implementations | Linux may use evdev/portal; macOS and Windows use native-compatible listeners |
| `src/wayfinder/ui/overlay.py`, tray startup, and audio ducking | Shared UI with guarded OS adapters | Never apply Linux compositor behavior on macOS/Windows or vice versa |
| `wayfinder_main.py` | Transitional shared UI | OS branches must be explicit and small; do not clone the file per platform |
| `flatpak/`, AppImage scripts/spec, Steam Deck scripts | Linux-only | macOS/Windows work must not edit these without a Linux-specific reason and Linux validation |
| `wayfinder-aura-macos.spec`, `launch-mac.sh`, `assets/icon.icns` | macOS-only packaging | Linux release jobs must not consume these files |
| Future `packaging/windows/` and Windows adapter modules | Windows-only | Add them without changing Linux or macOS packaging |

Platform dependencies in `pyproject.toml` must use PEP 508 environment markers.
Linux-only modules must never be imported unconditionally on macOS or Windows.
Keep one backward-compatible config schema: a config written on one supported
platform must load safely on another, with platform-specific defaults computed
at runtime rather than persisted into new duplicate schemas.

## Change workflow

1. Start from current `main` and use a narrow branch such as `feat/macos-*` or
   `feat/windows-*`. Avoid long-lived platform branches.
2. Put shared behavior in `src/wayfinder/`; add or extend an adapter at the
   seams above for OS behavior. Do not copy shared core modules into a platform
   folder.
3. Add unit tests for shared logic and platform selection. Tests must mock OS
   APIs; they must not send real keystrokes, open a microphone, or mutate a
   user's clipboard.
4. Run the Linux suite even for a macOS/Windows-only change. Linux is the
   production regression gate.
5. Let the macOS and Windows platform-smoke CI jobs pass before merge. These
   jobs prove imports, dependency markers, and directory contracts on native
   runners; they do not replace manual desktop testing.
6. In the pull request, label changed paths as shared, Linux-only, macOS-only,
   or Windows-only and record the manual checks performed.

Required local checks:

```bash
python scripts/platform_smoke.py --expected linux
python scripts/verify_structure.py
ruff check --select F821,F823,F722,E9 .
pytest tests/ -q --tb=short
```

## macOS port checklist

Before calling a macOS build releasable, test the finished `.app` on a clean
Apple Silicon Mac and, while Intel is supported, an Intel Mac.

- First launch and upgrades preserve the shared config schema and place data
  under `~/Library/Application Support/wayfinder-aura`.
- Microphone permission is requested with a useful explanation; recording,
  device selection, resampling, and playback work after denial/re-enable.
- Accessibility/Input Monitoring permission is handled clearly. The global
  record/style hotkeys work without leaking their keys into the foreground app.
- Dictation lands in the window focused at injection time. Verify Terminal,
  browser text fields, native text editors, Electron apps, and multi-monitor
  focus. Confirm the first character, capitalization, punctuation, Unicode,
  and clipboard restoration.
- Overlay and disappearing indicator appear on the active display without a
  top-left flash, stealing focus, or affecting the Linux/KWin placement path.
- Tray/menu-bar actions and Quit work from the packaged app.
- Audio ducking restores the original volume after success, cancellation,
  errors, sleep, and app exit.
- Bundled or discovered whisper/llama binaries work on a machine without the
  source checkout. Offline mode and optional cloud backends behave as labeled.
- The PyInstaller bundle is signed, hardened-runtime compatible, notarized,
  stapled, and validated from a quarantined download before distribution.

macOS implementation note: current text injection uses `pbcopy` plus Cmd+V and
global hotkeys use `pynput`. Treat those as adapters, not assumptions for shared
code. Permission failures must surface as actionable errors, never as a silent
fallback that types into an unknown window.

## Windows port checklist

Windows is a planned target, not an implemented release. A Windows developer
should begin by adding explicit adapters and packaging, not by widening Linux
or macOS conditionals until they happen to run.

- Add a Windows text-injection adapter that supports Unicode, preserves the
  clipboard when paste is used, verifies modifier release, and fails closed if
  the target focus is uncertain.
- Validate global hotkeys with standard and elevated foreground applications;
  document Windows integrity-level limitations rather than bypassing them.
- Use `%APPDATA%` for config and `%LOCALAPPDATA%` for data/cache as defined in
  `src/wayfinder/utils/platform.py`.
- Test WASAPI microphone enumeration, default-device changes, suspend/resume,
  Bluetooth transitions, and audio duck/restore behavior.
- Confirm overlay/tray behavior across DPI scaling, multiple monitors, virtual
  desktops, fullscreen applications, and taskbar positions.
- Build an installer in a new `packaging/windows/` boundary. Verify upgrades,
  uninstall, code signing, SmartScreen reputation expectations, and that no
  Linux/macOS artifacts are packaged.
- Run the complete shared suite plus native Windows adapter tests on the exact
  artifact intended for release.

## Review stop signs

Do not merge a platform change when it:

- duplicates `DEFAULT_CONFIG`, recording, transcription, or post-processing;
- imports evdev, D-Bus portal, KWin, AppKit, or Windows APIs without an OS guard;
- changes Linux packaging solely to make a macOS/Windows build pass;
- silently falls back to an injector that could paste into the wrong window;
- claims platform support based only on headless CI; or
- disables another platform's tests instead of introducing a proper adapter.
