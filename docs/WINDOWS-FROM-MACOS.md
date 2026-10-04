# Windows: parity with the macOS app

Windows carries the Windows side of everything the Mac port built, so the two
apps look, feel and behave the same wherever Windows allows it. It was built on
a `windows` branch kept in step with `macos`; both are merged into `main` and
retired (2026-09-30). New Windows work uses a short `win/*` branch from `main`
([branch rules](PLATFORM-DEVELOPMENT.md#branches-and-merges)).

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
| Retina-sharp at any display scale | Sharp at 125-175% too: the app is system-DPI aware and scales itself (it was bitmap-stretched, so soft). Same on-screen size; old saved window sizes carry over. A second monitor at a different scale is stretched as before. `WAYFINDER_WINDOWS_DPI_AWARE=0` reverts | `utils/windows_dpi.py` |
| Unified title bar | Dark caption bar in the app's ink, rim-coloured border, rounded corners (DWM) | `ui/windows_window.py` |
| App icon: dark squircle, glass rim, glowing arrow | The same artwork without the Mac grid margin and drop shadow (muddy in a taskbar), as one multi-size `.ico` for the exe, installer, title bar, taskbar and pill; re-rendered on every build | `packaging/windows/make_icon.py` |
| Header mark (gradient arrow, halo, stardust) drawn by Core Animation | The same `render_brand_mark`, as a 4x CTkImage (sharp at any scale) | `ui/macos_brand_mark.py` |
| Scroll views clipped by the rounded pane | Tk can't clip to a rounded shape: pages are inset half the pane radius, so a card scrolled under the edge is cut where the pane's side is straight, not inside its corner | `WINDOWS_MAC_LOOK` in `wayfinder_main.py` |
| Opens content-sized (800x780), centred | Same size, centred in the work area (taskbar excluded) | `ui/window_geometry.py` |
| No emoji as UI chrome | Same: lock icon on a locked Style tab, "Auto-detect", "GPU Acceleration (Ultra)" | |
| No duplicate close/hide in the header | Same (Windows has caption buttons) | |
| Welcome card fits the window | Same | `ui/welcome.py` |
| Pill wave as one path, on the display under the pointer | Same | `ui/overlay.py` |
| Hero ribbon drawn by the Metal shader (smooth glowing strands) | The same shader, evaluated in NumPy (~5.6 ms for a 1400 px strip; Aqua renderer as fallback). The Linux polyline ribbon rendered faceted on Windows | `ui/windows_hero_render.py` |
| Hero 30 fps while recording | Same | |
| Footer "handcrafted for Mac" | "handcrafted for Windows" | |

## Behaviour

| Mac | Windows |
|---|---|
| Close hides to the menu bar; Quit from its menu | Close hides to the tray; Quit from the tray menu. Ctrl+, opens Settings (⌘,) |
| Quits when macOS asks (logout, updater) | Quits on WM_ENDSESSION (sign-out, shutdown, the installer), restoring ducked audio first. The installer asks a running Aura to quit that way, then ends a copy that doesn't answer |
| Menu bar: Check for Updates… | Tray: Check for Updates… |
| Control socket (tray, second launch, CLI, `tab:`/`inspect:`) | Token-guarded loopback channel with the same verbs (`hotkeys/windows_control.py`). **This also fixes the Windows tray menu, which never reached the app (no AF_UNIX on Windows)** |
| First run: the tour downloads the free Base model, then resumes | Same (the installer bundles no model either); "this PC" copy |
| After the first dictation: "open aura when I log in" | Same, via the per-user Run key; Settings ▸ System ▸ Open at login; removed on uninstall |
| Right Option tap/hold hotkey (the default) | Right Alt / Alt Gr tap/hold is the default (many laptops have no Right Ctrl; Right Ctrl is offered too). Alt Gr alone types nothing; Alt Gr + a key cancels the gesture. On US layouts a lone Alt would open the front app's menu bar, so every Right Alt press sends the unassigned mask key 0xE8. Untouched Ctrl+Alt+Space configs move once (`windows_hotkey_defaults_v2`) |
| Hotkey conflict caption | Windows collisions: Alt+Space, Ctrl/Shift+Space (IME), Alt/Ctrl+Enter, bare F-keys, 1Password, Magnifier's Ctrl+Alt+Space (`utils/windows_hotkey_conflicts.py`) |
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
| Platform-aware update check (DMG) | Only releases carrying `WayfinderAura-Setup-<v>.exe` count (every release since Windows went public, 2026-10-04) |
| Transient clipboard | Excluded from Win+V history and Cloud Clipboard |
| Packaged logs in ~/Library/Logs | `%LOCALAPPDATA%\wayfinder-aura\logs\app.log` (the windowed exe dropped all output) |
| Benchmark system info | Present PCI display adapters (EnumDisplayDevices; the registry also lists removed cards) + GlobalMemoryStatusEx (was "Unknown") |
| CTk DPI poll hourly | Same (the app turns CTk DPI awareness off, so the poll can never find a change) |
| Metal transcription in Ultra's GPU mode | A Vulkan build of the same pinned whisper.cpp (`_internal/whisper-vulkan`, `utils/windows_whisper.py`) for AMD, NVIDIA and Intel GPUs; the CPU build is its crash fallback (Windows crashes are NTSTATUS codes, not signals). Measured on a 32 s clip, Large v3 Turbo Q5: 16.97 s on 12 CPU threads, 1.12 s on an RX 7900 XTX, 3.41 s on a Radeon 780M. Cleanup stays on the CPU (no Vulkan llama-cpp-python wheel) |
| GPU picked by Metal | Automatic = the discrete Vulkan device (probed with `whisper-cli --help`, only in GPU mode); Settings > GPU lists the devices when there are two or more |
| Speech models in Application Support | `%LOCALAPPDATA%\wayfinder-aura\whisper-models` (was `%USERPROFILE%\whisper.cpp\models`, still searched) |
| Hero idle at 30 fps, stopped by the compositor when hidden | Same rate; held while another window covers all of Aura (`windows_window.window_exposure`), as when minimized |
| Cleanup threads follow the P-core count | Kept at 4: Gemma 3 1B cleanup measured 1.02 s at 4, 0.97 s at 6, 1.01 s at 8, 1.16 s at 16 threads on a Ryzen 7 7840HS (memory-bound; prompt processing already uses every core). whisper stays at 75% of logical cores: 16.97 s at 12 vs 18.49 s at 8 |
| Audio resampling and rumble filter via SciPy | NumPy twins (`core/audio_dsp.py`), exact to 1e-9 against SciPy; the bundle drops SciPy (~72 MB) |

## Deliberately different on Windows

- **Pill stays bottom-centre.** Bottom-right is where Windows shows
  notifications (the Mac shows them top-right).
- **30 s warm mic** (the Mac uses 5-10 s). Opening a WASAPI/MME stream costs
  0.25-0.55 s (measured), not Core Audio's ~0.1 s.
- **Right Alt / Alt Gr tap/hold is the default hotkey** (2026-09-28), not
  Ctrl+Alt+Space: the Claude desktop app owns Ctrl+Alt+Space as its global
  shortcut, so both apps reacted. Right Ctrl and the chord keys are offered.
- **One full repaint after each focus change or page switch.** On a PC with
  two AMD GPUs and hardware GPU scheduling, parts of the window came back as
  small black rectangles after activation until each widget redrew
  (`windows_window.repaint_after_activation`, coalesced, 120 ms).

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
  macOS concepts. Windows signing (Authenticode) is in `packaging/windows/build.py`
  and turns on when a code-signing identity is configured.

## Follow-ups

- Try Gamer mode in the real games (as on the Mac, nothing is "Verified" yet).
- Label fitting for scaled Windows displays.
- A code-signing identity for the Authenticode step (SmartScreen).
- Confirm on the reporting PC that the focus-change repaint clears the black
  squares, and that a lone Right Alt tap never opens the front app's menus.
