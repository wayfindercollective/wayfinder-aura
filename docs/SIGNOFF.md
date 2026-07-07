# Wayfinder Aura — Ship Sign-off Matrix

Generated for **Item 3** of `docs/SHIP-PLAN-2026-07.md` (2026-07-02). Maps each
feature area to its automated coverage and to what still needs a human at the
keyboard. Rows marked **★ NEW** were added by the sign-off battery in this pass;
everything else pre-existed.

Run the automated suite. If host Python has Tk/PortAudio installed, a normal
venv works. On Bazzite/SteamOS hosts where host Python lacks those bindings,
use the installed Flatpak runtime for the app dependencies and a pytest-only
helper venv:

```
cd /var/home/bazzite/Dev/wayfinder-aura
python3 -m venv .tmp-pytest-only
.tmp-pytest-only/bin/python -m pip install -q pytest pytest-timeout pytest-mock pytest-cov pyyaml packaging ruff pillow

flatpak-spawn --host flatpak run --command=sh \
  --filesystem=/var/home/bazzite/Dev/wayfinder-aura:rw \
  --env=PYTHONPATH=/var/home/bazzite/Dev/wayfinder-aura/.tmp-pytest-only/lib/python3.13/site-packages:/var/home/bazzite/Dev/wayfinder-aura/src \
  io.wayfindercollective.WayfinderAura \
  -c 'cd /var/home/bazzite/Dev/wayfinder-aura && env -u FLATPAK_ID -u WAYFINDER_FLATPAK python3 -m pytest -q -o "addopts=-q --tb=short -p no:cacheprovider" tests'

rm -rf .tmp-pytest-only
```

Current verified baseline after the 2026-07-07 pre-ship audit:

- Host audit venv: **1141 collected with 9 collection-time skips; 1108 passed; 42 skipped**.
- Installed Flatpak runtime with current source mounted into the app runtime:
  **1276 collected; 1263 passed; 13 skipped**.

The skips are opt-in live/hardware or golden-audio checks. Additional verified
layers in this audit: live socket smoke **7/7 passed**, golden ASR **5/6 passed
then **6/6 passed** after mounting the host turbo model into the installed
Flatpak build on the AMD RX 9060 XT, perf **2/2 passed**, and Flatpak
in-process soak smoke **passed** after hardening warm-server silence
hallucination cleanup.
The local Flatpak manifest also clean-built/exported, was installed from the
local audit repo, and passed package-level `/app` smoke plus golden ASR and soak
smoke against the installed artifact. Lite and full CPU-fallback AppImages were
built locally; the full artifact extraction smoke verified executable
`whisper-cli`, `llama-cli`, `llama-simple`, `wtype`, and `ydotool` plus clean
desktop/AppStream metadata. The AppImage builder now copies the same desktop and
screenshot-bearing AppStream metadata used by the Flatpak package. Those
AppImage native binaries were verified on the Bazzite build host (`glibc 2.43`).
The GitHub AppImage job is now pinned to
`ubuntu-22.04`, installs Vulkan development packages, builds pinned Shaderc
`glslc` from source if the runner does not provide it, and extraction-smokes the
full artifact before upload; broad AppImage distribution still needs a real tag
or workflow-dispatch run proving that CI artifact.

---

## Coverage matrix

