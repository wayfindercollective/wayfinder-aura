# Promotion Readiness Checklist

Use this checklist before broadly promoting a Wayfinder Aura release. The goal
is to validate the exact downloadable package on clean hardware—not merely the
source checkout or a locally patched AppImage.

Last reviewed: 2026-08-04

Planned candidate: `v1.1.8-beta.1`

Approved source commit at review time: `8005ee6`

## Release-candidate preparation

- [ ] Confirm `main` contains every intended fix and has no unrelated changes.
- [x] Confirm all blocking GitHub CI jobs pass on `main`.
- [x] Review the non-blocking type-check output; record or fix new errors.
- [x] Bump the version consistently in:
  - [x] `pyproject.toml`
  - [x] `src/wayfinder/__init__.py`
  - [x] `scripts/build-appimage.sh`
  - [x] `flatpak/io.wayfindercollective.WayfinderAura.metainfo.xml`
- [x] Move the relevant `CHANGELOG.md` entries from Unreleased into the release.
- [x] Update stale version-specific install and shipping documentation.
- [ ] Tag a prerelease such as `v1.1.8-beta.1`.
- [ ] Let GitHub Actions build the AppImage from that tag.
- [ ] Confirm the packaged self-tests pass: imports, UI renderer, audio input,
      audio output, audio processing, TLS, Vulkan binaries, and CPU fallbacks.
- [ ] Download the GitHub-built candidate rather than using a locally patched
      AppImage.
- [ ] Record the candidate filename, source commit, size, and SHA-256 below.

Candidate record:

- Version/tag:
- Commit:
- Filename:
- SHA-256:
- GitHub Actions run:
- Tester/date:

Automated audit snapshot (2026-08-04):

- Candidate source is prepared locally as `v1.1.8-beta.1`; it is not committed,
  tagged, or published yet, so the approved commit and artifact record remain blank.
- Current `main` (`8005ee6`) blocking CI jobs are green, including tests,
  runtime syntax, structure verification, and the local-build Flatpak job.
- The non-blocking mypy output was reviewed. Existing project-wide annotation
  debt remains; the candidate's new overlay, geometry, and vocabulary paths add
  no new changed-line error. Release-blocking Ruff checks pass.
- Production license defaults, version parity, AppStream/desktop validation,
  release-manifest rendering, release shell syntax, and the static storefront
  gate pass locally.
- CI-equivalent source gate: 1,619 passed, 7 live-environment tests skipped,
  and 40 UI/slow/network/performance tests deselected. The separate performance
  gate passed 2/2. The currently running AppImage passed its 7/7 live smoke
  checks; it is not the unbuilt candidate artifact.
- The installed Flatpak (`v1.1.5`) passed 5/5 available Base CPU golden-audio
  checks with mean WER 0.089 and all key phrases, plus a 20-iteration memory,
  process, temporary-file, latency, and orphan soak. Its Ultra/Turbo check was
  skipped because that older installation does not contain the model; these are
  diagnostic packaging results, not candidate signoff.
- Host preflight: KDE Wayland, live Aura control socket, and AMD RX 9060 XT
  Vulkan visibility pass. Host `wtype` and the `ydotool` socket are missing;
  Steam Deck is not this machine.
- Storefront blocker: the currently deployed `/aura` page still claims Free GPU
  support. Corrected source and a regression test are prepared in the website
  repository's `dev` worktree, but have not been committed or deployed. The
  candidate release gate now rejects that stale live claim.

## Existing production key on another computer

This tests multi-device activation. It does **not** test purchasing or delivery
of a newly minted key.

- [ ] Use a genuinely separate computer and a clean Aura configuration.
- [ ] Do not copy `license.json`, configuration, models, or cache from the first
      computer.
- [ ] Install the GitHub-built candidate recorded above.
- [ ] Confirm Aura initially identifies the installation as Free.
- [ ] Enter the same production key already used on computer one.
- [ ] Confirm Settings displays a persistent **Ultra is active** state.
- [ ] Download Turbo Q5 successfully.
- [ ] Download Gemma 1B successfully.
- [ ] Restart Aura and confirm both models remain available.
- [ ] Disconnect the network, restart Aura, and confirm Ultra still works within
      its offline grace window.
- [ ] Run both GPU and CPU dictations.
- [ ] Record which activation slot this should represent, including any older
      machines or OS reinstalls that may already have consumed slots.

Result:

- Machine/distro:
- Desktop/session:
- CPU/GPU:
- Activation result:
- Offline result:
- Notes/log location:

Important: **Remove from this device** removes only the locally stored
credential. It does not release a server activation slot.

## Activation management and support

- [ ] Confirm there is an admin or support procedure to view a license's active
      machines without exposing its full key.
- [ ] Confirm support can reset or release obsolete activations when a customer
      replaces or reinstalls a computer.
- [x] Document how the customer proves ownership before a slot reset.
- [x] Document the expected response to `activation_limit`, revoked, refunded,
      offline, and invalid-key errors.
- [ ] Decide whether self-service device management is required after launch.

## Clean AppImage install

Run this section on every target machine.

