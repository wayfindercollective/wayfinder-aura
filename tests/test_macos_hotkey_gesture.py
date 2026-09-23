"""macOS Right Option tap/hold hotkey: gesture timing, listener wiring, app dispatch."""
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wayfinder.hotkeys import pynput_listener as pl  # noqa: E402
from wayfinder.hotkeys.pynput_listener import (  # noqa: E402
    HOLD_END, HOLD_START, SoloModifierGesture,
)


class _Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


class _Timer:
    """Manual one-shot timer: fire() runs the callback like the real thread would."""
    made = []

    def __init__(self, delay, fn):
        self.delay, self.fn, self.cancelled, self.started = delay, fn, False, False
        _Timer.made.append(self)

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True

    def fire(self):
        if not self.cancelled:
            self.fn()


@pytest.fixture
def gesture():
    _Timer.made.clear()
    clock, events = _Clock(), []
    g = SoloModifierGesture(events.append, hold_seconds=0.3, clock=clock, timer_factory=_Timer)
    return g, clock, events


class TestGesture:
    def test_quick_tap_toggles(self, gesture):
        g, clock, events = gesture
        g.press_target()
        clock.t += 0.12
        g.release_target()
        assert events == ["tap"]
        assert _Timer.made[0].cancelled

    def test_hold_starts_at_threshold_and_stops_on_release(self, gesture):
        g, clock, events = gesture
        g.press_target()
        clock.t += 0.3
        _Timer.made[0].fire()
        assert events == [HOLD_START]
        clock.t += 4.0
        g.release_target()
        assert events == [HOLD_START, HOLD_END]

    def test_option_letter_does_nothing(self, gesture):
        """Option+e types an accent: another key before the threshold cancels."""
        g, clock, events = gesture
        g.press_target()
        clock.t += 0.05
        g.press_other()
        _Timer.made[0].fire()  # cancelled timer: no hold
        clock.t += 0.05
        g.release_target()
        assert events == []

    def test_slow_release_without_hold_is_not_a_tap(self, gesture):
        g, clock, events = gesture
        g.press_target()
        clock.t += 0.5  # threshold passed but the timer never ran (interrupted)
        g.press_other()
        g.release_target()
        assert events == []

    def test_keys_during_a_hold_do_not_cancel_it(self, gesture):
        g, clock, events = gesture
        g.press_target()
        _Timer.made[0].fire()
        g.press_other()
        g.release_target()
        assert events == [HOLD_START, HOLD_END]

    def test_repeat_press_while_held_is_ignored(self, gesture):
        g, clock, events = gesture
        g.press_target()
        g.press_target()
        assert len(_Timer.made) == 1

    def test_timer_racing_a_release_cannot_start_a_hold(self, gesture):
        g, clock, events = gesture
        g.press_target()
        clock.t += 0.1
        g.release_target()
        _Timer.made[0].fn()  # the thread was already running when cancel() came
        assert events == ["tap"]

    def test_reset_emits_nothing(self, gesture):
        g, clock, events = gesture
        g.press_target()
        g.reset()
        g.release_target()
        assert events == [] and not g.is_down


class TestPresetsAndLabels:
    def test_macos_offers_tap_hold_keys_first(self):
        import wayfinder_main as wm
        options = wm.hotkey_key_options("darwin", available_pynput_codes={57, 28, 67, 100, 126})
        assert list(options)[:2] == ["Right Option", "Right Command"]
        assert options["Right Option"] == 100

    def test_linux_presets_are_unchanged(self):
        import wayfinder_main as wm
        options = wm.hotkey_key_options("linux")
        assert "Right Option" not in options and "Right Command" not in options

    def test_tap_hold_needs_macos_and_no_modifiers(self):
        import wayfinder_main as wm
        assert wm.is_tap_hold_hotkey(100, [], "darwin")
        assert not wm.is_tap_hold_hotkey(100, ["fn"], "darwin")
        assert not wm.is_tap_hold_hotkey(100, [], "linux")
        assert not wm.is_tap_hold_hotkey(57, [], "darwin")

    def test_macos_label(self):
        import wayfinder_main as wm
        with patch.object(wm.sys, "platform", "darwin"):
            assert wm._keycode_display(100) == "Right Option"
            assert wm._keycode_display(126) == "Right Command"


class _App:
    """Just enough of WayfinderApp for the HOTKEY_PRESSED branch."""

    def __init__(self):
        import wayfinder_main as wm
        self.wm = wm
        self.app_state = wm.AppState.IDLE
        self.calls = []

    def on_hotkey(self):
        if self.app_state == self.wm.AppState.IDLE:
            self.start_recording()
        elif self.app_state == self.wm.AppState.RECORDING:
            self.stop_recording_and_process()

    def start_recording(self):
        self.calls.append("start")
        self.app_state = self.wm.AppState.RECORDING

    def stop_recording_and_process(self):
        self.calls.append("stop")
        self.app_state = self.wm.AppState.PROCESSING

    def dispatch(self, data):
        self.wm.WayfinderApp.handle_event(self, self.wm.EventType.HOTKEY_PRESSED, data)


