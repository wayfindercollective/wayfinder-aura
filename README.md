# Wayfinder Voice

Local voice dictation for Linux (Wayland/X11) using whisper.cpp for transcription. Press a hotkey, speak, and your words are typed wherever your cursor is.

![Wayfinder Voice UI](assets/icon.png)

## Features

- **Local & Private**: All processing happens on your machine using whisper.cpp
- **Hotkey Triggered**: Press F3 (configurable) to start/stop recording
- **Wayland Support**: Works on modern Linux desktops including KDE Plasma on Wayland
- **System Tray**: Runs in background with status indicator
- **Multiple Input Devices**: Supports keyboards, gaming mice, keypads
- **Adjustable Typing Speed**: Instant paste or simulated typing
- **UI Scaling**: Ctrl+Plus/Minus for high-DPI screens (70%-250%)
- **Collapsible Activity Log**: Clean interface with expandable log
- **Model Selection**: Choose whisper model (tiny → large) based on speed/accuracy needs

## UI Design

Premium dark theme inspired by Wayfinder OS:
- Cyan accent color (#00D4FF)
- Gradient background with subtle glow
- Modern typography (Exo 2, Inter)
- Ring-style status indicator

## Requirements

### System Dependencies

```bash
# Fedora/Bazzite
sudo dnf install python3-tkinter whisper-cpp

# Or build whisper.cpp manually:
git clone https://github.com/ggerganov/whisper.cpp ~/whisper.cpp
cd ~/whisper.cpp && make
./models/download-ggml-model.sh small.en
```

### Python Dependencies

```bash
pip install -r requirements.txt
```

## Installation

### For Users (Installed App)

The app is installed to `~/.local/opt/wayfinder-voice/`

```bash
# Already set up:
# - App menu shortcut: "Wayfinder Voice"
# - Auto-starts on login
# - Run from: ~/.local/opt/wayfinder-voice/launch-wayfinder-voice.sh
```

### For Development

```bash
cd ~/Dev/wayfinder-voice
python main.py
```

### Updating Installed App

After making changes in the Dev folder:

```bash
cp ~/Dev/wayfinder-voice/*.py ~/.local/opt/wayfinder-voice/
```

## Configuration

Config is stored at `~/.config/wayfinder-voice/config.json`

| Setting | Default | Description |
|---------|---------|-------------|
| `hotkey_key` | 67 (F3) | Trigger key code |
| `hotkey_modifiers` | [] | Optional modifiers (ctrl, alt, shift) |
| `model_path` | `~/whisper.cpp/models/ggml-small.bin` | Whisper model |
| `typing_speed` | "instant" | instant, fast, normal, slow, very_slow |
| `ui_scale` | 1.0 | UI scaling factor (0.7 - 2.5) |
| `enabled_input_devices` | [] | Empty = all devices |
| `start_minimized` | true | Start in system tray |

## Keyboard Shortcuts

| Shortcut | Action |
|----------|--------|
| F3 (default) | Toggle recording |
| Ctrl + Plus | Increase UI scale |
| Ctrl + Minus | Decrease UI scale |
| Ctrl + 0 | Reset UI scale to 100% |

## File Structure

```
wayfinder-voice/
├── main.py              # Main application & UI
├── recorder.py          # Audio recording (sounddevice)
├── transcriber.py       # Whisper.cpp integration
├── injector.py          # Text injection (ydotool/xdotool)
├── trigger_record.py    # External trigger script
├── launch-wayfinder-voice.sh  # Launcher script
├── wayfinder-voice.desktop    # Desktop entry file
├── wayfinder-voice.spec       # PyInstaller spec
├── build.sh             # Build script
├── assets/
│   └── icon.png         # App icon
└── requirements.txt     # Python dependencies
```

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    WayfinderApp (main.py)               │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────┐  │
│  │   UI/CTk    │  │  Hotkey     │  │  System Tray    │  │
│  │  Interface  │  │  Listener   │  │   (pystray)     │  │
│  └─────────────┘  └─────────────┘  └─────────────────┘  │
└────────────┬────────────────────────────────────────────┘
             │
             ▼
┌─────────────────────────────────────────────────────────┐
│                    State Machine                         │
│   IDLE → RECORDING → PROCESSING → PASTING → IDLE       │
└────────────┬────────────────────────────────────────────┘
             │
     ┌───────┴───────┬───────────────┐
     ▼               ▼               ▼
┌─────────┐   ┌─────────────┐   ┌──────────┐
│Recorder │   │ Transcriber │   │ Injector │
│(sounddevice)│(whisper.cpp)│   │(ydotool) │
└─────────┘   └─────────────┘   └──────────┘
```

## Hotkey Detection Methods

1. **evdev** (X11): Direct input device monitoring
2. **Socket** (Wayland): Unix socket for external triggers
3. **KDE Shortcuts**: Configure in System Settings → Shortcuts

For Wayland/KDE, add a custom shortcut that runs:
```bash
~/.local/opt/wayfinder-voice/trigger_record.py
```

## Troubleshooting

### "No input devices found"
```bash
sudo usermod -aG input $USER
# Log out and back in
```

### Hotkey not working on Wayland
Configure a KDE shortcut to run `trigger_record.py`

### UI too small on 4K display
Press Ctrl+Plus to scale up, or set in Settings → UI Scale

### flexiblas errors (Fedora)
The launcher script handles this automatically by setting `FLEXIBLAS=OPENBLAS-OPENMP`

## Building Standalone Executable

```bash
./build.sh
# Output: dist/wayfinder-voice
```

Note: PyInstaller builds may have issues with flexiblas on Fedora. The Python launcher is more reliable.

## License

MIT License - See LICENSE file

## Credits

- [whisper.cpp](https://github.com/ggerganov/whisper.cpp) - Speech recognition
- [CustomTkinter](https://github.com/TomSchimansky/CustomTkinter) - Modern UI
- UI Design inspired by Wayfinder OS



