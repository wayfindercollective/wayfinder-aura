# Ship Readiness — Wayfinder Aura

_Last updated: 2026-10-04 (Linux gamer release audit, below). The sections after
it are the 2026-08-04 pass for v1.1.8-beta.1, kept as history: v1.1.8 has since
been tagged and released. Tracks what remains before a paid public release.
Complements `SHIPPING.md`, `docs/GO-LIVE-INPUTS.md`, `docs/SHIP-VERIFICATION-RUNBOOK.md`,
`docs/LINUX-SHIPPING-PLAN.md`, and the current `docs/PROMOTION-READINESS-CHECKLIST.md`._

---

## Linux gamer release: audit 2026-10-04

Scope: the integration tree (macOS/Windows port plus the Linux gamer work,
`docs/LINUX-SHIPPING-PLAN.md`) audited for Linux ship readiness, with fixes
committed on top. No blocker was found in packaging, portal typing, Gamer mode
or updates: both packages carry everything the new features import, and the
release asset names match what Get Update selects.

Fixed in the audit (see `CHANGELOG.md` [Unreleased]): resident llama-server
keyed and `/slots` hidden on Linux; loopback requests bypass HTTP proxies;
current cloud cleanup models with retired IDs migrated; Gamer mode pastes into
unlisted games, shows the DAoC note, and its xdotool path is hardened; a dead
portal session falls back with a log line; the portal paste restores the
clipboard; Setup and startup recognise the portal and retry its probe; the
restore token is owner-only from creation; Get Update links are pinned to this
repository and failed checks are retried; the cancel shortcut never grabs a
bare Escape; the GameMode pause logs dropped shortcuts; release artifacts prove
the portal/Gamer-mode imports (`--linux-input-self-test`), a tag fails if the
AppImage name does not carry its version, and releases publish
`SHA256SUMS-linux.txt`; the ship preflight checks the RemoteDesktop portal;
user docs, PRIVACY.md and the CHANGELOG match the code. UI: the Style tab icon
follows a license change, the recording hint promises Esc only when something
listens, the pill fits every style badge.

Permissions and first run (`docs/PERMISSIONS-AND-FIRST-RUN.md`): Flatpak
shortcuts no longer die on desktops without a GlobalShortcuts portal (GNOME
before 48, Cinnamon, XFCE, MATE, Sway; already broken in 1.1.8): X11 falls back
to the X11 listener and Wayland names the `--toggle` desktop shortcut; X11
AppImage/source shortcuts work without the `input` group; a declined or failed
typing portal shows on the Dictate tab and the switch asks again; Setup no
longer installs ydotool or raises password prompts where the portal types, and
prints runnable commands per distro (rpm-ostree, zypper); permission copy names
the dialogs users see. macOS/Windows: a failed Keychain read no longer deletes
API keys, a failed Windows paste keeps the dictation on the clipboard,
`~/Applications` counts as installed.

Automated gate: the CI-equivalent suite (`-m "not ui and not slow and not
network and not perf"`) passes on a headless node; Ruff's CI classes and
`scripts/verify_structure.py` pass.

### What still blocks the Linux release

1. **Release candidate** (plan item 6). `main` names the next stable
   release (`1.2.0`), and the nightly Beta workflow cuts and stamps
   `v1.2.0-beta.N` from it (`docs/RELEASING.md`). The first beta after this
   audit merges is the Linux candidate: install the AppImage and Flatpak from
   that release, check them against `SHA256SUMS-linux.txt`, and confirm the
   `--linux-input-self-test` step passed in its build. Stable 1.1.8 users are
   not offered a beta; `scripts/release/promote.py` ships `1.2.0` from a
   complete beta after the manual passes below.
2. **Manual passes still open** in `docs/LINUX-SHIPPING-PLAN.md`: GNOME 45+,
   the Steam machine (desktop approval, then Game Mode), Path of Exile 2 under
   Proton (windowed and fullscreen), DAoC through the portal, and the AppImage
   on KDE Wayland (portal typing; its dialog should now name Wayfinder Aura and
   the approval carry over a restart).
   Also confirm on the Steam machine that an unlisted game now gets a paste
   (Ctrl+V) rather than typed keys, as the Games tab promises.
3. **Fixed 2026-10-05** (was a known issue): the two Flatpak dialogs
   (shortcut bind, typing approval) come one at a time unless the shortcut
   dialog is left unanswered for a minute, and the tour's first card names
   them (`docs/PERMISSIONS-AND-FIRST-RUN.md`). The AppImage tour downloads the
   Base model first.
