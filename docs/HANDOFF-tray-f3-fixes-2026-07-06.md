# Handoff — Tray rebuild + F3 dictation fixes (2026-07-06)

**Machine:** desktop dev box (Bazzite/Fedora KDE Plasma 6, **Wayland**, RX 9060 XT / Ryzen 9600X).
Runs from **source** via `launch-wayfinder-aura.sh` → `venv-gpu/bin/python main.py`
(`venv-gpu` is just symlinks to the host `python3.14`; deps live in `~/.local/lib/python3.14`).
The **Steam Deck runs the Flatpak** — a different code path in places (noted below).

---

## TL;DR state

All changes are **UNCOMMITTED** in the working tree. Full test suite green (**1217 passed, 12 skipped**).
Three of four fixes are verified as far as is possible headlessly; **the tray still needs a
live restart + click/dictate test** (can't be done from this environment — see Constraints).

| # | Fix | Status |
|---|-----|--------|
| 1 | **F3 double-trigger / Brave-Find leak** — parser now matches Plasma 6's group format | ✅ **Verified live** (log: `evdev binding skipped`, dictation single-fires `2.2s`) |
| 2 | **Broken tray** — retire pystray on Linux, use native Qt `QSystemTrayIcon` | ⚠️ Code done; needs live click test |
| 3 | **"Notification icon crashed"** — give the Qt tray SNI metadata | ⚠️ Applied; needs live check |
| 4 | **Tray dies on dictate** — overlay couldn't `import wayfinder` standalone | ✅ Verified headlessly; needs restart to confirm live |
| — | **OPEN BUG:** `Chunk 1 error: [Errno 13] Permission denied: ''` → "No speech detected" | ❌ **Not diagnosed** — see Open Issues |

---

## Git state

- Branch **main** @ `9fef0f1` (pulled + fast-forwarded from origin this session — a big "preship" batch).
- **Uncommitted** (verify with `git diff`):
  - `wayfinder_main.py` — parser fix, `should_host_qt_tray`, `setup_tray` gate, `want_tray` gate.
  - `src/wayfinder/ui/overlay.py` — sys.path bootstrap, QApplication metadata, tray menu reorder.
  - `tests/test_kde_hotkey_defer.py` — added Plasma 6 format cases.
  - `tests/test_tray_backend.py` — **new**, 8 matrix tests for `should_host_qt_tray`.
- Pre-existing untracked (ignore): `.clinerules/`, `docs/qwen-plan-check.md`, `scripts/.lucide_cache/`, `toolbox/`.
- **Not committed on purpose** — waiting on the user's live restart test before committing + pushing all of it together.

---

## The four fixes in detail

### 1. F3 double-trigger + Brave-Find leak (THE important one, verified live)
**Symptom:** dictating typed into Brave's *Find* bar, and/or recorded `0.0s` "too short".
**Root cause:** F3 was firing **twice** — once via the app's passive **evdev** listener (can't
consume the key, so it also leaks to the focused window → Find bar) and once via KDE's global
shortcut → `trigger_record.py` → socket `toggle`. Net: record toggles on then instantly off.

The earlier defer fix (`d2daf7b`, prior session) was supposed to make evdev *defer* when KDE owns
the shortcut, but its parser `kde_record_shortcut_active` looked for the group
`[wayfinder-aura.desktop]` with a **comma-list** value. **Plasma 6 actually writes:**
```
[services][wayfinder-aura.desktop]
cycle-style=F2
toggle-recording=F3
```
i.e. a `[services]`-**prefixed** group and a **bare** value. The parser never matched → always
returned `False` → evdev never deferred.

**Fix** (`wayfinder_main.py:1878` `kde_record_shortcut_active`): match the group by **suffix**
(`line.endswith("[wayfinder-aura.desktop]")`) so the `[services]` prefix doesn't hide it; the
value already takes the first comma-field so bare `F3` works. **Verified against the live
`~/.config/kglobalshortcutsrc` → now returns `True`**, and the running app logged
`⌨️ Record hotkey handled by KDE global shortcut — evdev binding skipped`, then dictation
single-fired (`Duration: 2.2s`, not `0.0s`). Tests: `tests/test_kde_hotkey_defer.py`
(`test_active_plasma6_services_prefixed_group`).

