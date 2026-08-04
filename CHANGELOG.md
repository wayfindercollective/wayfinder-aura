# Changelog

All notable changes to Wayfinder Aura are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project aims to follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [1.1.8-beta.1] — 2026-08-04

### Added

- **Chunk Processing now supports Off, Auto, and On.** Auto is the first-run
  Ultra default: recordings under 30 seconds remain one Whisper request, while
  longer sessions begin with a 30-second segment and continue in the tested
  15-second/2-second-overlap profile. Existing Off/On preferences are preserved.

### Changed

- **New installs start with LLM post-processing disabled.** Raw Whisper output
  is the fastest and least surprising first experience; cleanup and Style
  transformations remain opt-in. Existing installs retain their saved choice.

### Fixed

- **Packaged Vulkan inference no longer inherits PyInstaller's private library
  directory.** On AMD this reduced Turbo Q5 transcription from roughly eight
  seconds to under half a second and also accelerated local Gemma cleanup.
- **Whisper server recovery can no longer starve its CLI salvage path.** A
  failed restart gets a fresh bounded fallback window instead of returning an
  empty transcription after the server deadline expires.
- **AppImage inference paths follow the current mount.** Saved paths from a
  disconnected prior AppImage no longer break downloads, benchmarks, Whisper,
  or local cleanup with `Transport endpoint is not connected`.
- **Opening and closing inline Settings panels no longer multiplies row
  spacing at HiDPI scales.** CustomTkinter pack padding is restored in logical
  units instead of being scaled twice.
- **The overlay remains above the Plasma panel at low screen positions.** KDE
  Wayland placement now explicitly restacks Aura after positioning, while the
  Flatpak's XWayland path restacks after native geometry changes.
- **The overlay position control reaches the physical screen bottom.** Its
  transparent glow may extend off-screen so the visible pill no longer stops
  short of the selected edge.
- **Custom vocabulary survives cleanup in every writing style.** Minimal,
  Professional, Casual, Dev, and Personal now protect user-provided spelling
  through both full prompts and the packaged `llama-simple` cleanup path.

## [1.1.7] — 2026-08-01

### Fixed

- **Model downloads now verify HTTPS correctly in portable Linux packages.**
  AppImage and Flatpak builds explicitly bundle and select a current CA trust
  store before license, catalog, cloud, or model-download traffic begins. This
  fixes `CERTIFICATE_VERIFY_FAILED` on Bazzite/Fedora and SteamOS without
  weakening TLS verification.
- **AppImage startup no longer depends on tmpfs capacity.** The large
  PyInstaller payload extracts into Wayfinder Aura's private, disk-backed user
  cache, avoiding failures when `/tmp` is a small RAM-backed filesystem.
- **Release artifacts prove real certificate trust before publishing.** CI now
  performs a verified production-CDN handshake from the finished AppImage and
  installed Flatpak, and rejects AppImages missing their CA bundle.

## [1.1.6] — 2026-07-31

### Fixed

- **AppImage dictation now records through the desktop PipeWire graph.** Frozen
  builds prefer the host's killable `pw-record` client instead of opening a raw
  ALSA device already owned by PipeWire, which previously produced silent
  recordings or wedged the microphone on Bazzite/SteamOS. Host `parec` remains
  a fallback for PulseAudio systems; ALSA-only distributions retain their
  PortAudio fallback.
- **Flatpak microphone capture uses its pinned Pulse/PipeWire boundary.** Both
  package formats now run release-blocking input smoke tests against the same
  capture helpers used by dictation and Mic Test.
- **Manual microphone choices survive device refreshes.** Friendly desktop
  source names are no longer overwritten with transient PortAudio names, and
  Auto-detect remains Auto-detect across restarts.

## [1.1.5] — 2026-07-31

Rendering + host-integration fixes from the first real customer-path install
of 1.1.4 (download from the site → run) on a 200%-scale KDE desktop.

### Fixed

- **Mic-test playback was silent in the AppImage on Bazzite/Fedora.** AppImage
  playback now uses the host's Pulse/PipeWire client with bundle library paths
  scrubbed, while its proven recording stack remains self-contained. Flatpak
  stays entirely within its pinned runtime. The completed AppImage and Flatpak
  both exercise their exact playback paths before publishing.
- **CPU-only now means CPU-only in packaged builds.** Free installs and Ultra
  users with GPU acceleration disabled invoke independently linked CPU Whisper
  and llama.cpp binaries. They never initialize Vulkan first, so a broken or
  unsupported GPU cannot crash the supposedly CPU path. The Steam Deck's Zen 2
  instruction baseline is also explicit instead of inherited from the build
  machine.
