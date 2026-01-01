"""
Hotkey detection modules for Wayfinder Voice.

Supports multiple detection methods:
- evdev: Direct input device monitoring (X11)
- socket: Unix socket for external triggers (Wayland)
- dbus: XDG GlobalShortcuts portal (Wayland)
"""

from .evdev import (
    EventType,
    hotkey_listener,
    get_all_input_devices,
    find_keyboard_devices,
)
from .socket import socket_listener, send_toggle, send_style
from .dbus import wayland_hotkey_listener, is_dbus_available

__all__ = [
    "EventType",
    "hotkey_listener",
    "get_all_input_devices",
    "find_keyboard_devices",
    "socket_listener",
    "send_toggle",
    "send_style",
    "wayland_hotkey_listener",
    "is_dbus_available",
]

