"""Focus-based Detect capture for environments with no global listener.

The Wayland Flatpak can see no keys globally — /dev/input is not granted
(evdev) and pynput needs X11 — which made Settings → Detect a dead button in
exactly the packaging Flathub ships. But Detect never needed a GLOBAL
listener: the user just clicked the button, so OUR window has keyboard focus
and the next press arrives to Tk itself. This module is the pure part of that
fallback — keysym mapping and payload construction — kept free of Tk so it is
testable headless.

The keycode space is evdev, like every other capture source (the config, the
display labels, and the portal trigger encoder all speak it). The map is the
inverse of the portal encoder's, imported rather than copied, so a key the
portal can express is exactly a key Detect-via-focus can capture.
"""

from typing import Optional

from .dbus import _KEYSYM_BY_CODE

# Tk keysym -> evdev keycode. Inverse of the portal's code->keysym map so the
# two stay in lockstep by construction.
KEYSYM_TO_KEYCODE = {keysym: code for code, keysym in _KEYSYM_BY_CODE.items()}

# Keysyms that are modifiers, not hotkeys: pressing one keeps Detect armed,
# the same rule the evdev and pynput paths apply.
TK_MODIFIER_KEYSYMS = frozenset({
    "Shift_L", "Shift_R", "Control_L", "Control_R", "Alt_L", "Alt_R",
    "Super_L", "Super_R", "Meta_L", "Meta_R", "Hyper_L", "Hyper_R",
    "ISO_Level3_Shift", "Caps_Lock", "Num_Lock",
})

# X11 event.state bits (XWayland delivers the same encoding).
_STATE_SHIFT = 0x1
_STATE_CTRL = 0x4
_STATE_ALT = 0x8      # Mod1
_STATE_SUPER = 0x40   # Mod4


def captured_payload(keysym: str, state: int,
                     device: str = "keyboard (window focus)") -> Optional[dict]:
    """Build a HOTKEY_CAPTURED payload from a focused Tk key event, or None.

    None means "keep waiting": a bare modifier, or a key with no evdev
    encoding (letters, media keys) — the same keep-waiting rule the pynput
    path applies, with the 8s safety timeout as the backstop. The caller adds
    the Detect generation stamp.
    """
    if not keysym or keysym in TK_MODIFIER_KEYSYMS:
        return None
    code = KEYSYM_TO_KEYCODE.get(keysym)
    if code is None:
        return None
    held = []
    if state & _STATE_SHIFT:
        held.append("shift")
    if state & _STATE_CTRL:
        held.append("ctrl")
    if state & _STATE_ALT:
        held.append("alt")
    if state & _STATE_SUPER:
        held.append("super")
    return {"code": code, "modifiers": held, "device": device}
