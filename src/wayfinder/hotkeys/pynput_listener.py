"""
Cross-platform hotkey listener using pynput.

Works on Linux, macOS, and Windows.
Uses pynput for global keyboard monitoring.
"""

import time
from queue import Queue
from threading import Event
from typing import Callable, Optional

# Import EventType from evdev module for consistency
from .evdev import EventType


# Check if pynput is available
try:
    from pynput import keyboard
    from pynput.keyboard import Key, KeyCode
    PYNPUT_AVAILABLE = True
except ImportError:
    PYNPUT_AVAILABLE = False
    keyboard = None
    Key = None
    KeyCode = None


def is_pynput_available() -> bool:
    """Check if pynput is installed and available."""
    return PYNPUT_AVAILABLE


# Mapping from evdev key codes to pynput Key objects
# This allows the same key codes to work across platforms
EVDEV_TO_PYNPUT = {
    # Function keys
    59: Key.f1,
    60: Key.f2,
    61: Key.f3,
    62: Key.f4,
    63: Key.f5,
    64: Key.f6,
    65: Key.f7,
    66: Key.f8,
    67: Key.f9,
    68: Key.f10,
    87: Key.f11,
    88: Key.f12,
    # Modifier keys
    29: Key.ctrl_l,
    97: Key.ctrl_r,
    42: Key.shift_l,
    54: Key.shift_r,
    56: Key.alt_l,
    100: Key.alt_r,
    125: Key.cmd,  # Super/Windows/Command key
    126: Key.cmd_r,
    # Special keys
    1: Key.esc,
    14: Key.backspace,
    15: Key.tab,
    28: Key.enter,
    57: Key.space,
    58: Key.caps_lock,
    111: Key.delete,
    110: Key.insert,
    102: Key.home,
    107: Key.end,
    104: Key.page_up,
    109: Key.page_down,
    103: Key.up,
    108: Key.down,
    105: Key.left,
    106: Key.right,
    # Media keys (if supported)
    164: Key.media_play_pause,
    163: Key.media_next,
    165: Key.media_previous,
    113: Key.media_volume_mute,
    114: Key.media_volume_down,
    115: Key.media_volume_up,
}

# Reverse mapping for display purposes
PYNPUT_TO_NAME = {
    Key.f1: "F1", Key.f2: "F2", Key.f3: "F3", Key.f4: "F4",
    Key.f5: "F5", Key.f6: "F6", Key.f7: "F7", Key.f8: "F8",
    Key.f9: "F9", Key.f10: "F10", Key.f11: "F11", Key.f12: "F12",
    Key.ctrl_l: "Ctrl", Key.ctrl_r: "Ctrl",
    Key.shift_l: "Shift", Key.shift_r: "Shift",
    Key.alt_l: "Alt", Key.alt_r: "Alt",
    Key.cmd: "Super", Key.cmd_r: "Super",
    Key.esc: "Escape", Key.space: "Space",
    Key.enter: "Enter", Key.backspace: "Backspace",
    Key.tab: "Tab", Key.caps_lock: "CapsLock",
    Key.delete: "Delete", Key.insert: "Insert",
    Key.home: "Home", Key.end: "End",
    Key.page_up: "PageUp", Key.page_down: "PageDown",
    Key.up: "Up", Key.down: "Down", Key.left: "Left", Key.right: "Right",
}

# Modifier key sets for checking combinations
MODIFIER_KEYS = {
    'ctrl': {Key.ctrl_l, Key.ctrl_r},
    'shift': {Key.shift_l, Key.shift_r},
    'alt': {Key.alt_l, Key.alt_r},
    'super': {Key.cmd, Key.cmd_r},
}


def evdev_code_to_pynput(evdev_code: int) -> Optional[Key | KeyCode]:
    """
    Convert an evdev key code to a pynput Key object.
    
    Args:
        evdev_code: The evdev key code (e.g., 67 for F9)
        
    Returns:
        pynput Key object, or KeyCode for letter/number keys, or None
    """
    if not PYNPUT_AVAILABLE:
        return None
    
    # Check if it's in our special keys mapping
    if evdev_code in EVDEV_TO_PYNPUT:
        return EVDEV_TO_PYNPUT[evdev_code]
    
    # Handle letter keys (KEY_Q=16 to KEY_P=25, KEY_A=30 to KEY_L=38, KEY_Z=44 to KEY_M=50)
    # Evdev key codes for letters:
    # Q=16, W=17, E=18, R=19, T=20, Y=21, U=22, I=23, O=24, P=25
    # A=30, S=31, D=32, F=33, G=34, H=35, J=36, K=37, L=38
    # Z=44, X=45, C=46, V=47, B=48, N=49, M=50
    letter_map = {
        16: 'q', 17: 'w', 18: 'e', 19: 'r', 20: 't', 21: 'y', 22: 'u', 23: 'i', 24: 'o', 25: 'p',
        30: 'a', 31: 's', 32: 'd', 33: 'f', 34: 'g', 35: 'h', 36: 'j', 37: 'k', 38: 'l',
        44: 'z', 45: 'x', 46: 'c', 47: 'v', 48: 'b', 49: 'n', 50: 'm',
    }
    if evdev_code in letter_map:
        return KeyCode.from_char(letter_map[evdev_code])
    
    # Handle number keys (KEY_1=2 to KEY_0=11)
    if 2 <= evdev_code <= 11:
        num = str((evdev_code - 1) % 10)  # Maps 2->1, 3->2, ..., 11->0
        return KeyCode.from_char(num)
    
    return None


