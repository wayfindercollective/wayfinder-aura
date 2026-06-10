# Building Wayfinder Aura Flatpak

This guide explains how to build and publish the Wayfinder Aura Flatpak.

## Prerequisites

Install Flatpak development tools:

```bash
# Fedora/Bazzite
sudo dnf install flatpak-builder appstream-util

# Install the SDK
flatpak install flathub org.freedesktop.Platform//24.08 org.freedesktop.Sdk//24.08
```

## Quick Build (Local Testing)

```bash
cd flatpak/

# Build and install locally
flatpak-builder --user --install --force-clean build-dir io.github.wayfindercollective.WayfinderAura.yml

# Run the app
flatpak run io.github.wayfindercollective.WayfinderAura
```

## Generating Python Dependencies

Before building, you need to generate the Python package sources:

```bash
# Install the generator tool
pip install flatpak-pip-generator

# Generate sources
./generate-pip-sources.sh
```

This creates `python-deps.json` which contains all the pip packages with their SHA256 hashes.

## File Structure

```
flatpak/
├── io.github.wayfindercollective.WayfinderAura.yml      # Main Flatpak manifest
├── io.github.wayfindercollective.WayfinderAura.desktop  # Desktop entry
├── io.github.wayfindercollective.WayfinderAura.metainfo.xml  # AppStream metadata
├── wayfinder-aura-launcher.sh            # Launch script
├── flatpak-requirements.txt               # Python deps for Flatpak
├── python-deps.json                       # Generated pip sources
├── generate-pip-sources.sh                # Helper script
└── BUILDING.md                            # This file
```

## App ID

The app ID is `io.github.wayfindercollective.WayfinderAura`, derived from the
upstream repository (github.com/wayfindercollective/wayfinder-aura) per Flathub
convention. If you fork this project under a different account, rename the three
`io.github.wayfindercollective.WayfinderAura.*` files and update the ID inside
the manifest (app-id and install paths), the metainfo (id, launchable,
developer), the desktop file (Icon), `flatpak/resize-icons.py`, and the
`FLATPAK_ID` fallbacks in `wayfinder_main.py` / `src/wayfinder/config.py`.

## Publishing to Flathub

### 1. Prepare Your Repository

1. Push your code to GitHub with the Flatpak files
2. Create a release/tag for the version
3. Take screenshots for the app listing (at least one required)
4. Update the metainfo.xml with real screenshot URLs

### 2. Submit to Flathub

1. Fork https://github.com/flathub/flathub
2. Create a new repository named after your app ID
3. Copy your manifest and supporting files
4. Submit a PR to add your app

Detailed instructions: https://docs.flathub.org/docs/for-app-authors/submission

### 3. Flathub Requirements Checklist

- [ ] Valid AppStream metainfo.xml
- [ ] Desktop file with correct categories
- [ ] Icon in SVG or PNG (at least 128x128)
- [ ] At least one screenshot
- [ ] OARS content rating
- [ ] Passes `appstream-util validate`
- [ ] Minimal permissions (avoid `--filesystem=home`)
- [ ] No network access at runtime unless necessary

### 4. Validate Before Submitting

```bash
# Validate AppStream metadata
appstream-util validate io.github.wayfindercollective.WayfinderAura.metainfo.xml

# Validate desktop file
desktop-file-validate io.github.wayfindercollective.WayfinderAura.desktop

# Test the built app
flatpak run io.github.wayfindercollective.WayfinderAura
```

## Permissions Explained

The manifest requests these permissions:

| Permission | Reason |
|------------|--------|
| `--socket=wayland` | Display on Wayland |
| `--socket=fallback-x11` | Display on X11 |
| `--socket=pulseaudio` | Record audio |
| `--talk-name=org.freedesktop.portal.GlobalShortcuts` | System-wide hotkey |
| `--talk-name=org.freedesktop.portal.RemoteDesktop` | Text injection |
| `--device=dri` | GPU acceleration for whisper |

## Troubleshooting

### "No audio devices found"
The app needs PulseAudio socket access. Make sure `--socket=pulseaudio` is set.

### Hotkey not working
Flatpak can't access `/dev/input` directly. Use the GlobalShortcuts portal or configure a system shortcut to send a D-Bus signal.

### Whisper model not found
Models are stored in `~/.local/share/whisper.cpp/`. The Flatpak can access this via the filesystem permission.

### Build fails on Python packages
Regenerate `python-deps.json` with the latest versions:
```bash
./generate-pip-sources.sh
```

## Testing Changes

```bash
# Rebuild after changes
flatpak-builder --user --install --force-clean build-dir io.github.wayfindercollective.WayfinderAura.yml

# Check logs
flatpak run io.github.wayfindercollective.WayfinderAura 2>&1 | tee app.log
```









