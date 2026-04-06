"""
Hotkey detection modules for Wayfinder Aura.

Supports multiple detection methods:
- pynput: Cross-platform (Linux, macOS, Windows) - RECOMMENDED
- evdev: Direct input device monitoring (Linux only)
- socket: Unix socket for external triggers (Linux/macOS)
- dbus: XDG GlobalShortcuts portal (Linux/Wayland)
"""

import sys

# Cross-platform pynput listener (available on all platforms)
from .pynput_listener import (
    pynput_hotkey_listener,
    is_pynput_available,
    evdev_code_to_pynput,
    get_key_name,
    get_available_hotkey_backends,
    EVDEV_TO_PYNPUT,
    PYNPUT_TO_NAME,
    MODIFIER_KEYS,
)

# EventType is always needed (defined in evdev but used everywhere)
from .types import EventType

# Linux-only: evdev for direct input device monitoring
# On non-Linux platforms, provide stub implementations
if sys.platform.startswith('linux'):
    from .evdev import (
        hotkey_listener,
        get_all_input_devices,
        find_keyboard_devices,
        MOUSE_BUTTON_CODES,
    )
    from .socket import socket_listener, send_toggle, send_style
    from .dbus import wayland_hotkey_listener, is_dbus_available
else:
    # Stub implementations for non-Linux platforms
    def hotkey_listener(*args, **kwargs):
        """evdev hotkey listener - Linux only. Use pynput_hotkey_listener instead."""
        raise NotImplementedError("evdev hotkey listener is Linux-only. Use pynput_hotkey_listener.")
    
    def get_all_input_devices():
        """evdev device listing - Linux only."""
        return []
    
    def find_keyboard_devices(enabled_devices=None):
        """evdev keyboard detection - Linux only."""
        return []
    
    MOUSE_BUTTON_CODES = set()
    
    # Socket is available on Unix-like systems (Linux + macOS)
    if sys.platform == 'darwin':
        from .socket import socket_listener, send_toggle, send_style
    else:
        # Windows doesn't have Unix sockets
        def socket_listener(*args, **kwargs):
            """Unix socket listener - not available on Windows."""
            raise NotImplementedError("Unix socket listener is not available on Windows.")
        
        def send_toggle():
            """Send toggle via Unix socket - not available on Windows."""
            return False
        
        def send_style(style=None):
            """Send style via Unix socket - not available on Windows."""
            return False
    
    def wayland_hotkey_listener(*args, **kwargs):
        """Wayland D-Bus hotkey listener - Linux only."""
        return False
    
    def is_dbus_available():
        """Check D-Bus availability - Linux only."""
        return False


def get_best_hotkey_listener():
    """
    Get the best available hotkey listener for the current platform.
    
    Returns:
        Tuple of (listener_function, backend_name)
    """
    if sys.platform.startswith('linux'):
        # On Linux, prefer evdev for lower latency, fall back to pynput
        try:
            import evdev
            return (hotkey_listener, "evdev")
        except ImportError:
            pass
    
    # All platforms: use pynput if available
    if is_pynput_available():
        return (pynput_hotkey_listener, "pynput")
    
    # No listener available
    return (None, "none")


__all__ = [
    # Event types (used by all listeners)
    "EventType",
    # Cross-platform pynput
    "pynput_hotkey_listener",
    "is_pynput_available",
    "evdev_code_to_pynput",
    "get_key_name",
    "get_available_hotkey_backends",
    "EVDEV_TO_PYNPUT",
    "PYNPUT_TO_NAME",
    "MODIFIER_KEYS",
    # Best listener helper
    "get_best_hotkey_listener",
    # Linux evdev (stubs on other platforms)
    "hotkey_listener",
    "get_all_input_devices",
    "find_keyboard_devices",
    "MOUSE_BUTTON_CODES",
    # Unix socket (Linux + macOS, stubs on Windows)
    "socket_listener",
    "send_toggle",
    "send_style",
    # Linux Wayland D-Bus
    "wayland_hotkey_listener",
    "is_dbus_available",
]
