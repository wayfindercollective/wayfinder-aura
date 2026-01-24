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
        - Linux/Wayland: "ydotool"
        - Linux/X11: "xdotool" or "ydotool"
        - macOS: "pyautogui"
        - Windows: "pyautogui"
        - If unavailable: "none"
    """
    if is_linux():
        if is_wayland():
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
    - Linux/macOS: ~/whisper.cpp/build/bin/whisper-cli
    - Windows: ~/whisper.cpp/build/bin/whisper-cli.exe
    """
    if is_windows():
        return str(Path.home() / "whisper.cpp" / "build" / "bin" / "whisper-cli.exe")
    else:
        return str(Path.home() / "whisper.cpp" / "build" / "bin" / "whisper-cli")


def get_default_llama_binary() -> str:
    """
    Get the default llama.cpp binary path for the current platform.
    
    Returns platform-appropriate path:
    - Linux/macOS: ~/llama.cpp/build/bin/llama-cli
    - Windows: ~/llama.cpp/build/bin/llama-cli.exe
    """
    if is_windows():
        return str(Path.home() / "llama.cpp" / "build" / "bin" / "llama-cli.exe")
    else:
        return str(Path.home() / "llama.cpp" / "build" / "bin" / "llama-cli")


def get_default_model_dir() -> Path:
    """
    Get the default directory for Whisper models.
    
    Returns platform-appropriate path inside the data directory.
    """
    return get_data_dir() / "models"


def get_default_llm_model_dir() -> Path:
    """
    Get the default directory for LLM models.
    
    Returns platform-appropriate path inside the data directory.
    """
    return get_data_dir() / "llm-models"


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
            "desktop_environment": get_desktop_environment(),
            "session_type": get_session_type(),
        })
    
    return info
