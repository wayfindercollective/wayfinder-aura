# Wayfinder Aura

**Press a key. Speak. Your words appear wherever your cursor is.**

Wayfinder Aura is local-first voice dictation for Linux. By default,
transcription runs on *your* machine with whisper.cpp, offline, in any app.
In local mode your voice never leaves your computer; optional local cleanup,
Ultra GPU acceleration, and cloud backends are available when you choose them.

![Wayfinder Aura UI](assets/icon.png)

## Why Wayfinder Aura

- **Local by default.** Out of the box, speech-to-text runs locally — no audio
  uploads. Optional cleanup also runs locally when enabled. Cloud backends
  (OpenAI/Groq/Anthropic) are opt-in and off by default. The app checks online
  for model updates weekly (toggleable), and activating a license contacts the
  activation server.
- **Reliable where it counts.** Free runs the broadly compatible Base model on
  CPU. Ultra unlocks Vulkan acceleration on supported AMD, Intel, and NVIDIA
  systems plus additional, more accurate speech models.
- **Writes like you, only cleaner.** Local cleanup strips the "um"s and fixes
  punctuation. Ultra adds larger cleanup models and selectable tones, from
  boardroom-professional to commit-message-dev.
- **Made for Linux, including the weird parts.** Targets Wayland and X11, KDE
  and GNOME, desktop PCs and Steam Deck workflows. Hotkeys pause automatically
  while a game is running so your push-to-talk key stays yours.

## Features

| | |
|---|---|
| 🎙️ **Hotkey dictation** | Ctrl+Alt+Space to start/stop (configurable), text lands at your cursor in any app |
| 🔒 **100% local pipeline** | whisper.cpp transcription plus optional llama.cpp cleanup, both on-device |
| ⚡ **GPU acceleration (Ultra)** | Vulkan on AMD/Intel/NVIDIA with per-machine CPU fallback |
| 🎨 **Tone presets (Ultra)** | Minimal, Professional, Casual, Dev, Personal — cycle with Ctrl+Alt+Enter |
| 🎮 **Game-aware** | Hotkeys pause while a GameMode game is registered (Lutris/Steam) |
| 🖥️ **Glassmorphic overlay** | Always-visible recording status, designed for Wayland |
| 🎧 **Smart mic handling** | Picker shows exactly the mics your OS sees; selections survive device renumbering |
| 🕹️ **Steam Deck workflow** | Trigger dictation from a back button with the bundled host helper |
| 📋 **Flexible output** | Instant paste or simulated typing at your preferred speed |

## Free vs Ultra

The free tier is a focused, usable local dictation experience — not a timed or
watermarked demo. Ultra adds speed, model choice, long-form tools, and styles.

| | Free | Ultra ($60 one-time — **$29.99 launch price**) |
|---|---|---|
| Base/Base.en transcription on CPU | ✅ | ✅ |
| GPU acceleration (Vulkan) | — | ✅ |
| Additional speech models | — | ✅ |
| Local minimal cleanup | ✅ | ✅ |
| Larger cleanup models | — | ✅ |
| Status overlay & tray | ✅ | ✅ |
| Large models (medium, large-v3-turbo) — GPU-accelerated too | — | ✅ |
| Faster-Whisper backend (CTranslate2) | — | ✅ |
| Cloud transcription (Groq / OpenAI, your keys) | — | ✅ |
| Unlimited-length chunked recording | — | ✅ |
| Tone presets & voice profiles | — | ✅ |
| Custom vocabulary | — | ✅ |
| Accuracy controls & audio preprocessing | ✅ | ✅ |

Ultra is a one-time purchase — no subscription, ever. It's $60, and **$29.99
during launch**. Hit **Get Ultra** in the app, enter your license key, done.
Keys activate online once and keep working offline.

## Install

### Download (recommended)

