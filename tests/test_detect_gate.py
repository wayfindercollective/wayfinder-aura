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
    evdev_capture_usable,
    listener_is_live,
)


class _FakeThread:
    def __init__(self, alive):
        self._alive = alive

    def is_alive(self):
        return self._alive


def test_a_dead_thread_beats_a_stale_started_flag():
    """_start_pynput_listener sets its flag before the thread runs, and the
    listener can return normally (an unmappable saved hotkey code does exactly
    that) without clearing it. Trusting the flag arms Detect with nothing
    listening, so capture can only time out."""
    assert listener_is_live(True, _FakeThread(alive=False)) is False


def test_a_live_thread_is_live():
    assert listener_is_live(True, _FakeThread(alive=True)) is True


def test_falls_back_to_the_flag_when_no_thread_handle_exists():
    assert listener_is_live(True, None) is True
    assert listener_is_live(False, None) is False


def test_flatpak_without_evdev_can_still_detect_via_pynput():
    """The regression: this combination is every Flatpak install on X11."""
    can_detect, message = detect_availability(
        is_flatpak=True, evdev_usable=False, pynput_started=True
    )

    assert can_detect is True
    assert message is None


def test_evdev_alone_is_enough():
    can_detect, message = detect_availability(
        is_flatpak=False, evdev_usable=True, pynput_started=False
    )

    assert can_detect is True
    assert message is None


def test_flatpak_with_no_listener_at_all_explains_the_flatpak_limitation():
    can_detect, message = detect_availability(
        is_flatpak=True, evdev_usable=False, pynput_started=False
    )

    assert can_detect is False
    assert message == FLATPAK_NO_INPUT_MESSAGE


def test_native_install_with_no_listener_gets_the_generic_message():
    can_detect, message = detect_availability(
        is_flatpak=False, evdev_usable=False, pynput_started=False
    )

    assert can_detect is False
    assert message == NO_LISTENER_MESSAGE


def test_importable_but_deviceless_evdev_is_not_usable():
    """The Flatpak bundles evdev (HAS_EVDEV is True) but has no /dev/input, so
    list_devices() returns []. Treating import success as a capture source let
    Detect arm with nothing able to see keys."""
    assert evdev_capture_usable(lambda: []) is False


def test_evdev_with_devices_is_usable():
    assert evdev_capture_usable(lambda: ["/dev/input/event0"]) is True


def test_absent_evdev_is_not_usable():
    assert evdev_capture_usable(None) is False


def test_a_raising_probe_is_not_usable():
    def boom():
        raise OSError("permission denied")

    assert evdev_capture_usable(boom) is False


def test_deviceless_flatpak_with_a_dead_listener_refuses_detect():
    """The real shipped combination: evdev imports, no devices, listener dead."""
    can_detect, message = detect_availability(
        is_flatpak=True,
        evdev_usable=evdev_capture_usable(lambda: []),
        pynput_started=listener_is_live(True, _FakeThread(alive=False)),
    )

    assert can_detect is False
    assert message == FLATPAK_NO_INPUT_MESSAGE
