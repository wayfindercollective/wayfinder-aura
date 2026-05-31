"""
Cross-platform hotkey listener using pynput.

Works on Linux, macOS, and Windows.
Uses pynput for global keyboard monitoring.
"""
# Defer annotation evaluation (PEP 563). Without this, the `Optional[Key | KeyCode]`
# annotation below is evaluated at import time and crashes the WHOLE app when pynput is
# unavailable — the import guard sets Key=KeyCode=None, and `None | None` is a TypeError.
from __future__ import annotations

import time
from queue import Queue
from threading import Event
from typing import Callable, Optional

# Import EventType from evdev module for consistency
from .types import EventType


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


def _k(*names):
    """Safely get the first matching pynput Key attribute, or None."""
    for name in names:
        v = getattr(Key, name, None)
        if v is not None:
            return v
    return None


# Mapping from evdev key codes to pynput Key objects
# Uses _k() so missing keys (e.g. Key.insert on macOS) are skipped safely.
_raw_evdev_map = {
    # Function keys
    59: _k("f1"), 60: _k("f2"), 61: _k("f3"), 62: _k("f4"),
    63: _k("f5"), 64: _k("f6"), 65: _k("f7"), 66: _k("f8"),
    67: _k("f9"), 68: _k("f10"), 87: _k("f11"), 88: _k("f12"),
    # Modifier keys — macOS uses ctrl/alt/shift, Linux uses ctrl_l/alt_l/shift_l
    29: _k("ctrl_l", "ctrl"),
    97: _k("ctrl_r"),
    42: _k("shift_l", "shift"),
    54: _k("shift_r"),
    56: _k("alt_l", "alt"),
    100: _k("alt_r"),
    125: _k("cmd"),   # Super/Windows/Command key
    126: _k("cmd_r"),
    # Special keys
    1:   _k("esc"),
    14:  _k("backspace"),
    15:  _k("tab"),
    28:  _k("enter"),
    57:  _k("space"),
    58:  _k("caps_lock"),
    111: _k("delete"),
    110: _k("insert"),   # Not on macOS — will be None, skipped
    102: _k("home"),
    107: _k("end"),
    104: _k("page_up"),
    109: _k("page_down"),
    103: _k("up"),
    108: _k("down"),
    105: _k("left"),
    106: _k("right"),
    # Media keys
    164: _k("media_play_pause"),
    163: _k("media_next"),
    165: _k("media_previous"),
    113: _k("media_volume_mute"),
    114: _k("media_volume_down"),
    115: _k("media_volume_up"),
}
EVDEV_TO_PYNPUT = {k: v for k, v in _raw_evdev_map.items() if v is not None}

# Reverse mapping for display purposes — built dynamically to avoid missing keys
PYNPUT_TO_NAME = {}
for _names, _label in [
    (("f1",), "F1"), (("f2",), "F2"), (("f3",), "F3"), (("f4",), "F4"),
    (("f5",), "F5"), (("f6",), "F6"), (("f7",), "F7"), (("f8",), "F8"),
    (("f9",), "F9"), (("f10",), "F10"), (("f11",), "F11"), (("f12",), "F12"),
    (("ctrl_l", "ctrl"), "Ctrl"), (("ctrl_r",), "Ctrl"),
    (("shift_l", "shift"), "Shift"), (("shift_r",), "Shift"),
    (("alt_l", "alt"), "Alt"), (("alt_r",), "Alt"),
    (("cmd",), "Super"), (("cmd_r",), "Super"),
    (("esc",), "Escape"), (("space",), "Space"),
    (("enter",), "Enter"), (("backspace",), "Backspace"),
    (("tab",), "Tab"), (("caps_lock",), "CapsLock"),
    (("delete",), "Delete"), (("insert",), "Insert"),
    (("home",), "Home"), (("end",), "End"),
    (("page_up",), "PageUp"), (("page_down",), "PageDown"),
    (("up",), "Up"), (("down",), "Down"), (("left",), "Left"), (("right",), "Right"),
]:
    _key = _k(*_names)
    if _key is not None:
        PYNPUT_TO_NAME[_key] = _label

