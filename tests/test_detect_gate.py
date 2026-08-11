"""Settings → Detect availability gate.

Detect was unreachable in every Flatpak install: the guard bailed on
`IS_FLATPAK and not HAS_EVDEV` before reaching the check that treats a live
pynput listener as a valid capture source. pynput implements the
HOTKEY_CAPTURED path itself, so no evdev is needed when it is running.
"""

from wayfinder.hotkeys.detect_gate import (
    FLATPAK_NO_INPUT_MESSAGE,
    NO_LISTENER_MESSAGE,
    detect_availability,
)


def test_flatpak_without_evdev_can_still_detect_via_pynput():
    """The regression: this combination is every Flatpak install on X11."""
    can_detect, message = detect_availability(
        is_flatpak=True, has_evdev=False, pynput_started=True
    )

    assert can_detect is True
    assert message is None


def test_evdev_alone_is_enough():
    can_detect, message = detect_availability(
        is_flatpak=False, has_evdev=True, pynput_started=False
    )

    assert can_detect is True
    assert message is None


def test_flatpak_with_no_listener_at_all_explains_the_flatpak_limitation():
    can_detect, message = detect_availability(
        is_flatpak=True, has_evdev=False, pynput_started=False
    )

    assert can_detect is False
    assert message == FLATPAK_NO_INPUT_MESSAGE


def test_native_install_with_no_listener_gets_the_generic_message():
    can_detect, message = detect_availability(
        is_flatpak=False, has_evdev=False, pynput_started=False
    )

    assert can_detect is False
    assert message == NO_LISTENER_MESSAGE
