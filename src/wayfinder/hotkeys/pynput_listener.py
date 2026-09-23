"""
Cross-platform hotkey listener using pynput.

Works on Linux, macOS, and Windows.
Uses pynput for global keyboard monitoring.
"""
# Defer annotation evaluation (PEP 563). Without this, the `Optional[Key | KeyCode]`
# annotation below is evaluated at import time and crashes the WHOLE app when pynput is
# unavailable — the import guard sets Key=KeyCode=None, and `None | None` is a TypeError.
from __future__ import annotations

import sys
import threading
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
    # Offered by the Settings hotkey dropdown — both must stay mappable or
    # selecting them yields a hotkey that never fires (tests/test_hotkey_dropdown.py).
    70:  _k("scroll_lock"),
    119: _k("pause"),
    # Media keys
    164: _k("media_play_pause"),
    163: _k("media_next"),
    165: _k("media_previous"),
    113: _k("media_volume_mute"),
    114: _k("media_volume_down"),
    115: _k("media_volume_up"),
}
EVDEV_TO_PYNPUT = {k: v for k, v in _raw_evdev_map.items() if v is not None}

# Reverse map for the Settings "Detect" capture (pynput key -> evdev code). Function/special
# keys are 1:1; modifier codes are skipped during capture (see _ALL_MODIFIER_KEYS below).
PYNPUT_TO_EVDEV = {v: k for k, v in EVDEV_TO_PYNPUT.items()}

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
    (("scroll_lock",), "ScrollLock"), (("pause",), "Pause"),
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

# Fn is a Quartz flag rather than a normal pynput Key on macOS. Keeping it in
# this table lets the shared modifier matcher accept it; the live state is
# populated from Quartz in ``pynput_hotkey_listener`` below.
MODIFIER_KEYS.setdefault("fn", set())

# Flat set of every modifier key — Detect skips pure modifier presses and waits for a real key.
_ALL_MODIFIER_KEYS = {k for ks in MODIFIER_KEYS.values() for k in ks}
# macOS Detect may bind these alone (tap/hold). Right-hand keys only: the left
# ones are pressed constantly as part of ordinary shortcuts.
_SOLO_CAPTURE_KEYS = {k for k in (_k("alt_r"), _k("cmd_r")) if k is not None}


# macOS tap/hold gesture for a hotkey that is one bare modifier (Right Option).
# Tap = press and release inside this window with no other key: toggles
# recording. Held longer = push-to-talk: recording starts at the threshold and
# stops on release. Any other key pressed first cancels (Option+e still types
# an accent). 0.3s separates a deliberate tap from a hold without making the
# hold feel laggy.
SOLO_HOLD_SECONDS = 0.3
HOLD_START = "hold_start"
HOLD_END = "hold_end"


class SoloModifierGesture:
    """Turns presses of a single modifier key into tap / hold-start / hold-end.

    ``emit`` receives "tap", HOLD_START or HOLD_END. Thread-safe: the hold
    threshold fires from a one-shot timer thread while press/release arrive on
    the event-tap thread.
    """

    def __init__(self, emit, hold_seconds: float = SOLO_HOLD_SECONDS,
                 clock=time.monotonic, timer_factory=threading.Timer):
        self._emit = emit
        self._hold_seconds = hold_seconds
        self._clock = clock
        self._timer_factory = timer_factory
        self._lock = threading.Lock()
        self._down_at: float | None = None
        self._interrupted = False
        self._holding = False
        self._timer = None

    @property
    def is_down(self) -> bool:
        return self._down_at is not None

    def press_target(self) -> None:
        with self._lock:
            if self._down_at is not None:
                return  # repeat / duplicate press while held
            self._down_at = self._clock()
            self._interrupted = False
            self._holding = False
            timer = self._timer_factory(self._hold_seconds, self._on_hold)
            timer.daemon = True
            self._timer = timer
        timer.start()

    def press_other(self) -> None:
        with self._lock:
            if self._down_at is None or self._holding:
                return
            self._interrupted = True
            timer, self._timer = self._timer, None
        if timer is not None:
            timer.cancel()

    def _on_hold(self) -> None:
        with self._lock:
            if self._down_at is None or self._interrupted or self._holding:
                return
            self._holding = True
        self._emit(HOLD_START)

    def release_target(self) -> None:
        with self._lock:
            if self._down_at is None:
                return
            held = self._clock() - self._down_at
            holding, interrupted = self._holding, self._interrupted
            timer, self._timer = self._timer, None
            self._down_at = None
            self._holding = self._interrupted = False
        if timer is not None:
            timer.cancel()
        if holding:
            self._emit(HOLD_END)
        elif not interrupted and held < self._hold_seconds:
            self._emit("tap")

    def reset(self) -> None:
        """Forget a gesture (hotkey changed) without emitting anything."""
        with self._lock:
            timer, self._timer = self._timer, None
            self._down_at = None
            self._holding = self._interrupted = False
        if timer is not None:
            timer.cancel()


