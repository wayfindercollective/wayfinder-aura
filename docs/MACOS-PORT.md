# Wayfinder Aura on macOS

What the macOS port changes, why, and where it lives. Written on the `macos`
branch (2026-09-23), with the ideas worth carrying to Linux and Windows at the
end.

**Ground rules the port follows**

- **Linux and Windows behave exactly as on `main`.** Every macOS change sits
  behind `sys.platform == "darwin"` / `IS_MACOS`, or lives in a macOS-only
  module (`src/wayfinder/**/macos_*.py`, `packaging/macos/`). Tests exercise
  both sides: each macOS test has a "Linux unchanged" neighbour.
- **Licensing is untouched.** Free/Ultra gates (`has_feature`, `FeatureGate`,
  model gating) behave identically; the Mac only adds UI around them.
- **Linux ideas are written down, not implemented** — see
  [LINUX-FOLLOWUPS-FROM-MACOS.md](LINUX-FOLLOWUPS-FROM-MACOS.md) and the table
  at the end of this file.
- **Exception — deliberate cross-platform product changes (2026-09-24).**
  Approved by the owner and backed by `docs/EVAL-2026-09-24.md`, these change
  Linux too and need a Linux check before `main`:
  - Normal (was "Minimal"/"Raw") is instant `normal_filler_removal()` — no
    cleanup model; `normal_llm_cleanup` opts back in.
  - Styles a cleanup model can't do are greyed out (`STYLE_SUPPORT`,
    `style_availability()`); cleanup falls back instead of running them.
  - Custom Vocabulary: Settings card, "heard -> write" corrections, and user
    terms last in Whisper's prompt budget.
  - The cleanup model is only kept loaded when the chosen style needs it.
  - Model descriptions and Ultra copy state measured results.
  - The Ultra upgrade panel uses Ultra gold and the brand mark; its benefits
    lead with the measured results and list Vocabulary on its own.
  - Setting hints are plain text (no emoji), rewritten to match current
    behaviour; unused ones were removed. The beam-search hint no longer shows
    hard-coded speed figures as if they were measured.
  - Hover hints stay on screen: long ones wrap wider, and a hint goes above,
    below or beside its icon inside the usable screen
    (`wayfinder.ui.tooltip_geometry`).
  - whisper.cpp decodes greedily everywhere and the Accuracy Mode dropdown
    is gone: beam search never beat greedy (EVAL-2026-09-24.md, "Beam
    search"). "[sound of wind]"-style annotations are stripped.
  Linux check: re-run `scripts/eval_matrix.py --stages tone` on the Linux
  cleanup path; confirm Normal-without-model is wanted there.

Target: macOS 14+ on Apple silicon (arm64 build), bundle ID
`io.wayfindercollective.WayfinderAura`, signed with *Developer ID Application:
Wayfinder Collective LLC (5JJQ8L5HHD)*.

---

## 1. Shape of the app on a Mac

| Process | What it is | macOS specifics |
|---|---|---|
| Main app | CustomTkinter window, hotkeys, pipeline | Aqua Tk 8.6; glass window chrome; native layers for the waves and logo |
| Overlay pill | Same executable, `--overlay-subprocess` (PyQt6) | Registered as an **accessory** app (no second Dock icon); Metal/Core Animation waves; answers Quit Apple events |
| `whisper-server` | Resident speech-to-text (bundled, pinned whisper.cpp) | Supervised child; CPU (Free) or Metal (Ultra GPU) |
| `llama-server` | Resident cleanup LLM (bundled, pinned llama.cpp) | Loopback only; ownership proven via libproc |
| Render thread | "Wayfinder waves" thread inside each process | Display-synced CADisplayLink; idle cost zero |

All children die with the app: `utils/child_supervisor.py` watches the parent
PID (macOS has no `PR_SET_PDEATHSIG`), and quitting from the Dock, menu bar,
AppleScript or logout shuts everything down (measured 0.7 s, no orphans).

## 2. Packaging, signing and releases

- **Build:** `packaging/macos/build.py` builds pinned whisper.cpp / llama.cpp
  (`WHISPER_COMMIT`, `LLAMA_COMMIT`, `MINIMUM_MACOS = "14.0"`), compiles the
  native renderers into `libwayfinder_hero.dylib`
  (`hero_renderer.m`, `overlay_renderer.m`, `wf_render_clock.m`), runs
  PyInstaller with `wayfinder-aura-macos.spec`, then self-tests the bundle
  (`--runtime-assets-self-test`, `--macos-native-renderers-self-test`).
- **Signing and notarization:** with `MACOS_CODESIGN_IDENTITY` set the app is
  signed with the hardened runtime (`packaging/macos/entitlements.plist`);
  with `MACOS_NOTARY_PROFILE` (keychain profile) or an App Store Connect API
  key it is notarized and stapled, then the DMG is built around the stapled
  app, signed, notarized and stapled too. Steps: `packaging/macos/README.md`.
- **DMG:** `dmg_assets.py` lays out the app beside an Applications link with a
  branded background (`dmgbuild`, `hdiutil` fallback).
- **CI:** `.github/workflows/release.yml` job `build-macos` produces a signed,
  notarized DMG on `v*` tags when the six `MACOS_*` / `APPLE_*` secrets exist,
  otherwise an unsigned workflow artifact only (never attached to a release).
  `macos-build.yml` is a manual candidate build.
- **Updates:** `core/app_updates.py` is platform-aware: a Mac only offers a
  release that carries a DMG for its architecture.
- **Trim:** unused Qt modules are excluded from the bundle
  (`wayfinder-aura-macos.spec`); dependency pins in
  `packaging/macos/constraints.txt`.

## 3. Permissions and onboarding

macOS gates dictation behind three privacy grants. Aura handles them in-app,
with no Terminal:

- **Setup checklist** (`ui/welcome.py`, `utils/macos_permissions.py`): the
  first-run guide opens with Microphone / Accessibility / Input Monitoring
  rows. "allow" shows macOS's own prompt and opens the right Settings pane; a
  1 s poll turns rows green as grants land and advances by itself.
- **Stale entries after updates:** a TCC entry left by an older build (a
  different signature) makes macOS skip the prompt, and its Settings switch
  then grants the *old* copy. While this copy isn't allowed, "allow" first
  runs `tccutil reset <service> io.wayfindercollective.WayfinderAura` — only
  Aura's own entry, refused for any other bundle and never when the grant
  already works.
- **Live grants:** Accessibility applies immediately — the hotkey listener is
  re-created on its own restart event (`_restart_pynput_listener`). Input
  Monitoring reaches new processes only, so after "allow" that row offers
  **relaunch**, which reopens the bundle through LaunchServices.
- **Everywhere else:** the Dictate banner, the hotkey step and a successful
  Ultra activation open the same checklist when something is missing. Startup
  never shows system prompts before the window is up.
- **Install location:** permissions are requested only for the copy in
  /Applications; running from the DMG shows "Open Applications" instead.

## 4. Hotkeys

- **Default:** **Right Option** — tap to start/stop, hold to talk
  (`SoloModifierGesture` in `hotkeys/pynput_listener.py`), chosen because many
  keyboards can't send Fn. Fn/Globe works where the keyboard supports it.
- **Secure Input:** detected (Carbon `IsSecureEventInputEnabled`, owner via
  `ioreg`) and explained in the log, since no app can see keys while it's on.
- **Conflicts:** `utils/macos_hotkey_conflicts.py` warns when a chosen chord
  collides with a system or popular app shortcut (read-only).
- **Latency:** `ui/macos_tk_wakeup.py` wakes Tk through a self-pipe the
  moment a worker queues an event, instead of Tk's 250 ms idle poll.

## 5. Putting text where the cursor is

`core/macos_paste.py` + `ui/clipboard.py` + `core/injector.py`:

- Cmd+V and Return are posted through Quartz, refused (text left on the
  clipboard) when Accessibility is missing instead of silently dropped.
- The pasteboard write is marked transient/auto-generated
  (`org.nspasteboard` types) so dictations don't pile up in clipboard
  managers, and the previous clipboard is restored.
- Auto-Enter checks the frontmost window hasn't changed before pressing
  Return.

## 6. Speech and cleanup pipeline

- **Resident whisper-server** (bundled): measured per 10 s clip on an M3
  Ultra, base.en: 0.22 s CPU with auto-tuned threads (0.40 s with the old
  first-session thread count), faster again on Metal.
- **ggml environment** (`utils/macos_ggml_env.py`): CPU mode doesn't pay a
  7-8 s Metal shader compile it never uses; GPU mode is Ultra-gated as on
  every platform.
- **Resident cleanup** (`core/llama_server.py`): the bundled `llama-server`
  with the model's chat template and thinking disabled cleans a dictation in
  ~0.3 s median (the `llama-simple` path took ~2 s and returned the text
  unchanged). Ownership of its loopback port is proven with libproc
  (`utils/macos_procinfo.py`), since macOS has no `/proc`.
- **No proxy on loopback** (`utils/loopback_http.py`): the system HTTP proxy
  is bypassed for Aura's own 127.0.0.1 servers.
- **Honest errors:** a missing speech model says so and points to the model
  download; `transcriber.py` no longer claims the binary is missing.

## 7. Audio

`utils/macos_audio.py` reads Core Audio directly (PortAudio's device table is
a startup snapshot): Aura follows the default input when the user changes it
in System Settings, knows each device's transport (USB / Bluetooth / built-in),
and ducks other audio through the output's virtual main volume
(`utils/audio_ducker.py`). Outputs without software volume (e.g. HDMI TVs)
simply aren't ducked.

## 8. Look, feel and performance

- **Waves at rest cost nothing.** The main-window hero and the pill hand their
  calm wave to Core Animation vector strands (`CAKeyframeAnimation` of paths,
  seamless loops of 13.1 s and 10.5 s): the render server animates them at the
  display's refresh with no app wakeups and no Metal memory.
- **Waves while speaking are Metal**, drawn on a private display-synced render
  thread (`wf_render_clock.m`: `CADisplayLink` from the layer's view, 60 fps,
  animated to each frame's display timestamp). A busy main thread can't skip
  frames, easing is per-second (identical at 60/120 Hz), and the pill eases
  the voice level (45 ms attack / 140 ms release). Hand-offs between Metal
  and vector carry phase, so crossfades line up.
- **Brand mark** (`ui/macos_brand_mark.py`): drawn for the window's backing
  scale in a CALayer straight on the glass — Aqua Tk draws images at 1x and
  won't draw them on transparent backgrounds.
- **Window:** unified transparent title bar + glass backdrop
  (`ui/macos_window.py`), off when *Reduce Transparency* is on; content-sized,
  centred first-run geometry (`ui/window_geometry.py`); bundled DejaVu
  registered per process (`utils/macos_fonts.py`).
- **Text fits its card** (`ui/macos_label_fit.py`): fixed wraplengths sized
  for the Linux 800 px window are clamped to the card each label sits in.
- **Pill placement:** inside the screen's visible frame, clear of the Dock
  at its real size.
- **Idle budget:** CustomTkinter's 100 ms DPI poll is stretched to hourly
  (Aqua scales on its own); the hero doesn't poll while hidden or occluded.

Measured (M3 Ultra, macOS 27, window open, calm):

| | Before | After |
|---|---|---|
| Pill process | 331-355 MB, 256 MB GPU | ~82-102 MB, 0 GPU, 0.1-0.2 % CPU |
| Main process | 5.5 % CPU + 256 MB GPU | ~1-2 % CPU, no GPU at rest |
| Cleanup per dictation | ~2 s, no change to text | ~0.3 s, cleaned |
| Quit | helper refused logout Quit | everything exits in 0.7 s |

## 9. Lifecycle and reliability

- **Sleep / wake / displays** (`utils/macos_lifecycle.py`): recording stops
  cleanly before sleep; on wake the mic and pill are re-established; screen
  changes reposition the pill and native layers.
- **No App Nap mid-task** (`utils/macos_activity.py`): user-initiated activity
  assertions keep the Mac awake during a dictation or a model download.
- **Quit Apple events:** the pill helper forwards Quit to the main app, so
  logout/shutdown never stalls on Aura.
- **Control socket** (`hotkeys/socket.py`): `toggle`, `show`, `hide`,
  `cancel`, `quit`, `tab:<id>` for automation and tests.

## 10. Security

- **API keys in the login Keychain** (`utils/macos_keychain.py`,
  Security.framework via ctypes): never on argv, never in config files.
- **System trust store for TLS** (`tls.py`, `truststore`), so corporate roots
  work and bundled CA lists can't go stale.
- **Cloud keys** (`core/cloud_keys.py`): links to each provider's key page, a
  free "Verify" that lists models on the official host with redirects
  refused; keys are scrubbed from child-process environments
  (`utils/hostexec.py`).
- **Loopback servers** accept only their own clients (see §6).

## 11. Testing

```bash
python3 -m pytest tests/ -v --tb=short
```

macOS behaviour is tested by patching `IS_MACOS` / `sys.platform` on plain
namespaces with the real methods (no windows are opened); native renderer
sources are checked for the display-synced, off-main-thread design; the
built bundle self-tests its runtimes and renderers. Tests never post real
keystrokes (`conftest.py` stubs the Quartz paste/Return).

## 12. Known gaps

- Apple silicon only (no Intel build yet).
- Mac App Store would need a sandboxed flavour (hotkey + paste under App
  Sandbox, In-App Purchase for Ultra, no self-updater) — not started.
- Feedback form: the server route isn't deployed (Wayfinder-OS PR #151).
- The remote model catalog is rejected on this build (entries reuse shipped
  filenames with different sizes) — the shipped catalog is used.

---

## Suggested for Linux and Windows

Found on the Mac, not changed elsewhere. Details, evidence and fix sketches
are in [LINUX-FOLLOWUPS-FROM-MACOS.md](LINUX-FOLLOWUPS-FROM-MACOS.md) (§ refs).

| Suggestion | Why | Linux | Windows |
|---|---|---|---|
| Resident `llama-server` with chat template, thinking off (§1.1) | Cleanup from ~2 s and no effect to ~0.3 s and real | ✔ | ✔ |
| Regex path for "minimal" style (§1.2) | Skips the LLM where a regex does the job | ✔ | ✔ |
| Keep whisper-server thread count in reuse check; 0.1 s readiness poll (§1.3-1.4) | ~0.2 s per dictation, faster restarts | ✔ | ✔ |
| Only count licence-loadable models as usable (§1.5) | Free installs with gated models couldn't dictate | ✔ | ✔ |
| CPU mode shouldn't initialise the GPU backend (§1.7) | Seconds of startup for nothing | ✔ (Vulkan) | ✔ |
| Harden the loopback llama-server (§1.8) | Any web page could reach it | ✔ | ✔ |
| Don't let the 800 ms "processing" display swallow the next hotkey (§2.1) | Back-to-back dictation | ✔ | ✔ |
| Start the mic before overlay/UI work (§2.2); wake the UI loop on events (§2.3) | Up to 250 ms less latency | ✔ | ✔ |
| Pause the hero when hidden/occluded; stretch CTk's DPI poll (§3.1, §3.3) | Idle CPU under the 2 % target | ✔ | ✔ |
| Animate waves to wall-clock time with per-second easing (§3.4) | No judder when frames arrive unevenly | ✔ | ✔ |
| Supervise native children (§4.1) | No orphaned servers after a crash | ✔ | ✔ (job objects) |
| Platform-aware update check (§4.3) | Don't offer an AppImage to a Flatpak/Windows user | ✔ | ✔ |
| Bypass proxies for loopback (§4.4) | Proxied setups broke local speech/cleanup | ✔ | ✔ |
| Keys in the OS keystore (§4.5) | Secret Service / Windows Credential Manager | ✔ | ✔ |
| Retired cloud models + key help panels (§4.6-4.7) | Cloud backends failing silently | ✔ | ✔ |
| Mark pasted dictations transient (§4.9) | Keep them out of clipboard history | ✔ (KDE Klipper hint) | ✔ (`ExcludeClipboardContentFromMonitorProcessing`) |
| Inhibit sleep during dictation/downloads (§4.10) | Downloads restarting from zero | ✔ (logind) | ✔ (`SetThreadExecutionState`) |
| Clamp fixed wraplengths to their card (§5.6) | Clipped text in narrow windows / high scaling | ✔ | ✔ |
| "Finish Setup" should scroll to the model panel (§5.4) | Button looks dead | ✔ | ✔ |
| Deploy the feedback route (§5.5) | Every submission 404s today | server | server |
| Sign the Windows installer (Authenticode) | SmartScreen warnings, like Gatekeeper on the Mac | — | ✔ |