| Feature area | Automated coverage (file :: class) | Still human-only |
|---|---|---|
| **Dictation pipeline (orchestration)** | **★ NEW `test_orchestration.py`** — the record→transcribe→paste path driven against a stub `self`: `on_record_button`/`on_hotkey` dispatch, `start_recording` (simple + chunked + remote-skip), `stop_recording_and_process`, `_finalize_chunked_transcription` (ordered combine, single post-process, overlap dedup, timeout, superseded-session drop), full happy-path state ladder IDLE→RECORDING→PROCESSING→PASTING→IDLE, transcription-failure recovery (no injection), empty/too-short/silent guards, injection handoff receives final text | The real `update_state` UI/tray/ducking side-effects (stubbed in tests); actual whisper/LLM output quality |
| Dictation pipeline (pure/core) | `test_state.py`, `test_e2e_flows.py`, `test_integration.py`, `test_transcriber.py`, `test_postprocessor.py`, `test_tone_eval.py`, `test_timeout.py` | Real transcription accuracy across accents/mics |
| **Audio / mic** | `test_recorder.py`, `test_recorder_rescan.py`, `test_warm_mic.py` (device resolve, silence guard, warm-mic healing) | Mic Test record/playback; ducking dips music + restores; real device hot-swap |
| **Hotkeys** | `test_hotkeys.py`, `test_single_instance.py` (evdev listener, exclusive-grab, gamemode release, capture/"Detect", single-instance socket handoff), `test_gamemode.py`, `test_steamdeck_scripts.py` (Deck host trigger daemon stays socket-based; legacy r4-f3 bridge disabled by default) | Physical F-key / mouse-remap presses; KDE global-shortcut conflicts; real Steam Deck R4 trigger |
| **UI tabs / panels** | `test_components.py`, `test_e2e_ui.py`, `test_theme.py`, `test_palette_parity.py`, `test_icons.py`, `test_design_ratchet.py`, `test_welcome.py` · **★ NEW `test_live_smoke.py`** exercises the `tab:*` socket switch against a live app | Actual rendering/scroll feel; the five inline-panel conversions; dropdown z-order on KDE (Item 1) |
| **Overlay** | `test_overlay.py`, `test_overlay_geometry.py`, `test_stdin_reader.py` (geometry, command drain, state; overlay bare-script import is covered) | Visual smoothness, focus-steal check, min-display timing on real Wayland |
| **Tray** | `test_mode_supervisor.py`, `test_game_mode_app.py`, `test_hotkeys.py`, `test_live_smoke.py` (Qt-tray socket verbs, ping health, tab switching through the live socket) | Tray icon rendering and menu Open/Reset/Quit clicks on the real shell after relaunch |
| **License / Ultra** | `test_license.py`, `test_gpu_premium.py` (gating, activate path, premium features) | Real activate/deactivate against WFOS; "Get Ultra" opens the browser-verified checkout; Ultra glow visuals |
| **Models / benchmark** | `test_e2e_setup.py` (setup.py download engine) · **★ NEW `test_benchmark.py`** (`benchmark.py`: `benchmark_whisper_cpp` happy/timeout/fail, `to_config_format` config contract, suite helpers, test-audio synth) · **★ NEW `test_model_download.py`** (`ModelDownloader` + inline llama.cpp `_download_selected_llamacpp_model`/`_cancel_llamacpp_download`: progress, cancel-midstream, error, retry — all mocked network) | Actual multi-GB downloads over real network; live GPU-vs-CPU benchmark timings |
| **System integration** | `test_platform.py`, `test_config.py`, `test_config_unification.py`, `test_gpu.py`, `test_utils.py`, `test_version.py`, `test_injector.py`, `test_release_metadata.py`, `test_flatpak_release_manifest.py`; local Flatpak build/export, guarded tag-sourced CI Flatpak path, lite/full CPU-fallback AppImage build smokes, and the guarded older-glibc/GPU-native CI AppImage path were verified in the 2026-07-07 audit | ydotool inject into real apps; real tag-sourced Flatpak artifact run; tag/workflow-dispatch AppImage artifact plus sign/upload if promoted; systemd autostart |

---

## Live socket smoke (`test_live_smoke.py`, `@pytest.mark.live`)

Skipped unless `WAYFINDER_LIVE=1` **and** a running app owns the control socket.
Sends `ping` plus `show` + `tab:dictate|settings|style|history` over
`/run/user/1000/wayfinder-aura/wayfinder-aura.sock`. `ping` must reply `pong`;
the other commands are fire-and-forget, then the test polls the live status
breadcrumb to prove the app actually switched tabs. Current 2026-07-07 run
against the restarted source app passed **7/7**. A later host-namespace rerun
from the Flatpak-hosted editor environment also passed **7/7** after correcting
the preflight helper to ping the host-visible socket through `flatpak-spawn`.

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
   dictation. Real license activate/deactivate; "Get Ultra" opens the
   browser-verified checkout.
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
