# Shipping Checklist — Flathub / Discover / Bazaar

Discover (KDE) and Bazaar (GNOME/Bazzite) install Flatpaks from Flathub, so
"in the app store" means "accepted on Flathub". This file tracks the exact gap
between the repo and a live listing.

## Done (verified in-repo)

- ✅ Real app ID `io.wayfindercollective.WayfinderAura` everywhere
  (manifest, desktop, metainfo, code `FLATPAK_ID` fallbacks, CI, icon script).
- ✅ `appstreamcli validate --no-net` passes; `desktop-file-validate` clean.
  Full network URL validation waits on the public GitHub repo/screenshots.
- ✅ `LICENSE` is the **Elastic License 2.0** (source-available; SPDX `Elastic-2.0`).
  Chosen deliberately for the paid premium model ($60, $29.99 launch price): the repo can stay public
  (required for Flathub source review), Flathub may legally build and redistribute, but stripping or
  circumventing the license-key functionality is prohibited by the license
  text itself. Free download + external license purchase is the established
  Flathub pattern (Sublime Text, Bitwig, Master PDF Editor); Flathub has no
  native payments as of mid-2026.
- ✅ Metainfo: real developer/URLs, ≤35-char summary, OARS rating, branding
  colors, 1.1.0 release notes with date.
- ✅ Screenshot paths referenced by AppStream exist and are refreshed at
  1920×1080:
  `screenshots/main-window.png` and `screenshots/settings.png`.
- ✅ Clean local Flatpak build/export passes with the real app ID, bundled
  base.en model, Xft Tk fonts, Vulkan binaries, CPU fallback binaries, wtype,
  and xdotool. The freshly exported local build was installed and smoke-tested
  from `/app`.
- ✅ Flathub-style Python deps: PyQt6 is provided by
  `com.riverbankcomputing.PyQt.BaseApp//6.10`; the remaining Python packages
  are generated in `flatpak/python-deps.json` with SHA256-pinned sources.
- ✅ External git sources in the Flatpak manifest are tag + commit pinned.
- ✅ Hardware safety nets: name-based mic persistence + pactl-curated picker,
  silent-capture guard, sample-rate fallback w/ resampling, whisper-cli flag
  probing (old/new binaries), download integrity checks, GameMode-aware
  hotkeys, Super+F2/F3 defaults.
- ✅ Official Flathub manifest linter is clean locally. Builddir/repo lint now
  reaches the exported artifact; its only current errors are screenshot
  mirroring errors while the public GitHub screenshot URLs are unreachable.

## Remaining for Flathub submission (in order)

### 1. Production license + storefront
- Set the production activation URL and matching Ed25519 public key in
  `src/wayfinder/license.py`.
- Fix/confirm the checkout and landing URLs, then confirm pricing in
  `src/wayfinder/config.py` and README. Current configured storefront URLs
  return HTTP 200; the Aura landing shell is present, but the checkout route
  exposes only a `Loading checkout...` shell and no Ultra or `$29.99` / `$60`
  copy in the server-rendered payload. Browser-rendered verification reaches the
  card form and shows subtotal `$29.99`, processing fee `$0.90`, and total
  `$30.89`; confirm that fee/copy treatment before tagging.
- See `docs/GO-LIVE-INPUTS.md` for the exact values to provide.

`flatpak/prepare-release-manifest.py` intentionally refuses to generate the
submission manifest while the checked-in license defaults still point at the dev
backend. `--allow-dev-license` is only for local dry-runs. Tag-triggered GitHub
release artifact jobs run the same guarded generation before building or
publishing tag artifacts.

### 2. Public repo + tag
- Push to `github.com/wayfindercollective/wayfinder-aura` (must be public —
  AppStream and screenshot URL checks currently fail while it is private/missing).
- Tag `v1.1.0` matching the metainfo release entry.

### 3. Git source for the app module (tag-time blocker)
The `wayfinder-aura` module uses `type: dir, path: ..` (local builds only).
After the release commit is tagged, generate the submission manifest:

```bash
cd flatpak/
./prepare-release-manifest.py --tag v1.1.0
```

This writes `release/io.wayfindercollective.WayfinderAura.yml` with the app
module sourced from the tag + full commit SHA and `python-deps.json` copied next
to it. The GitHub `build-flatpak` job performs this same selection
automatically for tag refs and builds the generated release manifest; main-branch
CI keeps using the local manifest.

### 4. Clean Flatpak build on the target manifest
Run a clean build after the release source pin is in place:
`flatpak-builder --user --install --force-clean build-dir release/io.wayfindercollective.WayfinderAura.yml`

Tag-time CI is wired to run this against the generated release manifest. It
still needs a real `v1.1.0` tag run after production license defaults are set.

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
PR against `flathub/flathub` (new-pr branch) containing the manifest +
generated pip sources. After acceptance, claim the app via the Flathub
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
- Global hotkeys inside the sandbox use the XDG GlobalShortcuts portal — the
  user binds the shortcut once in their DE's settings when prompted.

## Non-Flatpak channels (later)
- AppImage: lite and full CPU-fallback builds verified locally. PyInstaller
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
