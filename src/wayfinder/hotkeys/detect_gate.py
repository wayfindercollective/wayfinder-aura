"""Whether Settings → Detect can capture a key, and why not when it cannot.

Kept as a pure function so the gate is testable without Tk or /dev/input.

A live pynput listener implements the HOTKEY_CAPTURED path on its own (see
``pynput_listener.on_press``), so a Flatpak with no ``/dev/input`` can still
run Detect.

The distinction that matters is *usable* versus *importable*: the Flatpak
bundles evdev but grants no ``/dev/input``, so anything keyed on import
success alone silently claims a capture source that cannot see a single key.
"""

from typing import Callable, Optional

FLATPAK_NO_INPUT_MESSAGE = (
    "🎯 Detect needs host input access — in Flatpak use System Settings → Shortcuts"
)
NO_LISTENER_MESSAGE = "🎯 Detect unavailable (no evdev / pynput listener)"


def evdev_capture_usable(list_devices: Optional[Callable[[], list]]) -> bool:
    """Whether evdev can actually see input devices, not merely import.

    The Flatpak bundles evdev — ``HAS_EVDEV`` is True there — but grants only
    ``devices=dri``, so ``/dev/input`` is empty and ``list_devices()`` returns
    ``[]`` (the same reasoning as the hotkey supervisor in wayfinder_main).
    Treating import success as a capture source lets Detect arm when nothing
    can see keys, so it can only time out.

    ``list_devices`` is None when evdev is not installed at all.
    """
    if list_devices is None:
        return False
    try:
        return bool(list_devices())
    except Exception:
        return False


def detect_availability(
    *, is_flatpak: bool, evdev_usable: bool, pynput_started: bool
) -> tuple[bool, str | None]:
    """Return ``(can_detect, message)``; ``message`` is None when it can.

    ``evdev_usable`` must mean evdev can really read devices (see
    ``evdev_capture_usable``), NOT that the module imported — otherwise this
    short-circuits True in every Flatpak and no listener check ever runs.
    """
    if evdev_usable or pynput_started:
        return True, None
    return False, FLATPAK_NO_INPUT_MESSAGE if is_flatpak else NO_LISTENER_MESSAGE