Consumers already wired: `_compositor_owns_hotkeys` (`wayfinder_main.py:13290`) reads the file;
`_start_evdev_listener` (`:13313`) returns early when it's True; the hotkey supervisor skips its
restart in defer mode. So once the parser is right, everything downstream just works.

### 2. Broken tray → native Qt tray on Linux
**Symptom (original request):** right-clicking the tray, **none** of the menu items did anything
(Toggle/Reset/Open Settings/Model/Microphone/Quit). Proven dead: the app stayed alive after
clicking **Quit**.
**Root cause:** on the desktop the tray was **pystray**, which fell back to the old
**libappindicator** backend (Ayatana's maintained GI namespace is absent). On Plasma 6 that
backend *renders* the exported DBusMenu but its **click activations never dispatch** to the Python
callbacks. (The GLib loop is running — `py-spy` confirmed — it just doesn't get the events.)

**Fix:** on Linux, retire pystray and use the **native Qt `QSystemTrayIcon`** already hosted in
the overlay subprocess (the exact tray the Deck Flatpak uses — reliable StatusNotifier, routed
over the Unix socket, the same channel F3 uses). macOS keeps pystray.
- Pure decision `should_host_qt_tray(platform, has_pystray, enable_tray_icon)` (`wayfinder_main.py:1948`),
  used in **both** the `want_tray` gate (`:3776`) and `setup_tray` (`:12466`) so they can't drift.
- Qt tray menu reordered/renamed to the essential set the user chose (`overlay.py:2265`):
  **Toggle Recording · Reset · — · Open Settings · — · Quit** (user picked "essential first";
  Model/Audio-Processing quick-submenus were intentionally dropped, reachable via Open Settings —
  they can be added later via new socket verbs + overlay-side submenus).
- Tests: `tests/test_tray_backend.py`.

### 3. "Notification icon crashed" — Qt tray SNI metadata
**Symptom:** when the new Qt tray registered, `plasmashell` spammed
`BackgroundAppItem.qml: Cannot read property 'name'/'icon'/'itemId' of null` and the icon rendered
broken.
**Cause:** the overlay's `QApplication` set no app name / desktop-file name / window icon, so the
StatusNotifierItem had null Id/Title/Icon.
**Fix** (`overlay.py:2007`, right after `QApplication(...)`): set `setApplicationName`,
`setApplicationDisplayName`, `setDesktopFileName(FLATPAK_ID or "wayfinder-aura")`, and
`setWindowIcon(ICON_PATH)`. **Not yet visually verified** (needs a display).

### 4. Tray dies on dictate — overlay `import wayfinder` failed standalone (verified headlessly)
**Symptom:** after fixes 2–3, the tray still died the moment you dictated. Log:
`tray: setup failed: No module named 'wayfinder'`.
**Root cause:** the overlay runs as a **bare-script subprocess** (`python …/src/wayfinder/ui/overlay.py`),
so only `ui/` is on `sys.path`, **not `src/`**. The tray block's `from wayfinder.ui.tray_icon import
make_arrow_icon` (and `wayfinder.config`) therefore fail. This never bit before because the desktop
overlay was launched **without** `--tray` (fix #2 enabled `--tray` on desktop), and the Flatpak
installs `wayfinder` as a real package so it's importable there.
**Fix** (`overlay.py:28`): a guarded `sys.path` bootstrap at the top —
`try: import wayfinder / except ImportError: sys.path.insert(0, <src>)`. **Verified headlessly**:
mimicking the bare-script path, `import wayfinder`, `wayfinder.config`, and
`wayfinder.ui.tray_icon` all resolve after the bootstrap. No-op in the Flatpak.
*(Recommended: add a regression test that the overlay is importable with only `ui/` on the path.)*

---

## OPEN ISSUES for the next agent

1. **`Chunk 1 error: [Errno 13] Permission denied: ''` → "No speech detected" / `0 chunks`.**
   In the 16:58 run, dictation recorded 2.2s but wrote **0 chunks** — something in the chunked
   transcription path tried to open an **empty path** (`''`) for writing. This is why no text was
   produced. **Not investigated.** Likely a chunk temp-file path resolving to `''` (config/temp-dir
   or the chunked recorder). Unrelated to the tray/parser work. Start at the chunked recorder /
   `transcriber.py` chunk-writing code; grep for where a chunk filename is built. Reproduce by
   dictating and watching `~/.cache/wayfinder-aura/activity.log`.

2. **Live tray verification.** Confirm on a real relaunch: (a) one tray icon, rendered correctly,
   no `BackgroundAppItem` spam in `journalctl --user`; (b) Toggle/Reset/Open Settings/Quit each
   work; (c) the icon survives a dictation (state change) — this is the fix-#4 check.

3. **Then commit + push** all uncommitted changes to `main` (and mirror `feat/premium-feel-polish`
   per the repo's branch-sync habit), once the user confirms.

---

## How to work here (constraints — important)

- **Tests:** `flatpak-spawn --host sh -c 'cd /var/home/bazzite/Dev/wayfinder-aura; PYTHONPATH=src python3 -m pytest tests/ -o "addopts=-q --tb=short -p no:cacheprovider"'`
  (host `python3.14` has deps; the VS Code sandbox `python3` does **not** — but sandbox
  `python3 -m py_compile` is fine for syntax). `pyproject` sets `--timeout=60` (pytest-timeout not
  installed) → always override `addopts` as shown.
- **Diagnostics on the host:** `flatpak-spawn --host …` (the sandbox can't see host processes,
  `/run/user`, or the real session). `py-spy` via `venv-gpu/bin/py-spy dump --pid <pid>`.
- **Logs:** `~/.cache/wayfinder-aura/activity.log` (app) and `overlay-debug.log` (overlay subprocess,
  where `tray: …` lines go). KDE tray errors show in `journalctl --user`.
- **Do NOT** run `printenv` / `echo $VAR` (a Bash hook rejects env-value echoing).
- **Do NOT** broad-`pkill -f main.py`/`overlay.py` or aggressively restart — it can kill other
  worktree instances and trips `launch-wayfinder-aura.sh`'s crash-loop guard (5 crashes/60s → bails).
- **You cannot launch the GUI** from `flatpak-spawn --host` (no `WAYLAND_DISPLAY`). Ask the user to
  relaunch. **To stop a stuck instance cleanly**, send the socket quit (its own shutdown path):
  `python3 -c 'import socket; s=socket.socket(socket.AF_UNIX); s.connect("/run/user/1000/wayfinder-aura/wayfinder-aura.sock"); s.send(b"quit"); s.close()'`
  — clean exit (code 0) so the launcher does **not** relaunch; then the user starts it again to load new code.
- **Socket verbs** (`wayfinder_main.py` `socket_listener`): `toggle`, `style`, `style:<x>`, `show`,
  `reset`, `quit`, `tab:<id>`. This is how the Qt tray and KDE shortcuts drive the app.
- Design tokens are ratchet-enforced (fonts/radii/colors/spacing via tokens; mirror COLORS/RADIUS/
  SPACING into `src/wayfinder/ui/theme.py`); no new sub-100ms self-rearming timers. See `CLAUDE.md`.

## Key file map
- `wayfinder_main.py` — main app (~13k lines). Pure helpers near the top: `kde_record_shortcut_active`
  (1878), `resolve_hotkey_backend` (1917), `should_host_qt_tray` (1948). Tray: `setup_tray` (12466).
  Hotkey/defer: `_compositor_owns_hotkeys` (13290), `_start_evdev_listener` (13313).
- `src/wayfinder/ui/overlay.py` — PyQt6 overlay **and** the Qt `QSystemTrayIcon` (tray block ~2185+,
  menu ~2265). sys.path bootstrap at top (28). QApplication metadata (2007).
- `trigger_record.py` — the script KDE's global shortcut runs → sends `toggle` to the socket.
- `~/.config/kglobalshortcutsrc` `[services][wayfinder-aura.desktop]` — KDE's F3/F2 bindings (Plasma 6).
