# Linux follow-ups found during the macOS pass

Written on the `macos` branch (2026-09-23). **Nothing here was changed on Linux.**
Every macOS change on this branch is behind a `darwin` check or lives in a
macOS-only module; Linux/Windows behaviour matches `main` (the hero ribbon and
overlay wave renderers were verified byte-for-byte). Each item below was
observed or measured on macOS and very likely applies to Linux too. A Linux
agent should reproduce each one on real Linux hardware (KDE Wayland, X11, Steam
Deck) before changing anything.

Legend — **Evidence** is what was seen on macOS; **Linux check** is how to
confirm it on Linux; **Fix sketch** is the macOS fix, for reference.

---

## 1. Speech/cleanup pipeline

### 1.1 Local LLM cleanup does no cleanup and costs ~2 s per dictation
- **Evidence:** `LlamaCppCliBackend` runs `llama-simple` with a raw prompt
  (`postprocessor.py` ~L2990-3016), reloading the model per call. With the
  shipped Qwen3.5-2B-Q4_K_M it takes 1.9-2.3 s and returns the input
  unchanged: raw completion skips the model's chat template, so the model
  emits an empty `<think></think>` and echoes the text ("um" kept, no
  capitalisation). Same model resident in `llama-server` with `--jinja` and
  `chat_template_kwargs: {"enable_thinking": false}`: **0.17 s** (short) /
  **0.52 s** (two sentences), correctly cleaned.
- **Linux check:** enable post-processing (minimal), dictate "um so send the
  report to maria by friday" and time it; check the output actually changed.
  Linux already has a resident `llama_server.py` path — confirm which path the
  default config takes (`llama_cpp_use_cli: True`) and whether it applies the
  chat template / disables thinking.
- **Fix sketch:** prefer the resident server with the chat template, pass
  `enable_thinking: false` for Qwen3.x, keep `llama-simple` only as fallback.

### 1.2 "Minimal" style runs the LLM even though a regex does the job
- **Evidence:** `fast_filler_removal` defaults to `False` (`config.py` L417),
  so minimal cleanup pays the full LLM cost. Whisper output is already
  capitalised/punctuated.
- **Fix sketch:** default minimal to the regex path; use the LLM for tones.

### 1.3 The resident whisper-server keeps its first-session thread count
- **Evidence:** warm-up starts the server before first-run thread auto-tuning;
  `_server_reusable()` compares model and GPU mode but not `-t`, so the first
  session keeps 4 threads. On macOS base.en CPU: 4 threads 0.40 s vs 12
  threads 0.22 s for a 10 s clip.
- **Linux check:** fresh config, start app, check `ps` for `whisper-server -t`.
- **Fix sketch (macOS):** record `_server_threads` at spawn and compare it
  (`transcriber.py`, darwin-guarded).

### 1.4 whisper-server readiness waits 0.5 s before its first probe
- **Evidence:** warm server is ready in ~0.1-0.2 s; the fixed 0.5 s sleep is
  pure latency on every start/restart (warm-up, GPU toggle, model change,
  recovery). macOS now polls every 0.1 s.

### 1.5 Speech models the license cannot load counted as "usable"
- **Evidence:** `_has_usable_whisper_model()` accepted any known GGML model on
  disk. A Free install with a gated model (e.g. `small.en` in
  `~/whisper.cpp/models`) skipped the Base download, while config repair never
  points Free at a gated model → dictation failed with a missing model.
- **Linux check:** Free license, place only `ggml-small.en.bin` in
  `~/whisper.cpp/models`, fresh config → does Setup/Welcome offer Base?
- **Fix sketch (macOS):** apply `transcription_model_allowed()` to each
  candidate (`wayfinder_main.py`, darwin-guarded).

### 1.6 Misleading "whisper-server binary not found" message
- **Evidence:** `WhisperServerBackend.is_available()` also requires the model
  file, so a missing model logs "binary not found" (`transcriber.py` ~L2317).

### 1.7 CPU mode may still initialise the GPU backend (check Vulkan)
- **Evidence (Metal):** with `-ng`, ggml still registered Metal: a 7-8 s shader
  compile after every install/update and a residency heartbeat waking the idle
  server ~170x/s. `GGML_METAL_DEVICES=0` fixed it with identical output.
- **Linux check:** on a Vulkan build run `whisper-server -ng` idle and watch
  wakeups (`perf`/`powertop`) and first-start time; if Vulkan initialises, try
  `GGML_VK_VISIBLE_DEVICES=` (empty) for CPU mode.

## 2. Responsiveness

### 2.1 The 800 ms minimum "processing" display swallows the next hotkey
- **Evidence:** after text is inserted, `_finish_injection` is delayed up to
  800 ms (`wayfinder_main.py` ~L20932); the app is not IDLE, so an immediate
  second dictation is ignored. Text itself is not delayed.
