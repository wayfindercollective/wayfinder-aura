"""
Text injection module for Wayfinder Aura.
Handles text injection on Wayland using ydotool.
"""

import os
import subprocess
import shutil
from pathlib import Path


class InjectionError(Exception):
    """Raised when text injection fails."""

    pass


def _get_ydotool_binary() -> str:
    """
    Find the ydotool binary, checking AppImage bundle first.
    
    Returns:
        Path to ydotool binary, defaults to "ydotool" (relies on PATH).
    """
    # Check AppImage bundle
    appdir = os.environ.get("APPDIR")
    if appdir:
        bundled = Path(appdir) / "usr" / "bin" / "ydotool"
        if bundled.exists():
            return str(bundled)
    
    return "ydotool"


def _get_ydotool_env() -> dict:
    """Get environment with correct ydotool socket path."""
    env = os.environ.copy()
    
    # Check common socket locations (varies by distro/setup)
    socket_paths = [
        "/run/ydotool/ydotool.sock",  # System service (Bazzite/Fedora)
        f"/run/user/{os.getuid()}/.ydotool_socket",  # User service
        "/tmp/.ydotool_socket",  # Fallback
    ]
    
    for socket_path in socket_paths:
        if Path(socket_path).exists():
            env["YDOTOOL_SOCKET"] = socket_path
            break
    
    return env


# Typing speed presets: (key_delay_ms, key_hold_ms)
TYPING_SPEEDS = {
    "instant": (0, 0),       # No delays - truly instant
    "fast": (1, 1),          # 1ms delays
    "normal": (12, 12),      # Comfortable speed
    "slow": (50, 20),        # Slower, more natural
    "very_slow": (100, 50),  # Very slow, like watching someone type
}


def inject_text(text: str, typing_speed: str = "instant") -> None:
    """
    Inject text into the active window using ydotool.

    Args:
        text: Text to inject
        typing_speed: Speed preset - "instant", "fast", "normal", "slow", "very_slow"
    """
    if not text:
        return

    # Clean up the text - remove leading/trailing whitespace
    text = text.strip()
    
    if not text:
        return

    # Get delay values from preset
    if typing_speed in TYPING_SPEEDS:
        key_delay, key_hold = TYPING_SPEEDS[typing_speed]
    else:
        key_delay, key_hold = 0, 0  # Default to instant

    try:
        # Use ydotool to type the text
        # --key-delay: milliseconds between key events
        # --key-hold: milliseconds between key down and key up
        # Both must be 0 for truly instant typing
        ydotool_bin = _get_ydotool_binary()
        cmd = [
            ydotool_bin, "type",
            "--key-delay", str(key_delay),
            "--key-hold", str(key_hold),
            "--", text
        ]
        
        # Get environment with correct socket path
        env = _get_ydotool_env()
        
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
        )
        
        if result.returncode != 0:
            raise InjectionError(f"ydotool failed: {result.stderr}")
                
    except subprocess.TimeoutExpired:
        raise InjectionError("ydotool timed out")
    except FileNotFoundError:
        raise InjectionError("ydotool not found. Install ydotool package.")