- **Free-tier restrictions are enforced at runtime, not just hidden in the
  interface.** Free is Base/Base.en transcription on CPU with local cleanup;
  GPU acceleration, other speech models, large cleanup models, Style/voice
  profiles, custom vocabulary, cloud/Faster-Whisper backends, and chunked
  recording require Ultra. Stale or hand-edited settings are repaired before
  any backend starts.
- **Benchmark results now expose the upgrade tradeoff.** Base transcription
  timing is always shown, and supported machines compare the current CPU path
  with an explicitly labeled Ultra GPU preview rather than showing cleanup
  timing alone.
- **Audio-processing modes are now meaningfully distinct and package-tested.**
  Light safely normalizes, Medium also filters low-frequency rumble, and Heavy
  adds conservative noise gating. Stationary noise is no longer amplified into
  likely Whisper hallucinations, and the settings tooltips describe when each
  mode is useful.
- **Chunking defaults favor accuracy.** One-shot transcription is now the
  default. Ultra users can opt into chunked processing for long recordings;
  its tested default is 15-second chunks with 2 seconds of overlap to reduce
  boundary word loss and duplication.
- **Steam Deck setup is rootless and hardware-aware.** Desktop Mode now shows
  Deck-specific back-button guidance, while the optional helper installs from
  the GitHub Flatpak release and manages Desktop/Game Mode without unlocking
  SteamOS or installing system Python packages.
- **Text rendered 2× too large on HiDPI desktops (AppImage).** The bundled
  Tk 8.6.12 converts pixel font sizes through the screen DPI while Xft
  renders them at the `Xft.dpi` resource (192 on 200% desktops) — every
  widget font inflated 2× while widget dimensions stayed put (verified with
  widget-level probes; Tk 8.6.13+ renders pixel-exact, which is why
  from-source builds always looked right). The app now sets Tk's
  pixels-per-point from `Xft.dpi` at startup, making the AppImage render
  identically to the approved from-source look on every machine. Font size
  tokens are unchanged.
- **Overlay stuck mid-screen; placement/position controls did nothing
  (AppImage on KDE).** KWin placement shelled out to `qdbus`, which is named
  `qdbus-qt6` on Fedora and — worse — died loading the bundle's older
  libstdc++ from the inherited `LD_LIBRARY_PATH`. Placement now talks to
  KWin's scripting API in-process via Qt D-Bus (no host binary at all), with
  a host-clean `qdbus`/`qdbus6`/`qdbus-qt6` fallback.
- **Host tools launched with the bundle's libraries.** Every host-binary
  spawn (ydotool, xdotool, systemctl/pkexec, pactl/paplay, xrandr,
  kscreen-doctor, xdg-open for the Get Ultra checkout link, desktop-database
  refreshers) now runs with a scrubbed environment that strips
  bundle-owned path entries — the class of failure behind "browser doesn't
  open" / "mic list empty" reports on newer distros.

### Added

- **The product typeface ships with the app.** The UI font is now DejaVu Sans
  by decision — the typeface the product was actually designed and approved
  against (the nominal Inter/JetBrains Mono families were never installed
  anywhere, so fontconfig had silently substituted DejaVu all along). It is
  bundled (with JetBrains Mono for the status overlay) and exposed to
  fontconfig on top of the host's fonts, so the same build no longer looks
  different on every distro (1.1.4 rendered Noto on Fedora).
- **Coarser UI-scale stepping.** The header −/+ buttons now step 25% per
  click (the Settings slider remains the fine control) — dropping from 200%
  no longer takes a dozen full-relayout clicks.

## [1.1.4] — 2026-07-30

### Added

- **AppImage self-integration:** first launch adds a menu entry + icon
  pointing at the AppImage's current location; idempotent and self-repointing
  when the file moves. ("I don't see an icon or install" — fixed.)

### Fixed (release pipeline)

- The v1.1.3 release surfaced and closed four silent release-killers, now
  permanently gated in CI: Vulkan-less inference binaries (jammy needed modern
  Vulkan-Headers), strip-corrupted numpy openblas (strip=False; jammy binutils
  mangles 64KB-aligned libs), a PortAudio native crash on zero-audio machines
  (CI gate runs a null-sink audio server; app-side guard tracked), and — the
  gate that catches them all — every release AppImage must now literally BOOT
  under xvfb before anything publishes.

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
