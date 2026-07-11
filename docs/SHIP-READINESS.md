# Ship Readiness — Wayfinder Aura

_Last updated: 2026-07-07. Tracks what remains before a paid public release. Complements `SHIPPING.md`, `docs/GO-LIVE-INPUTS.md`, and `docs/SHIP-VERIFICATION-RUNBOOK.md`._

---

## What Still Blocks Shipping

1. **Production license backend.** `src/wayfinder/license.py` still defaults to the dev Convex deployment:
   - `LICENSE_API_URL`: `https://shiny-goshawk-432.convex.site/activate` (prod)
   - `LICENSE_PUBLIC_KEY_HEX`: dev Ed25519 public key

   I need the production activation URL and matching production Ed25519 public key. They must change together; switching only one side makes production tokens fail offline verification after activation. The release manifest helper now refuses to generate a submission manifest while these defaults are still dev values.

2. **Storefront confirmation.** The in-repo values are internally consistent,
   and the public checkout is browser-verified as of the 2026-07-07 audit.
   Direct HTTP probes returned HTTP 200. The Aura landing route server-rendered
   the expected Wayfinder Aura shell, while the checkout route server-rendered
   only a `Loading checkout...` shell. After installing Playwright Chromium in
   the local audit venv, the rendered checkout was verified to show a real card
   form, `Wayfinder Aura`, `One-time license`, subtotal `$29.99`, a `$0.90`
   processing fee, and total `$30.89`. Confirm the processing-fee copy and
   whether the checkout intentionally omits the words `Ultra` and `$60` regular
   price:
   - `premium_url` in `src/wayfinder/config.py`
   - `premium_info_url` in `src/wayfinder/config.py`
   - README pricing copy
   - in-app Ultra upgrade prompts

3. **Ship channel decision.** Flathub is still the strictest channel. The manifest now has PyQt BaseApp + SHA256-pinned Python deps and tag+commit-pinned third-party git sources, but submission still needs:
   - Public repo + `v1.1.0` tag.
   - Production license defaults set in `src/wayfinder/license.py`.
   - App source generated from the release tag/commit instead of the local `type: dir` source. Local dry-run rendering was verified with `--allow-dev-license --allow-dirty --commit fa483ccf3211fff9041a7a225bf1806815c73c4b` and produced a tag+commit-pinned app source for `v1.1.0`; real tag resolution is still unverified because the `v1.1.0` tag does not exist yet.
   - A clean Flatpak build from that release manifest after production license defaults and the public tag exist. The GitHub `build-flatpak` job now generates and builds the release manifest on tag refs; the remaining gate is a real tag run proving that artifact. The local `type: dir` manifest now builds cleanly on this machine using the `org.flatpak.Builder` app with explicit Flatpak installation-dir env vars.
   - Final tag-time metadata verification.

