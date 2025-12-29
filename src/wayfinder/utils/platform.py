"""
Platform-specific utilities for Wayfinder Voice.

Handles platform detection, environment setup, and OS-specific helpers.
"""

import os
import subprocess
from pathlib import Path


def is_flatpak() -> bool:
    """Check if running in a Flatpak environment."""
    return os.environ.get("FLATPAK_ID") is not None or os.environ.get("WAYFINDER_FLATPAK") is not None


def is_wayland() -> bool:
    """Check if running on Wayland."""
    return os.environ.get("XDG_SESSION_TYPE") == "wayland"


def is_x11() -> bool:
    """Check if running on X11."""
    return os.environ.get("XDG_SESSION_TYPE") == "x11"


def is_kde() -> bool:
    """Check if running on KDE Plasma desktop."""
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "").upper()
    return "KDE" in desktop


def is_gnome() -> bool:
    """Check if running on GNOME desktop."""
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "").upper()
    return "GNOME" in desktop


def get_desktop_environment() -> str:
    """Get the current desktop environment name."""
    return os.environ.get("XDG_CURRENT_DESKTOP", "Unknown")


def get_session_type() -> str:
    """Get the session type (wayland, x11, tty)."""
    return os.environ.get("XDG_SESSION_TYPE", "unknown")


def get_config_dir() -> Path:
    """Get the XDG config directory for the app."""
    xdg_config = os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
    return Path(xdg_config) / "wayfinder-voice"


def get_data_dir() -> Path:
    """Get the XDG data directory for the app."""
    xdg_data = os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))
    return Path(xdg_data) / "wayfinder-voice"


def get_cache_dir() -> Path:
    """Get the XDG cache directory for the app."""
    xdg_cache = os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))
    return Path(xdg_cache) / "wayfinder-voice"


def ensure_directories():
    """Ensure all XDG directories exist."""
    get_config_dir().mkdir(parents=True, exist_ok=True)
    get_data_dir().mkdir(parents=True, exist_ok=True)
    get_cache_dir().mkdir(parents=True, exist_ok=True)


def is_ydotool_available() -> bool:
    """Check if ydotool is available for text injection."""
    try:
        result = subprocess.run(["which", "ydotool"], capture_output=True, timeout=5)
        return result.returncode == 0
    except Exception:
        return False


def is_xdotool_available() -> bool:
    """Check if xdotool is available for text injection."""
    try:
        result = subprocess.run(["which", "xdotool"], capture_output=True, timeout=5)
        return result.returncode == 0
    except Exception:
        return False


def get_text_injector() -> str:
    """
    Determine the best text injection method for the current environment.
    
    Returns:
        "ydotool" for Wayland, "xdotool" for X11, or "none" if unavailable.
    """
    if is_wayland():
        if is_ydotool_available():
            return "ydotool"
    else:
        if is_xdotool_available():
            return "xdotool"
        elif is_ydotool_available():
            return "ydotool"
    return "none"




