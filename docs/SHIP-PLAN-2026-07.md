# Ship Plan — July 2026 polish/ship round

Status snapshot (2026-07-02, HEAD `bcb5dc6`, **972 tests green**): grey-overlay
boot fix, dead-popup purge (+dropdown token normalization), all 5 in-app popups
converted to inline UI (CI-ratcheted: no Toplevels outside ToolTip/
FloatingIndicator, no grab_set), editable + distinctiveness-scored + phrase-aware
vocabulary, wheel-scroll teleport fixed (Tk 9 compat shim, ratcheted).
The license UI Activate bug from June notes is ALREADY FIXED in current code
(`_activate_license` relies on online `store_license`, no HMAC pre-check).

## Ground rules (every item)

- Obey `CLAUDE.md` in full: design tokens only (`self.font_sizes`/`RADIUS`/
  `SPACING`/`COLORS` — `tests/test_design_ratchet.py` fails bare literals), NO
  new `CTkToplevel`/`grab_set` (CI-enforced), no repeating timers <100ms, no
  `unbind_all` in wayfinder_main.py (CI-enforced), standard CTk widgets,
  wrap Tk canvas ops in try/except.
- Tests run on the HOST python:
  `flatpak-spawn --host sh -c 'cd /var/home/bazzite/Dev/wayfinder-aura && python3 -m pytest tests/ -o "addopts=-q --tb=short -p no:cacheprovider"'`
  Baseline 972 passed — never fewer.
- Line numbers in this doc drift; locate by symbol.
- One reviewed commit per item. Do not push without review sign-off.
- Do not restart the user's running app; the reviewer handles live verification
  (launcher respawn recipe: kill the `main.py` python; the crash-restart
  launcher respawns it — systemd restart does NOT work for a standalone launcher).

## Item 1 — In-window dropdown panel (replaces the tk.Menu popup list)

**Why:** CustomTkinter's dropdown list is a native `tkinter.Menu`
(`DropdownMenu.open()` → `tk_popup` on Linux). It can NEVER have rounded
corners or a shadow, and on KDE/Wayland its stacking is WM-owned — the proven
root cause of the long-open mic-dropdown z-order bug (list appears behind /
detached). No workaround exists.

**What:** Keep every closed control exactly as it renders today. Replace only
the OPEN list: a `place()`'d rounded `CTkFrame` inside the main window
(`RADIUS["md"]`, `bg_surface`, `border_subtle` 1px border), rows with
`bg_hover` hover state and the selected row marked (accent text + check glyph
from `wayfinder.ui.icons` if a suitable icon exists, else a text ✓ with
whitelist comment). Behavior: opens under the control (flips above when the
window edge would clip; clamp within window bounds), dismisses on outside
click, Escape, wheel scroll outside the list, and window resize; long lists cap
height (~8 rows) and scroll inside a standard `CTkScrollableFrame`.
Implement behind `create_dropdown_row()` (wayfinder_main.py ~:9700s) so all 12
callers upgrade in one change; then migrate the 3 hand-rolled live
`CTkOptionMenu` sites (llama.cpp GGUF picker, OpenAI + Claude model pickers in
the postproc panel) onto the same helper. The setup-wizard dropdown is out of
scope here (Item 2 replaces that whole surface).

**Positioning math must be a pure module-level function** (input: control
x/y/w/h in window coords, list height, window w/h → output: panel x/y/h,
opens-upward flag) so it's unit-testable headless.

**Tests:** positioning-math unit tests (below-fit, flip-above, clamped, capped
height); source-parse ratchet asserting `create_dropdown_row` no longer
constructs `CTkOptionMenu` (or that no `tk_popup`-based dropdown remains
reachable from it); token compliance rides the existing ratchets.

**Out of scope:** arrow-key navigation (nice-to-have, only if trivial),
type-ahead, multi-select.

## Item 2 — SetupWizard → inline first-run flow (the LAST popup in the product)

**Why:** `SetupWizard` (`src/wayfinder/ui/dialogs/setup_wizard.py`, ~570 lines,
`CTkToplevel` + `grab_set` + `wait_window`) blocks in `main.py` (~:286) BEFORE
`mainloop()`. It is the only remaining popup and the new user's first
impression.

