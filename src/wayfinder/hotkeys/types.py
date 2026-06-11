"""
Shared types for Wayfinder Aura hotkey modules.

EventType is used by all hotkey backends (evdev, pynput, socket, dbus)
and the main application event loop. Defined here to avoid importing
platform-specific modules (e.g., evdev) on unsupported platforms.
"""

from enum import Enum, auto


class EventType(Enum):
    """Event types for the application event queue."""
    HOTKEY_PRESSED = auto()
    STYLE_TOGGLE = auto()  # Cycle through output styles (Professional/AI Prompt/Casual)
    SHOW_WINDOW = auto()   # Tray "Open" — raise/restore the main window
    FORCE_RESET = auto()   # Tray "Reset" — unstick overlay / abort in-flight dictation
    QUIT_APP = auto()      # Tray "Quit" — clean full shutdown
    HOTKEY_CAPTURED = auto()  # Settings "Detect" — next pressed key reported to the UI
    TRANSCRIPTION_DONE = auto()
    TRANSCRIPTION_ERROR = auto()
    INJECTION_DONE = auto()
    INJECTION_ERROR = auto()
    CHUNK_TRANSCRIBED = auto()  # A chunk was transcribed during recording
    CHUNKED_TRANSCRIPTION_DONE = auto()  # All chunks transcribed
    LOG_MESSAGE = auto()  # Thread-safe log message (avoids Tk threading crash)
