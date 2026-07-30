# Changelog

All notable changes to Wayfinder Aura are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project aims to follow [Semantic Versioning](https://semver.org/).

## [1.1.3] — 2026-07-30

Field-driven reliability release: a full "act as a customer" session on a
Fedora Atomic (Bazzite) machine surfaced and fixed every barrier between
downloading the app and dictating with it. Independently reviewed (Codex,
3 rounds) and live-verified end to end. 1461 tests.

### Fixed

- **Startup crash on newer distros:** gi/GLib import failures (AssertionError
  on GLib 2.88 hosts) no longer kill the app; CI now bundles matched
  PyGObject + GI typelibs so portal shortcuts genuinely work.
- **Icons in frozen builds:** asset resolution is `sys._MEIPASS`-aware.
- **Immutable-distro setup:** Atomic hosts (`/run/ostree-booted`) never get
  `dnf` attempts; bundled runs no longer demand build tools or a system
  ydotool; guidance uses `rpm-ostree` wording.
- **Silent no-op typing:** the bundled ydotool client is gone (host-client-only
  policy — a mismatched client "types" into a device nothing reads and exits
  0); ydotool readiness and socket selection are connect-probed (DGRAM-aware);
  "backend unavailable" is surfaced as uncertain delivery instead of success,
  and Game Mode's paste retry is suppressed for it to prevent duplicates.
- **wtype refusals:** compositor refusal (GNOME always; KWin can revoke) now
  falls back to ydotool at injection time, with a session cache and a clear
  enable-hint when no fallback exists.
- **Model selection:** license-aware everywhere — Setup can't count an
  Ultra-gated weight for a free install, the wizard recommends free models to
  unlicensed users, the best installed usable model is auto-selected on load
  (stale AppImage-mount paths self-repair), and recording with no usable model
  is refused with a download CTA instead of typing nothing.
- **Overlay in packaged builds:** the app binary doubles as the overlay via an
  `--overlay-subprocess` sentinel (shipped builds previously had NO overlay);
  the forced CTk fallback (hard black edges on Wayland) is gone; the KWin
  placement script is position-only; persistent mode uses static window
  geometry with the pill painted centered (no more clipped edges); boot maps
  invisible until placement lands.
- **Mic test playback:** retries at the output device's native rate.
- **Whisper noise annotations:** "[audience noise]", "(clapping)" and the whole
  bracketed/parenthesized family are stripped; dictated brackets survive.
- **GPU detection:** the device probe finds the bundled/configured whisper-cli
  (AppImages on discrete-GPU machines reported "No discrete GPU").

### Added

- **One-click typing setup:** the Setup wizard self-provisions a user-level
  ydotoold service (host binary + `/dev/uinput` ACL + systemd --user; no
  root), reusing an existing user unit when present and verifying delivery by
  connecting to the exact socket it owns.
- **Auto press Enter after dictation** (opt-in, Settings → System): dictate →
  text lands → Enter fires — chat messages send hands-free.
- **New-install hotkey defaults:** Ctrl+Alt+Space (record) / Ctrl+Alt+Enter
  (style), with Super rendered as "⊞ Win"; Space and Enter join the hotkey
  dropdown. Existing configs keep their saved chords.
- **Overlay field debugging:** `WAYFINDER_OVERLAY_DEBUG=1` geometry traces and
  `WAYFINDER_OVERLAY_DEBUG_GRAB` buffer dumps.
- **Release guardrails in CI:** the AppImage job fails loudly if Vulkan
  silently fell back to CPU (spirv-headers now installed), if gi/dbus or GI
  typelibs are missing from the bundle, or if a ydotool client sneaks back in.
- **Vulkan GPU inference in the Flatpak:** the long-standing "toolchain bug"
  (undefined matmul_id_subgroup_* at link) was actually fork-ENOMEM during
  shader generation on CI runners; with always-overcommit + swap headroom in
  CI, the primary whisper/llama modules build GGML_VULKAN=ON — the free-tier
  GPU-on-Tiny/Base promise now holds on Flathub. CPU fallback binaries remain
  for machines with broken Vulkan.

## [Unreleased]

Ship-readiness verification pass — defects found by a code audit + model review
and fixed test-first, plus test infrastructure to guard the fixes. The full
gating suite is green.

### Fixed

- **Wedged-chunk salvage:** a wedged transcription chunk now salvages against a
  correctly resolved whisper-cli binary (a real in-module resolver, not a blind
  string replace), so a server-only install recovers the chunk instead of
  silently dropping its words.
- **Chunk-drop traceability:** a failed chunk save is now counted and surfaced
  as an activity-log warning, so an intermittent missing section in a long
  dictation is traceable instead of an invisible hole.
- **GPU state race:** the GPU retry/CPU-fallback state shared between the chunk
  workers and the GPU-probe thread is now serialized under a lock, and the retry
  backoff exponent is clamped (fixes a crash on a pathologically flapping GPU).

### Changed

- **GPU upsell nudge:** the free-tier "try GPU" nudge after a long CPU dictation
  is now gated on cached GPU detection — suppressed on GPU-less machines, still
  offered on the Steam Deck's integrated GPU.

### Added

- **Golden-audio ASR harness:** a deterministic, synthetic (license-clean)
  golden-audio corpus + generator and an opt-in accuracy test that transcribes
  it with the real whisper-cli and asserts per-clip Word Error Rate ceilings and
  key-phrase survival.
- **Soak / leak-detection harness:** a standalone tool that hammers the real
  transcription → post-processing pipeline for many iterations and watches for
  whisper-server / llama subprocess leaks and orphaned processes.
- Characterization tests pinning the adaptive request-timeout behavior, and a
  `perf` test marker so load-sensitive wall-clock tests run locally/nightly
  instead of flaking the gating run.

## [1.1.0] - 2026-06-10

First public release of Wayfinder Aura.

### Added

- Local voice dictation with whisper.cpp — hotkey-triggered, on Wayland and X11.
- Steam Deck support: SIGILL-safe CPU build, R4 trigger socket, system tray,
  and font/scaling fixes.
- Stable microphone selection by device name — survives PipeWire device
  renumbering.
- Game-aware hotkeys: dictation keys pause while a Game Mode game is running.
- Default hotkeys **Super+F2 / Super+F3** to avoid in-game F-key collisions.
- Tone-aware local post-processing with Gemma 3 1B as the default model.
- System tray integration and configurable typing speeds.

[Unreleased]: https://github.com/wayfindercollective/wayfinder-aura/compare/v1.1.0...HEAD
[1.1.0]: https://github.com/wayfindercollective/wayfinder-aura/releases/tag/v1.1.0
