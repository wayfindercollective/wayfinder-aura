# Windows: parity with the macOS app

The `windows` branch is `macos` plus the Windows side of everything the Mac
port built, so the two apps look, feel and behave the same wherever Windows
allows it. Kept in step with `macos` by merging it (latest: 89c8942).

**Ground rules** (the platform contract, as for the Mac port):

- **Linux behaves exactly as before.** Every change is a `win32` /
  `IS_WINDOWS` branch beside an existing `darwin` one, or a Windows-only
  module (`src/wayfinder/**/windows_*.py`). Where a condition was widened
  from `darwin` to `("darwin", "win32")`, it still evaluates the same on
  Linux. Tests that simulate Linux or macOS on a Windows host pin
  `IS_WINDOWS = False`; each Windows behaviour has its own test in
  `tests/test_windows_parity.py`.
- **macOS is untouched.** Windows branches that could also run under a
  simulated Mac are written `IS_WINDOWS and not IS_MACOS`.
- **Licensing is untouched.** Display strings only (no lock emoji).

## Look

| Mac | Windows | Where |
|---|---|---|
| Frosted glass window, deeper ink panes, lifted rim on every pane, rounded content pane | Same palette and rims on a flat deep-ink surface (Tk can't show real vibrancy), rounded rimmed content pane with inset pages, no gradient. `WAYFINDER_WINDOWS_MAC_LOOK=0` restores the old look | `WINDOWS_MAC_LOOK` in `wayfinder_main.py` |
| Unified title bar | Dark caption bar in the app's ink, rim-coloured border, rounded corners (DWM) | `ui/windows_window.py` |
| Opens content-sized (800x780), centred | Same size, centred in the work area (taskbar excluded) | `ui/window_geometry.py` |
| No emoji as UI chrome | Same: lock icon on a locked Style tab, "Auto-detect", "GPU Acceleration (Ultra)" | |
| No duplicate close/hide in the header | Same (Windows has caption buttons) | |
| Welcome card fits the window | Same | `ui/welcome.py` |
| Pill wave as one path, on the display under the pointer | Same | `ui/overlay.py` |
| Hero 30 fps while recording | Same | |
| Footer "handcrafted for Mac" | "handcrafted for Windows" | |

## Behaviour

| Mac | Windows |
|---|---|
| Close hides to the menu bar; Quit from its menu | Close hides to the tray; Quit from the tray menu. Ctrl+, opens Settings (⌘,) |
| Menu bar: Check for Updates… | Tray: Check for Updates… |
| Control socket (tray, second launch, CLI, `tab:`/`inspect:`) | Token-guarded loopback channel with the same verbs (`hotkeys/windows_control.py`). **This also fixes the Windows tray menu, which never reached the app (no AF_UNIX on Windows)** |
| First run: the tour downloads the free Base model, then resumes | Same (the installer bundles no model either); "this PC" copy |
| After the first dictation: "open aura when I log in" | Same, via the per-user Run key; Settings ▸ System ▸ Open at login; removed on uninstall |
| Right Option tap/hold hotkey | Right Ctrl tap/hold (Right Alt is AltGr on most layouts). Default stays Ctrl+Alt+Space |
| Hotkey conflict caption | Windows collisions: Alt+Space, Ctrl/Shift+Space (IME), Alt/Ctrl+Enter, bare F-keys, 1Password (`utils/windows_hotkey_conflicts.py`) |
| Hotkey changes apply live | Same (no evdev restart and its "evdev not installed" warning) |
| Escape cancels a recording from any app | Same |
| Secure Input warning | Administrator-app warning: Windows (UIPI) hides an elevated window's keys and blocks typing into it |
| Physical-key repair of lost key-ups | Same, with GetAsyncKeyState |
| Blocked-microphone banner and error | Same, from Windows' three microphone privacy switches (`utils/windows_privacy.py`) |
| Auto-Enter focus guard | Same (foreground HWND) |
| Paste watchdog (10 s) | Same; the rescue says Ctrl+V |
| Gamer mode + Games tab (WoW first) | Same (`core/windows_game_chat.py` reuses the Mac module; games found by exe name; Ctrl+V/Enter as held scan codes; never types keys in a game) |
| Reduce Motion holds idle waves | "Animation effects" off does the same |

## Backend

| Mac | Windows |
|---|---|
| whisper-server: text only, 0.1 s readiness poll, respawn on thread change, honest missing-model message | Same (pinned b4938 accepts `no_timestamps`: ~5% faster, one line of text) |
| Never adopt an unowned whisper-server; children die with the app | Same: job object (KILL_ON_JOB_CLOSE) for whisper-server and the pill |
| Mic before the overlay; press during the 800 ms tail starts the next dictation | Same |
| Only licence-loadable models count as installed | Same |
| Sleep/wake/display notifications | Same (hidden window, WM_POWERBROADCAST / WM_DISPLAYCHANGE: `utils/windows_lifecycle.py`) |
| No idle sleep during dictation/downloads | Same (power requests: `utils/windows_power.py`) |
| Audio ducking via the main volume, crash journal | Same (MMDevice endpoint volume: `utils/windows_audio.py`). Windows had no ducking before |
| "Auto" mic = the system's input, followed live | Same (Windows' default capture device). An explicitly chosen mic still wins |
| Bluetooth mic released right after dictating | Same |
| Keys in the Keychain | Credential Manager (`utils/windows_credentials.py`) |
| System trust store, loopback without proxy, keys scrubbed from children, retired cloud models, key help / Verify / Remove | Same |
| Platform-aware update check (DMG) | Only releases carrying `WayfinderAura-Setup-<v>.exe` count (none today: Windows is internal) |
| Transient clipboard | Excluded from Win+V history and Cloud Clipboard |
| Packaged logs in ~/Library/Logs | `%LOCALAPPDATA%\wayfinder-aura\logs\app.log` (the windowed exe dropped all output) |
| Benchmark system info | Registry + GlobalMemoryStatusEx (was "Unknown") |
| CTk DPI poll hourly | Same (the app turns CTk DPI awareness off, so the poll can never find a change) |

## Deliberately different on Windows

- **Pill stays bottom-centre.** Bottom-right is where Windows shows
  notifications (the Mac shows them top-right).
- **30 s warm mic** (the Mac uses 5-10 s). Opening a WASAPI/MME stream costs
  0.25-0.55 s (measured), not Core Audio's ~0.1 s.
- **Ctrl+Alt+Space stays the default hotkey**; Right Ctrl tap/hold is offered.

## Not ported, with reasons

- **Real glass (vibrancy)**: Tk can't draw per-pixel transparency; the palette,
  rims and dark caption bar reproduce the look on the Mac's dark glass.
- **Label wraplength fitting** (`ui/macos_label_fit.py`): mixes CTk's scaled
  wraplength with raw container widths - right only at the Mac's 1x. Needs a
  scaled variant and a check at 125-150%.
- **"Show menu bar item" toggle**: Linux doesn't offer it either, and Windows
  already folds tray icons into the overflow.
- **Resident llama-server hardening / chat template**: Windows cleans up with
  the bundled in-process llama-cpp-python wheel.
- **Tk self-pipe wake-up**: `createfilehandler` doesn't exist on Windows Tk.
- **Permissions checklist (TCC), Dock, Metal, App Nap, DMG, notarization**:
  macOS concepts. Signing the Windows installer (Authenticode) is its own task.

## Follow-ups

- Try Gamer mode in the real games (as on the Mac, nothing is "Verified" yet).
- Label fitting for scaled Windows displays.
- Authenticode signing (SmartScreen).