4. **On-device final QA.** Automated tests cover most behavior, but the following still need real hardware validation:
   - One full microphone → transcription → injection run on Wayland.
   - One full run on X11.
   - Steam Deck Desktop Mode and Game Mode trigger flow.
   - Full microphone → injection path on a dedicated-GPU desktop. The AMD RX 9060 XT turbo/GPU ASR path itself passed golden-audio verification in the installed Flatpak build.
   - Tray menu actions on the real desktop shell.

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
| Release manifest helper | Hardened. `prepare-release-manifest.py` now rejects tags that do not match `pyproject.toml`, validates full commit SHAs, refuses dirty-tree generation unless explicitly overridden, refuses submission-manifest generation while license defaults still point at the dev backend, refuses to overwrite the local manifest, and is covered through its CLI entry point. Local dry-runs require the explicit `--allow-dev-license` escape hatch until production license values are set. It writes `release/io.wayfindercollective.WayfinderAura.yml` with `python-deps.json` beside it so Flathub's app-id filename lint is satisfied. |
| Release license gate | Added. `scripts/ci/check-release-license-defaults.py` parses `src/wayfinder/license.py` without importing the app and fails release-artifact builds while `LICENSE_API_URL` or `LICENSE_PUBLIC_KEY_HEX` still default to the dev backend. Current tree correctly fails that check until production values are provided. |
| Storefront release gate | Added. `scripts/ci/check-storefront-readiness.py` checks that config, README, and in-app fallback values stay synced. Tag/manual release-readiness now installs Playwright Chromium and probes the rendered Aura landing + checkout URLs for positive release markers: product identity, one-time license, card payment, and launch price. Local Playwright verification now reaches the rendered checkout; the remaining storefront call is copy/business confirmation around the visible `$0.90` processing fee and the absence of `Ultra`/`$60` on the checkout page. |
| Hardware preflight | Added. `scripts/ship_preflight.py` performs non-invasive host checks for the remaining manual hardware matrix, including foreign-Flatpak host access, Wayland/X11 session signal, host `wtype`/`xdotool`/`ydotool`/`flatpak`, ydotool daemon socket, Wayfinder control socket ping, Vulkan GPU visibility, dedicated-GPU detection, and Steam Deck OS detection. Current host preflight confirms KDE Wayland, host injectors, ydotool socket, live Wayfinder control socket `pong`, and Vulkan-visible AMD RX 9060 XT dGPU. |
| Source launch venv guard | Fixed. `main.py` no longer exits solely because `venv-gpu/pyvenv.cfg` contains stale Python-version metadata after an OS update. It now smoke-tests essential imports first: if imports pass, launch continues with a warning; if imports fail, it still blocks with rebuild instructions and names the failed imports. Missing Tk now gets an explicit OS-package hint (`python3-tkinter` on Fedora/Bazzite, `python3-tk` on Debian/Ubuntu) before asking for a venv rebuild. |
| GitHub release gate | Added. The CI workflow now triggers on `v*` tag pushes, and tag-triggered PyInstaller, AppImage, Flatpak, and GitHub Release jobs depend on a `release-readiness` job that runs the guarded release-manifest generation without `--allow-dev-license`, so tag artifacts are not built or published from CI while dev license defaults remain embedded. Manual artifact builds also run the production-license-default check. PyInstaller/AppImage artifacts are limited to tag or manual runs, while normal main-branch pushes no longer upload PyInstaller artifacts. The Flatpak tag job now performs a full checkout, generates `release/io.wayfindercollective.WayfinderAura.yml` with `python-deps.json` beside it, and passes that selected release manifest to `flatpak-builder`; main-branch builds continue to use the local manifest. |
| AppStream screenshots | Refreshed. `screenshots/main-window.png` and `screenshots/settings.png` are clean free-tier 1920×1080 captures from the current UI on KDE Wayland using a temporary clean profile. They were visually inspected in this audit, and AppStream local validation passes against the screenshot-bearing metadata. |
| AppImage packaging | Verified for lite and full CPU-fallback artifacts on the current Bazzite host, with a guarded CI path for broad artifacts. The maintained AppImage builder now reuses the checked-in desktop file and screenshot-bearing AppStream metadata, its native dependency sources are tag + commit pinned to match the Flatpak, optional Linux tray/D-Bus integrations no longer hard-fail PyInstaller when absent, and the root `build-appimage.sh` delegates to it instead of carrying stale metadata. PyInstaller 6.17.0 under `venv-gpu` produced `dist/wayfinder-aura`; `scripts/build-appimage.sh --lite --skip-build` produced `Wayfinder_Aura-1.1.0-x86_64.AppImage` (201 MB) plus `.zsync`; `scripts/build-appimage.sh --full --skip-build` produced a 211 MB artifact with CPU-built `whisper-cli`, `llama-cli`, `llama-simple`, `wtype`, and `ydotool` after Vulkan configuration failed cleanly on this host. Both extraction smokes passed, the extracted desktop/AppStream metadata validates, and the native binaries print help on the host (`glibc 2.43`). The GitHub AppImage job is now pinned to `ubuntu-22.04`, installs Vulkan development packages, builds pinned Shaderc `glslc` from source if it is not already available, builds `--full`, and extraction-smokes the bundled binaries/metadata before upload. The remaining AppImage gate is a real tag or workflow-dispatch run that proves the older-glibc/GPU-native artifact in CI. |
| Release shell scripts | Covered. Release-facing shell scripts are parsed with `bash -n` by the release metadata suite, and the root AppImage wrapper is checked for executable mode and delegation to the maintained builder. |
| Runtime path fallback isolation | Fixed. Source/test runs no longer accidentally select `/app/bin` or `/app/share` Flatpak-bundled tools unless `IS_FLATPAK` is true; installed Flatpak smoke confirms bundled `/app` paths still resolve inside the bundle. |
| Wayland/X11 injection packaging | Verified inside the installed Flatpak build. `/app/bin/wtype` and `/app/bin/xdotool` are present; Tk, CustomTkinter, and PyQt6 import successfully. The platform selector returns `wtype` for a simulated Wayland session and `xdotool` for a simulated X11 session. Focused rerun passed `tests/test_platform.py tests/test_injector.py`: 127 passed. Real focused-window injection still remains in the manual hardware QA matrix. |
| Automated suite | Verified on the current tree. Latest host audit venv full suite: 1157 collected with 9 collection-time skips; 1141 passed, 25 skipped after installing `evdev` and `cryptography` in `venv-gpu`. Live host socket smoke passed 7/7 through `flatpak-spawn --host`. Installed Flatpak runtime suite with current source mounted into the installed app runtime: 1276 collected, 1263 passed, 13 skipped. Skips are opt-in live/hardware, UI-display, or golden-audio checks. |
| Perf gate | Verified. `pytest -m perf` passed 2/2 with `QT_QPA_PLATFORM=offscreen`. |
| Golden ASR | Verified in the installed Flatpak runtime and again against the freshly installed local build artifact. With the host turbo model mounted read-only, `WAYFINDER_GOLDEN=1 tests/test_golden_asr.py` passed 6/6 including the premium GPU/turbo aggregate check on the AMD RX 9060 XT. `scripts/eval_asr.py` reported free/base.en mean WER `0.089` and premium/turbo GPU mean WER `0.023`; both kept all configured key phrases. The free model still visibly misheard two phrases (“riding oats...” and “public west”), so free-tier ASR quality remains a product caveat, not a gate failure. |
| Soak smoke | Verified in the installed Flatpak runtime and again against the freshly installed local build artifact after the silence-hallucination fix. `scripts/soak.py --iters 5 --orphan-check` passed RSS leak, child census, temp WAV cleanup, and latency drift; VRAM was advisory/stable, and orphan check skipped because no whisper-server process was started by that run. This is not a substitute for the 30-minute Mode B Deck soak. |
| Local Flatpak build | Verified on the current tree. The local manifest built and exported successfully with `flatpak-builder --force-clean --repo=.tmp-flatpak-repo .tmp-flatpak-build flatpak/io.wayfindercollective.WayfinderAura.yml` through `org.flatpak.Builder` after setting `FLATPAK_USER_DIR=/home/bazzite/.local/share/flatpak` and `FLATPAK_SYSTEM_DIR=/home/bazzite/.local/share/flatpak`. The build exported app commit `48637bc061f4a83c34a8760c4c4527ab717091e57ec2f092961ba8a55bb9c350` and debug commit `4c19ba4c9f541ead64e4ff531974e0d1eb037d95697c1179d61d8aa8a865acd1`. |
| Installed Flatpak smoke | Verified against the freshly installed local build artifact from `wfa-audit-local`. `flatpak info` reports version `1.1.0`, runtime `org.kde.Platform//6.10`, SDK `org.kde.Sdk//6.10`, and origin `wfa-audit-local`. `/app/bin/wayfinder-aura`, `/app/bin/whisper-cli`, `/app/bin/whisper-cli-cpu`, `/app/bin/llama-simple`, `/app/bin/llama-simple-cpu`, `/app/bin/wtype`, `/app/bin/xdotool`, and `/app/share/whisper-models/ggml-base.en.bin` all resolved inside the installed build. Package-level Python import and `py_compile` smoke passed using `/app/lib/wayfinder-aura`. |
| Flathub lint/metadata | Partially verified locally. Official `flatpak-builder-lint manifest` is clean for the local manifest through host Flatpak access, and previous dry-run validation covered the generated `release/io.wayfindercollective.WayfinderAura.yml` staged with the required relative `python-deps.json`; `appstreamcli validate --no-net` and `desktop-file-validate` pass. `flatpak-builder-lint builddir` and `repo` currently fail only on screenshot mirroring (`appstream-missing-screenshots` / `appstream-screenshots-not-mirrored-in-ostree`) because the public GitHub screenshot URLs are not reachable yet. |

---

## Current Verdict

Not ship-ready yet.

The app is much closer: the immediate dictation-path failure, stale tray handoff, dev unlock backdoor, dead local license-key path, impossible CI lint gate, and major Flathub reproducibility gaps have been addressed locally. The remaining hard blockers are production licensing material, storefront confirmation, release-source packaging, and real hardware sign-off across Wayland, X11, Steam Deck, and dedicated GPU systems.

Do not tag a paid public release until the production license URL/public key pair is set and the hardware matrix above is checked.
