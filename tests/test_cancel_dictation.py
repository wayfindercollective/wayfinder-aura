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


def test_non_portal_listeners_cancel_with_bare_escape():
    for backend in ("pynput", "evdev", None):
        assert wm.WayfinderApp._cancel_hotkey_display(_app(backend)) == "Esc"


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


def test_idle_ribbon_pauses_while_another_app_has_focus(monkeypatch):
    monkeypatch.setattr(wm, "_IS_LINUX", True)
    focused = SimpleNamespace(focus_displayof=lambda: object())
    elsewhere = SimpleNamespace(focus_displayof=lambda: None)
    assert wm.WayfinderApp._hero_idle_paused(focused) is False
    assert wm.WayfinderApp._hero_idle_paused(elsewhere) is True
    monkeypatch.setattr(wm, "_IS_LINUX", False)
    assert wm.WayfinderApp._hero_idle_paused(elsewhere) is False  # macOS/Windows unchanged
