# Wayfinder Aura

**Press a key. Speak. Your words appear wherever your cursor is.**

Wayfinder Aura is local voice dictation for Linux. Transcription runs on *your*
machine with whisper.cpp — GPU-accelerated, working offline, in any app. Your
voice never leaves your computer.

![Wayfinder Aura UI](assets/icon.png)

## Why Wayfinder Aura

- **Actually private.** No cloud account, no audio uploads, no telemetry.
  Speech-to-text and AI cleanup both run locally. Cloud backends exist, but
  they're opt-in and off by default.
- **Fast where it counts.** Vulkan GPU acceleration on AMD, Intel, and NVIDIA,
  with automatic CPU fallback on machines where GPU inference isn't available —
  dictation works everywhere, it's just faster where it can be.
- **Writes like you, only cleaner.** A small local LLM (Gemma 3 1B by default)
  strips the "um"s, fixes punctuation, and matches your chosen tone — from
  boardroom-professional to commit-message-dev.
- **Made for Linux, including the weird parts.** Wayland and X11. KDE and
  GNOME. Desktop and Steam Deck. Hotkeys that pause automatically while a
  game is running so your push-to-talk key stays yours.

## Features

| | |
|---|---|
| 🎙️ **Hotkey dictation** | Super+F2 to start/stop (configurable), text lands at your cursor in any app |
| 🔒 **100% local pipeline** | whisper.cpp transcription + llama.cpp cleanup, both on-device |
| ⚡ **GPU acceleration** | Vulkan with per-machine CPU auto-fallback |
| 🎨 **Tone presets** | Minimal, Professional, Casual, Dev, Personal — cycle with Super+F3 |
| 🎮 **Game-aware** | Hotkeys pause while a GameMode game is registered (Lutris/Steam) |
| 🖥️ **Glassmorphic overlay** | Always-visible recording status, designed for Wayland |
| 🎧 **Smart mic handling** | Picker shows exactly the mics your OS sees; selections survive device renumbering |
| 🕹️ **Steam Deck ready** | Trigger dictation from a back button; validated on SteamOS |
| 📋 **Flexible output** | Instant paste or simulated typing at your preferred speed |

## Free vs Premium

The free tier is the full dictation experience — not a demo.

| | Free | Premium ($20, one-time) |
|---|---|---|
| Local transcription (whisper.cpp) | ✅ | ✅ |
| GPU acceleration | ✅ | ✅ |
| Standard models (tiny/base/small) | ✅ | ✅ |
| Local LLM cleanup | ✅ | ✅ |
| Status overlay & tray | ✅ | ✅ |
| Large models (medium, large-v3-turbo) | — | ✅ |
| Faster-Whisper backend (CTranslate2) | — | ✅ |
| Cloud transcription (Groq / OpenAI, your keys) | — | ✅ |
| Unlimited-length chunked recording | — | ✅ |
| Tone presets & voice profiles | — | ✅ |
| Custom vocabulary | — | ✅ |
| High-accuracy beam search & audio preprocessing | — | ✅ |

Premium is a one-time purchase — hit **Get Premium** in the app, enter your
license key, done. Keys activate online once and keep working offline.

## Install

### Flatpak (recommended — coming to Flathub)

The Flatpak bundles everything: whisper.cpp (GPU + CPU builds), a starter
model, and text-injection tools. No dependencies to install.

```bash
flatpak-builder --user --install --force-clean build-dir \
  flatpak/io.github.wayfindercollective.WayfinderAura.yml
flatpak run io.github.wayfindercollective.WayfinderAura
```

### From source

```bash
# System deps (Fedora/Bazzite shown; see INSTALL-UBUNTU.md for Debian/Ubuntu)
sudo dnf install python3-tkinter

# whisper.cpp
git clone https://github.com/ggerganov/whisper.cpp ~/whisper.cpp
cd ~/whisper.cpp && cmake -B build -DGGML_VULKAN=ON && cmake --build build -j
./models/download-ggml-model.sh base.en

# Wayfinder Aura
git clone https://github.com/wayfindercollective/wayfinder-aura.git
cd wayfinder-aura
pip install -r requirements.txt
python main.py
```

## Quick start

1. Launch Wayfinder Aura — it lives in your system tray.
2. Press **Super+F2**, speak, press **Super+F2** again.
3. Your words are typed at the cursor, cleaned up and punctuated.
4. Press **Super+F3** to cycle output styles (Minimal → Professional → Casual
   → Dev → Personal).

First run walks you through model download and microphone selection. The
defaults (base.en + auto-detected mic) work well on most machines.

## Output styles

The local LLM guides your transcript without rewriting it:

- **Minimal** — strips um/uh, fixes punctuation. Fastest.
- **Professional** — clean, business-appropriate phrasing.
- **Casual** — relaxed, texting-style.
- **Dev** — understands git, code terms, and technical phrasing.
- **Personal** — learns your speech patterns over time.

Each style has standard and strong intensities. (There may also be a secret
third one. Try typing something on the Style tab.)

## Steam Deck

Wayfinder Aura runs on SteamOS with a CPU-tuned build that avoids the APU's
instruction-set pitfalls. Bind a back button (R4 works great) to toggle
dictation from Game Mode. Voice-to-chat in any game, no keyboard needed.

## Configuration

Settings live in the app; the file is `~/.config/wayfinder-aura/config.json`.

| Setting | Default | Description |
|---------|---------|-------------|
| `hotkey_key` + `hotkey_modifiers` | Super+F2 | Recording toggle |
| `style_toggle_key` + `style_toggle_modifiers` | Super+F3 | Style cycle |
| `audio_device` / `audio_device_name` | auto | Microphone (saved by name — index-proof) |
| `typing_speed` | instant | instant, fast, normal, slow, very_slow |
| `ui_scale` | 1.0 | 0.7–2.5, or Ctrl +/- in app |
| `start_minimized` | true | Start in system tray |

## Troubleshooting

- **"No audio detected" after recording** — your mic is muted or the wrong
  input device is selected. Settings → Audio, or pick "Auto-detect".
- **Hotkey does nothing on Wayland** — the app uses the GlobalShortcuts portal;
  approve the shortcut prompt from your desktop, or bind it in System
  Settings → Shortcuts.
- **"No input devices found" (from-source installs)** —
  `sudo usermod -aG input $USER`, then log out and back in.
- **UI too small on 4K** — Ctrl+Plus, or Settings → UI Scale.

## For developers

- Run: `python main.py` · Test: `python3 -m pytest tests/ -v`
- Architecture and contribution notes: [AGENTS.md](AGENTS.md)
- Packaging and store submission: [SHIPPING.md](SHIPPING.md) and
  [flatpak/BUILDING.md](flatpak/BUILDING.md)

## License

Wayfinder Aura is **source-available** under the
[Elastic License 2.0](LICENSE). In short: you're free to use, read, and build
the software; you may not remove or circumvent the license-key functionality,
or resell it as a hosted service. Premium features support continued
development.

## Credits

- [whisper.cpp](https://github.com/ggerganov/whisper.cpp) — speech recognition
- [llama.cpp](https://github.com/ggerganov/llama.cpp) — local LLM inference
- [CustomTkinter](https://github.com/TomSchimansky/CustomTkinter) — UI toolkit
- UI design inspired by Wayfinder OS
