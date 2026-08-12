"""Whether Settings → Detect can capture a key, and why not when it cannot.

Kept as a pure function so the gate is testable without Tk or /dev/input.

A live pynput listener implements the HOTKEY_CAPTURED path on its own (see
``pynput_listener.on_press``), so a Flatpak with no ``/dev/input`` can still
run Detect. Gating on evdev alone made Detect dead in every Flatpak install.
"""

FLATPAK_NO_INPUT_MESSAGE = (
    "🎯 Detect needs host input access — in Flatpak use System Settings → Shortcuts"
)
NO_LISTENER_MESSAGE = "🎯 Detect unavailable (no evdev / pynput listener)"


def listener_is_live(started_flag: bool, thread) -> bool:
    """Whether the pynput listener is actually running.

    Prefer the thread over the flag: ``_start_pynput_listener`` sets its flag
    before the thread starts and the listener can return normally without
    clearing it (an unmappable saved hotkey code returns early), so the flag
    alone can claim a listener that is gone. ``thread`` is None on callers that
    have no handle, where the flag is the only signal available.
    """
    if thread is not None:
        return bool(thread.is_alive())
    return bool(started_flag)


def detect_availability(
    *, is_flatpak: bool, has_evdev: bool, pynput_started: bool
) -> tuple[bool, str | None]:
    """Return ``(can_detect, message)``; ``message`` is None when it can."""
    if has_evdev or pynput_started:
        return True, None
    return False, FLATPAK_NO_INPUT_MESSAGE if is_flatpak else NO_LISTENER_MESSAGE