class TestAppDispatch:
    @pytest.fixture(autouse=True)
    def _no_capture(self):
        import wayfinder_main as wm
        with patch.dict(wm._HOTKEY_CAPTURE, {"armed": False, "suppress_until": 0}):
            yield

    def test_push_to_talk(self):
        app = _App()
        app.dispatch(HOLD_START)
        app.dispatch(HOLD_END)
        assert app.calls == ["start", "stop"]

    def test_hold_that_stops_a_tapped_recording_does_not_restart_on_release(self):
        app = _App()
        app.dispatch(None)           # tap: start
        app.dispatch(HOLD_START)     # hold: stops it
        app.app_state = app.wm.AppState.IDLE
        app.dispatch(HOLD_END)       # release: must NOT start again
        assert app.calls == ["start", "stop"]

    def test_hold_end_after_escape_cancel_is_a_no_op(self):
        app = _App()
        app.dispatch(HOLD_START)
        app.app_state = app.wm.AppState.IDLE  # Escape discarded it
        app.dispatch(HOLD_END)
        assert app.calls == ["start"]

    def test_plain_press_is_still_a_toggle(self):
        app = _App()
        app.dispatch(None)
        app.dispatch(None)
        assert app.calls == ["start", "stop"]


@pytest.mark.skipif(not pl.PYNPUT_AVAILABLE, reason="pynput not installed")
class TestListenerWiring:
    def test_right_option_maps_to_a_capturable_modifier(self):
        assert pl.evdev_code_to_pynput(100) in pl._SOLO_CAPTURE_KEYS
        assert pl.PYNPUT_TO_EVDEV[pl.evdev_code_to_pynput(100)] == 100


@pytest.mark.skipif(not pl.PYNPUT_AVAILABLE, reason="pynput not installed")
class TestListenerEndToEnd:
    """Drive the real pynput_hotkey_listener callbacks with a fake event tap."""

    @pytest.fixture
    def listener(self, monkeypatch):
        from queue import Queue
        from threading import Event
        from types import ModuleType

        captured = {}

        class FakeListener:
            def __init__(self, **kwargs):
                captured.update(kwargs)

            def start(self):
                pass

            def stop(self):
                pass

        quartz = ModuleType("Quartz")
        for name, value in dict(
            kCGEventFlagMaskSecondaryFn=0x800000, kCGEventFlagMaskAlternate=0x080000,
            kCGEventFlagMaskCommand=0x100000, kCGEventFlagMaskControl=0x040000,
            kCGEventFlagMaskShift=0x020000, kCGEventKeyDown=10, kCGEventKeyUp=11,
            kCGKeyboardEventKeycode=9, kCGEventSourceStateCombinedSessionState=0,
        ).items():
            setattr(quartz, name, value)
        quartz.CGEventGetFlags = lambda event: event["flags"]
        quartz.CGEventGetIntegerValueField = lambda event, _field: event["keycode"]
        quartz.CGEventSourceFlagsState = lambda _state: 0
        quartz.CGEventSourceKeyState = lambda _state, _vk: False
        monkeypatch.setitem(pl.sys.modules, "Quartz", quartz)
        monkeypatch.setattr(pl.sys, "platform", "darwin")
        monkeypatch.setattr(pl.keyboard, "Listener", FakeListener)

        events, stop = Queue(), Event()
        stop.set()
        capture = {"armed": False}
        pl.pynput_hotkey_listener(events, hotkey_key=100, hotkey_modifiers=[],
                                  stop_event=stop, capture_state=capture)

        def drain():
            out = []
            while not events.empty():
                out.append(events.get_nowait())
            return out

        return captured["on_press"], captured["on_release"], drain, capture

    def test_tap_emits_one_toggle(self, listener):
        press, release, drain, _ = listener
        press(pl.Key.alt_r)
        release(pl.Key.alt_r)
        assert drain() == [(pl.EventType.HOTKEY_PRESSED, None)]

    def test_hold_is_push_to_talk(self, listener):
        import time
        press, release, drain, _ = listener
        press(pl.Key.alt_r)
        time.sleep(pl.SOLO_HOLD_SECONDS + 0.15)
        release(pl.Key.alt_r)
        assert drain() == [(pl.EventType.HOTKEY_PRESSED, HOLD_START),
                           (pl.EventType.HOTKEY_PRESSED, HOLD_END)]

    def test_option_accent_does_not_record(self, listener):
        press, release, drain, _ = listener
        e = pl.KeyCode.from_char("e")
        press(pl.Key.alt_r)
        press(e)
        release(e)
        release(pl.Key.alt_r)
        assert drain() == []

    def test_left_option_is_not_the_hotkey(self, listener):
        press, release, drain, _ = listener
        press(pl.Key.alt)
        release(pl.Key.alt)
        assert drain() == []

    def test_detect_can_bind_right_option_alone(self, listener):
        press, release, drain, capture = listener
        capture.update(armed=True, gen=7)
        press(pl.Key.cmd_r)
        release(pl.Key.cmd_r)
        assert drain() == [(pl.EventType.HOTKEY_CAPTURED,
                            {"code": 126, "modifiers": [], "device": "keyboard", "gen": 7})]
        assert capture["armed"] is False

    def test_detect_still_binds_a_chord(self, listener):
        press, release, drain, capture = listener
        capture.update(armed=True, gen=8)
        press(pl.Key.alt_r)
        press(pl.Key.space)
        release(pl.Key.space)
        release(pl.Key.alt_r)
        (kind, payload), = drain()
        assert kind == pl.EventType.HOTKEY_CAPTURED
        assert payload["code"] == 57 and payload["modifiers"] == ["alt"]


@pytest.mark.skipif(not pl.PYNPUT_AVAILABLE, reason="pynput not installed")
def test_keypad_enter_counts_as_enter():
    assert pl._darwin_normalize_key(pl.KeyCode.from_vk(76)) == pl.Key.enter
    assert pl._darwin_normalize_key(pl.Key.space) == pl.Key.space
