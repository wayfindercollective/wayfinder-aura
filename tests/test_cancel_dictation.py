"""Cancel dictation: how the app tells the user to discard a recording.

Linux portal sessions cancel through the cancel-dictation shortcut (default
Shift+Esc: a portal shortcut grabs its key in every app, so never a bare
Escape); every other listener takes a bare Escape while recording.
"""
from __future__ import annotations

from types import SimpleNamespace

import wayfinder_main as wm

AppState = wm.AppState  # the enum the app compares against


def _app(backend=None, triggers=None, config=None):
    app = SimpleNamespace(
        config=config or {"hotkey_key": 61, "hotkey_modifiers": []},
        get_hotkey_display=lambda: "F3",
        _record_hotkey_is_tap_hold=lambda: False,
        _hotkey_backend=backend,
    )
    if triggers is not None:
        app._portal_triggers = triggers
    app._hero_hotkey_hint_text = lambda: wm.WayfinderApp._hero_hotkey_hint_text(app)
    app._cancel_hotkey_display = lambda: wm.WayfinderApp._cancel_hotkey_display(app)
    return app


class _Thread:
    def __init__(self, alive):
        self._alive = alive

    def is_alive(self):
        return self._alive


def test_non_portal_listeners_teach_shift_esc_everywhere():
    """Escape cancels with or without Shift on these listeners, and Shift+Esc is
    the portal default, so one key is taught on every platform."""
    for backend in ("pynput", None):
        assert wm.WayfinderApp._cancel_hotkey_display(_app(backend)) == "Shift+Esc"
    evdev = _app("evdev")
    evdev._hotkey_thread = _Thread(alive=True)
    assert wm.WayfinderApp._cancel_hotkey_display(evdev) == "Shift+Esc"


def test_no_escape_promise_without_a_listener():
    # KDE owns the record key (evdev binding skipped) or no backend at all:
    # nothing outside the window hears Esc, so the hint only names the stop key.
    kde = _app("evdev")
    kde._hotkey_thread = None
    assert wm.WayfinderApp._cancel_hotkey_display(kde) is None
    stopped = _app("evdev")
    stopped._hotkey_thread = _Thread(alive=False)
    assert wm.WayfinderApp._cancel_hotkey_display(stopped) is None
    assert wm.WayfinderApp._cancel_hotkey_display(_app("unavailable")) is None
    assert wm.WayfinderApp._hero_state_hint_text(kde, AppState.RECORDING) == "press F3 to stop"


def test_portal_shows_the_desktop_bound_trigger():
    app = _app("portal", {"record-toggle": "F3", "cancel-dictation": "Ctrl+Esc"})
    assert wm.WayfinderApp._cancel_hotkey_display(app) == "Ctrl+Esc"


def test_portal_before_bind_shows_the_requested_default():
    app = _app("portal", None, {"cancel_hotkey_key": 1, "cancel_hotkey_modifiers": ["shift"]})
    assert wm.WayfinderApp._cancel_hotkey_display(app) == "Shift+Esc"


def test_portal_with_cancel_left_unbound_offers_no_cancel_key():
    app = _app("portal", {"record-toggle": "F3"})
    assert wm.WayfinderApp._cancel_hotkey_display(app) is None
    hint = wm.WayfinderApp._hero_state_hint_text(app, AppState.RECORDING)
    assert hint == "press F3 to stop"


def test_recording_hint_names_stop_and_cancel_idle_hint_unchanged():
    app = _app("portal", {"cancel-dictation": "Shift+Esc"})
    assert wm.WayfinderApp._hero_state_hint_text(app, AppState.RECORDING) == (
        "press F3 to stop  ·  Shift+Esc cancels, nothing is typed")
    assert wm.WayfinderApp._hero_state_hint_text(app, AppState.IDLE) == (
        "press to start/stop  ·  words appear at your cursor")


def test_idle_ribbon_goes_to_background_pace_while_another_app_has_focus(monkeypatch):
    monkeypatch.setattr(wm, "_IS_LINUX", True)
    focused = SimpleNamespace(focus_displayof=lambda: object())
    elsewhere = SimpleNamespace(focus_displayof=lambda: None)
    assert wm.WayfinderApp._hero_idle_backgrounded(focused) is False
    assert wm.WayfinderApp._hero_idle_backgrounded(elsewhere) is True
    monkeypatch.setattr(wm, "_IS_LINUX", False)
    assert wm.WayfinderApp._hero_idle_backgrounded(elsewhere) is False  # macOS/Windows unchanged
    # Background drift: slower and on a lower frame rate, never frozen.
    assert 0 < wm._HERO_BACKGROUND_SPEED < 1
    assert wm._HERO_BACKGROUND_INTERVAL_MS > wm._hero_idle_interval_ms("linux")


def test_benchmark_bar_is_placed_by_wall_clock_at_30_fps(monkeypatch):
    drawn, scheduled = [], []
    bar = SimpleNamespace(_indeterminate_value=0.0, _draw=lambda: drawn.append(bar._indeterminate_value),
                          step=lambda: None, pack_forget=lambda: None)
    clock = {"t": 100.0}
    monkeypatch.setattr(wm.time, "monotonic", lambda: clock["t"])
    app = SimpleNamespace(_benchmark_running=True, benchmark_progress=bar, _benchmark_bar_t0=100.0,
                          after=lambda ms, fn: scheduled.append(ms), _tick_benchmark_bar=None)
    clock["t"] = 101.5  # a late frame: 1.5 s in
    wm.WayfinderApp._tick_benchmark_bar(app)
    assert drawn == [15.0]          # CTk's .step() pace (10 units/s), from elapsed time
    assert scheduled == [33]        # ~30 fps while a benchmark runs
    app._benchmark_running = False
    wm.WayfinderApp._tick_benchmark_bar(app)
    assert scheduled == [33]        # stops (and hides) when the run ends


# ---------------------------------------------------------------- overlay hint
def test_overlay_controller_passes_the_hint_on_every_start():
    sent = []
    ctl = wm.OverlayController(cancel_hint="Shift+Esc")
    ctl._send_command = lambda cmd, **k: sent.append(cmd) or True
    ctl.set_cancel_hint("Shift+Esc")  # unchanged: nothing sent
    ctl.set_cancel_hint("Ctrl+Esc")
    assert sent == [{"cmd": "cancel_hint", "value": "Ctrl+Esc"}]
    assert ctl._cancel_hint == "Ctrl+Esc"


class _Overlay:
    def __init__(self):
        self.calls = []

    def set_cancel_hint(self, hint):
        self.calls.append(("cancel_hint", hint))

    def show(self, state):
        self.calls.append(("show", state))
        return True


def test_overlay_hint_follows_the_listener_at_each_recording():
    # The overlay is started with "Shift+Esc"; KDE then takes the record key
    # and evdev stops. The next recording's pill must not still promise Esc.
    app = _app("evdev")
    app._hotkey_thread = _Thread(alive=True)
    app.overlay_controller = _Overlay()
    app._has_visual_pyqt_overlay = lambda: True
    wm.WayfinderApp._set_status_indicator(app, "listening")
    app._hotkey_thread = None  # KDE owns the key now
    wm.WayfinderApp._set_status_indicator(app, "listening")
    assert app.overlay_controller.calls == [
        ("cancel_hint", "Shift+Esc"), ("show", "listening"),
        ("cancel_hint", ""), ("show", "listening"),
    ]