- [ ] Download the candidate from GitHub and verify its SHA-256.
- [ ] Mark it executable and launch it directly from Downloads.
- [ ] First launch succeeds without a terminal workaround or missing dependency.
- [ ] The first usable window appears promptly.
- [ ] The application-menu entry launches the same candidate.
- [ ] Settings shows the expected version.
- [ ] Closing and reopening Aura works.
- [ ] Launching Aura twice does not create conflicting instances.
- [ ] Reboot or log out/in, then launch Aura again.
- [ ] Suspend/resume, then complete the first post-resume dictation.
- [ ] Aura runs without network access after required models and the license have
      been activated/downloaded.
- [ ] Idle CPU remains near zero and memory does not continuously increase.

## UI and desktop integration

- [ ] Main window is correct at 100% display scale.
- [ ] Main window is correct at 200% display scale.
- [ ] Settings has normal row spacing with no flat or layered rendering artifacts.
- [ ] Settings opens promptly after the background preload has had time to run.
- [ ] Clicking Settings immediately after launch still completes correctly.
- [ ] Icons, fonts, dropdowns, toggles, tooltips, and long text are readable.
- [ ] The overlay shows Ready, Listening, Processing, and completion states.
- [ ] The overlay does not jump or steal focus during dictation.
- [ ] Tray actions Open, Reset, and Quit work.
- [ ] The tray icon and state remain responsive after repeated dictations.

## Audio and device handling

- [ ] Auto-detect selects a working microphone.
- [ ] A manually selected microphone survives restart.
- [ ] Refresh Devices detects a newly connected USB microphone.
- [ ] Disconnecting the selected microphone fails clearly and allows recovery.
- [ ] Mic Test records audible audio.
- [ ] Mic Test playback is audible.
- [ ] Light, Medium, and Heavy audio processing do not create hallucinated speech
      from stationary noise.
- [ ] Silence produces a clear no-speech result instead of invented text.

## Core dictation

Perform at least 20 successful short dictations per target machine.

- [ ] Default raw dictation works with post-processing off.
- [ ] Post-processing works with Gemma 1B.
- [ ] Auto-Enter disabled leaves text ready for review.
- [ ] Auto-Enter enabled injects final text first and presses Enter afterward.
- [ ] Auto-Enter occurs after post-processing when cleanup is enabled.
- [ ] Text injection works in a browser text field.
- [ ] Text injection works in a terminal.
- [ ] Text injection works in an Electron app or IDE.
- [ ] Focus remains in the intended field while recording and injecting.
- [ ] No dictation produces empty output for clearly audible speech.
- [ ] No dictation hangs on Processing or hits an avoidable server timeout.
- [ ] Repeated start/stop presses do not create overlapping sessions.
- [ ] Benchmark completes without stale AppImage mount-path errors.
- [ ] GPU mode is measurably active where supported.
- [ ] Disabling GPU uses the packaged CPU binaries without initializing Vulkan.
- [ ] GPU failure falls back cleanly to CPU and explains the reason in the log.

Short-dictation run:

- Machine:
- Attempted:
- Successful:
- Empty outputs:
- Hangs/timeouts:
- Wrong-focus injections:
- Notes:

## Auto chunk processing and long dictation

- [ ] A 20–29 second recording remains one-shot in Auto mode.
- [ ] A 45–60 second recording uses chunk processing in Auto mode.
- [ ] Off always remains one-shot.
- [ ] On always uses chunk processing as designed.
- [ ] No phrase is lost at the 30-second boundary.
- [ ] No words or sentences are duplicated across chunk overlap.
- [ ] Post-processing receives the complete assembled transcript.
- [ ] Auto-Enter occurs only after the entire long transcript is assembled,
      optionally cleaned, and injected.
- [ ] A several-minute recording completes without runaway memory use or a stuck
      overlay.

## Model downloads and packaged inference

- [ ] Base model download succeeds on a clean Free installation.
- [ ] Ultra model download succeeds after activation.
- [ ] HTTPS certificate verification succeeds without disabling TLS checks.
- [ ] Canceling a download leaves the UI usable.
- [ ] Retrying a canceled or interrupted download succeeds.
- [ ] A partial or corrupted download is detected rather than loaded.
- [ ] Downloaded model paths remain valid after closing and remounting the
      AppImage.
- [ ] Whisper server starts, remains warm, and recovers after a forced failure.
- [ ] CLI salvage runs when server recovery fails instead of returning immediate
      empty output.

## Platform and hardware matrix

Record at least three clean systems covering the major advertised paths.

- [ ] Bazzite/Fedora KDE Wayland with AMD Vulkan.
- [ ] Ubuntu 22.04 or 24.04, preferably GNOME Wayland.
- [ ] CPU-only dictation with GPU acceleration disabled.
- [ ] NVIDIA or Intel GPU if those families are advertised broadly.
- [ ] X11 text injection and overlay behavior.
- [ ] Steam Deck Desktop Mode.
- [ ] Steam Deck Game Mode and the configured hardware trigger.

| System | Distro | Session | CPU/GPU | Clean install | License | Short dictation | Long dictation | Result |
|---|---|---|---|---|---|---|---|---|
| 1 |  |  |  |  |  |  |  |  |
| 2 |  |  |  |  |  |  |  |  |
| 3 |  |  |  |  |  |  |  |  |

