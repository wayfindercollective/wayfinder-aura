# Shipping Checklist — Flathub / Discover / Bazaar

Discover (KDE) and Bazaar (GNOME/Bazzite) install Flatpaks from Flathub, so
"in the app store" means "accepted on Flathub". This file tracks the exact gap
between the repo and a live listing.

## Done (verified in-repo)

- ✅ Real app ID `io.github.wayfindercollective.WayfinderAura` everywhere
  (manifest, desktop, metainfo, code `FLATPAK_ID` fallbacks, CI, icon script).
- ✅ `appstreamcli validate` clean; `desktop-file-validate` clean (both files).
- ✅ `LICENSE` is the **Elastic License 2.0** (source-available; SPDX `Elastic-2.0`).
  Chosen deliberately for the $20 premium model: the repo can stay public
  (required for the io.github app ID — Flathub's linter HTTP-checks the repo
  URL), Flathub may legally build and redistribute, but stripping or
  circumventing the license-key functionality is prohibited by the license
  text itself. Free download + external license purchase is the established
  Flathub pattern (Sublime Text, Bitwig, Master PDF Editor); Flathub has no
  native payments as of mid-2026.
- ✅ Metainfo: real developer/URLs, ≤35-char summary, OARS rating, branding
  colors, 1.1.0 release notes with date.
- ✅ Manifest builds and runs on SteamOS (Deck-validated, SIGILL-safe CPU
  baseline, bundled base.en model, Xft Tk fonts).
- ✅ Hardware safety nets: name-based mic persistence + pactl-curated picker,
  silent-capture guard, sample-rate fallback w/ resampling, whisper-cli flag
  probing (old/new binaries), download integrity checks, GameMode-aware
  hotkeys, Super+F2/F3 defaults.

## Remaining for Flathub submission (in order)

### 1. Offline Python sources (Flathub blocker)
The `python-deps` module pip-installs `requirements.txt` with
`--share=network` at build time. Flathub builds run **without network** —
replace it with generated, hashed sources:

```bash
# Sync flatpak/generate-pip-sources.sh's embedded flatpak-requirements.txt
# with the real requirements.txt FIRST — it currently omits PyQt6, pynput,
# requests, openai, groq.
pip install flatpak-pip-generator   # or use flathub/flatpak-builder-tools
cd flatpak && ./generate-pip-sources.sh
```

Then swap the module in the manifest for the generated `python-deps.json`
and verify with a clean local build:
`flatpak-builder --user --install --force-clean build-dir io.github.wayfindercollective.WayfinderAura.yml`

**PyQt6 note:** pip-generating PyQt6 is the known hard case (huge sdist, sip).
The standard Flathub route is `base: com.riverbankcomputing.PyQt.BaseApp`
(provides PyQt6 prebuilt) + pip-generated sources for the rest. Prefer that
over fighting the PyQt6 wheel.

### 2. Git source for the app module (Flathub blocker)
The `wayfinder-aura` module uses `type: dir, path: ..` (local builds only).
For submission, point it at the tagged release:

```yaml
sources:
  - type: git
    url: https://github.com/wayfindercollective/wayfinder-aura.git
    tag: v1.1.0
    commit: <sha>
```

### 3. Screenshots (metainfo URLs 404 until these exist)
Commit real screenshots at the paths the metainfo references:
- `screenshots/main-window.png` (default screenshot — main window, recording)
- `screenshots/settings.png` (settings panel)
16:9-ish, ≥1248×702 preferred by Flathub quality guidelines, dark theme.

### 4. Public repo + tag
- Push to `github.com/wayfindercollective/wayfinder-aura` (must be public —
  Flathub verifies `io.github.wayfindercollective.*` ownership against it).
- Tag `v1.1.0` matching the metainfo release entry.

### 5. Submit
PR against `flathub/flathub` (new-pr branch) containing the manifest +
generated pip sources. After acceptance, claim the app via the Flathub
dashboard for the verified checkmark.

## Known limitations to disclose in the listing
- GNOME Wayland / sway: the overlay can't self-position (no KWin scripting,
  Qt can't place windows on Wayland) — it appears as a normal window where
  the compositor puts it. X11 and KDE Plasma are fully supported.
  (Future fix: LayerShellQt.)
- Whisper runs CPU-only inside the sandbox (ggml-vulkan is disabled in the
  manifest — it SIGSEGVs in-sandbox on RDNA2). base.en is ~2× real-time on a
  Zen 2 APU; faster on desktop CPUs.
- Global hotkeys inside the sandbox use the XDG GlobalShortcuts portal — the
  user binds the shortcut once in their DE's settings when prompted.

## Non-Flatpak channels (later)
- AppImage: CI builds one (`build-appimage` job) — needs the same metadata
  refresh + update-information embedding before promoting it.
- Distro packages (AUR/COPR/PPA): out of scope until Flathub is live.
