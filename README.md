# Wayfinder Aura

Local voice dictation for Linux (Wayland/X11) using whisper.cpp for transcription. Press a hotkey, speak, and your words are typed wherever your cursor is.

![Wayfinder Aura UI](assets/icon.png)

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

The app is installed to `~/.local/opt/wayfinder-aura/`

```bash
# Already set up:
# - App menu shortcut: "Wayfinder Aura"
# - Auto-starts on login
# - Run from: ~/.local/opt/wayfinder-aura/launch-wayfinder-aura.sh
```

### For Development

```bash
cd ~/Dev/wayfinder-aura
python main.py
```

### Updating Installed App

After making changes in the Dev folder:

```bash
cp ~/Dev/wayfinder-aura/*.py ~/.local/opt/wayfinder-aura/
```

## Configuration

Config is stored at `~/.config/wayfinder-aura/config.json`

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
wayfinder-aura/
├── src/wayfinder/           # Main package (modular structure)
│   ├── config.py            # Configuration management
│   ├── state.py             # State machine
│   ├── core/                # Core functionality
│   │   ├── recorder.py      # Audio recording
│   │   ├── transcriber.py   # Speech-to-text
│   │   ├── injector.py      # Text injection
│   │   └── postprocessor.py # LLM post-processing
│   ├── ui/                  # User interface
│   │   ├── theme.py         # Design system
│   │   ├── components.py    # Reusable widgets
│   │   └── overlay.py       # Status overlay
│   ├── hotkeys/             # Hotkey detection
│   │   ├── evdev.py         # X11 input
│   │   ├── socket.py        # Unix socket
│   │   └── dbus.py          # Wayland portal
│   └── utils/               # Utilities
│       ├── gpu.py           # GPU detection
│       └── platform.py      # Platform helpers
├── main.py                  # Entry point
├── wayfinder_main.py        # Legacy main (being migrated)
├── scripts/                 # Utility scripts
│   └── trigger_record.py    # External trigger
├── assets/
│   └── icon.png             # App icon
├── pyproject.toml           # Modern Python packaging
└── requirements.txt         # Dependencies
```

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    WayfinderApp                          │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────┐  │
│  │   UI/CTk    │  │  Hotkey     │  │  System Tray    │  │
│  │  Interface  │  │  Listener   │  │   (pystray)     │  │
│  └─────────────┘  └─────────────┘  └─────────────────┘  │
└────────────┬────────────────────────────────────────────┘
             │
             ▼
┌─────────────────────────────────────────────────────────┐
│              State Machine (wayfinder.state)            │
│   IDLE → RECORDING → PROCESSING → PASTING → IDLE       │
└────────────┬────────────────────────────────────────────┘
             │
     ┌───────┴───────┬───────────────┐
     ▼               ▼               ▼
┌─────────┐   ┌─────────────┐   ┌──────────┐
│core/    │   │core/        │   │core/     │
│recorder │   │transcriber  │   │injector  │
└─────────┘   └─────────────┘   └──────────┘
```

## Hotkey Detection Methods

1. **evdev** (X11): Direct input device monitoring
2. **Socket** (Wayland): Unix socket for external triggers
3. **KDE Shortcuts**: Configure in System Settings → Shortcuts

For Wayland/KDE, add a custom shortcut that runs:
```bash
~/.local/opt/wayfinder-aura/trigger_record.py
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
# Output: dist/wayfinder-aura
```

Note: PyInstaller builds may have issues with flexiblas on Fedora. The Python launcher is more reliable.

## License

MIT License - See LICENSE file

## Credits

- [whisper.cpp](https://github.com/ggerganov/whisper.cpp) - Speech recognition
- [CustomTkinter](https://github.com/TomSchimansky/CustomTkinter) - Modern UI
- UI Design inspired by Wayfinder OS