- **Fix sketch:** let a new recording pre-empt the pending finish job.

### 2.2 Overlay handshake and UI work run before the mic starts
- **Evidence:** `start_recording` does window probing, `update_state` and a
  critical overlay `show` (reply awaited, up to 3 x 250 ms) before
  `recorder.start()`; audio before that is lost with a warm mic. Same on stop.
- **Linux check:** time hotkey→first captured frame; KDE focus-steal
  constraints may explain the order on Linux — validate before changing.

### 2.3 Hotkey → action waits for the Tk poll (up to 250 ms idle)
- **Evidence:** the listener thread only enqueues; the Tk loop drains every
  250 ms idle / 100 ms active. A self-pipe + `createfilehandler` wakes Tk
  immediately (works on Linux Tk too).

## 3. Idle CPU / energy

### 3.1 The hero waveform animates at 30 fps even when nobody can see it
- **Evidence (macOS):** main process 5.5% CPU idle on the Dictate tab vs 1.8%
  with the hero paused (Settings tab). The window was in the background.
- **Linux check:** measure idle CPU with the window unfocused / covered /
  minimized (CLAUDE.md target: < 2%).
- **Fix sketch:** pause or slow the idle breath when the window is not focused
  or is occluded; resume on focus.

### 3.2 The always-on overlay pill costs ~2-3% CPU idle
- **Linux check:** measure the overlay process idle; consider a lower idle
  frame rate once the pill has been idle for a few seconds.

## 4. Reliability

### 4.1 Orphaned whisper-server after a crash (Linux has no supervisor)
- **Evidence:** this branch keeps Linux's "reuse our own server" behaviour
  because Linux has no child supervisor. Adopting a listener by probing
  `/inference` is a (local) trust risk; not adopting stacks servers per crash.
- **Fix sketch:** `PR_SET_PDEATHSIG` on the server spawn (or a pidfile + kill
  of our own stale server), then stop adopting unowned listeners.

### 4.2 Tests write into the real user profile
- **Evidence:** the suite wrote "RuntimeError: boom" tracebacks into the real
  activity/app log; config/socket paths are built from the real HOME at import.
- **Fix sketch:** isolate HOME/XDG dirs in `tests/conftest.py`.

### 4.3 Update check is not platform-aware
- **Evidence:** `app_updates.py` notifies whenever a newer tag exists. v1.1.8
  ships only Linux assets, so a Mac user would be told to update to a release
  with no Mac download — and a future Mac-only hotfix would nag Linux users.
- **Fix sketch:** only notify when the release has an asset for this platform
  (`.AppImage`/`.flatpak` on Linux, `-macOS-arm64.dmg` on macOS) and link the
  asset directly.

## 5. UI

### 5.1 Welcome card clips in narrow windows (Steam Deck right-half = 640 px)
- **Evidence:** the fixed 520x360 welcome card is centred in the tab area; in
  a ~640 px window it overflows every edge (title hidden, line starts cut).
- **Fix sketch (macOS):** `fit_card_size()` + `_fit_card()` in `welcome.py`
  (darwin-gated) shrink the card, re-wrap copy and pin the top-left in view.

### 5.2 Emoji used as UI chrome (breaks rule 11)
- "Style  🔒" sidebar label, "🎤 Auto-detect (Recommended)" microphone option,
  "🔒 … is a Wayfinder Ultra feature." prompt subtitle.
- **Fix sketch (macOS):** `assets/icons/lock.png` (Lucide lock) replaces the
  pen icon for a locked Style tab; emoji dropped from the strings.

### 5.3 Dropdown panels may clip their longest row
- **Evidence (Aqua):** rows inset text ~20 px more than the width budget in
  `dropdown_panel_geometry` callers assumed. Check long device/model names on
  KDE at 1x and 2x.

## 6. Shared changes already on this branch that Linux should validate

The audit accepted these low-risk shared changes (they run on Linux after the
merge); a Linux pass should confirm each behaves well:

- **Escape cancels a recording** from the main window (+ socket verb `cancel`)
  — the owner wants this; test Escape during recording, processing and idle.
- Nested list scrolling hands off to the Settings scroller at its edge.
- Model catalog: cached copy at startup, remote refresh in the background.
- pystray (Linux fallback tray): Model/Microphone submenus + device list.
- Overlay: wave timing uses real elapsed time; the pill quits on stdin EOF.
- Benchmark hardware info loads off-thread; mic changes blocked while
  recording; overlay-change restart also restarts whisper-server/mic/ducking.
- Flatpak-X11 hotkey fallback: unknown key code falls back to Ctrl+Alt+Space,
  key-repeat latch, modifier hot-reload.
- New config keys (`macos_hotkey_defaults_v2`, `macos_overlay_anchor_defaults_v1`,
  `enable_tray_icon`) are written into Linux configs (additive).
- AppImage whisper-cli resolution prefers the bundled binary over a stale
  configured path.