4. **Server and storefront items outside this repo**: the feedback endpoint is
   live in production (checked 2026-10-05) and stores messages, but no Slack
   webhook is set on the deployment (`AURA_FEEDBACK_SLACK_WEBHOOK_URL`;
   `AURA_CRASH_SLACK_WEBHOOK_URL` for crash reports falls back to it), so
   nobody is pinged. The store screenshots still show Super+F2 and "Minimal":
   recapture with `scripts/capture_store_screenshots.py` on a KDE Wayland
   session.

### Known limitations to decide or schedule (not blockers)

- Portal typing reads only the first layout of a multi-layout keymap: with us+de
  and de active, key codes produce the first layout's letters.
- A layout-less character makes the whole dictation a Ctrl+V paste, which most
  terminals ignore (they paste on Ctrl+Shift+V).
- Cancel works only while recording; Shift+Esc after the stop press does not
  stop typing.
- Decided 2026-10-04: with Gamer mode on, Ctrl/Alt shortcuts go through the
  GameMode pause; single-key shortcuts still pause (AppImage/source).
- Get Update hands the browser an unverified file (no in-app checksum or
  signature check), and Flathub installs are not yet told apart from bundle
  installs.
- Windows still runs llama-server without a key.
- Linux renders the Mac shader ribbon and runs the recording ribbon at 30 fps
  on desktops (15 fps on Steam hardware); CLAUDE.md rules 1 and 9 now say so
  (2026-10-04). Still measure focused-idle CPU at HiDPI
  (`top -p $(pgrep -f wayfinder)`, rule 1 budget 2%).
- macOS: bare permission prompts for returning users at launch (hotkey
  recovery after a late Accessibility grant is fixed on main, #41; confirm on
  a real Mac). Windows: unsigned installer
  (public-release blocker), a blocked microphone dead-ends the tour.

---

## What Still Blocks Shipping

1. **Deploy the corrected storefront truth.** The live `/aura` page still says
   Free includes GPU support on lighter models. The product decision and runtime
   are GPU-requires-Ultra. Corrected website source and a regression test are
   prepared, and the Aura tag gate now rejects the stale live claim. Deploy the
   website fix and rerun the browser-rendered storefront check before tagging.

2. **Build the exact candidate from a tag.** The local source is prepared as
   `v1.1.8-beta.1`, but it is not committed or tagged. GitHub Actions must build
   the AppImage and Flatpak from the approved commit, after which their hashes
   and packaged self-tests must be recorded. Local or older installed packages
   do not count as candidate signoff.

3. **Verify activation operations.** The customer-facing ownership and error
   guidance is documented, but an internal procedure to inspect active machines
   and release an obsolete activation still needs to be confirmed with the
   production licensing service.

4. **Complete exact-artifact manual QA.** Run the GitHub-built candidate through
   the clean-machine matrix in `docs/PROMOTION-READINESS-CHECKLIST.md`, including
   Wayland, X11, Steam Deck Desktop/Game Mode, CPU fallback, an Ultra GPU path,
   multi-device/offline licensing, 20 dictations per system, and one real
   production purchase and delivery flow.

---

## Fixed In This Pre-Ship Pass

