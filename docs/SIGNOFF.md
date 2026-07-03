# Wayfinder Aura — Ship Sign-off Matrix

Generated for **Item 3** of `docs/SHIP-PLAN-2026-07.md` (2026-07-02). Maps each
feature area to its automated coverage and to what still needs a human at the
keyboard. Rows marked **★ NEW** were added by the sign-off battery in this pass;
everything else pre-existed.

Run the automated suite (host python):

```
flatpak-spawn --host sh -c 'cd <repo> && python3 -m pytest tests/ \
  -o "addopts=-q --tb=short -p no:cacheprovider"'
```

Baseline before this battery: **972 passed**. After: **1031 passed, 6 skipped**
(the 6 skips are the live socket smoke tests — see below).

---

## Coverage matrix

| Feature area | Automated coverage (file :: class) | Still human-only |
|---|---|---|
| **Dictation pipeline (orchestration)** | **★ NEW `test_orchestration.py`** — the record→transcribe→paste path driven against a stub `self`: `on_record_button`/`on_hotkey` dispatch, `start_recording` (simple + chunked + remote-skip), `stop_recording_and_process`, `_finalize_chunked_transcription` (ordered combine, single post-process, overlap dedup, timeout, superseded-session drop), full happy-path state ladder IDLE→RECORDING→PROCESSING→PASTING→IDLE, transcription-failure recovery (no injection), empty/too-short/silent guards, injection handoff receives final text | The real `update_state` UI/tray/ducking side-effects (stubbed in tests); actual whisper/LLM output quality |
| Dictation pipeline (pure/core) | `test_state.py`, `test_e2e_flows.py`, `test_integration.py`, `test_transcriber.py`, `test_postprocessor.py`, `test_tone_eval.py`, `test_timeout.py` | Real transcription accuracy across accents/mics |
| **Audio / mic** | `test_recorder.py`, `test_recorder_rescan.py`, `test_warm_mic.py` (device resolve, silence guard, warm-mic healing) | Mic Test record/playback; ducking dips music + restores; real device hot-swap |
| **Hotkeys** | `test_hotkeys.py`, `test_single_instance.py` (evdev listener, exclusive-grab, gamemode release, capture/"Detect", single-instance socket handoff), `test_gamemode.py` | Physical F-key / mouse-remap presses; KDE global-shortcut conflicts; Steam Deck R4 trigger |
| **UI tabs / panels** | `test_components.py`, `test_e2e_ui.py`, `test_theme.py`, `test_palette_parity.py`, `test_icons.py`, `test_design_ratchet.py`, `test_welcome.py`, `test_dev_unlock.py` · **★ NEW `test_live_smoke.py`** exercises the `tab:*` socket switch against a live app | Actual rendering/scroll feel; the five inline-panel conversions; dropdown z-order on KDE (Item 1) |
| **Overlay** | `test_overlay.py`, `test_overlay_geometry.py`, `test_stdin_reader.py` (geometry, command drain, state) | Visual smoothness, focus-steal check, min-display timing on real Wayland |
| **Tray** | `test_mode_supervisor.py`, `test_game_mode_app.py` (supervisor/mode wiring) | Tray menu Open/Reset/Quit + icon states (needs a real tray host) |
| **License / Ultra** | `test_license.py`, `test_gpu_premium.py` (gating, activate path, premium features) | Real activate/deactivate against WFOS; "Get Ultra" opens checkout; Ultra glow visuals |
| **Models / benchmark** | `test_e2e_setup.py` (setup.py download engine) · **★ NEW `test_benchmark.py`** (`benchmark.py`: `benchmark_whisper_cpp` happy/timeout/fail, `to_config_format` config contract, suite helpers, test-audio synth) · **★ NEW `test_model_download.py`** (`ModelDownloader` + inline llama.cpp `_download_selected_llamacpp_model`/`_cancel_llamacpp_download`: progress, cancel-midstream, error, retry — all mocked network) | Actual multi-GB downloads over real network; live GPU-vs-CPU benchmark timings |
| **System integration** | `test_platform.py`, `test_config.py`, `test_config_unification.py`, `test_gpu.py`, `test_utils.py`, `test_version.py`, `test_injector.py` | ydotool inject into real apps; Flatpak/AppImage packaging; systemd autostart |

---

## Live socket smoke (`test_live_smoke.py`, `@pytest.mark.live`)

Skipped unless `WAYFINDER_LIVE=1` **and** a running app owns the control socket.
Sends `show` + `tab:dictate|settings|style|history` over
`/run/user/1000/wayfinder-aura/wayfinder-aura.sock`. The listener is
fire-and-forget (no reply protocol) — the test's ACK is a successful connect +
send that the listener consumes and closes. The reviewer confirms the resulting
tab switch / window raise via screenshots.

```
WAYFINDER_LIVE=1 python3 -m pytest tests/test_live_smoke.py -v   # app must be up
```

---

## Human keyboard checklist (owner, ~15 min — from SHIP-PLAN Item 4)

1. Dictate into a browser field, a terminal, and a chat app — accurate text,
   overlay never steals focus. One long dictation past the chunk boundary.
2. Cycle all 5 styles via hotkey; spot-check Strong + Caricature quality.
3. Mic Test record/playback; ducking dips music and restores.
4. Tray menu Open/Reset/Quit; icon states.
5. The five inline conversions: Clear-armed button (red "Really clear?" + 4s
   auto-revert), restart banner under Indicator Style, Groq/OpenAI API panel,
   Voice Profile panel, Cloud LLM panel.
6. Vocabulary: delete `cloak` → add `Claude`; `don` → `Daan`; dictate both names
   once; reopen panel later — edits stuck, bias works.
7. Scroll feel: Settings, Style, Whisper Models panel (post-fix smooth scroll).
8. Fresh-profile first run (after Item 2): wizard pane → welcome tour → first
   dictation. Real license activate/deactivate; "Get Ultra" opens checkout.
9. Steam Deck: Game Mode R4 trigger + Desktop Mode hotkey still work.

---

## Notes for reviewers

- The orchestration tests attach the **real** `WayfinderApp` methods to a stub
  `self`, so they are the production code paths — not reimplementations — and
  will fail if the orchestration regresses (state ladder, injection handoff,
  chunk combination order, stale-session drops are all asserted directly, not
  via mock-call counts).
- `benchmark.py` and `ModelDownloader` live at repo root / in `wayfinder_main.py`
  (not `src/wayfinder/core/`); the setup.py whisper downloader is separately
  covered by `test_e2e_setup.py`.
