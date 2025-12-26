"""
evdev-based hotkey listener for Wayfinder Voice.

Monitors input devices directly for hotkey presses.
Works on X11 and can work on Wayland with proper permissions.
"""

import select
from enum import Enum, auto
from queue import Queue
from threading import Event
from typing import Callable, Optional

import evdev
from evdev import InputDevice, categorize, ecodes

from ..config import MODIFIER_CODES


class EventType(Enum):
    """Event types for the application event queue."""
    HOTKEY_PRESSED = auto()
    TRANSCRIPTION_DONE = auto()
    TRANSCRIPTION_ERROR = auto()
    INJECTION_DONE = auto()
    INJECTION_ERROR = auto()
    CHUNK_TRANSCRIBED = auto()  # A chunk was transcribed during recording
    CHUNKED_TRANSCRIPTION_DONE = auto()  # All chunks transcribed


def get_all_input_devices() -> list[dict]:
    """
    Get all input devices that could potentially send hotkeys.
    
    Returns:
        List of dicts with device info (name, path, type, device object).
    """
    devices = [evdev.InputDevice(path) for path in evdev.list_devices()]
    result = []
    
    for device in devices:
        capabilities = device.capabilities()
        if ecodes.EV_KEY in capabilities:
            key_caps = capabilities[ecodes.EV_KEY]
            has_fkeys = ecodes.KEY_F1 in key_caps and ecodes.KEY_F12 in key_caps
            
            name_lower = device.name.lower()
            is_virtual = "virtual" in name_lower or "ydotool" in name_lower
            
            if has_fkeys and not is_virtual:
                # Determine device type for display
                device_type = "keyboard"
                if "mouse" in name_lower:
                    device_type = "mouse"
                elif "gamepad" in name_lower or "controller" in name_lower:
                    device_type = "gamepad"
                
                result.append({
                    "name": device.name,
                    "path": device.path,
                    "type": device_type,
                    "device": device,
                })
    
    return result


def find_keyboard_devices(enabled_devices: Optional[list[str]] = None) -> list[InputDevice]:
    """
    Find input devices to listen to based on config.
    
    Args:
        enabled_devices: List of device names to enable. None = all devices.
        
    Returns:
        List of InputDevice objects to monitor.
    """
    all_devices = get_all_input_devices()
    
    if enabled_devices:
        # Only use explicitly enabled devices
        return [d["device"] for d in all_devices if d["name"] in enabled_devices]
    else:
        # Default: use all available devices
        return [d["device"] for d in all_devices]


def hotkey_listener(
    event_queue: Queue,
    hotkey_key: int,
    hotkey_modifiers: list[str],
    stop_event: Event,
    enabled_devices: Optional[list[str]] = None,
    log_callback: Optional[Callable[[str], None]] = None
):
    """
    Listen for hotkey presses using evdev.
    
    This function runs in a background thread and monitors input devices
    for the configured hotkey combination.
    
    Args:
        event_queue: Queue to put events into
        hotkey_key: The main key code (e.g., F9 = 67)
        hotkey_modifiers: List of modifier names (e.g., ["ctrl", "shift"])
        stop_event: Threading event to signal shutdown
        enabled_devices: List of device names to monitor (None = all)
        log_callback: Optional function to call with log messages
    """
    def log(msg: str):
        if log_callback:
            try:
                log_callback(msg)
            except Exception:
                pass
    
    devices = find_keyboard_devices(enabled_devices)
    if not devices:
        log("⚠️ No input devices found!")
        log("   Check: sudo usermod -aG input $USER")
        return

    log(f"🎮 Monitoring {len(devices)} input device(s):")
    for dev in devices:
        short_name = dev.name[:40] + "..." if len(dev.name) > 40 else dev.name
        log(f"   • {short_name}")
    
    # Build set of required modifier key codes
    required_modifiers = set()
    for name in hotkey_modifiers:
        if name.lower() in MODIFIER_CODES:
            required_modifiers.update(MODIFIER_CODES[name.lower()])
    
    pressed_modifiers = set()
    all_modifier_codes = set()
    for codes in MODIFIER_CODES.values():
        all_modifier_codes.update(codes)

    # Map file descriptors to devices
    fd_to_device = {dev.fd: dev for dev in devices}

    try:
        while not stop_event.is_set():
            # Wait for input from any device
            r, _, _ = select.select(list(fd_to_device.keys()), [], [], 0.5)
            
            for fd in r:
                device = fd_to_device[fd]
                for event in device.read():
                    if event.type == ecodes.EV_KEY:
                        key_event = categorize(event)
                        keycode = key_event.scancode
                        
                        # Track modifier key state
                        if keycode in all_modifier_codes:
                            if key_event.keystate == 1:  # Key down
                                pressed_modifiers.add(keycode)
                            elif key_event.keystate == 0:  # Key up
                                pressed_modifiers.discard(keycode)
                        
                        # Check for hotkey press
                        if keycode == hotkey_key and key_event.keystate == 1:
                            if required_modifiers:
                                # Check if all required modifiers are pressed
                                all_mods_pressed = all(
                                    pressed_modifiers & set(MODIFIER_CODES.get(mod.lower(), []))
                                    for mod in hotkey_modifiers
                                )
                                if all_mods_pressed:
                                    event_queue.put((EventType.HOTKEY_PRESSED, None))
                            else:
                                # No modifiers required
                                event_queue.put((EventType.HOTKEY_PRESSED, None))
    except Exception as e:
        log(f"⚠️ Hotkey error: {e}")

