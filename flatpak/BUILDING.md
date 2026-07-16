# Building Wayfinder Aura Flatpak

This guide explains how to build and publish the Wayfinder Aura Flatpak.

## Prerequisites

Install Flatpak development tools:

```bash
# Fedora/Bazzite
sudo dnf install flatpak-builder appstream desktop-file-utils

# Install the SDK
flatpak install flathub \
  org.kde.Platform//6.10 \
  org.kde.Sdk//6.10 \
  com.riverbankcomputing.PyQt.BaseApp//6.10 \
  org.freedesktop.Sdk.Extension.rust-stable//25.08
```

KDE 6.10 is based on Freedesktop **25.08**, so the rust-stable SDK
extension branch is `25.08` (needed to compile cryptography / jiter /
pydantic-core from source for Flathub).


## Quick Build (Local Testing)

```bash
cd flatpak/

# Build and install locally
flatpak-builder --user --install --force-clean build-dir io.wayfindercollective.WayfinderAura.yml

# Run the app
flatpak run io.wayfindercollective.WayfinderAura
```

On Bazzite systems where host `flatpak-builder` is not installed, the Flathub
Builder Flatpak can run the same build. If its host-command path cannot see the
user-installed KDE SDK/BaseApp, pass the Flatpak installation directories
explicitly:

```bash
cd /var/home/bazzite/Dev/wayfinder-aura
flatpak run --command=sh \
  --filesystem="$PWD":rw \
  --env=FLATPAK_USER_DIR="$HOME/.local/share/flatpak" \
  --env=FLATPAK_SYSTEM_DIR="$HOME/.local/share/flatpak" \
  org.flatpak.Builder \
  -c 'flatpak-builder --force-clean --repo=.tmp-flatpak-repo .tmp-flatpak-build flatpak/io.wayfindercollective.WayfinderAura.yml'
```

## Generating Python Dependencies

Before building, you need to generate the Python package sources:

```bash
# Install the generator tool
python -m pip install flatpak-pip-generator

# Generate sources
./generate-pip-sources.sh
```

This creates `python-deps.json` which contains all the pip packages with their SHA256 hashes.
PyQt6 itself is provided by `com.riverbankcomputing.PyQt.BaseApp`; the generated
file covers the remaining Python runtime packages. The generated file is checked
in so Flatpak builds do not need network access for Python packages.

## File Structure

```
flatpak/
├── io.wayfindercollective.WayfinderAura.yml      # Main Flatpak manifest
├── io.wayfindercollective.WayfinderAura.desktop  # Desktop entry
├── io.wayfindercollective.WayfinderAura.metainfo.xml  # AppStream metadata
├── wayfinder-aura-launcher.sh            # Launch script
├── flatpak-requirements.txt               # Python deps for Flatpak
├── python-deps.json                       # Generated pip sources
├── generate-pip-sources.sh                # Helper script
└── BUILDING.md                            # This file
```

## App ID

The app ID is `io.wayfindercollective.WayfinderAura`, derived from the
Wayfinder Collective domain (`wayfindercollective.io`). The upstream source
still lives at `github.com/wayfindercollective/wayfinder-aura`; using the owned
domain for the app ID avoids Flathub's `io.github` repo-name coupling. If you
fork this project under a different account, rename the three
`io.wayfindercollective.WayfinderAura.*` files and update the ID inside
the manifest (app-id and install paths), the metainfo (id, launchable,
developer), the desktop file (Icon), `flatpak/resize-icons.py`, and the
`FLATPAK_ID` fallbacks in `wayfinder_main.py` / `src/wayfinder/config.py`.

## Publishing to Flathub

### 1. Prepare Your Repository

1. Push your code to the public GitHub repository
2. Create a signed release/tag for the version
3. Confirm the screenshots in `screenshots/` match the current UI
4. Set the production license activation URL and matching Ed25519 public key in
   `src/wayfinder/license.py`; see `docs/GO-LIVE-INPUTS.md`
5. Generate a release manifest from the tag:

```bash
python flatpak/prepare-release-manifest.py --tag v1.1.0
```

