"""
Platform-specific utilities for Wayfinder Aura.

Handles platform detection, environment setup, and OS-specific helpers.
Supports Linux, macOS, and Windows.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path


# =============================================================================
# Platform Detection
# =============================================================================

def get_platform() -> str:
    """
    Get the current platform identifier.
    
    Returns:
        'linux', 'darwin' (macOS), or 'windows'
    """
    if sys.platform == 'darwin':
        return 'darwin'
    elif sys.platform == 'win32':
        return 'windows'
    else:
        return 'linux'


def is_linux() -> bool:
    """Check if running on Linux."""
    return sys.platform.startswith('linux')


def is_macos() -> bool:
    """Check if running on macOS."""
    return sys.platform == 'darwin'


def is_windows() -> bool:
    """Check if running on Windows."""
    return sys.platform == 'win32'


# =============================================================================
# Linux-Specific Detection
# =============================================================================

def is_flatpak() -> bool:
    """Check if running in a Flatpak environment (Linux only)."""
    if not is_linux():
        return False
    return os.environ.get("FLATPAK_ID") is not None or os.environ.get("WAYFINDER_FLATPAK") is not None


def is_appimage() -> bool:
    """
    Check if running from an AppImage.
    
    The AppImage runtime sets the APPIMAGE and APPDIR environment variables.
    APPIMAGE points to the AppImage file path, APPDIR points to the mounted directory.
    """
    return os.environ.get("APPIMAGE") is not None or os.environ.get("APPDIR") is not None


def get_appimage_dir() -> Path | None:
    """
    Get the mounted AppImage directory (APPDIR).
    
    Returns:
        Path to the AppDir mount point, or None if not running from an AppImage.
    """
    appdir = os.environ.get("APPDIR")
    if appdir:
        return Path(appdir)
    return None


def is_wayland() -> bool:
    """Check if running on Wayland (Linux only)."""
    if not is_linux():
        return False
    return os.environ.get("XDG_SESSION_TYPE") == "wayland"


def is_x11() -> bool:
    """Check if running on X11 (Linux only)."""
    if not is_linux():
        return False
    return os.environ.get("XDG_SESSION_TYPE") == "x11"


def is_kde() -> bool:
    """Check if running on KDE Plasma desktop (Linux only)."""
    if not is_linux():
        return False
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "").upper()
    return "KDE" in desktop


def is_gnome() -> bool:
    """Check if running on GNOME desktop (Linux only)."""
    if not is_linux():
        return False
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "").upper()
    return "GNOME" in desktop


def get_desktop_environment() -> str:
    """Get the current desktop environment name (Linux only)."""
    if not is_linux():
        return get_platform().capitalize()
    return os.environ.get("XDG_CURRENT_DESKTOP", "Unknown")


def get_session_type() -> str:
    """Get the session type (wayland, x11, tty) - Linux only."""
    if not is_linux():
        return get_platform()
    return os.environ.get("XDG_SESSION_TYPE", "unknown")


# =============================================================================
# SteamOS Game Mode (gamescope session)
# =============================================================================
#
# The host-side `wayfinder-mode-supervisor` is the authority on the current
# mode (it can read `systemctl --user is-active gamescope-session.service`,
# which a sandboxed XWayland client can't). It publishes the mode to a marker
# the app reads, so the app doesn't have to guess from in-sandbox env vars
# (which may not survive into the Flatpak).

def get_runtime_dir() -> Path:
    """App runtime dir (`$XDG_RUNTIME_DIR/wayfinder-aura`), host-shared by the Flatpak."""
    xdg_run = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return Path(xdg_run) / "wayfinder-aura"


def read_mode_marker() -> str | None:
    """Read the supervisor-published mode: 'game', 'desktop', or None if absent/unreadable."""
    try:
        value = (get_runtime_dir() / "mode").read_text().strip().lower()
    except OSError:
        return None
    return value if value in ("game", "desktop") else None


def is_game_mode(wait_secs: float = 0.0) -> bool:
    """True if running inside SteamOS Game Mode (gamescope session).

    Primary signal is the supervisor's mode marker; ``wait_secs`` lets startup
    briefly wait for it to appear (it races the supervisor's first write on a
    cold boot). Falls back to in-sandbox env vars only if the marker never
    shows. Non-Linux always returns False.
    """
    if not is_linux():
        return False
    deadline_steps = max(0, int(wait_secs / 0.1))
    for _ in range(deadline_steps + 1):
        mode = read_mode_marker()
        if mode is not None:
            return mode == "game"
        if deadline_steps:
            import time
            time.sleep(0.1)
    # Marker never appeared — best-effort env fallback (gamescope advertises itself).
    if os.environ.get("GAMESCOPE_WAYLAND_DISPLAY"):
        return True
    return "gamescope" in os.environ.get("XDG_CURRENT_DESKTOP", "").lower()


def write_game_mode_marker(enabled: bool) -> None:
    """Write the toggle marker the host supervisor polls.

    Path is ``~/.config/wayfinder-aura/game-mode-dictation`` ("1"/"0"). We use
    ``Path.home()/.config`` (NOT get_config_dir(), which honors XDG_CONFIG_HOME)
    on purpose: inside the Flatpak, XDG_CONFIG_HOME points at the sandbox-private
    ``~/.var/app/<id>/config`` which the host supervisor can't see, whereas
    ``~/.config/wayfinder-aura`` is bind-mounted to the real host dir
    (--filesystem=xdg-config/wayfinder-aura). This also matches both the host
    supervisor's read path and the app's own CONFIG_DIR, so all three agree on
    one host-visible file. Atomic (temp + replace); never raises.
    """
    try:
        d = Path.home() / ".config" / "wayfinder-aura"
        d.mkdir(parents=True, exist_ok=True)
        target = d / "game-mode-dictation"
        tmp = d / ".game-mode-dictation.tmp"
        tmp.write_text("1" if enabled else "0")
        os.replace(tmp, target)
    except OSError:
        pass  # never break the UI/startup on a marker-write failure


# =============================================================================
# Cross-Platform Directory Paths
# =============================================================================

def get_config_dir() -> Path:
    """
    Get the configuration directory for the app.
    
    Returns platform-appropriate path:
    - Linux: ~/.config/wayfinder-aura (XDG)
    - macOS: ~/Library/Application Support/wayfinder-aura
    - Windows: %APPDATA%/wayfinder-aura
    """
    if is_macos():
        return Path.home() / "Library" / "Application Support" / "wayfinder-aura"
    elif is_windows():
        appdata = os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming"))
        return Path(appdata) / "wayfinder-aura"
    else:  # Linux
        xdg_config = os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
        return Path(xdg_config) / "wayfinder-aura"


def get_data_dir() -> Path:
    """
    Get the data directory for the app.
    
    Returns platform-appropriate path:
    - Linux: ~/.local/share/wayfinder-aura (XDG)
    - macOS: ~/Library/Application Support/wayfinder-aura
    - Windows: %LOCALAPPDATA%/wayfinder-aura
    """
    if is_macos():
        # macOS uses the same directory for config and data
        return Path.home() / "Library" / "Application Support" / "wayfinder-aura"
    elif is_windows():
        localappdata = os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))
        return Path(localappdata) / "wayfinder-aura"
    else:  # Linux
        xdg_data = os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))
        return Path(xdg_data) / "wayfinder-aura"


def get_cache_dir() -> Path:
    """
    Get the cache directory for the app.
    
    Returns platform-appropriate path:
    - Linux: ~/.cache/wayfinder-aura (XDG)
    - macOS: ~/Library/Caches/wayfinder-aura
    - Windows: %LOCALAPPDATA%/wayfinder-aura/cache
    """
    if is_macos():
        return Path.home() / "Library" / "Caches" / "wayfinder-aura"
    elif is_windows():
        localappdata = os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))
        return Path(localappdata) / "wayfinder-aura" / "cache"
    else:  # Linux
        xdg_cache = os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))
        return Path(xdg_cache) / "wayfinder-aura"


def ensure_directories():
    """Ensure all app directories exist."""
    get_config_dir().mkdir(parents=True, exist_ok=True)
    get_data_dir().mkdir(parents=True, exist_ok=True)
    get_cache_dir().mkdir(parents=True, exist_ok=True)


# =============================================================================
# Text Injection Tool Detection
# =============================================================================

def is_ydotool_available() -> bool:
    """Check if ydotool is available for text injection (Linux only)."""
    if not is_linux():
        return False
    try:
        result = subprocess.run(["which", "ydotool"], capture_output=True, timeout=5)
        return result.returncode == 0
    except Exception:
        return False


def is_xdotool_available() -> bool:
    """Check if xdotool is available for text injection (Linux/X11 only)."""
    if not is_linux():
        return False
    try:
        result = subprocess.run(["which", "xdotool"], capture_output=True, timeout=5)
        return result.returncode == 0
    except Exception:
        return False


def is_wtype_available() -> bool:
    """Check if wtype is available (Wayland virtual-keyboard injection, Linux only)."""
    if not is_linux():
        return False
    try:
        result = subprocess.run(["which", "wtype"], capture_output=True, timeout=5)
        return result.returncode == 0
    except Exception:
        return False


def is_pyautogui_available() -> bool:
    """Check if pyautogui is available for text injection (macOS/Windows)."""
    try:
        import pyautogui
        return True
    except ImportError:
        return False


def get_text_injector() -> str:
    """
    Determine the best text injection method for the current platform.
    
    Returns:
        - Linux/Wayland: "wtype" (preferred) or "ydotool"
        - Linux/X11: "xdotool" or "ydotool"
        - macOS: "pyautogui"
        - Windows: "pyautogui"
        - If unavailable: "none"
    """
    if is_linux():
        if is_wayland():
            # Prefer ydotool when its daemon is reachable: KDE Plasma's KWin shows a per-use
            # "allow input control" security prompt for wtype's Wayland virtual-keyboard
            # protocol, but ydotool injects via /dev/uinput (kernel level) with no prompt.
            # wtype stays the fallback for Flatpak (no uinput device/daemon) and for wlroots
            # compositors without a running ydotoold. (Lazy import avoids a platform<->injector
            # import cycle.)
            if is_ydotool_available():
                try:
                    from ..core.injector import check_ydotool_ready
                    if check_ydotool_ready()[0]:
                        return "ydotool"
                except Exception:
                    pass
            if is_wtype_available():
                return "wtype"
            if is_ydotool_available():
                return "ydotool"
        else:
            if is_xdotool_available():
                return "xdotool"
            elif is_ydotool_available():
                return "ydotool"
        return "none"
    elif is_macos() or is_windows():
        if is_pyautogui_available():
            return "pyautogui"
        return "none"
    return "none"


# =============================================================================
# Cross-Platform Binary Detection
# =============================================================================

def find_executable(name: str) -> str | None:
    """
    Find an executable by name in the system PATH.
    
    Works cross-platform (uses shutil.which).
    
    Args:
        name: The executable name (e.g., 'whisper-cli')
        
    Returns:
        Full path to the executable, or None if not found
    """
    return shutil.which(name)


def get_default_whisper_binary() -> str:
    """
    Get the default whisper.cpp binary path for the current platform.

    Returns platform-appropriate path:
    - AppImage: $APPDIR/usr/bin/whisper-cli
    - Flatpak: /app/bin/whisper-cli
    - macOS: Homebrew paths (/opt/homebrew/bin or /usr/local/bin)
    - Linux: ~/whisper.cpp/build/bin/whisper-cli
    - Windows: ~/whisper.cpp/build/bin/whisper-cli.exe
    """
    appdir = get_appimage_dir()
    if appdir:
        bundled = appdir / "usr" / "bin" / "whisper-cli"
        if bundled.exists():
            return str(bundled)
    if is_flatpak():
        return "/app/bin/whisper-cli"
    if is_macos():
        # Check Homebrew paths (ARM then Intel)
        for brew_path in [Path("/opt/homebrew/bin/whisper-cli"),
                          Path("/usr/local/bin/whisper-cli")]:
            if brew_path.exists():
                return str(brew_path)
        # Also check PATH
        found = find_executable("whisper-cli")
        if found:
            return found
    if is_windows():
        return str(Path.home() / "whisper.cpp" / "build" / "bin" / "whisper-cli.exe")
    return str(Path.home() / "whisper.cpp" / "build" / "bin" / "whisper-cli")


def get_default_llama_binary() -> str:
    """
    Get the default llama.cpp binary path for the current platform.

    Returns platform-appropriate path:
    - AppImage: $APPDIR/usr/bin/llama-cli
    - Flatpak: /app/bin/llama-cli
    - macOS: Homebrew paths (/opt/homebrew/bin or /usr/local/bin)
    - Linux: ~/llama.cpp/build/bin/llama-cli
    - Windows: ~/llama.cpp/build/bin/llama-cli.exe
    """
    appdir = get_appimage_dir()
    if appdir:
        bundled = appdir / "usr" / "bin" / "llama-cli"
        if bundled.exists():
            return str(bundled)
    if is_flatpak():
        return "/app/bin/llama-cli"
    if is_macos():
        # Check Homebrew paths (ARM then Intel)
        for brew_path in [Path("/opt/homebrew/bin/llama-cli"),
                          Path("/usr/local/bin/llama-cli")]:
            if brew_path.exists():
                return str(brew_path)
        found = find_executable("llama-cli")
        if found:
            return found
    if is_windows():
        return str(Path.home() / "llama.cpp" / "build" / "bin" / "llama-cli.exe")
    return str(Path.home() / "llama.cpp" / "build" / "bin" / "llama-cli")


def get_default_model_dir() -> Path:
    """
    Get the default directory for Whisper models.
    
    Returns platform-appropriate path:
    - AppImage: $APPDIR/usr/share/whisper-models (if bundled), else data dir
    - Flatpak: /app/share/whisper-models
    - Other: data_dir/models
    """
    appdir = get_appimage_dir()
    if appdir:
        bundled = appdir / "usr" / "share" / "whisper-models"
        if bundled.exists():
            return bundled
    return get_data_dir() / "models"


def get_default_llm_model_dir() -> Path:
    """
    Get the default directory for LLM models.
    
    Returns platform-appropriate path:
    - AppImage: $APPDIR/usr/share/llm-models (if bundled), else data dir
    - Flatpak: /app/share/llm-models
    - Other: data_dir/llm-models
    """
    appdir = get_appimage_dir()
    if appdir:
        bundled = appdir / "usr" / "share" / "llm-models"
        if bundled.exists():
            return bundled
    return get_data_dir() / "llm-models"


def get_default_ydotool_binary() -> str | None:
    """
    Get the ydotool binary path, checking AppImage bundle first.
    
    Returns:
        Path to ydotool binary, or None if not found.
    """
    appdir = get_appimage_dir()
    if appdir:
        bundled = appdir / "usr" / "bin" / "ydotool"
        if bundled.exists():
            return str(bundled)
    return find_executable("ydotool")


# =============================================================================
# Platform Info Summary
# =============================================================================

def get_platform_info() -> dict:
    """
    Get a summary of platform information.
    
    Returns:
        Dictionary with platform details for debugging/display.
    """
    info = {
        "platform": get_platform(),
        "is_linux": is_linux(),
        "is_macos": is_macos(),
        "is_windows": is_windows(),
        "config_dir": str(get_config_dir()),
        "data_dir": str(get_data_dir()),
        "cache_dir": str(get_cache_dir()),
        "text_injector": get_text_injector(),
    }
    
    if is_linux():
        info.update({
            "is_wayland": is_wayland(),
            "is_x11": is_x11(),
            "is_flatpak": is_flatpak(),
            "is_appimage": is_appimage(),
            "desktop_environment": get_desktop_environment(),
            "session_type": get_session_type(),
        })
    
    return info
