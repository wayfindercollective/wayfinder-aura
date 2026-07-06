# Changelog

All notable changes to Wayfinder Aura are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project aims to follow [Semantic Versioning](https://semver.org/).

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