The helper writes `release/io.wayfindercollective.WayfinderAura.yml` and copies
`python-deps.json` beside it, replacing the local `type: dir` source with the
pinned public git tag and commit that Flathub expects. It refuses to run while
the checked-in license defaults still point at the dev backend.
`--allow-dev-license` is for local dry-runs only, not submission builds.

### 2. Submit to Flathub

1. Fork https://github.com/flathub/flathub
2. Create a new repository named after your app ID
3. Copy your manifest and supporting files
4. Submit a PR to add your app

Detailed instructions: https://docs.flathub.org/docs/for-app-authors/submission

### 3. Flathub Requirements Checklist

- [x] Valid AppStream metainfo.xml
- [x] Desktop file with correct categories
- [x] Icon in PNG format at 128px and larger
- [x] Current screenshots in `screenshots/` (recaptured 2026-07-09)
- [x] OARS content rating
- [x] Passes `appstreamcli validate --no-net`
- [x] No build-time network for bundled Python dependencies
- [x] Runtime network permission is justified for cloud APIs, license activation, and model downloads
- [x] Passes official `flatpak-builder-lint manifest`
- [ ] Production license endpoint and Ed25519 public key are set
- [ ] Public GitHub tag exists and matches the release manifest commit
- [ ] Screenshot raw.githubusercontent.com URLs return HTTP 200 on the public default branch
- [ ] Flathub submission PR opened + reviewer signoff on final permissions

Step-by-step owner actions: **`docs/FLATHUB-HANDOFF.md`**.

### 4. Validate Before Submitting

```bash
# Validate AppStream metadata
appstreamcli validate --no-net io.wayfindercollective.WayfinderAura.metainfo.xml

# Validate desktop file
desktop-file-validate io.wayfindercollective.WayfinderAura.desktop

# Validate the manifest with Flathub's linter
flatpak run --command=flatpak-builder-lint org.flatpak.Builder manifest io.wayfindercollective.WayfinderAura.yml

# After a local build, lint the actual build output and exported repo
flatpak run --command=flatpak-builder-lint \
  --filesystem="$PWD":rw org.flatpak.Builder builddir "$PWD/.tmp-flatpak-build"
flatpak run --command=flatpak-builder-lint \
  --filesystem="$PWD":rw org.flatpak.Builder repo "$PWD/.tmp-flatpak-repo"

# Test the built app
flatpak run io.wayfindercollective.WayfinderAura
```

Until the GitHub repository and screenshot URLs are public, the builddir/repo
lint can fail with `appstream-missing-screenshots` or
`appstream-screenshots-not-mirrored-in-ostree`. Treat any other error as a real
packaging issue.

## Permissions Explained

The manifest requests these permissions:

| Permission | Reason |
|------------|--------|
| `--socket=wayland` | Display on Wayland |
| `--socket=fallback-x11` | Display on X11 |
| `--socket=pulseaudio` | Record audio |
| `--talk-name=org.freedesktop.Notifications` | Desktop notifications |
| `--talk-name=org.kde.StatusNotifierWatcher` | KDE/StatusNotifier tray icon |
| `--filesystem=xdg-run/wayfinder-aura:create` | Host-trigger socket for shortcuts and Steam Deck R4 |
| `--device=dri` | GPU acceleration for whisper |

## Troubleshooting

### "No audio devices found"
The app needs PulseAudio socket access. Make sure `--socket=pulseaudio` is set.

### Hotkey not working
Flatpak can't access `/dev/input` directly. Use the GlobalShortcuts portal or configure a system shortcut to send a D-Bus signal.

### Whisper model not found
Bundled models live under `/app/share/whisper-models`; user-downloaded models
live under `~/.local/share/wayfinder-aura/whisper-models/`.

### Build fails on Python packages
Regenerate `python-deps.json` with the latest versions:
```bash
./generate-pip-sources.sh
```

## Testing Changes

```bash
# Rebuild after changes
flatpak-builder --user --install --force-clean build-dir io.wayfindercollective.WayfinderAura.yml

# Check logs
flatpak run io.wayfindercollective.WayfinderAura 2>&1 | tee app.log
```