**What:** Show the main window immediately on first run with a setup pane
rendered inline OVER the tab content (same `place()`-over mechanic as the
welcome pane — see `src/wayfinder/ui/welcome.py` and the `_show_inline_panel`
pattern). The pane hosts the existing dependency checklist / install / model
download work (keep the threaded workers; marshal UI updates via one-shot
`self.after(...)`; keep the queue-poll cadence ≥100ms). Completion or skip sets
`setup_completed=True`, tears down the pane, and hands off to the welcome tour
if `welcome_completed` is false. Preserve: distro probes (apt/dnf/pacman),
whisper.cpp build, model download w/ progress, skip path, frozen-build bypass,
and the `.result` semantics main.py relies on (replace `wait_window` with a
completion callback).

**Startup ordering (trace before coding, preserve semantics):** what main.py
does between constructing the app and `mainloop()` today (hotkey listeners,
overlay spawn, mic warm-up) must not start doing new work before setup
completes if it doesn't today — match current behavior, document what you
found in the commit message.

**Tests:** flow state machine tests mirroring `tests/test_welcome.py`
(advance/skip/complete → `setup_completed`); extend the Toplevel ratchet in
`tests/test_design_ratchet.py` to ALSO scan `src/wayfinder/ui/**` once the
wizard Toplevel is gone (zero allowed there); existing e2e setup tests
(`test_e2e_setup.py`, 65 tests) stay green — the install/download engine in
`core/setup.py` must not change behavior.

**Fresh-run verification:** launch from source with a temp `XDG_CONFIG_HOME`
(or config path override) so `setup_completed` is false, screenshot the inline
first-run pane, complete/skip it, confirm hand-off. Reviewer assists with the
live screenshot loop.

## Item 3 — Automated sign-off battery (parallel-safe: touches tests/ only)

**Why:** the `src/wayfinder/` library is well covered; the ship-risky gaps are
the orchestration layer and engines with ZERO direct tests.

**What (new test files, no product-code edits; if a seam is genuinely needed,
propose it in the report instead of editing):**
1. **Record orchestration** — `on_record_button` / `start_recording` /
   `stop_recording_and_process` / `_finalize_chunked_transcription`
   (wayfinder_main.py): drive the unbound methods against a stub `self`
   (fake recorder/transcriber/injector/overlay/log), asserting state
   transitions, injection handoff, chunked finalize, and error paths
   (transcription failure → state recovery, no injection).
   `wayfinder.state.get_next_state` is the existing testable core — build on it.
2. **Benchmark engine** (`benchmark.py`, 44KB, zero tests): headless run with
   mocked backends; assert result shape written to `benchmark_results` /
   `gpu_benchmark_cache` config keys and that failure paths don't corrupt config.
3. **Inline model downloader** (`_download_selected_llamacpp_model` et al.):
   mocked-network progress / cancel-midstream / error / retry paths.
4. **Socket-driven live smoke** (document as a runnable script or pytest marked
   `@pytest.mark.live`, skipped by default): `show` + `tab:dictate|settings|style|history`
   against `/run/user/1000/wayfinder-aura/wayfinder-aura.sock`, asserting
   socket ACK; screenshots are the reviewer's job.

**Deliverable:** tests + a short `docs/SIGNOFF.md` matrix: feature → automated
coverage (file) → still-human-only.

## Item 4 — Human sign-off checklist (owner, ~15 min at the keyboard)

1. Dictate into a browser field, a terminal, and a chat app — accurate text,
   overlay never steals focus. One long dictation past the chunk boundary.
2. Cycle all 5 styles via hotkey; spot-check Strong + Caricature quality.
3. Mic Test record/playback; ducking dips music and restores.
4. Tray menu Open/Reset/Quit; icon states.
5. The five inline conversions: Clear-armed button (red "Really clear?" +
   4s auto-revert), restart banner under Indicator Style, Groq/OpenAI API
   panel, Voice Profile panel, Cloud LLM panel.
6. Vocabulary: delete `cloak` → add `Claude`; `don` → `Daan`; dictate both
   names once; reopen panel later — edits stuck, bias works.
7. Scroll feel: Settings, Style, Whisper Models panel (post-fix smooth scroll).
8. Fresh-profile first run (after Item 2): wizard pane → welcome tour → first
   dictation. Real license activate/deactivate; "Get Ultra" opens checkout.
9. Steam Deck: Game Mode R4 trigger + Desktop Mode hotkey still work.

## Sequencing

- Item 1 and Item 3 run in PARALLEL now (different files: Item 3 must not edit
  product code).
- Item 2 starts only after Item 1 is committed (both rework wayfinder_main.py).
- Reviewer (main session) gates each commit: diff review, full suite, live
  visual verification, then push.