_DARWIN_KEYPAD_ENTER_VK = 76


def _darwin_normalize_key(key):
    """Keypad Enter (and Fn+Return on laptops) arrives as a bare vk 76 KeyCode;
    treat it as Enter so an Enter-based hotkey works from either key."""
    if KeyCode is not None and isinstance(key, KeyCode) and getattr(key, "vk", None) == _DARWIN_KEYPAD_ENTER_VK:
        return _k("enter") or key
    return key


def _darwin_fn_pressed() -> bool:
    """Return whether the Mac Fn/Globe modifier is physically held."""
    if sys.platform != "darwin":
        return False
    try:
        from Quartz import (
            CGEventSourceFlagsState,
            kCGEventFlagMaskSecondaryFn,
            kCGEventSourceStateCombinedSessionState,
        )

        flags = CGEventSourceFlagsState(kCGEventSourceStateCombinedSessionState)
        return bool(flags & kCGEventFlagMaskSecondaryFn)
    except Exception:
        return False


_DARWIN_EVDEV_VK = {
    # ANSI letters/numbers that pynput KeyCode.from_char does not expose with
    # a virtual keycode. Special/function keys are resolved dynamically below.
    30: 0, 31: 1, 32: 2, 33: 3, 35: 4, 34: 5,
    44: 6, 45: 7, 46: 8, 47: 9, 48: 11,
    16: 12, 17: 13, 18: 14, 19: 15, 21: 16, 20: 17,
    2: 18, 3: 19, 4: 20, 5: 21, 7: 22, 6: 23,
    10: 25, 8: 26, 9: 28, 11: 29,
    24: 31, 22: 32, 23: 34, 25: 35,
    38: 37, 36: 38, 37: 40, 49: 45, 50: 46,
}


def _darwin_virtual_keycode(evdev_code: object) -> int | None:
    """Map a shared evdev-style key code to a macOS virtual keycode."""
    try:
        code = int(evdev_code)
    except (TypeError, ValueError):
        return None
    if code in _DARWIN_EVDEV_VK:
        return _DARWIN_EVDEV_VK[code]
    key = evdev_code_to_pynput(code)
    value = getattr(key, "value", key)
    vk = getattr(value, "vk", None)
    return int(vk) if isinstance(vk, int) else None


def _darwin_key_pressed(evdev_code: object) -> bool:
    """Read physical key state so a missed key-up cannot wedge a latch."""
    if sys.platform != "darwin":
        return False
    virtual_key = _darwin_virtual_keycode(evdev_code)
    if virtual_key is None:
        return False
    try:
        from Quartz import (
            CGEventSourceKeyState,
            kCGEventSourceStateCombinedSessionState,
        )

        return bool(
            CGEventSourceKeyState(
                kCGEventSourceStateCombinedSessionState,
                virtual_key,
            )
        )
    except Exception:
        # Fail closed: only an observed physical key-up may clear the latch.
        return True