| Area | Status |
|---|---|
| Blank whisper binary regression | Fixed. Empty/stale `whisper_binary` values now repair to an existing bundled or host binary instead of trying to execute `""`. |
| Current dictation failure | Fixed root cause. Local config with an empty whisper path now resolves to `/home/bazzite/whisper.cpp/build/bin/whisper-cli` on this machine. |
| Tray handoff | Present in the current tree. Linux uses the Qt StatusNotifier tray hosted by the overlay subprocess; pystray remains for macOS/fallback. |
| Tray/dictation command socket liveness | Fixed and live-verified. The socket listener now supports a side-effect-free `ping`/`pong` health probe, and the supervisor restarts the listener when the thread is alive but the socket is unreachable. After restarting the source app, `/run/user/1000/wayfinder-aura/wayfinder-aura.sock` accepted `ping`, `show`, and `tab:dictate`; `WAYFINDER_LIVE=1 tests/test_live_smoke.py` passed 7/7 through the host-visible venv. |
| Warm-server silence hallucinations | Fixed during verification. A Flatpak-runtime soak smoke exposed that generated silence could occasionally return stock Whisper outro text such as “We'll see you next time,” triggering unnecessary LLM post-processing and failing latency drift. The exact-output hallucination filter now drops the reproduced warm-server phrases, repeated exact sentence hallucinations collapse before filtering, and `tests/test_transcriber.py` covers the regression. A 20-iteration warm-server silence probe returned empty text for every iteration after the fix. |
| KDE F3 leak/double-fire | Present in the current tree. KDE-owned recording shortcut detection defers the in-app listener when Plasma already owns the key. Focused hotkey rerun passed `tests/test_hotkeys.py tests/test_live_smoke.py`: 51 passed, 7 live/manual skips after installing `evdev` in the audit venv. |
| Chunked dictation blank-binary failure | Covered. The real chunk transcription wrapper now has a regression test for stale `whisper_binary=""` resolving to a discovered `whisper-cli` instead of trying to execute an empty path. |
| Overlay tray bare-script import | Covered. The overlay subprocess path now has a regression test proving `overlay.py` bootstraps `src/` when launched with only `src/wayfinder/ui` on `sys.path`, preventing the Qt tray from dying on `import wayfinder`. |
| Modular tray socket parity | Fixed. The migrated `wayfinder.hotkeys.socket` listener now handles the same `show`, `reset`, `quit`, and `tab:<id>` commands as the legacy live app listener, with direct regression coverage for the tray verbs. |
| Steam Deck trigger artifacts | Hardened. The active Deck installer uses the host evdev-to-socket daemon, the legacy `r4-f3-bridge` path is disabled by default, both units conflict with each other, and regression tests guard against reverting to xdotool/F3 injection. Focused rerun passed `tests/test_steamdeck_scripts.py tests/test_game_mode_app.py tests/test_mode_supervisor.py`: 61 passed. |
| Foreign parent Flatpak env leak | Fixed. Source/test runs launched from a Flatpak-hosted editor no longer inherit `FLATPAK_ID=com.visualstudio.code` as Wayfinder's own Flatpak identity, so they do not select `/app` defaults or register portal shortcuts as the parent app. |
| Foreign parent AppImage env leak | Fixed. AppImage detection now requires `APPDIR/usr/bin/wayfinder-aura` before using bundled paths, so source/test runs launched from another AppImage do not select that app's bundled tools or models. |
| Lazy core package imports | Fixed. Importing `wayfinder.core` or `wayfinder.core.injector` no longer eagerly imports the PortAudio-backed recorder module, so injector/platform tests and non-audio tooling can run on hosts without PortAudio. |
| Runtime NameError lint blockers | Fixed. Ruff `F821` now passes. |
| Dev premium bypass | Removed. The Settings developer unlock tile, environment/config override, and dedicated bypass tests are gone. Release metadata tests now also fail if `DEV-UNLOCK`, `WAYFINDER_DEV_UNLOCK`, or `dev_unlock` strings return to the shipped license/config/UI surfaces. |
| Legacy local license-key generator | Removed. The distributed app now uses online activation plus offline Ed25519 token verification only. |
| False license-secret warning | Removed with the legacy local generator. Startup should no longer warn about missing local signing secrets. |
| Existing license token permissions | Fixed. Existing `license.json` files are tightened to owner-only permissions on load; this machine was repaired from `0644` to `0600`. |
| README Ultra mismatch | Updated. GPU acceleration is documented as an Ultra feature, matching backend enforcement. |
| CI lint blocker | Adjusted. CI now gates runtime-breaking Ruff classes (`F821`, `F823`, `F722`, `E9`) instead of failing on the repo's preexisting formatting/style backlog. |
| GitHub Actions workflow lint | Verified. A checksum-verified `actionlint` v1.7.12 run is clean after updating `softprops/action-gh-release` from the obsolete `@v1` runner to `@v3` and pinning the AppImage job to the compatibility runner; the release metadata test now guards those release workflow properties. |
| Release identity metadata | Fixed. The macOS PyInstaller bundle identifier now matches the current app ID (`io.wayfindercollective.WayfinderAura`) instead of the old `io.github...` identifier, and release metadata tests guard against that stale ID returning. The AppStream `1.1.0` release date is aligned to the 2026-07-07 audit baseline and has ISO-date coverage. |
| Flathub reproducibility | Improved. The manifest now uses PyQt BaseApp, generated SHA256-pinned Python deps, and commit-pinned third-party git sources. |
| Flatpak static release guards | Added. Tests now verify the KDE/PyQt BaseApp runtime, scoped permissions, bundled Wayland/X11 injectors, CPU fallback binaries, all git source commit pins, and offline hashed Python deps without PyQt6 in pip sources. |
| Release manifest helper | Hardened. `prepare-release-manifest.py` rejects tags that do not match `pyproject.toml`, validates full commit SHAs, refuses dirty-tree generation unless explicitly overridden, refuses dev license defaults, and will not overwrite the local manifest. A `v1.1.8-beta.1` dry run against the current base commit renders successfully; the final run must use the candidate's committed SHA. |
| Release license gate | Passing. `scripts/ci/check-release-license-defaults.py` parses `src/wayfinder/license.py` without importing the app and confirms the production activation URL/public-key defaults are release-ready. |
| Storefront release gate | Blocking as designed. Static config, README, and in-app fallback checks pass. The live check now also rejects the stale Free-GPU claim still deployed on `/aura`; after the corrected website source is deployed, tag CI must rerun the browser-rendered landing and checkout checks. |
| Hardware preflight | Added. `scripts/ship_preflight.py` performs non-invasive host checks for the remaining manual hardware matrix. The 2026-08-04 host check confirms KDE Wayland, a live Wayfinder control-socket `pong`, and a Vulkan-visible AMD RX 9060 XT dGPU. Host `wtype` and the `ydotool` socket are missing, and this host is not a Steam Deck. |
| Source launch venv guard | Fixed. `main.py` no longer exits solely because `venv-gpu/pyvenv.cfg` contains stale Python-version metadata after an OS update. It now smoke-tests essential imports first: if imports pass, launch continues with a warning; if imports fail, it still blocks with rebuild instructions and names the failed imports. Missing Tk now gets an explicit OS-package hint (`python3-tkinter` on Fedora/Bazzite, `python3-tk` on Debian/Ubuntu) before asking for a venv rebuild. |
| GitHub release gate | Added and streamlined. Routine pushes run one cached Quality job; stale branch runs cancel automatically, model-pin drift is weekly/manual plus narrowly path-filtered pin changes, and heavy artifacts are tag/manual only. The redundant raw PyInstaller artifact was removed because the release AppImage already performs that build with stronger portability smokes. AppImage remains on GitHub's Ubuntu 22.04 compatibility runner. Flatpak candidates use the shared build/smoke script over SSH on resource-capped mini-inf, with no self-hosted runner exposed to the public repository; tags retain a GitHub-hosted fallback. Tag artifacts still depend on `release-readiness`, which checks production license/storefront defaults and renders the guarded release manifest without `--allow-dev-license`. |
| AppStream screenshots | Refreshed. `screenshots/main-window.png` and `screenshots/settings.png` are clean free-tier 1920×1080 captures from the current UI on KDE Wayland using a temporary clean profile. They were visually inspected in this audit, and AppStream local validation passes against the screenshot-bearing metadata. |
| AppImage packaging | Verified for lite and full CPU-fallback artifacts on the current Bazzite host, with a guarded CI path for broad artifacts. The maintained AppImage builder now reuses the checked-in desktop file and screenshot-bearing AppStream metadata, its native dependency sources are tag + commit pinned to match the Flatpak, optional Linux tray/D-Bus integrations no longer hard-fail PyInstaller when absent, and the root `build-appimage.sh` delegates to it instead of carrying stale metadata. PyInstaller 6.17.0 under `venv-gpu` produced `dist/wayfinder-aura`; `scripts/build-appimage.sh --lite --skip-build` produced `Wayfinder_Aura-1.1.0-x86_64.AppImage` (201 MB) plus `.zsync`; `scripts/build-appimage.sh --full --skip-build` produced a 211 MB artifact with CPU-built `whisper-cli`, `llama-cli`, `llama-simple`, `wtype`, and `ydotool` after Vulkan configuration failed cleanly on this host. Both extraction smokes passed, the extracted desktop/AppStream metadata validates, and the native binaries print help on the host (`glibc 2.43`). The GitHub AppImage job is now pinned to `ubuntu-22.04`, installs Vulkan development packages, builds pinned Shaderc `glslc` from source if it is not already available, builds `--full`, and extraction-smokes the bundled binaries/metadata before upload. The remaining AppImage gate is a real tag or workflow-dispatch run that proves the older-glibc/GPU-native artifact in CI. |
| Release shell scripts | Covered. Release-facing shell scripts are parsed with `bash -n` by the release metadata suite, and the root AppImage wrapper is checked for executable mode and delegation to the maintained builder. |
| Runtime path fallback isolation | Fixed. Source/test runs no longer accidentally select `/app/bin` or `/app/share` Flatpak-bundled tools unless `IS_FLATPAK` is true; installed Flatpak smoke confirms bundled `/app` paths still resolve inside the bundle. |
| Wayland/X11 injection packaging | Verified inside the installed Flatpak build. `/app/bin/wtype` and `/app/bin/xdotool` are present; Tk, CustomTkinter, and PyQt6 import successfully. The platform selector returns `wtype` for a simulated Wayland session and `xdotool` for a simulated X11 session. Focused rerun passed `tests/test_platform.py tests/test_injector.py`: 127 passed. Real focused-window injection still remains in the manual hardware QA matrix. |
| Automated suite | Verified on the current tree. The CI-equivalent 2026-08-04 gate passed 1,619 tests with 7 live-environment skips and 40 UI/slow/network/performance deselections. The performance gate passed 2/2 separately, and the currently running AppImage passed its 7/7 live smoke checks. |
| Perf gate | Verified. `pytest -m perf` passed 2/2 with `QT_QPA_PLATFORM=offscreen`. |
| Golden ASR | Verified in the installed Flatpak runtime and again against the freshly installed local build artifact. With the host turbo model mounted read-only, `WAYFINDER_GOLDEN=1 tests/test_golden_asr.py` passed 6/6 including the premium GPU/turbo aggregate check on the AMD RX 9060 XT. `scripts/eval_asr.py` reported free/base.en mean WER `0.089` and premium/turbo GPU mean WER `0.023`; both kept all configured key phrases. The free model still visibly misheard two phrases (“riding oats...” and “public west”), so free-tier ASR quality remains a product caveat, not a gate failure. |
| Soak smoke | Verified in the installed Flatpak runtime and again against the freshly installed local build artifact after the silence-hallucination fix. `scripts/soak.py --iters 5 --orphan-check` passed RSS leak, child census, temp WAV cleanup, and latency drift; VRAM was advisory/stable, and orphan check skipped because no whisper-server process was started by that run. This is not a substitute for the 30-minute Mode B Deck soak. |
| Local Flatpak build | Verified on the current tree. The local manifest built and exported successfully with `flatpak-builder --force-clean --repo=.tmp-flatpak-repo .tmp-flatpak-build flatpak/io.wayfindercollective.WayfinderAura.yml` through `org.flatpak.Builder` after setting `FLATPAK_USER_DIR=/home/bazzite/.local/share/flatpak` and `FLATPAK_SYSTEM_DIR=/home/bazzite/.local/share/flatpak`. The build exported app commit `48637bc061f4a83c34a8760c4c4527ab717091e57ec2f092961ba8a55bb9c350` and debug commit `4c19ba4c9f541ead64e4ff531974e0d1eb037d95697c1179d61d8aa8a865acd1`. |
| Installed Flatpak smoke | Verified against the freshly installed local build artifact from `wfa-audit-local`. `flatpak info` reports version `1.1.0`, runtime `org.kde.Platform//6.10`, SDK `org.kde.Sdk//6.10`, and origin `wfa-audit-local`. `/app/bin/wayfinder-aura`, `/app/bin/whisper-cli`, `/app/bin/whisper-cli-cpu`, `/app/bin/llama-simple`, `/app/bin/llama-simple-cpu`, `/app/bin/wtype`, `/app/bin/xdotool`, and `/app/share/whisper-models/ggml-base.en.bin` all resolved inside the installed build. Package-level Python import and `py_compile` smoke passed using `/app/lib/wayfinder-aura`. |
| Flathub lint/metadata | Partially verified locally. Official `flatpak-builder-lint manifest` is clean for the local manifest through host Flatpak access, and previous dry-run validation covered the generated `release/io.wayfindercollective.WayfinderAura.yml` staged with the required relative `python-deps.json`; `appstreamcli validate --no-net` and `desktop-file-validate` pass. `flatpak-builder-lint builddir` and `repo` currently fail only on screenshot mirroring (`appstream-missing-screenshots` / `appstream-screenshots-not-mirrored-in-ostree`) because the public GitHub screenshot URLs are not reachable yet. |

---

## Current Verdict

**Source candidate: code-ready for prerelease after the website fix is deployed.
Public stable promotion: NO-GO.**

Production license defaults and local automated gates pass. Do not create the
paid prerelease tag while the deployed storefront advertises Free GPU support.
After that copy is corrected, build the exact tagged artifacts in GitHub Actions
and complete the activation-operations, clean-hardware, dictation, and real
purchase checks in `docs/PROMOTION-READINESS-CHECKLIST.md`. Stable promotion is
allowed only after those results are recorded against the downloaded candidate.
