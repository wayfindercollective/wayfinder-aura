"""
Text injection module for Wayfinder Aura.

Platform dispatch:
- Linux: ydotool (works on both X11 and Wayland)
- macOS: pyautogui
"""

import os
import subprocess
import shutil
import sys
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
# Minimum 1ms delays to prevent ydotool Shift key race conditions
# (0ms causes Shift to bleed into adjacent keys: a→A, comma→<, period→>)
TYPING_SPEEDS = {
    "instant": (1, 1),       # 1ms delays — prevents Shift bleed, effectively instant
    "fast": (1, 1),          # Same as instant (safe minimum)
    "normal": (12, 12),      # Comfortable speed
    "slow": (50, 20),        # Slower, more natural
    "very_slow": (100, 50),  # Very slow, like watching someone type
}


def check_ydotool_ready() -> tuple[bool, str]:
    """Check if ydotool is installed and the daemon is running.

    Returns:
        (ready, message) tuple
    """
    ydotool_bin = _get_ydotool_binary()
    if not shutil.which(ydotool_bin):
        return False, "ydotool not found. Install with: sudo dnf install ydotool"

    env = _get_ydotool_env()
    socket_path = env.get("YDOTOOL_SOCKET")
    if not socket_path:
        # No socket found - daemon may not be running
        return False, (
            "ydotool daemon socket not found. "
            "Start the daemon: sudo systemctl enable --now ydotoold"
        )

    return True, f"ydotool ready (socket: {socket_path})"


def inject_text(text: str, typing_speed: str = "instant") -> None:
    """
    Inject text into the active window.

    Dispatches to platform-specific backend:
    - Linux: ydotool
    - macOS: pyautogui

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

    if sys.platform == "darwin":
        _inject_text_pyautogui(text, typing_speed)
    else:
        _inject_text_ydotool(text, typing_speed)


# =============================================================================
# macOS backend: pyautogui
# =============================================================================

# Typing speed → pyautogui interval (seconds between keystrokes)
PYAUTOGUI_INTERVALS = {
    "instant": 0.0,
    "fast": 0.0,
    "normal": 0.012,
    "slow": 0.05,
    "very_slow": 0.1,
}


def _inject_text_pyautogui(text: str, typing_speed: str = "instant") -> None:
    """Inject text on macOS using clipboard paste (Cmd+V).

    pyautogui.typewrite() only handles ASCII and is slow. Instead, we copy
    the text to clipboard and simulate Cmd+V for instant, reliable paste
    that supports all Unicode characters.
    """
    try:
        import pyautogui
        import subprocess
    except ImportError:
        raise InjectionError(
            "pyautogui not installed. Install with: pip install pyautogui"
        )

    try:
        # Save current clipboard contents
        try:
            old_clipboard = subprocess.run(
                ["pbpaste"], capture_output=True, text=True, timeout=5
            ).stdout
        except Exception:
            old_clipboard = None

        # Copy text to clipboard via pbcopy
        proc = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE)
        proc.communicate(text.encode("utf-8"))

        # Small delay to ensure clipboard is ready
        import time
        time.sleep(0.05)

        # Paste with Cmd+V
        pyautogui.hotkey("command", "v")

        # Restore original clipboard after a short delay
        if old_clipboard is not None:
            time.sleep(0.2)
            proc = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE)
            proc.communicate(old_clipboard.encode("utf-8"))

    except Exception as e:
        raise InjectionError(f"macOS text injection failed: {e}")


# =============================================================================
# Linux backend: ydotool
# =============================================================================


def _inject_text_ydotool(text: str, typing_speed: str = "instant") -> None:
    """Inject text on Linux using ydotool."""
    # Pre-flight check
    ready, msg = check_ydotool_ready()
    if not ready:
        raise InjectionError(msg)

    # Get delay values from preset
    if typing_speed in TYPING_SPEEDS:
        key_delay, key_hold = TYPING_SPEEDS[typing_speed]
    else:
        key_delay, key_hold = 2, 2  # Safe default

    try:
        ydotool_bin = _get_ydotool_binary()
        cmd = [
            ydotool_bin, "type",
            "--key-delay", str(key_delay),
            "--key-hold", str(key_hold),
            "--", text
        ]

        env = _get_ydotool_env()

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
        )

        if result.returncode != 0:
            stderr = result.stderr.strip()
            stdout = result.stdout.strip()
            error_detail = stderr or stdout or "(no output)"
            raise InjectionError(
                f"ydotool failed (exit {result.returncode}): {error_detail}"
            )

    except subprocess.TimeoutExpired:
        raise InjectionError("ydotool timed out after 120s")
    except FileNotFoundError:
        raise InjectionError(
            "ydotool not found. Install with: sudo dnf install ydotool"
        )
