# Screenshots

Screenshots for AppStream/Flathub and release listings.

**Last recapture:** 2026-07-09 via `scripts/capture_store_screenshots.py` (isolated free-tier profile, 1920×1080 brand composite).

## Current published captures (1920×1080)

| File | Content |
|------|---------|
| `main-window.png` | Dictate mid-recording (rose waveform, Listening, demo transcript, Free pill) |
| `settings.png` | Settings → Audio essentials (free-tier profile, no Turbo) |
| `overlay.png` | Live recording feedback / status chrome |
| `style.png` | Style tab with Professional selected |
| `welcome.png` | First-run welcome card (mic step) |

## Recapture

From a KDE Wayland session (portal screenshot permission already granted once):

```bash
cd /path/to/wayfinder-aura
python3 scripts/capture_store_screenshots.py
```

The script:

1. Quits any running instance (does **not** touch your real `~/.config/wayfinder-aura`)
2. Launches with an isolated free-tier profile (`base.en`, Local, no license)
3. Uses the control socket for tab/recording state
4. Captures via xdg-desktop-portal, crops the window, composites on brand `#0D1117`
5. Writes the five PNGs above

### Profile flags

| Shots | `setup_completed` | `welcome_completed` |
|-------|-------------------|---------------------|
| main-window, settings, style, overlay | `true` | `true` |
| welcome | `true` | `false` |

Force free-legal model: `~/whisper.cpp/models/ggml-base.en.bin` (or Flatpak bundled base.en).
