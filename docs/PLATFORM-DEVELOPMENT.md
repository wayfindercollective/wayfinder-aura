# Platform Development Contract

Wayfinder Aura uses one repository for every desktop platform. Shared product
behavior belongs in shared code; operating-system behavior belongs behind a
small platform seam. Separate repositories would make fixes, configuration,
and tests drift, so they are not the development model for this project.

## Support status

| Platform | Status | Release promise |
|---|---|---|
| Linux | Production baseline | AppImage and Flatpak behavior must not regress |
| macOS | Active port | Source and bundle work lands on `main` through short `mac/*` branches |
| Windows | Internal candidate | Installer built and smoke-tested in CI; not attached to public releases until sign-off |

Do not advertise macOS or Windows as generally available until their manual
release checklist has been completed and the owner signs off (decision
2026-09-21: Windows stays internal until then, and is published to neither the
releases page nor the storefront).

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

## Branches and merges

`main` is the only long-lived branch. Operating systems are separated in code
(the seams above), not in branches. Adopted 2026-09-30, after per-OS branches
drifted: one commit existed as seven copies across branches, pushes to `macos`
and `windows` ran no CI, and a Linux branch would have conflicted in 109 files
although 174 of its 179 commits were already on `main`.

- **One task, one short branch.** Branch from the latest `origin/main` and name
  it by area: `mac/`, `win/`, `linux/`, `deck/`, `ci/`, `docs/` (`fix/` and
  `feat/` also fine). Merge within days, then delete it.
- **`main` is protected.** Changes land only by pull request, after the
  `Quality` (Linux), `Platform smoke (macOS)` and `Windows tests` checks pass.
  The rule applies to admins and agents too; nobody pushes to `main` directly.
  Windows jobs are admitted automatically ([CI.md](CI.md)).
- **Land shared changes once.** CI, docs, runner policy and test infrastructure
  go to `main` in one pull request. Never cherry-pick a commit onto several
  branches; a branch picks up `main` by merging or rebasing on it.
- **One release line for every OS.** A release is a `vX.Y.Z` tag on `main`
  that builds every platform's packages (see
  [CI.md](CI.md#user-update-contract)). To patch a shipped release, cut
  `release/<version>` from its tag: one branch for all operating systems, never
  one per OS. Carry fixes from `main` into it, never the other way.
- **One parked branch.** `release/windows-public-pending-signoff` keeps the
  release workflow from before the 2026-09-21 decision, which attached the
  Windows installer. At sign-off, redo that change as a fresh pull request from
  `main`; do not merge the parked branch.
- **Retired branches.** `macos`, `windows`, `develop` and
  `feat/premium-feel-polish` are retired: do not commit to or recreate them.
  Their tips are kept as `archive/<name>` tags. A machine with unpushed work on
  one of them rebases that work onto `main` in a new task branch.
- **AI agents.** One git worktree per task, created from the latest
  `origin/main`, and a pull request back. Do not reuse another session's branch
  or worktree.

## Change workflow

1. Start from current `main` on a short task branch (see Branches and merges).
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

Windows has a working, CI-tested installer that is not public yet. Windows
development must use explicit adapters and packaging, not widen Linux or macOS
conditionals until they happen to run. Complete this manual checklist on the
exact artifact before any public release.

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
- Build the installer in the `packaging/windows/` boundary. Verify upgrades,
  uninstall, the unsigned SmartScreen warning disclosure (until code signing),
  and that no Linux/macOS artifacts are packaged.
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