**[⬇ Download the latest AppImage](https://github.com/wayfindercollective/wayfinder-aura/releases/latest)** — one file, everything bundled: GPU (Vulkan) + CPU speech engines, text-injection tools, the works.

1. Download `Wayfinder_Aura-*-x86_64.AppImage` from the link above.
2. Make it executable: right-click → **Properties** → check **"Executable as
   Program"** (or `chmod +x Wayfinder_Aura-*.AppImage`).
3. Run it. On first launch it **adds itself to your applications menu** with an
   icon, walks you through a quick setup, and downloads a starter speech model.
   That's it — press **Ctrl+Alt+Space** and start talking.

Works on Linux distributions from 2022 onward — Ubuntu 22.04+, Debian 12+,
Fedora 35+, Bazzite, and the Steam Deck. Wayland and X11.

### Flathub (coming soon)

One-click install from KDE Discover / GNOME Software is on the way. Until
then, the AppImage above is the fastest path. (Developers can build the
Flatpak locally from `flatpak/io.wayfindercollective.WayfinderAura.yml` with
`flatpak-builder`.)

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
2. Press **Ctrl+Alt+Space**, speak, press **Ctrl+Alt+Space** again.
3. Your raw transcript is typed at the cursor. Enable local LLM cleanup in
   Settings when you want filler removal, punctuation cleanup, or styles.
4. With Ultra, press **Ctrl+Alt+Enter** to cycle output styles (Minimal →
   Professional → Casual → Dev → Personal).

First run walks you through model download, microphone selection, and — where
your system allows it — sets up the background typing service with one click,
no terminal needed. The defaults (base.en + auto-detected mic) work well on
most machines, and hotkeys are fully rebindable in Settings.

### Dictation defaults and safety

- **Post-processing starts off on new installs.** Raw transcription is the
  lowest-latency default. Existing installs retain their saved cleanup choice.
- **Ultra Chunk Processing defaults to Auto.** Recordings under 30 seconds stay
  one-shot; longer recordings are chunked and fully assembled before cleanup,
  injection, or Auto-Enter. Free remains one-shot. Off and On remain available.
- **Auto press Enter starts off.** Turning it on submits after text injection,
  which can send chat messages, terminal commands, or AI prompts immediately.
  Keep it off anywhere you need to review dictated text before submission.

## Output styles (Ultra)

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

For a fresh Deck, use the GitHub Flatpak plus the rootless host-helper
installer from Desktop Mode:

```bash
git clone https://github.com/wayfindercollective/wayfinder-aura.git
cd wayfinder-aura
./scripts/steamdeck/install-steamdeck.sh
```

It downloads/installs the latest release Flatpak if needed and enables the
controller trigger and Desktop/Game Mode lifecycle. It never unlocks the
SteamOS root filesystem or installs pacman/Python packages. Afterwards, bind
R4 (or another back/custom button) to **Right Joystick Click** in Steam's
Desktop Layout. Game Mode uses per-game controller layouts, so repeat that
assignment in each game or apply a reusable layout template. The game may also
receive its normal right-stick-click action. See
[`scripts/steamdeck/README.md`](scripts/steamdeck/README.md) for Game Mode and
update options.

## Configuration

Settings live in the app; the file is `~/.config/wayfinder-aura/config.json`.

| Setting | Default | Description |
|---------|---------|-------------|
| `hotkey_key` + `hotkey_modifiers` | Ctrl+Alt+Space | Recording toggle |
| `style_toggle_key` + `style_toggle_modifiers` | Ctrl+Alt+Enter | Style cycle |
| `audio_device` / `audio_device_name` | auto | Microphone (saved by name — index-proof) |
| `typing_speed` | instant | instant, fast, normal, slow, very_slow |
| `post_processing_enabled` | false | Optional local LLM cleanup; existing installs keep their saved choice |
| `chunked_mode` | Auto for Ultra; Off for Free | Under 30s stays one-shot in Auto; Off and On are selectable |
| `press_enter_after_dictation` | false | Automatically submits after injection; review terminal/AI text before enabling |
| `ui_scale` | 1.0 | 0.7–2.5, or Ctrl +/- in app |
| `start_minimized` | true | Start in system tray |

## Troubleshooting

- **"No audio detected" after recording** — your mic is muted or the wrong
  input device is selected. Settings → Audio, or pick "Auto-detect".
- **Hotkey does nothing on Wayland** — Flatpak installs register hotkeys
  through the GlobalShortcuts portal: approve the shortcut prompt from your
  desktop, or bind it in System Settings → Shortcuts. From-source and
  AppImage installs don't use the portal — they read input devices directly
  (evdev), so see the next item instead.
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
or resell it as a hosted service. Ultra features support continued
development.

## Credits

- [whisper.cpp](https://github.com/ggerganov/whisper.cpp) — speech recognition
- [llama.cpp](https://github.com/ggerganov/llama.cpp) — local LLM inference
- [CustomTkinter](https://github.com/TomSchimansky/CustomTkinter) — UI toolkit
- UI design inspired by Wayfinder OS
