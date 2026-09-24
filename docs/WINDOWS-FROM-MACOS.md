# Windows: what came over from the macOS branch

Written on the `windows` branch (2026-09-24), which starts from `macos`
(b9ebbf3). It takes the items marked **Windows ✔** in
[MACOS-PORT.md](MACOS-PORT.md) ("Suggested for Linux and Windows") and applies
the ones that fit Windows.

**Ground rules** (same as the macOS port):

- **Linux behaves exactly as on `macos`/`main`.** Every change is a
  `win32` branch next to an existing `darwin` one, or lives in a Windows-only
  module (`src/wayfinder/utils/windows_*.py`). Where a test simulated "not
  macOS", it now also pins `IS_WINDOWS = False` for the Linux case and has a
  Windows neighbour.
- **macOS is untouched.** Every `darwin` path runs the same code as before.
- **Licensing is untouched.** The one licence-adjacent change (§1) only stops
  counting a model the licence cannot load as "installed".

## Ported

| # | What | Where | Checked on Windows |
|---|---|---|---|
| 1 | Only count speech models the licence may load as usable (a Free install with a gated model on disk is offered Base again) | `wayfinder_main._has_usable_whisper_model` | unit test |
| 2 | whisper-server: text only (`no_timestamps`), 0.1 s readiness poll, respawn when the thread count changes, honest "no speech model" message | `core/transcriber.py` | pinned b4938 server accepts the field: ~5% faster (base.en), one line of text instead of segment line breaks; server ready in 0.6 s |
| 3 | Start the mic before the (acked) RECORDING overlay; a press during the 800 ms "processing" tail starts the next dictation | `wayfinder_main.start_recording`, `on_hotkey` | orchestration tests |
| 4 | Auto-Enter focus guard: `get_active_window()` returns the foreground HWND (it ran xdotool, so the guard never fired) | `core/injector.py`, `core/injector_windows.foreground_window_id` | unit test |
| 5 | Dictations stay out of Win+V clipboard history and Cloud Clipboard (`ExcludeClipboardContentFromMonitorProcessing`, `CanIncludeInClipboardHistory=0`, `CanUploadToCloudClipboard=0`), for the dictation and the restored clipboard | `core/injector_windows.py` | live: plain copy appeared in history, marked copy did not |
| 6 | No idle sleep during a dictation or a model download (power request, thread-independent) | `utils/windows_power.py`, dispatched from `utils/macos_activity.py` | live begin/end |
| 7 | Children die with the app, crash included (job object, KILL_ON_JOB_CLOSE) for the resident whisper-server | `utils/child_supervisor.bind_to_app_lifetime` | live: parent killed with `taskkill /F`, server gone |
| 8 | Update check only offers a release that carries `WayfinderAura-Setup-<version>.exe`; "Get Update" downloads it | `core/app_updates.py` | unit tests (today's tags carry no Setup exe, so Windows is correctly quiet) |
| 9 | Loopback requests to Aura's own servers bypass a configured proxy | `utils/loopback_http.py` | unit test |
| 10 | HTTPS verified against the Windows certificate store (corporate TLS inspection), certifi as fallback | `tls.py`, `main.py`, `truststore` added for Windows | live GitHub + wayfindercollective.io |
| 11 | Cloud API keys in Windows Credential Manager (DPAPI, per user, not roamed), moved out of config.json on load; stripped of stray whitespace; scrubbed from native children's environment | `utils/windows_credentials.py`, `config.py`, `utils/hostexec.py` | live round trip + config integration |
| 12 | Retired cloud model IDs migrate to current ones; Claude Haiku 4.5 default; current model lists, key help, Verify and Remove key in the cloud panels ("this PC" copy) | `config.py`, `wayfinder_main` cloud panels | unit tests |
| 13 | Settings ▸ System ▸ "Open at login" (per-user Run key; honours Task Manager's "disabled"; removed on uninstall) | `utils/windows_login_item.py`, `packaging/windows/installer.iss` | live registry round trip (throwaway value name) |
| 14 | "Finish Setup" scrolls to the model panel | `wayfinder_main._open_setup_from_cue` | — |
| 15 | Settings footer says "handcrafted for Windows" (it said Linux) | `wayfinder_main._footer_tagline` | unit test |

## Deliberately not ported

- **5 s warm-mic window.** Opening the mic costs 0.25-0.55 s on Windows
  (measured, WASAPI, USB mic), not the Mac's ~0.1 s, so the 30 s window still
  pays for itself.
- **Label wraplength fitting** (`ui/macos_label_fit.py`). It mixes CTk's scaled
  wraplength with unscaled container widths, which is only right at the Mac's
  1x Tk scaling. Windows runs at the user's display scale; needs a scaled
  variant and a visual check at 125-150%.
- **CustomTkinter DPI poll.** On Windows the poll does real work (per-monitor
  DPI changes), unlike Aqua.
- **Resident llama-server hardening / chat template.** Windows cleans up with
  the bundled in-process llama-cpp-python wheel, not llama-server.
- **Self-pipe Tk wake-up.** `createfilehandler` does not exist on Windows Tk.
- Everything Metal / Core Audio / TCC / Dock / DMG specific.

## Tests

`tests/test_windows_parity.py` covers the new Windows modules (the real-API
tests run only on Windows, with throwaway names). Tests that simulate macOS
with POSIX-only facilities (`os.fchmod`, `AF_UNIX`, PyObjC, Tk file handlers,
file modes) are marked `posix_only` and skipped on Windows only; they still run
on Linux and macOS. Full suite on Windows: 0 failures apart from the
load-sensitive `perf`-marked hero timing test, which is excluded from gating.

## Follow-ups

- Authenticode signing of the installer (SmartScreen), as before.
- The Mac's first-dictation "open at login" offer in the setup guide
  (`ui/welcome.py`) could be offered on Windows too, now that the toggle exists.
- Label fitting for scaled Windows displays (see above).
