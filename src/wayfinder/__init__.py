"""
Wayfinder Aura - Local voice dictation for Linux.

A privacy-focused voice dictation app using whisper.cpp for transcription.

Package Structure:
    wayfinder/
    ├── config.py       - Configuration management
    ├── state.py        - Application state machine
    ├── app.py          - Main exports and app bridge
    ├── core/           - Core functionality (recorder, transcriber, etc.)
    ├── ui/             - User interface (theme, components, overlay)
    ├── hotkeys/        - Hotkey detection (evdev, socket, dbus)
    └── utils/          - Utilities (gpu, platform detection)

Usage:
    python main.py                  # Traditional entry point
    python -m wayfinder             # Package-style entry (future)
"""

__version__ = "1.1.1"  # Keep in sync with pyproject.toml [project] version
__app_name__ = "Wayfinder Aura"


def main():
    """Main entry point for the application."""
    from .__main__ import main as run_main
    run_main()