## Production purchase and fulfillment

This is separate from reusing an existing key. Perform at least one real
production purchase before promotion.

- [ ] Open <https://wayfindercollective.io/aura> in a private browser window.
- [ ] Confirm product claims, supported platforms, screenshots, price, refund,
      privacy, Terms, and Support links are accurate.
- [ ] Confirm Get Ultra opens
      <https://wayfindercollective.io/checkout/aura-ultra>.
- [ ] Confirm checkout shows launch price `$29.99`, processing fee `$0.90`, and
      total `$30.89`, or update every surface if pricing changes.
- [ ] Complete one real card payment using an inbox that can be monitored.
- [ ] Confirm the success page displays the new license key.
- [ ] Confirm the license email arrives.
- [ ] Confirm the emailed key exactly matches the success-page key.
- [ ] Confirm the receipt amount and product name are correct.
- [ ] Confirm the Download button points to the intended stable Aura release.
- [ ] Activate the new key on a clean Aura installation.
- [ ] Restart offline and confirm Ultra remains active.
- [ ] Confirm the order/customer portal can retrieve the license when expected.
- [ ] Confirm the documented refund path is usable.
- [ ] Separately verify in a safe test environment that refund/revocation removes
      entitlement after the cached grace rules allow a refresh.

Purchase record:

- Date:
- Order/receipt reference:
- Email received after:
- Key displayed on success page: yes/no
- Key activated: yes/no
- Offline restart: pass/fail
- Download link target:
- Notes:

## Failure and recovery checks

- [ ] Launch with no network connection.
- [ ] Launch with no readable microphone.
- [ ] Try an invalid license key and confirm the error is understandable.
- [ ] Confirm an activation-limit response directs the customer toward support.
- [ ] Interrupt model download and recover without deleting configuration.
- [ ] Force-close during recording, relaunch, and complete a new dictation.
- [ ] Reset a stuck overlay from the tray.
- [ ] Upgrade from the previous stable AppImage while preserving user settings.
- [ ] Test a genuinely fresh install with no prior configuration.
- [ ] Confirm logs identify GPU fallback, model failure, audio failure, and
      injection failure without exposing full license keys or bearer tokens.

## Documentation and support readiness

- [x] README Download Latest points to the intended stable release.
- [x] Installation instructions use current filenames and versions.
- [ ] Supported platforms and limitations are accurate.
- [x] The default post-processing and Auto chunk behavior are documented.
- [x] Auto-Enter includes an appropriate warning about submitting terminal or AI
      prompts automatically.
- [x] Support instructions identify `~/.cache/wayfinder-aura/activity.log` and
      remind users to redact private transcript text.
- [ ] A user can find the app version, license status, issue tracker, and support
      contact without opening a terminal.
- [x] Known Wayland, Steam Deck, and activation-slot limitations are disclosed.
- [x] A rollback plan exists if the new stable release has a critical defect.

Rollback plan:

- Keep `v1.1.7` and its artifacts available throughout the prerelease and stable
  rollout.
- If the beta has a P0 defect, do not promote it or mark it latest; fix forward
  with another prerelease.
- If a new stable release has a P0 defect, roll `releases/latest` back to
  `v1.1.7`, verify the storefront Download button resolves to `v1.1.7`, and
  publish a fixed version only after repeating the candidate matrix.

  **If immutable releases are enabled** (see
  `docs/SECURITY-AUDIT-APPIMAGE-FLATPAK-2026-08-17.md` §0.1), only a published
  release's title and notes stay editable — the latest/prerelease designation is
  fixed at publication and its assets and tag are locked. Rolling back then
  means **publishing a new release** (e.g. `v1.1.9` carrying the 1.1.7
  artifacts, or re-publishing the bad version's predecessor under a fresh tag),
  not editing the bad one. Plan the designation before you click publish:
  draft → attach every asset → set prerelease/latest → publish.

## Stable release and promotion gate

- [ ] Every P0 failure found during prerelease testing is fixed and retested on
      the machine that found it.
- [ ] Create the final stable tag only after prerelease signoff.
- [ ] Confirm the final tag builds from the approved commit.
- [ ] Download the final stable AppImage from GitHub Releases.
- [ ] Run a last clean smoke test on at least two machines against the final
      downloadable file.
- [ ] Confirm `releases/latest` resolves to the new stable version.
- [ ] Confirm the storefront and purchase email Download buttons resolve to that
      same release.
- [ ] Keep the previous stable release available for rollback.

Promotion is **GO** only when:

- [ ] The exact final AppImage passes on at least three clean systems.
- [ ] At least 20 short dictations per system complete without empty output or
      hangs for clearly audible speech.
- [ ] Auto chunking, cleanup, Auto-Enter, model downloads, CPU fallback, and
      offline licensing pass.
- [ ] One complete production purchase and key-delivery flow passes.
- [ ] There is a documented way to handle consumed activation slots.
- [ ] No unresolved issue would prevent a new customer from installing,
      activating, downloading a model, or completing their first dictation.