def get_key_name(key) -> str:
    """Get a human-readable name for a pynput key."""
    if key in PYNPUT_TO_NAME:
        return PYNPUT_TO_NAME[key]
    if isinstance(key, KeyCode):
        if key.char:
            return key.char.upper()
        elif key.vk:
            return f"Key{key.vk}"
    return str(key)


def pynput_hotkey_listener(
    event_queue: Queue,
    hotkey_key: int,
    hotkey_modifiers: list[str],
    stop_event: Event,
    log_callback: Optional[Callable[[str], None]] = None,
    style_toggle_key: Optional[int] = None,
    style_toggle_modifiers: Optional[list[str]] = None,
):
    """
    Cross-platform hotkey listener using pynput.
    
    This function runs in a background thread and monitors for the configured
    hotkey combinations.
    
    Args:
        event_queue: Queue to put events into
        hotkey_key: The main key code (evdev format, e.g., F9 = 67)
        hotkey_modifiers: List of modifier names (e.g., ["ctrl", "shift"])
        stop_event: Threading event to signal shutdown
        log_callback: Optional function to call with log messages
        style_toggle_key: Key code for style toggle (e.g., F10 = 68)
        style_toggle_modifiers: List of modifier names for style toggle
    """
    def log(msg: str):
        if log_callback:
            try:
                log_callback(msg)
            except Exception:
                pass
    
    if not PYNPUT_AVAILABLE:
        log("⚠️ pynput not available. Install with: pip install pynput")
        return
    
    # Convert evdev key codes to pynput keys
    target_key = evdev_code_to_pynput(hotkey_key)
    if target_key is None:
        log(f"⚠️ Unknown hotkey code: {hotkey_key}")
        return
    
    style_target_key = None
    if style_toggle_key:
        style_target_key = evdev_code_to_pynput(style_toggle_key)
    
    # Build required modifier sets
    required_modifiers = set()
    for mod_name in hotkey_modifiers:
        if mod_name.lower() in MODIFIER_KEYS:
            required_modifiers.add(mod_name.lower())
    
    style_required_modifiers = set()
    if style_toggle_modifiers:
        for mod_name in style_toggle_modifiers:
            if mod_name.lower() in MODIFIER_KEYS:
                style_required_modifiers.add(mod_name.lower())
    
    # Track currently pressed modifiers
    pressed_modifiers = set()  # Set of modifier names ('ctrl', 'shift', etc.)
    
    # Display hotkey info
    hotkey_display = get_key_name(target_key)
    if required_modifiers:
        mod_str = "+".join(mod.capitalize() for mod in sorted(required_modifiers))
        hotkey_display = f"{mod_str}+{hotkey_display}"
    log(f"🎹 Listening for hotkey: {hotkey_display}")
    
    if style_target_key:
        style_display = get_key_name(style_target_key)
        if style_required_modifiers:
            mod_str = "+".join(mod.capitalize() for mod in sorted(style_required_modifiers))
            style_display = f"{mod_str}+{style_display}"
        log(f"✎ Style toggle hotkey: {style_display}")
    
    def check_modifiers(required: set[str]) -> bool:
        """Check if all required modifiers are currently pressed."""
        if not required:
            return True
        return required <= pressed_modifiers
    
    def on_press(key):
        nonlocal pressed_modifiers
        
        # Track modifier state
        for mod_name, mod_keys in MODIFIER_KEYS.items():
            if key in mod_keys:
                pressed_modifiers.add(mod_name)
        
        # Check for main hotkey
        if key == target_key and check_modifiers(required_modifiers):
            log("🎯 Hotkey activated!")
            event_queue.put((EventType.HOTKEY_PRESSED, None))
        
        # Check for style toggle hotkey
        if style_target_key and key == style_target_key and check_modifiers(style_required_modifiers):
            log("✎ Style toggle activated!")
            event_queue.put((EventType.STYLE_TOGGLE, None))
    
    def on_release(key):
        nonlocal pressed_modifiers
        
        # Track modifier state
        for mod_name, mod_keys in MODIFIER_KEYS.items():
            if key in mod_keys:
                pressed_modifiers.discard(mod_name)
    
    # Start the listener
    listener = keyboard.Listener(on_press=on_press, on_release=on_release)
    listener.start()
    
    log("🎧 Cross-platform hotkey listener active (pynput)")
    
    try:
        while not stop_event.is_set():
            time.sleep(0.1)
    finally:
        listener.stop()


def get_available_hotkey_backends() -> list[str]:
    """
    Get a list of available hotkey backends for the current platform.
    
    Returns:
        List of backend names (e.g., ['pynput', 'evdev', 'socket'])
    """
    backends = []
    
    if PYNPUT_AVAILABLE:
        backends.append("pynput")
    
    # Check for evdev (Linux only)
    try:
        import evdev
        backends.append("evdev")
    except ImportError:
        pass
    
    # Socket is always available on Unix-like systems
    import sys
    if sys.platform != 'win32':
        backends.append("socket")
    
    # D-Bus is available on Linux with proper packages
    try:
        import dbus
        from gi.repository import GLib
        backends.append("dbus")
    except ImportError:
        pass
    
    return backends
