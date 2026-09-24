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

### 1.8 The resident llama-server is reachable by any web page
- **Evidence:** llama-server answers CORS from every origin and, with no API
  key, accepts `POST /completion` from any site the user visits (the page can
  read the reply). `/slots` is on by default and can expose the cached prompt,
  i.e. the last dictation.
- **Fix sketch (macOS):** a random key per spawn in the child's environment
  (`LLAMA_API_KEY`, never argv), `Authorization: Bearer` on our requests, and
  `--no-slots` (`llama_server.py`, darwin-gated).

### 1.9 whisper-server computes token timestamps nobody reads
- **Evidence:** requests send only file/response_format/prompt, so the server
  computes per-token timestamps. `no_timestamps=true`: 3-14% faster, same WER
  (base.en, M3 Ultra). Shared request code; Linux would gain the same.

### 1.10 Cleanup model quality
- **Evidence (server path, all 5 tones):** Gemma 3 1B (the recommended
  default) cleans well; Qwen 3.5 2B barely changes text with or without the
  chat template (it Title-Cases whole sentences in professional). If Linux
  users have Qwen 3.5 selected, suggest Gemma.

### 1.11 Cleanup models graded per style (matrix 2026-09-24)
- **Evidence (macOS, resident llama-server, 22 samples x 5 styles x 2
  strengths):** Qwen3 4B graded A on every style (~0.3 s). Gemma 3 1B
  rewrote meaning — "diff" -> "difference", "auth module" -> "authentication
  module", "a couple more days" -> "two more days" — failing Dev and
  Professional (F), Casual/Personal (C), even Normal (C). Qwen 3.5 2B echoed
  its input (fillers kept; Professional F). Full tables: `docs/EVAL-2026-09-24.md`.
- **Linux check:** re-run `scripts/eval_matrix.py --stages tone` on the
  Linux cleanup path (llama-simple CLI vs server, §1.1) before relying on
  these grades; the prompt path differs.
- **Done on `feature/styles-vocabulary` (cross-platform):** STYLE_SUPPORT
  greys out styles a model fails, with the reason; runtime falls back to
  Standard/Normal instead of producing a bad result.

### 1.12 `fast_filler_removal` deletes words that carry meaning
- **Evidence:** its patterns remove every "right" (`turn right` -> `turn`),
  bare "you know" (`do you know him` -> `do him`), "actually", "honestly",
  "kind of", and collapse legitimate repeats ("had had", "that that").
- **Done on `feature/styles-vocabulary`:** Normal uses a conservative
  `normal_filler_removal()` (filler sounds + doubled small words only);
  the aggressive regex stays behind its explicit toggle.

### 1.13 Bigger speech models write more fillers down
- **Evidence (66 spoken clips):** Large v3 Turbo Q5 kept a filler in 27
  clips, Medium 21, Small.en 18, Base 3. Ultra users need filler removal
  more than Free users, not less.

### 1.14 Custom vocabulary was cut first (whisper keeps the prompt's tail)
- **Evidence:** user terms were placed before the built-in Dev/Casual lists
  and whisper.cpp keeps only the last ~224 prompt tokens; the server path
  also sliced the first 50 terms. **Done on `feature/styles-vocabulary`**
  (user terms last, 560-char budget; "heard -> write" corrections).

### 1.15 The live remote model catalog is stale
- **Evidence:** `.../v1/catalog` is from 2026-07-11: rounded sizes (every
  shipped entry is rejected as "weakens its size") and Small/Tiny listed
  as Free. Republish it from `catalog/v1.json`
  (`scripts/publish_model_catalog.py`). Until then its descriptions also
  override the shipped ones.

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

**Done on macOS (for reference):** self-pipe wake-up (`ui/macos_tk_wakeup.py`),
capture-before-overlay in `start_recording`, and a press during the 800 ms tail
pre-empts it (`on_hotkey`). All darwin-gated.

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

