# Wayfinder Aura - Development Guide

## Project Overview

Wayfinder Aura is a local voice dictation app for Linux. It uses whisper.cpp for speech-to-text and injects the transcribed text at the cursor position.

## Development Setup

```bash
# Clone/navigate to project
cd ~/Dev/wayfinder-aura

# Install dependencies
pip install -r requirements.txt

# Run in development mode
python main.py

# Or use the package directly
PYTHONPATH=src python -m wayfinder
```

## Project Structure

The project is organized as a Python package for better maintainability:

```
wayfinder-aura/
├── src/wayfinder/          # Main package (modular, agent-friendly)
│   ├── __init__.py         # Package init
│   ├── __main__.py         # Entry point for python -m wayfinder
│   ├── app.py              # Main exports bridge
│   ├── config.py           # Configuration (defaults, load/save)
│   ├── state.py            # State machine (IDLE → RECORDING → PROCESSING)
│   ├── core/               # Core functionality
│   │   ├── recorder.py     # Audio recording (sounddevice)
│   │   ├── transcriber.py  # Speech-to-text (whisper.cpp, etc.)
│   │   ├── injector.py     # Text injection (ydotool)
│   │   ├── postprocessor.py # LLM post-processing
│   │   └── ollama_manager.py # Ollama service management
│   ├── ui/                 # User interface
│   │   ├── theme.py        # Colors, fonts, styling
│   │   ├── components.py   # Reusable widgets (ToolTip, etc.)
│   │   ├── overlay.py      # Glassmorphic status overlay (PyQt6)
│   │   └── dialogs/        # Dialog windows
│   ├── hotkeys/            # Hotkey detection
│   │   ├── evdev.py        # Direct input monitoring (X11)
│   │   ├── socket.py       # Unix socket for KDE shortcuts
│   │   └── dbus.py         # XDG GlobalShortcuts portal
│   └── utils/              # Utilities
│       ├── gpu.py          # GPU detection
│       └── platform.py     # Platform-specific helpers
├── main.py                 # Entry point (backwards compatible)
├── wayfinder_main.py       # Legacy monolithic module (being migrated)
├── scripts/                # Utility scripts
│   ├── trigger_record.py   # External trigger for KDE shortcuts
│   └── benchmark.py        # Performance benchmarking
├── assets/                 # Icons and images
├── flatpak/                # Flatpak packaging
├── pyproject.toml          # Modern Python packaging
├── requirements.txt        # Dependencies
└── wayfinder-aura.spec    # PyInstaller spec
```

## Key Modules

### src/wayfinder/config.py
Configuration management with defaults, load/save functions.

### src/wayfinder/state.py
Application state machine: IDLE → RECORDING → PROCESSING → PASTING

### src/wayfinder/core/recorder.py
Audio recording using sounddevice with chunked recording support.

### src/wayfinder/core/transcriber.py
Multi-backend transcription (whisper.cpp, Faster-Whisper, Groq, OpenAI).

### src/wayfinder/ui/theme.py
Design system: COLORS, FONTS, SPACING, RADIUS tokens.

### src/wayfinder/hotkeys/
Hotkey detection for X11 (evdev), Wayland socket, and D-Bus portal.

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
2. **Socket** (Wayland): Listens on `/tmp/wayfinder-aura.sock`
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
cp ~/Dev/wayfinder-aura/*.py ~/.local/opt/wayfinder-aura/

# Restart the app
pkill -f "opt/wayfinder-aura"
~/.local/opt/wayfinder-aura/launch-wayfinder-aura.sh &
```

## Building Executable

```bash
./build.sh
# Creates: dist/wayfinder-aura

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

## GPU Acceleration

Wayfinder Aura supports GPU acceleration for faster transcription. Two backends are available:

### Option 1: whisper.cpp with Vulkan (Recommended for AMD)

Rebuild whisper.cpp with Vulkan support for AMD GPU acceleration:

```bash
# Install Vulkan SDK (Fedora/Bazzite)
sudo dnf install vulkan-headers vulkan-loader-devel vulkan-validation-layers

# Rebuild whisper.cpp
cd ~/whisper.cpp
rm -rf build
cmake -B build -DGGML_VULKAN=ON
cmake --build build --config Release -j$(nproc)

# Verify GPU support
./build/bin/whisper-cli --help | grep -i gpu
# Should show -ngl flag
```

After rebuilding, enable GPU in Wayfinder Aura settings:
1. Open Settings → Advanced → GPU Acceleration
2. Enable "GPU Acceleration"
3. GPU Layers: "Auto (all)" for maximum speed

### Option 2: Faster-Whisper with ROCm (AMD) or CUDA (NVIDIA)

For the Faster-Whisper backend with PyTorch GPU support:

```bash
# AMD GPU (ROCm)
pip install torch --index-url https://download.pytorch.org/whl/rocm6.0

# NVIDIA GPU (CUDA)
pip install torch --index-url https://download.pytorch.org/whl/cu121

# Install Faster-Whisper
pip install faster-whisper
```

Configure in Wayfinder Aura:
1. Open Settings → Advanced → GPU Acceleration
2. Set Backend to "Faster-Whisper"
3. Enable "GPU Acceleration"
4. Choose model size and compute type

### Performance Comparison

| Backend | CPU (6 threads) | GPU (AMD RX 7000) |
|---------|-----------------|-------------------|
| whisper.cpp small | ~3-5s/10s audio | ~0.5-1s/10s audio |
| Faster-Whisper small | ~2-3s/10s audio | ~0.3-0.8s/10s audio |

### Troubleshooting GPU

**Vulkan not detected:**
```bash
# Check Vulkan devices
vulkaninfo --summary
# Should show your AMD GPU
```

**ROCm not working:**
```bash
# Verify ROCm installation
rocminfo
# Check PyTorch sees GPU
python -c "import torch; print(torch.cuda.is_available())"
```

**Out of VRAM:**
- Use a smaller model (tiny, base)
- Reduce GPU layers (set specific number instead of Auto)
- Use int8 compute type for Faster-Whisper

## Future Improvements

- [ ] Mac/Windows platform support
- [ ] AppImage packaging for easier Linux distribution
- [ ] Voice activity detection (auto-stop recording)
- [ ] Multiple language support
- [ ] Custom wake word detection

