# Wayfinder Voice - Development Guide

## Project Overview

Wayfinder Voice is a local voice dictation app for Linux. It uses whisper.cpp for speech-to-text and injects the transcribed text at the cursor position.

## Development Setup

```bash
# Clone/navigate to project
cd ~/Dev/wayfinder-voice

# Install dependencies
pip install -r requirements.txt

# Run in development mode
python main.py
```

## Code Structure

### main.py (Core Application)

The main file contains:

1. **Configuration** (lines 41-69): Default settings, key codes, paths
2. **Color Scheme** (lines 107-145): Wayfinder OS-inspired palette
3. **State Machine** (lines 100-105): IDLE → RECORDING → PROCESSING → PASTING
4. **WayfinderApp Class** (line 463+): Main application

Key methods:
- `setup_ui()`: Builds the CustomTkinter interface
- `setup_scaling_shortcuts()`: Ctrl+/- for UI scaling
- `_draw_gradient_bg()`: Radial gradient background effect
- `_draw_status_indicator()`: Ring-style status with glow
- `update_state()`: State machine transitions
- `poll_events()`: Event loop for hotkey/transcription events

### recorder.py

Audio recording using sounddevice:
- `start()`: Begin recording to buffer
- `stop()`: Stop and save to temp WAV file
- `get_duration()`: Get recording length

### transcriber.py

Whisper.cpp integration:
- `transcribe_with_config()`: Run whisper-cli subprocess
- Handles model paths, prompts, threading

### injector.py

Text injection for Wayland/X11:
- Uses `ydotool` (Wayland) or `xdotool` (X11)
- Supports typing speeds: instant, fast, normal, slow, very_slow

## UI Customization

### Color Palette (COLORS dict)

```python
COLORS = {
    "bg_base": "#0D0D12",      # Background
    "bg_card": "#141419",       # Card surfaces
    "accent": "#00D4FF",        # Primary cyan
    "accent_red": "#FF4466",    # Recording state
    "accent_yellow": "#FFB830", # Processing state
    "accent_green": "#00E5A0",  # Typing state
    "text_primary": "#E8E8F0",  # Main text
    "text_secondary": "#8888A0", # Secondary text
}
```

### Fonts

```python
self.font_header = ("Exo 2", "Rajdhani", "Segoe UI")  # Headers
self.font_body = ("Inter", "DM Sans", "Segoe UI")     # Body text
self.font_mono = ("JetBrains Mono", "Fira Code")      # Log text
```

### Adding New Settings

1. Add to `DEFAULT_CONFIG` dict
2. Create UI row in `setup_ui()` using `create_setting_row()`
3. Create dialog method `open_X_settings()`
4. Save with `save_config(self.config)`

## State Machine

```
IDLE ──[hotkey]──► RECORDING
  ▲                    │
  │               [hotkey]
  │                    ▼
  │              PROCESSING
  │                    │
  │             [transcribe]
  │                    ▼
  └────[done]───── PASTING
```

Events flow through `event_queue` and are processed by `poll_events()`.

## Hotkey Detection

Three methods (used based on environment):

1. **evdev** (X11): Monitors /dev/input devices
2. **Socket** (Wayland): Listens on `/tmp/wayfinder-voice.sock`
3. **D-Bus** (Wayland): XDG GlobalShortcuts portal (experimental)

## Testing Changes

```bash
# Run from dev folder
python main.py

# Test specific component
python -c "from recorder import AudioRecorder; r = AudioRecorder(); print('OK')"
```

## Deploying to Installed App

```bash
# Copy changed files to install location
cp ~/Dev/wayfinder-voice/*.py ~/.local/opt/wayfinder-voice/

# Restart the app
pkill -f "opt/wayfinder-voice"
~/.local/opt/wayfinder-voice/launch-wayfinder-voice.sh &
```

## Building Executable

```bash
./build.sh
# Creates: dist/wayfinder-voice

# Note: flexiblas issues on Fedora - use Python launcher instead
```

## Common Issues

### ImportError: No module named '_tkinter'
Your shell is using the wrong Python (e.g., linuxbrew). The launcher uses `/usr/bin/python3` explicitly.

### evdev PermissionError
Add user to input group: `sudo usermod -aG input $USER`

### Hotkey not detected on Wayland
Wayland blocks direct input monitoring. Use:
1. Socket method with KDE shortcut calling `trigger_record.py`
2. Or configure in System Settings → Shortcuts

## Future Improvements

- [ ] Mac/Windows platform support
- [ ] AppImage packaging for easier Linux distribution
- [ ] Voice activity detection (auto-stop recording)
- [ ] Multiple language support
- [ ] Custom wake word detection