### 3.3 CustomTkinter polls DPI every 100 ms for nothing
- **Evidence (sampled on macOS):** `ScalingTracker.check_dpi_scaling` re-arms
  `after(100)` forever and calls `winfo_exists()` + `wm state` on every CTk
  window: 10 wakeups/s. Its DPI query returns a constant 1 on Linux ("not
  implemented") as on macOS, so it can never detect anything.
- **Fix sketch (macOS):** `ctk.ScalingTracker.update_loop_interval = 3_600_000`
  right after importing customtkinter (`wayfinder_main.py`, darwin-gated).

### 3.4 Wave animation judders when frames arrive unevenly
- **Evidence (macOS):** the Metal waves were driven by a 30/15 fps timer on the
  main loop: not aligned to the display, and skipped whenever the main thread
  was busy. Motion advanced per *tick*, so an uneven tick became visible
  judder; the pill's level also stepped at the ~20 Hz update rate.
- **Linux check:** watch the hero and pill while the app transcribes or the
  window is dragged; log tick intervals of the `after()` loops.
- **Fix sketch (macOS):** a display-synced render thread (`wf_render_clock.m`),
  wave time taken from each frame's display timestamp, per-second easing
  (`1 - k**(dt*30)` instead of a per-frame factor), and eased audio level.
  The Linux analogue: compute wave time from `time.monotonic()` and make the
  easing dt-based, so a late frame shows the right phase instead of lagging.

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

### 4.4 Loopback requests go through `http_proxy`
- **Evidence:** `urlopen` to 127.0.0.1 honours `http_proxy` unless `no_proxy`
  lists it, so dictation audio and cleanup text are sent to the proxy.
- **Fix sketch (macOS):** `utils/loopback_http.py` (proxy-free opener).

### 4.5 API keys are stored and handled loosely
- Plain text in `config.json`; the temp file is created 0644 before chmod
  (brief world-readable window); the config dir is not 0700; every native
  child (whisper/llama servers) inherits the keys through `os.environ`.
- **Fix sketch (macOS):** Keychain + owner-only writes + env scrub
  (`utils/macos_keychain.py`, `config.save_config`, `hostexec.bundle_binary_env`).
  Linux equivalent: Secret Service (libsecret) with the 0600 file as fallback.

### 4.6 Retired / retiring cloud models (affects Linux today)
- **Every Anthropic model offered is retired**, including the default
  `claude-3-haiku-20240307` (retired 2026-04-20): Anthropic cleanup fails on
  every request. Replacements: `claude-haiku-4-5-20251001`, `claude-sonnet-4-6`.
- `gpt-4-turbo` / `gpt-3.5-turbo` shut down 2026-10-23; `whisper-1` is
  deprecated (shutdown 2027-02-26, replacement `gpt-transcribe`).
- **Fix sketch (macOS):** `core/cloud_keys.py` model lists + a config
  migration of retired IDs (darwin-gated in `config.py`).

### 4.7 Cloud key panels
- A saved key cannot be removed (empty field + Save keeps it).
- Saving cleanup settings marks the Groq/OpenAI transcription row
  "Configured" if any cleanup key exists; the Anthropic label shows "3: ✓".
- "Set ANTHROPIC_API_KEY env var" wording is stale (keys are in config).
- No way to verify a key; paste via Ctrl+V keeps stray whitespace.
- **Fix sketch (macOS):** `_build_macos_key_help` (Verify via each
  provider's models endpoint, redirects refused, Remove key, format hints).

### 4.8 Auto-Enter focus guard is blind on Wayland
- `get_active_window()` uses xdotool; on native Wayland it returns None, so
  "focus changed → skip Enter" never fires. (macOS now uses NSWorkspace.)

### 4.9 Dictations land in clipboard history
- The paste path puts the dictation on the clipboard without a "transient"
  hint, so Klipper/CopyQ record every dictation (and the restore). KDE honours
  `x-kde-passwordManagerHint: secret`; CopyQ honours `application/x-copyq-hidden`.

### 4.10 No sleep inhibit during a dictation or a model download
- Idle sleep mid-memo cancels the recording; a slept download restarts from
  zero. Linux: `systemd-inhibit`/portal Inhibit for the same windows.

### 4.11 Misleading logs
- "✅ Transcription model loaded" is logged even when no model exists
  (`warm_up_transcription` now returns whether a server started; the UI only
  uses it on macOS). Concurrent startup logging writes several
  "session start" headers.

### 4.12 Warm mic keeps Bluetooth headsets in call-quality mode for 30 s
- macOS now releases Bluetooth mics after 1 s and others after 10 s.

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

### 5.4 "Finish Setup" (no-model cue) seems to do nothing
- **Evidence (macOS, likely identical on Linux):** `_open_setup_from_cue`
  opens the model panel inside `mode_settings_container`, mid-way down the
  Settings scroll, while the view stays at the top (Audio). Users read it as
  a dead button.
- **Fix sketch (macOS, darwin-gated):** `_scroll_settings_to(container)`
  moves the Settings canvas so the panel is at the top after layout.

### 5.5 Feedback form posts to an endpoint that isn't deployed
- **Evidence:** `POST https://fine-shrimp-886.convex.site/api/aura/feedback`
  answers 404 "No matching routes found" (checked 2026-09-23). The Linux
  v1.1.8 form shows "Couldn't send feedback (HTTP 404)"; nothing is stored.
- **Server code exists, unmerged:** Wayfinder-OS PR #151,
  branch `feat/aura-feedback-dev` ("desktop feedback ingestion + Slack
  notify"). Needs merge + prod deploy + the Slack webhook env var. No client
  change needed.

### 5.6 Fixed wraplengths clip text in narrower cards
- **Evidence (macOS):** ~25 labels use fixed `wraplength` values (430-560 px)
  sized for the 800 px Linux window. In a narrower card the label requests
  more width than it has and Tk centres the overflow, clipping both edges
  (Benchmark description, "No results yet" line).
- **Linux check:** Steam Deck right-half (640 px), KDE at 1.25-1.5x, and the
  narrowest window the app allows.
- **Fix sketch (macOS):** `ui/macos_label_fit.py` clamps every stacked
  label's wraplength to its container (min of the designed value and the
  width minus padding) on `<Map>`/`<Configure>`.

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
