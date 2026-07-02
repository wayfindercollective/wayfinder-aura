# Wayfinder Aura

Local voice dictation for Linux. CustomTkinter UI + PyQt6 overlay + whisper.cpp + llama.cpp.

- Run: `python main.py`
- Test: `python3 -m pytest tests/ -v --tb=short`
- Architecture details: see `AGENTS.md`

## Project Rules (from 221 commits of hard-won lessons)

### 1. No timers or polling under 100ms
QTimer(0) caused 100% CPU. Overlay idle polling must be >=500ms, active transitions >=100ms. After any timer change, verify idle CPU stays under 2% (`top -p $(pgrep -f wayfinder)`).

**Sanctioned sub-100ms exceptions (grandfathered, CPU-validated <1%, enforced by the whitelist in `tests/test_design_ratchet.py`):** the overlay's 50ms command-drain timer (it *coalesces* the drain of a queue fed by an always-on stdin reader thread, applying only the last level update per tick — an event-per-line design would *increase* active-use wakeups) and the named animation loops (hero 66ms active / 33ms idle breath, tray pulse 50ms, mic-test meter 50ms). Any **new** sub-100ms self-rearming repeating timer fails CI.

### 2. No popup dialogs — use inline panels
Every popup (Whisper Models, Audio Calibration, Advanced Settings, Benchmark) took 6+ commits to fix sizing/display/focus bugs on Linux/Wayland. Always show/hide a `CTkFrame` within the main window instead of spawning `CTkToplevel`.

### 3. Single config source: `src/wayfinder/config.py`
`DEFAULT_CONFIG`, `load_config`, and `save_config` live ONLY in `src/wayfinder/config.py`; `wayfinder_main.py` imports them. This is now literally true — the legacy duplicate `DEFAULT_CONFIG`/`load_config`/`save_config` in `wayfinder_main.py` (which had caused silent drift) was deleted in 2026-07. When adding config keys, add them in one place only.

### 4. All new code goes in `src/wayfinder/`
Root-level files (`wayfinder_main.py`, `status_overlay.py`) are legacy. New modules go under `src/wayfinder/core/`, `ui/`, `utils/`, or `hotkeys/`. The only root exception is `main.py` as the entry point.

### 5. Test LLM prompts across all tones before committing
The `casual strong` tone alone took 4 fix commits (too much slang, no slang, light slang, gender-neutral). Before changing anything in `postprocessor.py`, test all 5 tones (minimal, professional, casual, dev, personal) at both standard and strong intensity. Check for self-annotations like "[Here's the cleaned version]".

### 6. Extend `clean_whisper_artifacts()`, don't add new cleanup layers
Whisper output has mixed-case corruption, hallucinated punctuation, and silence markers. All cleanup goes through the single `clean_whisper_artifacts()` function in `transcriber.py`. Adding separate cleanup stages creates a layered mess that's hard to debug.

### 7. Overlay state changes need forced repaint + minimum display time
State transitions (RECORDING -> PROCESSING) can complete in milliseconds. Users need to see feedback. Every `set_state()` must trigger an explicit repaint, and short-lived states (especially PROCESSING) need a minimum display duration (currently 800ms).

### 8. Use standard CustomTkinter widgets, not custom wrappers
`SmoothScrollableFrame`, custom button wrappers, and fancy subclasses were all replaced with standard `CTkScrollableFrame`, `CTkFrame`, `CTkButton`. Custom widgets introduce subtle bugs (bind parameter issues, constructor incompatibilities). Use the framework as-is.

### 9. Performance optimizations must preserve visual quality
One render optimization was reverted because it degraded overlay animations. When batching canvas operations or reducing frame rates, visually confirm animations still look smooth. The hero waveform renders at 30fps idle / 15fps active, and the overlay at 15fps — don't reduce these.

### 10. Wrap all Tk canvas operations in try/except
Tk 9.0 throws unexpected exceptions during rapid canvas redraws and window destruction. All `canvas.create_*`, `canvas.delete`, and `canvas.itemconfig` calls must be wrapped to prevent app crashes.

### 11. All font sizes / radii / colors / spacing go through design tokens
Use `self.font_sizes[...]`, `RADIUS[...]`, `SPACING[...]`, `COLORS[...]` — never bare `font=("Inter", 13)` or `corner_radius=12`. Tokens are mirrored in `src/wayfinder/ui/theme.py` (keep the two mirrors in sync). `tests/test_design_ratchet.py` enforces this: hardcoded font/radius literals fail CI unless they carry a sanctioned whitelist comment (`# optical glyph size` for single-glyph buttons and hero numerals, `# module-scope: no font_sizes access` / `# module-scope squircle: no matching token` for module-level helper classes outside `WayfinderApp`, `# pill: height/2` and `# mic meter: intentional` for the two off-ramp shapes). Icons come from `wayfinder.ui.icons` (tinted Lucide PNGs) — no emoji as UI chrome; log lines and the 😇/🎭 personality strings are the only sanctioned emoji.

## First Principles Checklist
Before any change, ask:
- **Single source of truth?** Don't duplicate state, config, or logic across files.
- **Simplest approach?** If you're writing a custom widget, timer, or abstraction — the standard one probably works.
- **Tested the matrix?** Tones, intensities, X11/Wayland, idle CPU — the bugs hide in the combinations.
- **Reversible?** If a perf optimization degrades UX, can you revert cleanly?