def _modifier_display_name(name: str) -> str:
    if sys.platform == "darwin":
        return {
            "alt": "Option",
            "ctrl": "Control",
            "super": "Command",
            "fn": "Fn",
        }.get(name.lower(), name.capitalize())
    return name.capitalize()


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
    capture_state: Optional[dict] = None,
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
    record_fell_back = target_key is None
    if target_key is None:
        # A shared config may carry a Linux-only mouse/keyboard code to macOS
        # or Windows. Keep that saved preference intact, but fail safely to the
        # cross-platform default for this session instead of disabling the
        # listener altogether.
        target_key = evdev_code_to_pynput(57)  # Space
        log(f"⚠️ Hotkey code {hotkey_key} is unavailable here; using Space")
        if target_key is None:
            return

    style_target_key = evdev_code_to_pynput(style_toggle_key) if style_toggle_key else None
    style_fell_back = bool(style_toggle_key) and style_target_key is None
    if style_toggle_key and style_target_key is None:
        style_target_key = evdev_code_to_pynput(28)  # Enter
        log(f"⚠️ Style hotkey code {style_toggle_key} is unavailable here; using Enter")

    def _build_mod_set(mod_list):
        return {m.lower() for m in (mod_list or []) if m.lower() in MODIFIER_KEYS}

    required_modifiers = _build_mod_set(hotkey_modifiers)
    style_required_modifiers = _build_mod_set(style_toggle_modifiers)
    if record_fell_back:
        required_modifiers = {"fn"} if sys.platform == "darwin" else {"ctrl", "alt"}
    if style_fell_back:
        style_required_modifiers = {"fn"} if sys.platform == "darwin" else {"ctrl", "alt"}

    # Track currently pressed modifiers
    pressed_modifiers = set()
    active_actions: set[str] = set()

    # Debounce
    _last_hotkey_time = 0.0
    _last_style_time = 0.0
    DEBOUNCE_SECONDS = 0.5

    # Display hotkey info
    hotkey_display = get_key_name(target_key)
    if required_modifiers:
        mod_str = "+".join(_modifier_display_name(mod) for mod in sorted(required_modifiers))
        hotkey_display = f"{mod_str}+{hotkey_display}"
    log(f"🎹 Listening for hotkey: {hotkey_display}")

    if style_target_key:
        style_display = get_key_name(style_target_key)
        if style_required_modifiers:
            mod_str = "+".join(_modifier_display_name(mod) for mod in sorted(style_required_modifiers))
            style_display = f"{mod_str}+{style_display}"
        log(f"✎ Style toggle hotkey: {style_display}")

    def _get_current_keys():
        """Read live hotkey config if available, otherwise use initial values."""
        nonlocal target_key, style_target_key, required_modifiers, style_required_modifiers
        if config_ref is not None:
            new_key = evdev_code_to_pynput(config_ref.get("hotkey_key", hotkey_key))
            new_style = evdev_code_to_pynput(config_ref.get("style_toggle_key", style_toggle_key or 0))
            new_required = _build_mod_set(config_ref.get("hotkey_modifiers", []))
            new_style_required = _build_mod_set(config_ref.get("style_toggle_modifiers", []))
            if new_key is None:
                new_key = evdev_code_to_pynput(57)
                new_required = {"fn"} if sys.platform == "darwin" else {"ctrl", "alt"}
            changed = new_key != target_key or new_required != required_modifiers
            target_key = new_key
            required_modifiers = new_required
            if changed:
                log(f"🎹 Hotkey changed to: {get_key_name(target_key)}")
            if sys.platform == "darwin":
                if style_toggle_key:
                    if new_style is None:
                        new_style = evdev_code_to_pynput(28)
                        new_style_required = {"fn"}
                    style_target_key = new_style
                    style_required_modifiers = new_style_required
            elif new_style and (
                new_style != style_target_key
                or new_style_required != style_required_modifiers
            ):
                # Linux/Windows as on main: a style key set after start is picked
                # up live, and an unset/unknown code keeps the current one.
                style_target_key = new_style
                style_required_modifiers = new_style_required

    def check_modifiers(required: set[str]) -> bool:
        if not required:
            return True
        return required <= pressed_modifiers

    solo_capture: dict = {"key": None}

    def _solo_target_active() -> bool:
        """macOS: the record hotkey is one bare modifier (e.g. Right Option)."""
        return (
            sys.platform == "darwin"
            and not required_modifiers
            and target_key in _ALL_MODIFIER_KEYS
        )

    def _emit_solo(kind: str) -> None:
        nonlocal _last_hotkey_time
        if kind == "tap":
            now = time.time()
            if now - _last_hotkey_time < DEBOUNCE_SECONDS:
                return
            _last_hotkey_time = now
            print(f"[Hotkey] {get_key_name(target_key)} tap — activating!", flush=True)
            event_queue.put((EventType.HOTKEY_PRESSED, None))
            return
        # Hold start/end are never debounced: a release must always be able to
        # stop the push-to-talk recording its own hold began.
        if kind == HOLD_START:
            _last_hotkey_time = time.time()
            print(f"[Hotkey] {get_key_name(target_key)} held — push-to-talk", flush=True)
        event_queue.put((EventType.HOTKEY_PRESSED, kind))

    solo_gesture = SoloModifierGesture(_emit_solo)


    def on_press(key):
        nonlocal pressed_modifiers, _last_hotkey_time, _last_style_time

        if sys.platform == "darwin":
            key = _darwin_normalize_key(key)
            if _darwin_fn_pressed():
                pressed_modifiers.add("fn")
            else:
                pressed_modifiers.discard("fn")

        # Track modifier state
        for mod_name, mod_keys in MODIFIER_KEYS.items():
            if key in mod_keys:
                pressed_modifiers.add(mod_name)

        # Settings "Detect": while armed, report the next non-modifier key to the app and
        # SUPPRESS the normal hotkey action (don't record/toggle while detecting). Mouse side
        # buttons grabbed by the host trigger daemon never reach here — this is keyboard keys.
        if capture_state is not None and capture_state.get("armed"):
            if key in _ALL_MODIFIER_KEYS:
                if sys.platform == "darwin" and key in _SOLO_CAPTURE_KEYS:
                    solo_capture["key"] = key  # captured on release if nothing else is pressed
                return  # wait for a real (non-modifier) key
            solo_capture["key"] = None
            code = PYNPUT_TO_EVDEV.get(key)
            if code is None:
                return  # unmapped key (e.g. a letter) — keep waiting; Detect times out otherwise
            # Snapshot gen before disarm so a concurrent re-arm cannot re-stamp
            # this press with a newer Detect session id.
            cap_gen = capture_state.get("gen")
            capture_state["armed"] = False
            event_queue.put((EventType.HOTKEY_CAPTURED,
                             {"code": code, "modifiers": sorted(pressed_modifiers),
                              "device": "keyboard",
                              "gen": cap_gen}))
            return

        # Read live config for hotkey changes
        _get_current_keys()

        now = time.time()

        # macOS has no compositor-owned global-shortcut portal. While Aura is
        # recording, the app consumes this event as "discard"; at all other
        # times it is a harmless no-op. Do not suppress the physical Escape —
        # the foreground application should still receive its normal key.
        if sys.platform == "darwin" and key == _k("esc"):
            event_queue.put((EventType.CANCEL_RECORDING, None))

        solo_mode = _solo_target_active()
        if solo_mode:
            if key == target_key:
                solo_gesture.press_target()
            else:
                solo_gesture.press_other()

        # Check for main hotkey (with debounce)
        if (
            not solo_mode
            and key == target_key
            and check_modifiers(required_modifiers)
            and "record" not in active_actions
        ):
            active_actions.add("record")
            if now - _last_hotkey_time >= DEBOUNCE_SECONDS:
                _last_hotkey_time = now
                print(f"[Hotkey] {get_key_name(target_key)} — activating!", flush=True)
                log("🎯 Hotkey activated!")
                event_queue.put((EventType.HOTKEY_PRESSED, None))

        # Check for style toggle hotkey (with debounce)
        if (
            style_target_key
            and key == style_target_key
            and check_modifiers(style_required_modifiers)
            and "style" not in active_actions
        ):
            active_actions.add("style")
            if now - _last_style_time >= DEBOUNCE_SECONDS:
                _last_style_time = now
                log("✎ Style toggle activated!")
                event_queue.put((EventType.STYLE_TOGGLE, None))
    
    def on_release(key):
        nonlocal pressed_modifiers

        if sys.platform == "darwin":
            key = _darwin_normalize_key(key)

        if (
            capture_state is not None
            and capture_state.get("armed")
            and solo_capture["key"] is not None
            and key == solo_capture["key"]
        ):
            # A bare right-side modifier pressed and released on its own
            # during Detect: bind it as a tap/hold hotkey (macOS).
            code = PYNPUT_TO_EVDEV.get(key)
            solo_capture["key"] = None
            if code is not None:
                cap_gen = capture_state.get("gen")
                capture_state["armed"] = False
                event_queue.put((EventType.HOTKEY_CAPTURED,
                                 {"code": code, "modifiers": [],
                                  "device": "keyboard", "gen": cap_gen}))

        if key == target_key:
            active_actions.discard("record")
            if _solo_target_active():
                solo_gesture.release_target()
        if key == style_target_key:
            active_actions.discard("style")
        
        # Track modifier state
        for mod_name, mod_keys in MODIFIER_KEYS.items():
            if key in mod_keys:
                pressed_modifiers.discard(mod_name)

        if sys.platform == "darwin" and not _darwin_fn_pressed():
            pressed_modifiers.discard("fn")
    
    # Start the listener
    listener_kwargs = {}
    if sys.platform == "darwin":
        try:
            from Quartz import (
                CGEventGetFlags,
                CGEventGetIntegerValueField,
                kCGEventFlagMaskSecondaryFn,
                kCGEventFlagMaskAlternate,
                kCGEventFlagMaskCommand,
                kCGEventFlagMaskControl,
                kCGEventFlagMaskShift,
                kCGEventKeyDown,
                kCGEventKeyUp,
                kCGKeyboardEventKeycode,
            )

            suppressed_keycodes: set[int] = set()

            modifier_masks = {
                "fn": kCGEventFlagMaskSecondaryFn,
                "alt": kCGEventFlagMaskAlternate,
                "super": kCGEventFlagMaskCommand,
                "ctrl": kCGEventFlagMaskControl,
                "shift": kCGEventFlagMaskShift,
            }

            def _raw_chord_matches(keycode, configured_key, configured_modifiers, flags):
                target_vk = _darwin_virtual_keycode(configured_key)
                if target_vk is None or keycode != target_vk:
                    return False
                required = {str(value).lower() for value in configured_modifiers or ()}
                return all(flags & modifier_masks[name] for name in required if name in modifier_masks)

            def _darwin_intercept(event_type, event):
                """Track Fn and stop Aura's Fn+Space from typing a space."""
                flags = CGEventGetFlags(event)
                fn_active = bool(flags & kCGEventFlagMaskSecondaryFn)
                if fn_active:
                    pressed_modifiers.add("fn")
                else:
                    pressed_modifiers.discard("fn")

                keycode = CGEventGetIntegerValueField(event, kCGKeyboardEventKeycode)
                current_key = (
                    config_ref.get("hotkey_key", hotkey_key)
                    if config_ref is not None else hotkey_key
                )
                current_modifiers = {
                    str(value).lower()
                    for value in (
                        config_ref.get("hotkey_modifiers", hotkey_modifiers)
                        if config_ref is not None else hotkey_modifiers
                    )
                }
                current_style_key = (
                    config_ref.get("style_toggle_key", style_toggle_key)
                    if config_ref is not None else style_toggle_key
                )
                current_style_modifiers = (
                    config_ref.get("style_toggle_modifiers", style_toggle_modifiers)
                    if config_ref is not None else style_toggle_modifiers
                ) or []
                if _darwin_virtual_keycode(current_key) is None:
                    current_key, current_modifiers = 57, {"fn"}
                if current_style_key and _darwin_virtual_keycode(current_style_key) is None:
                    current_style_key, current_style_modifiers = 28, {"fn"}

                matches = _raw_chord_matches(
                    keycode, current_key, current_modifiers, flags
                ) or _raw_chord_matches(
                    keycode, current_style_key, current_style_modifiers, flags
                )
                if event_type == kCGEventKeyDown and matches:
                    suppressed_keycodes.add(int(keycode))
                    return None
                if event_type == kCGEventKeyUp and int(keycode) in suppressed_keycodes:
                    suppressed_keycodes.discard(int(keycode))
                    return None
                return event

            listener_kwargs["darwin_intercept"] = _darwin_intercept
        except Exception as exc:
            log(f"⚠️ Fn hotkey support unavailable: {exc}")

    listener = keyboard.Listener(
        on_press=on_press,
        on_release=on_release,
        **listener_kwargs,
    )
    listener.start()

    print(f"[Hotkey] pynput listener started, waiting for: {get_key_name(target_key)}", flush=True)
    log("🎧 Cross-platform hotkey listener active (pynput)")
    
    try:
        while not stop_event.is_set():
            if sys.platform == "darwin":
                # Aqua can occasionally omit a key-up across sleep/wake or an
                # event-tap restart. Reconcile our repeat-suppression latches
                # against Quartz's physical state so one lost release cannot
                # disable only that chord for the rest of the process.
                current_record_code = (
                    config_ref.get("hotkey_key", hotkey_key)
                    if config_ref is not None else hotkey_key
                )
                current_style_code = (
                    config_ref.get("style_toggle_key", style_toggle_key or 0)
                    if config_ref is not None else (style_toggle_key or 0)
                )
                if (
                    "record" in active_actions
                    and not _darwin_key_pressed(current_record_code)
                ):
                    active_actions.discard("record")
                if solo_gesture.is_down and (
                    not _solo_target_active()
                    or not _darwin_key_pressed(current_record_code)
                ):
                    # Lost key-up, or the hotkey changed mid-gesture: end it so
                    # a push-to-talk recording cannot run on forever.
                    solo_gesture.release_target()
                if (
                    "style" in active_actions
                    and not _darwin_key_pressed(current_style_code)
                ):
                    active_actions.discard("style")
                if not getattr(listener, "running", True):
                    raise RuntimeError("macOS event tap stopped")
            time.sleep(0.1)
    finally:
        # DELIBERATELY no liveness monitoring here. Watching the inner listener
        # and reporting its death upward was implemented and then reverted:
        # pynput's Xorg stop() itself performs an unbounded wait() when X init
        # failed before its context existed, so any teardown path that touches
        # a half-started listener can hang this thread forever. Earlier
        # attempts also produced a startup race that could leave two live
        # listeners double-firing every hotkey.
        #
        # The cost of leaving it out is small and bounded: Detect can arm when
        # the listener is already gone and then time out. That is strictly
        # better than a hang or duplicate hotkey delivery.
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
