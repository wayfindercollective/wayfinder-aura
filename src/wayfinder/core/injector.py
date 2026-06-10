"""
Text injection module for Wayfinder Aura.

Platform dispatch:
- Linux/X11: xdotool (preferred — no daemon, no uinput, present in stock SteamOS image)
- Linux/Wayland: wtype (virtual-keyboard protocol — no daemon/uinput, so it works inside a
  Flatpak sandbox), falling back to ydotool, then the RemoteDesktop portal
- Linux/X11 fallback: ydotool if xdotool unavailable
- macOS: clipboard paste via pbcopy + Cmd-V
"""

import os
import subprocess
import shutil
import sys
from pathlib import Path

IS_MACOS = sys.platform == 'darwin'


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

    # A user-set YDOTOOL_SOCKET is authoritative (custom daemon setups) —
    # only probe the common locations when it's unset or stale.
    preset = env.get("YDOTOOL_SOCKET")
    if preset and Path(preset).exists():
        return env

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
        from wayfinder.core.setup import _get_install_hint
        return False, f"ydotool not found. Install with: {_get_install_hint('ydotool')}"

    env = _get_ydotool_env()
    socket_path = env.get("YDOTOOL_SOCKET")
    if not socket_path:
        # No socket found - daemon may not be running
        if shutil.which("systemctl"):
            hint = "Start the daemon: sudo systemctl enable --now ydotoold"
        else:
            hint = "Start the ydotoold daemon with your init system"
        return False, f"ydotool daemon socket not found. {hint}"

    return True, f"ydotool ready (socket: {socket_path})"


def _inject_text_macos(text: str) -> None:
    """
    Inject text on macOS by writing to clipboard then simulating Cmd+V.
    This is the most reliable method on macOS — works in any app.
    """
    import subprocess
    import time

    # Write text to clipboard using pbcopy
    proc = subprocess.run(
        ["pbcopy"],
        input=text.encode("utf-8"),
        capture_output=True,
        timeout=5,
    )
    if proc.returncode != 0:
        raise InjectionError(f"pbcopy failed: {proc.stderr.decode()}")

    # Small delay to ensure clipboard is ready
    time.sleep(0.05)

    # Simulate Cmd+V using osascript
    script = 'tell application "System Events" to keystroke "v" using command down'
    proc = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
        timeout=5,
    )
    if proc.returncode != 0:
        raise InjectionError(f"osascript paste failed: {proc.stderr.decode()}")


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

        # Paste with Cmd+V (no sleep needed — proc.communicate() is synchronous)
        pyautogui.hotkey("command", "v")

        # Restore original clipboard after paste completes
        if old_clipboard is not None:
            import time
            time.sleep(0.1)
            # Smart restore: only restore if clipboard still contains our injected text
            # (user may have copied something new during the delay)
            try:
                current = subprocess.run(
                    ["pbpaste"], capture_output=True, text=True, timeout=2
                ).stdout
                if current == text:
                    proc = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE)
                    proc.communicate(old_clipboard.encode("utf-8"))
            except Exception:
                pass  # Best-effort restore

    except Exception as e:
        raise InjectionError(f"macOS text injection failed: {e}")


def warmup_clipboard() -> None:
    """Pre-warm the macOS pasteboard daemon (best-effort, non-blocking).

    The first pbcopy/pbpaste call can be slow (~100ms) due to pasteboard daemon
    startup. Calling this during app initialization eliminates the delay from
    the hot path.
    """
    if sys.platform != "darwin":
        return
    try:
        import subprocess
        subprocess.run(["pbpaste"], capture_output=True, timeout=2)
    except Exception:
        pass  # Best-effort — never block startup


def _inject_text_xdotool(text: str, typing_speed: str = "instant") -> None:
    """Inject text on Linux/X11 using xdotool type.

    xdotool synthesizes keystrokes via X11 XTEST — no daemon, no uinput, no input-group
    membership required. Works in stock SteamOS where ydotool packages get wiped by updates.
    """
    if typing_speed in TYPING_SPEEDS:
        key_delay, _ = TYPING_SPEEDS[typing_speed]
    else:
        key_delay = 2

    cmd = [
        "xdotool", "type",
        "--clearmodifiers",
        "--delay", str(key_delay),
        "--", text,
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            error_detail = result.stderr.strip() or result.stdout.strip() or "(no output)"
            raise InjectionError(f"xdotool failed (exit {result.returncode}): {error_detail}")
    except subprocess.TimeoutExpired:
        raise InjectionError("xdotool timed out after 120s")
    except FileNotFoundError:
        raise InjectionError("xdotool not found in PATH")


def _inject_text_wtype(text: str) -> None:
    """Inject text on Linux/Wayland using wtype (virtual-keyboard protocol).

    Needs no uinput device and no daemon, so it works inside a Flatpak sandbox (unlike
    ydotool). No per-key delay control. The compositor must implement the virtual-keyboard
    protocol (KDE Plasma 6 does); if it doesn't, wtype errors and the caller can fall back.
    """
    try:
        result = subprocess.run(
            ["wtype", text],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            error_detail = result.stderr.strip() or result.stdout.strip() or "(no output)"
            raise InjectionError(f"wtype failed (exit {result.returncode}): {error_detail}")
    except subprocess.TimeoutExpired:
        raise InjectionError("wtype timed out after 120s")
    except FileNotFoundError:
        raise InjectionError("wtype not found in PATH")


def inject_text(text: str, typing_speed: str = "instant") -> None:
    """
    Inject text into the active window.

    Dispatches to platform-specific backend:
    - Linux/X11: xdotool (preferred); ydotool as fallback
    - Linux/Wayland: wtype (preferred — sandbox-safe); ydotool as fallback
    - macOS: clipboard paste (pbcopy + Cmd-V)

    Args:
        text: Text to inject
        typing_speed: Speed preset (Linux only) - "instant", "fast", "normal", "slow", "very_slow"
    """
    if not text:
        return

    # Clean up the text - remove leading/trailing whitespace
    text = text.strip()

    if not text:
        return

    if sys.platform == "darwin":
        _inject_text_pyautogui(text, typing_speed)
        return

    # Linux: prefer xdotool on X11 (no daemon, survives SteamOS pacman wipes);
    # fall back to ydotool on Wayland or when xdotool missing.
    from ..utils.platform import get_text_injector
    tool = get_text_injector()
    if tool == "xdotool":
        _inject_text_xdotool(text, typing_speed)
        return
    if tool == "wtype":
        _inject_text_wtype(text)
        return
    if tool == "none":
        raise InjectionError(
            "No text injection tool available on Linux. "
            "Install xdotool (X11) or ydotool (Wayland)."
        )

    # Linux/ydotool path — pre-flight check
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
        from wayfinder.core.setup import _get_install_hint
        raise InjectionError(
            f"ydotool not found. Install with: {_get_install_hint('ydotool')}"
        )
