# Wayfinder Aura — Website Copy Brief

_Source of truth for public product copy. Keep in sync with `README.md` Free vs Ultra table and in-app Ultra prompts. Last aligned: presentation launch plan v3._

## Product Summary

**Wayfinder Aura** is local-first voice dictation for Linux. Press a hotkey, speak, and your words are typed wherever your cursor is. By default, transcription runs on *your* machine with whisper.cpp — offline, in any app. Optional GPU and cloud backends are opt-in.

## Hero statements (pick one)

1. "Your voice, your machine, your privacy."
2. "Local AI dictation that feels instant."
3. "Type with your voice. No cloud required."
4. "Press a key. Speak. Your words appear wherever your cursor is."

## Core value propositions

### 1. Local by default
- Speech-to-text and AI cleanup both run locally out of the box
- No audio uploads unless you enable cloud backends and supply your own keys
- Weekly model-update checks are toggleable; license activation contacts the server once

### 2. Fast where it counts
- Ultra unlocks Vulkan GPU acceleration (AMD / Intel / NVIDIA) with CPU auto-fallback
- Chunked recording (Ultra) for long dictations with live feedback

### 3. Writes like you, only cleaner
- Local LLM cleanup (Gemma 3 1B by default): strip ums, fix punctuation, match tone
- Output styles: Minimal, Professional, Casual, Dev, Personal
- Cycle styles with Super+F3

### 4. Made for Linux
- Wayland and X11, KDE and GNOME
- Steam Deck workflow (back-button trigger, Game Mode–aware hotkeys)
- System tray; hotkeys pause while a GameMode game is running

## Free vs Ultra

Public product name for the paid tier: **Ultra** only.

| | Free | Ultra ($60 one-time — **$29.99 launch**) |
|---|---|---|
| Local transcription (whisper.cpp) | ✅ | ✅ |
| GPU acceleration | — | ✅ |
| Standard models (tiny/base/small) | ✅ | ✅ |
| Local LLM cleanup | ✅ | ✅ |
| Status overlay & tray | ✅ | ✅ |
| Large models (medium, large-v3-turbo) | — | ✅ |
| Faster-Whisper backend | — | ✅ |
| Cloud transcription (your keys) | — | ✅ |
| Unlimited-length chunked recording | — | ✅ |
| Tone presets & voice profiles | — | ✅ |
| Custom vocabulary | — | ✅ |
| High-accuracy beam search & audio preprocessing | — | ✅ |

Ultra is one-time — no subscription. Activate a key in-app; works offline after activation.

## Defaults to advertise

- Hotkey: **Super+F2** (start/stop)
- Style cycle: **Super+F3**
- Free default model (Flatpak): **base.en**
- Processing: **Local** by default

## Landing page requirements (`/aura`)

Must prove the product in ~5 seconds:

1. Hero still or GIF: record → process → text at cursor  
2. Privacy claim with honest caveats (local default; optional cloud; model-update check; license activation)  
3. Price: one-time Ultra, launch vs regular if claimed  
4. Install path (Flatpak when ready)  
5. Screenshots from `screenshots/` (recording + overlay preferred)

Checkout copy must not claim UI elements the storefront does not show (confirm fee treatment vs `$60` regular price).

## AppStream summary

`Local-first voice dictation — speak, text appears at your cursor`

## Key phrases

- "Local-first & private"
- "Works everywhere you type"
- "GPU-accelerated Ultra"
- "Game-aware hotkeys"
- "Steam Deck ready"
- "One-time Ultra — no subscription"

## Pain points

- Cloud dictation subscriptions and usage limits  
- Sensitive voice data leaving the machine  
- Linux / Wayland tools that feel second-class  
- Dictation UI that looks abandoned  

## Call to action

- "Download and dictate in minutes"  
- "Get Ultra — one-time license"  
- "Try Free; upgrade when you need GPU and large models"  

## Assets

See `screenshots/README.md` for the five-shot capture recipe (main-window, settings, overlay, style, welcome).
