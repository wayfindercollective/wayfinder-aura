# Screenshots

Screenshots for AppStream/Flathub and release listings.

## Current published captures

| File | Content |
|------|---------|
| `main-window.png` | Dictate tab (recapture as **mid-recording** before Flathub) |
| `settings.png` | Settings tab (must show free-legal **base.en**, not Turbo) |

Both images are 1920×1080 PNGs. Re-shoot before store submission using the recipe below.

## Planned store set (5 required for launch)

| File | Content | Caption angle |
|------|---------|---------------|
| `main-window.png` | Dictate **mid-recording**: live waveform, recording state, short Last Transcription | Dictate into any app |
| `settings.png` | Free-legal defaults: **base.en**, Local mode, privacy banner, mic | Private local processing |
| `overlay.png` | Glassmorphic status overlay over real desktop (browser/IDE) | Always-visible status |
| `style.png` | Style tab, one clear preset (e.g. Professional); no caricature | Tone-aware cleanup |
| `welcome.png` | Welcome card (hotkey step preferred) | Guided first run |

After capturing the new files, add them under `<screenshots>` in
`flatpak/io.wayfindercollective.WayfinderAura.metainfo.xml`.

## Capture recipe (mandatory)

Shared for all shots:

- Clean temporary free-tier profile, **no license**.
- Force free-legal model: **`ggml-base.en.bin` / base.en** — do **not** capture
  from default source/dev config (that defaults to turbo). Prefer an installed
  Flatpak or an explicit config override before capture.
- 1920×1080 PNG on KDE Wayland (or document the DE).
- Prefer Lucide/plain chrome in stills; no decorative emoji clutter.

Profile flags by shot (welcome is incompatible with a completed welcome flag):

| Shots | `setup_completed` | `welcome_completed` | Notes |
|-------|-------------------|---------------------|-------|
| `main-window`, `settings`, `overlay`, `style` | `true` | `true` | Returning free user; no welcome card |
| `welcome.png` | `true` | **`false`** | Startup shows welcome via `first_run_plan`, **or** explicitly invoke `show_welcome_pane` |

### Suggested temp-profile workflow

```bash
# Example: isolated config dir (adjust launch method for Flatpak/source)
export XDG_CONFIG_HOME=/tmp/wfa-shot-config
export XDG_DATA_HOME=/tmp/wfa-shot-data
mkdir -p "$XDG_CONFIG_HOME/wayfinder-aura"
# Write a minimal config.json with:
#   setup_completed, welcome_completed (per table), model_path → ggml-base.en.bin,
#   processing_mode=local, no license file
```

Document the DE, resolution, and model path used in each recapture commit message.