if hasattr(Key, 'insert'):
    PYNPUT_TO_NAME[Key.insert] = "Insert"

# Modifier key sets for checking combinations
MODIFIER_KEYS = {}
for _mod, _key_names in [
    ('ctrl', ("ctrl_l", "ctrl")), ('ctrl', ("ctrl_r",)),
    ('shift', ("shift_l", "shift")), ('shift', ("shift_r",)),
    ('alt', ("alt_l", "alt")), ('alt', ("alt_r",)),
    ('super', ("cmd",)), ('super', ("cmd_r",)),
]:
    _key = _k(*_key_names)
    if _key is not None:
        MODIFIER_KEYS.setdefault(_mod, set()).add(_key)


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
    config_ref: Optional[dict] = None,
):
    """
    Cross-platform hotkey listener using pynput.

    If config_ref is provided, the listener reads hotkey settings from it
    on each keypress, allowing live hotkey changes without restarting.
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

    # Initial key setup
    target_key = evdev_code_to_pynput(hotkey_key)
    if target_key is None:
        log(f"⚠️ Unknown hotkey code: {hotkey_key}")
        return

    style_target_key = evdev_code_to_pynput(style_toggle_key) if style_toggle_key else None

    def _build_mod_set(mod_list):
        return {m.lower() for m in (mod_list or []) if m.lower() in MODIFIER_KEYS}

    required_modifiers = _build_mod_set(hotkey_modifiers)
    style_required_modifiers = _build_mod_set(style_toggle_modifiers)

    # Track currently pressed modifiers
    pressed_modifiers = set()

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

    def _get_current_keys():
        """Read live hotkey config if available, otherwise use initial values."""
        nonlocal target_key, style_target_key, required_modifiers, style_required_modifiers
        if config_ref is not None:
            new_key = evdev_code_to_pynput(config_ref.get("hotkey_key", hotkey_key))
            new_style = evdev_code_to_pynput(config_ref.get("style_toggle_key", style_toggle_key or 0))
            if new_key and new_key != target_key:
                target_key = new_key
                required_modifiers = _build_mod_set(config_ref.get("hotkey_modifiers", []))
                log(f"🎹 Hotkey changed to: {get_key_name(target_key)}")
            if new_style and new_style != style_target_key:
                style_target_key = new_style
                style_required_modifiers = _build_mod_set(config_ref.get("style_toggle_modifiers", []))

    def check_modifiers(required: set[str]) -> bool:
        if not required:
            return True
        return required <= pressed_modifiers

    # Debounce
    _last_hotkey_time = 0.0
    _last_style_time = 0.0
    DEBOUNCE_SECONDS = 0.5

    def on_press(key):
        nonlocal pressed_modifiers, _last_hotkey_time, _last_style_time

        # Track modifier state
        for mod_name, mod_keys in MODIFIER_KEYS.items():
            if key in mod_keys:
                pressed_modifiers.add(mod_name)

        # Read live config for hotkey changes
        _get_current_keys()

        now = time.time()

        # Check for main hotkey (with debounce)
        if key == target_key and check_modifiers(required_modifiers):
            if now - _last_hotkey_time >= DEBOUNCE_SECONDS:
                _last_hotkey_time = now
                print(f"[Hotkey] {get_key_name(target_key)} — activating!", flush=True)
                log("🎯 Hotkey activated!")
                event_queue.put((EventType.HOTKEY_PRESSED, None))

        # Check for style toggle hotkey (with debounce)
        if style_target_key and key == style_target_key and check_modifiers(style_required_modifiers):
            if now - _last_style_time >= DEBOUNCE_SECONDS:
                _last_style_time = now
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

    print(f"[Hotkey] pynput listener started, waiting for: {get_key_name(target_key)}", flush=True)
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
