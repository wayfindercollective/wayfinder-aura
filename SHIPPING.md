# Shipping Checklist — Flathub / Discover / Bazaar

Discover (KDE) and Bazaar (GNOME/Bazzite) install Flatpaks from Flathub, so
"in the app store" means "accepted on Flathub". This file tracks the exact gap
between the repo and a live listing.

## Done (verified in-repo)

- ✅ Real app ID `io.wayfindercollective.WayfinderAura` everywhere
  (manifest, desktop, metainfo, code `FLATPAK_ID` fallbacks, CI, icon script).
- ✅ `appstreamcli validate --no-net` passes; `desktop-file-validate` clean;
  every public GitHub screenshot URL returns HTTP 200.
- ✅ `LICENSE` is the **Elastic License 2.0** (source-available; SPDX `Elastic-2.0`).
  Chosen deliberately for the paid premium model ($60, $29.99 launch price): the repo can stay public
  (required for Flathub source review), Flathub may legally build and redistribute, but stripping or
  circumventing the license-key functionality is prohibited by the license
  text itself. Free download + external license purchase is the established
  Flathub pattern (Sublime Text, Bitwig, Master PDF Editor); Flathub has no
  native payments as of mid-2026.
- ✅ Metainfo: real developer/URLs, ≤35-char summary, OARS rating, branding
  colors, and a dated `1.1.8-beta.10` candidate entry.
- ✅ Screenshot paths referenced by AppStream exist and are refreshed at
  1920×1080:
  `screenshots/main-window.png` and `screenshots/settings.png`.
- ✅ Clean local Flatpak build/export passes with the real app ID, bundled
  base.en model, Xft Tk fonts, Vulkan binaries, CPU fallback binaries, wtype,
  and xdotool. The freshly exported local build was installed and smoke-tested
  from `/app`.
- ✅ Flathub-style Python deps: PyQt6 is provided by
  `com.riverbankcomputing.PyQt.BaseApp//6.11`; the remaining Python packages
  are generated in `flatpak/python-deps.json` with SHA256-pinned sources.
- ✅ External git sources in the Flatpak manifest are tag + commit pinned.
- ✅ Hardware safety nets: name-based mic persistence + pactl-curated picker,
  silent-capture guard, sample-rate fallback w/ resampling, whisper-cli flag
  probing (old/new binaries), download integrity checks, GameMode-aware
  hotkeys, Super+F2/F3 defaults.
- ✅ Official Flathub manifest linter is clean locally. Builddir/repo lint now
  reaches the exported artifact; its only current errors are the expected
  screenshot-mirroring checks that clear after Flathub imports the screenshots.

## Remaining for Flathub submission (in order)

See **`docs/FLATHUB-HANDOFF.md`** for the full owner checklist (AI exception,
reviewer Ultra keys, human PR only).

### 1. Production license + storefront
- ✅ Production activation URL is set (`shiny-goshawk-432`).
- Deploy the pending storefront correction so Free is Base/Base.en on CPU and
  every GPU path is Ultra-only, then rerun the browser storefront gate.
- Confirm checkout fee/copy is final (`premium_url` stable alias).
- See `docs/GO-LIVE-INPUTS.md`.

### 2. Public repo + tag
- Repo is public. **`v1.1.8-beta.10`** is published; create stable **`v1.1.8`**
  only after hands-on signoff. Never move an existing tag.
- CI on `main` must be green before tagging.

### 3. Git source for the app module (tag-time blocker)
The `wayfinder-aura` module uses `type: dir, path: ..` (local builds only).
After the release commit is tagged:

```bash
python3 flatpak/prepare-release-manifest.py --tag v1.1.8
```

### 4. Clean Flatpak build on the target manifest
`flatpak-builder` (or `org.flatpak.Builder`) against the **release** YAML.
Compiled Python deps are still pinned **manylinux wheels**. A first-submission
exception is required unless they are converted to complete offline source builds.

Current Bazzite audit note: host `flatpak-builder` is absent, but the
`org.flatpak.Builder` app works when its host-command path is pointed at the
user Flatpak installation:

```bash
flatpak run --command=sh \
  --filesystem="$PWD":rw \
  --env=FLATPAK_USER_DIR="$HOME/.local/share/flatpak" \
  --env=FLATPAK_SYSTEM_DIR="$HOME/.local/share/flatpak" \
  org.flatpak.Builder \
  -c 'flatpak-builder --force-clean --repo=.tmp-flatpak-repo .tmp-flatpak-build flatpak/io.wayfindercollective.WayfinderAura.yml'
```

### 5. Submit
PR against `flathub/flathub` (new-pr branch) containing the manifest,
generated pip sources, and `flathub.json`. After acceptance, claim the app via the Flathub
dashboard for the verified checkmark.

## Known limitations to disclose in the listing
- GNOME Wayland / sway: the overlay can't self-position (no KWin scripting,
  Qt can't place windows on Wayland) — it appears as a normal window where
  the compositor puts it. X11 and KDE Plasma are intended supported paths and
  remain part of final release signoff.
  (Future fix: LayerShellQt.)
- GPU transcription is packaged for the sandbox: the Flatpak ships a Vulkan
  `whisper-cli` plus `whisper-cli-cpu`, and the app has fallback logic for
  machines where Vulkan init fails. Final runtime signoff is still required on
  Steam Deck and at least one dedicated GPU system before a paid public release.
- Global hotkeys inside the sandbox use the XDG GlobalShortcuts portal
  (PyGObject is bundled for this; record and style-cycle are both registered).
  The user approves the binding once when prompted, or sets it in System
  Settings → Shortcuts. Note the portal only makes the hotkeys *fire* on
  Wayland — text injection into native Wayland windows remains a separate,
  open limitation (the bundled xdotool injects into X11/XWayland windows).
  AppImage and from-source installs do not use the portal; they read
  /dev/input directly via evdev.

## Non-Flatpak channels (later)
- AppImage: historical lite and full CPU-fallback builds were verified locally.
  PyInstaller
  6.17.0 under `venv-gpu` produced `dist/wayfinder-aura`;
  `scripts/build-appimage.sh --lite --skip-build` produced
  `Wayfinder_Aura-1.1.0-x86_64.AppImage` (201 MB) and `.zsync`;
  `scripts/build-appimage.sh --full --skip-build` produced a 211 MB artifact
  containing executable CPU-built `whisper-cli`, `llama-cli`, `llama-simple`,
  `wtype`, and `ydotool` after clean Vulkan-to-CPU fallback. Extraction smoke
  and extracted desktop/AppStream validation passed for the full artifact, and
  the native binaries print help on the Bazzite build host (`glibc 2.43`). The
  AppImage builder now copies the same desktop and screenshot-bearing AppStream
  metadata used by the Flatpak package.
  The GitHub AppImage job is pinned to `ubuntu-22.04` for an older-glibc
  baseline, installs Vulkan development packages, builds pinned Shaderc `glslc`
  from source if the runner does not provide it, and extraction-smokes bundled
  binaries/metadata before upload. Broad AppImage distribution still needs a
  tag or workflow-dispatch run proving that CI artifact.
- Distro packages (AUR/COPR/PPA): out of scope until Flathub is live.
