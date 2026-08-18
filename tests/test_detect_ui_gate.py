"""Settings → Detect must LOOK like what it is in this environment.

Regression: in the shipped Flatpak the portal backend takes the listener slot,
so there is no evdev (no /dev/input in the sandbox) and no pynput listener.
Detect could not capture anything — yet the button rendered fully enabled and
its refusal went only to the activity log. The Deck report was three clicks and
three silent no-ops.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import wayfinder_main
from wayfinder.hotkeys.detect_gate import FLATPAK_NO_INPUT_MESSAGE


def _app(monkeypatch, *, is_flatpak=True, has_evdev=False, devices=(),
         pynput=False, portal=False):
    """Stand-in for WayfinderApp's Detect surface without constructing Tk."""
    monkeypatch.setattr(wayfinder_main, "IS_FLATPAK", is_flatpak)
    monkeypatch.setattr(wayfinder_main, "HAS_EVDEV", has_evdev)
    if has_evdev:
        monkeypatch.setattr(
            wayfinder_main, "evdev",
            SimpleNamespace(list_devices=lambda: list(devices)),
        )

    logs = []
    app = SimpleNamespace(
        _detect_btn_record=MagicMock(),
        _detect_btn_style=MagicMock(),
        _detect_hint=MagicMock(),
        _detect_set_shortcut_btn=MagicMock(),
        _pynput_listener_started=pynput,
        # Backend CHOICE (sticky) vs thread liveness — display and the
        # "Set shortcut…" affordance key off the former.
        _portal_backend_active=portal,
        _portal_listener_started=portal,
        _portal_rebind_pending=False,
        log=logs.append,
    )
    for name in ("_detect_gate", "_sync_detect_availability"):
        setattr(app, name,
                getattr(wayfinder_main.WayfinderApp, name).__get__(app, SimpleNamespace))
    return app, logs


def _state(mock):
    """The `state=` kwarg of the last configure() call."""
    assert mock.configure.called
    return mock.configure.call_args.kwargs.get("state")


def _text(mock):
    return mock.configure.call_args.kwargs.get("text")


def test_flatpak_portal_disables_detect_and_explains_why(monkeypatch):
    app, _logs = _app(monkeypatch, portal=True)
    app._sync_detect_availability()

    assert _state(app._detect_btn_record) == "disabled"
    assert _state(app._detect_btn_style) == "disabled"
    assert _text(app._detect_hint) == FLATPAK_NO_INPUT_MESSAGE
    assert "System Settings" in _text(app._detect_hint)


def test_portal_offers_set_shortcut_instead(monkeypatch):
    """The dead affordance is replaced by the one that works."""
    app, _logs = _app(monkeypatch, portal=True)
    app._sync_detect_availability()

    app._detect_set_shortcut_btn.pack.assert_called_once()
    app._detect_set_shortcut_btn.pack_forget.assert_not_called()


def test_a_working_capture_source_keeps_detect_enabled(monkeypatch):
    """Never weaken the gate: a live pynput listener CAN capture, Flatpak or not."""
    app, _logs = _app(monkeypatch, pynput=True, portal=True)
    app._sync_detect_availability()

    assert _state(app._detect_btn_record) == "normal"
    assert _text(app._detect_hint) == ""
    app._detect_set_shortcut_btn.pack_forget.assert_called_once()


def test_native_linux_with_real_devices_keeps_detect_enabled(monkeypatch):
    app, _logs = _app(monkeypatch, is_flatpak=False, has_evdev=True,
                      devices=("/dev/input/event0",))
    app._sync_detect_availability()

    assert _state(app._detect_btn_record) == "normal"
    assert _text(app._detect_hint) == ""


def test_importable_evdev_with_no_devices_is_not_a_capture_source(monkeypatch):
    """The Flatpak bundles evdev but grants no /dev/input; import success alone
    would arm Detect against nothing."""
    app, _logs = _app(monkeypatch, has_evdev=True, devices=(), portal=True)
    app._sync_detect_availability()

    assert _state(app._detect_btn_record) == "disabled"


def test_set_shortcut_hidden_when_the_portal_is_not_the_backend(monkeypatch):
    """A non-Flatpak host with no input access gets the message, but re-binding
    through the portal is not the fix there."""
    app, _logs = _app(monkeypatch, is_flatpak=False, portal=False)
    app._sync_detect_availability()

    assert _state(app._detect_btn_record) == "disabled"
    app._detect_set_shortcut_btn.pack_forget.assert_called_once()


def test_sync_survives_a_panel_that_was_never_built(monkeypatch):
    """Settings may not have been opened yet; nothing here may raise."""
    monkeypatch.setattr(wayfinder_main, "IS_FLATPAK", True)
    monkeypatch.setattr(wayfinder_main, "HAS_EVDEV", False)
    app = SimpleNamespace(_pynput_listener_started=False, log=lambda _m: None)
    for name in ("_detect_gate", "_sync_detect_availability"):
        setattr(app, name,
                getattr(wayfinder_main.WayfinderApp, name).__get__(app, SimpleNamespace))
    app._sync_detect_availability()  # must not raise


# ── the app-side of the portal binding report ────────────────────────────────

def _binding_app(monkeypatch, *, portal=True, config=None):
    monkeypatch.setattr(wayfinder_main, "IS_FLATPAK", True)
    monkeypatch.setattr(wayfinder_main, "HAS_EVDEV", False)
    logs = []
    app = SimpleNamespace(
        config=config or {"hotkey_key": 57, "hotkey_modifiers": ["ctrl", "alt"],
                          "style_toggle_key": 28, "style_toggle_modifiers": ["ctrl", "alt"]},
        _portal_backend_active=portal,
        _portal_listener_started=portal,
        _portal_rebind_pending=False,
        _portal_bindings=None,
        _pynput_listener_started=False,
        _welcome_pane=None,
        hotkey_label=MagicMock(),
        _refresh_dictate_setup_chip=MagicMock(),
        _detect_btn_record=MagicMock(),
        _detect_btn_style=MagicMock(),
        _detect_hint=MagicMock(),
        _detect_set_shortcut_btn=MagicMock(),
        log=logs.append,
    )
    for name in ("_config_hotkey_label", "_portal_display", "get_hotkey_display",
                 "get_style_hotkey_display", "_on_portal_bindings_changed",
                 "_refresh_hotkey_labels", "_detect_gate",
                 "_sync_detect_availability"):
        setattr(app, name,
                getattr(wayfinder_main.WayfinderApp, name).__get__(app, SimpleNamespace))
    return app, logs


def test_display_is_config_until_the_portal_reports(monkeypatch):
    app, _logs = _binding_app(monkeypatch)
    assert app.get_hotkey_display() == "Ctrl+Alt+Space"


def test_nothing_bound_stops_advertising_the_config_chord(monkeypatch):
    """The exact Deck failure: bind returned code 0 having bound nothing."""
    app, logs = _binding_app(monkeypatch)
    app._on_portal_bindings_changed({"record-toggle": None, "style-toggle": None})

    label = app.get_hotkey_display()
    assert "Ctrl+Alt+Space" not in label
    assert "System Settings" in label
    assert any("NOT bound" in m for m in logs)
    app.hotkey_label.configure.assert_called_with(text=label)


def test_bound_shows_the_compositors_chord(monkeypatch):
    app, logs = _binding_app(monkeypatch)
    app._on_portal_bindings_changed({"record-toggle": "Meta+Insert",
                                     "style-toggle": None})

    assert app.get_hotkey_display() == "Meta+Insert"
    assert app.get_style_hotkey_display() != "Ctrl+Alt+Enter"
    assert any("Meta+Insert" in m for m in logs)


def test_unchanged_bindings_do_not_re_log(monkeypatch):
    app, logs = _binding_app(monkeypatch)
    payload = {"record-toggle": "Meta+Insert", "style-toggle": None}
    app._on_portal_bindings_changed(payload)
    app._on_portal_bindings_changed(dict(payload))
    assert len([m for m in logs if "Meta+Insert" in m]) == 1


@pytest.mark.parametrize("payload", [None, "nope", 7, ["record-toggle"]])
def test_malformed_payloads_are_ignored(monkeypatch, payload):
    app, _logs = _binding_app(monkeypatch)
    app._on_portal_bindings_changed(payload)
    assert app._portal_bindings is None
    assert app.get_hotkey_display() == "Ctrl+Alt+Space"


def test_without_the_portal_bindings_never_override_config(monkeypatch):
    """evdev / pynput hosts own their binding — config stays authoritative."""
    app, _logs = _binding_app(monkeypatch, portal=False)
    app._on_portal_bindings_changed({"record-toggle": None, "style-toggle": None})
    assert app.get_hotkey_display() == "Ctrl+Alt+Space"


# ── "Set shortcut…" — the rebind that replaces the dead Detect button ────────

def _rebind_app(monkeypatch):
    """Stand-in for the portal rebind dance, with a controllable clock."""
    monkeypatch.setattr(wayfinder_main, "IS_FLATPAK", True)
    monkeypatch.setattr(wayfinder_main, "HAS_EVDEV", False)
    clock = {"t": 1000.0}
    monkeypatch.setattr(wayfinder_main.time, "monotonic", lambda: clock["t"])

    scheduled = []
    logs = []
    app = SimpleNamespace(
        _portal_backend_active=True,
        _portal_listener_started=True,
        _portal_stop_event=SimpleNamespace(set=MagicMock()),
        _portal_started_at=900.0,
        _pynput_listener_started=False,
        _detect_btn_record=MagicMock(),
        _detect_btn_style=MagicMock(),
        _detect_hint=MagicMock(),
        _detect_set_shortcut_btn=MagicMock(),
        _start_portal_listener=MagicMock(),
        after=lambda _ms, fn=None: scheduled.append(fn),
        log=logs.append,
    )
    for name in ("_request_portal_rebind", "_restart_portal_after_stop",
                 "_finish_portal_rebind", "_detect_gate",
                 "_sync_detect_availability"):
        setattr(app, name,
                getattr(wayfinder_main.WayfinderApp, name).__get__(app, SimpleNamespace))
    return app, scheduled, logs, clock


def test_rebind_stops_the_current_session(monkeypatch):
    app, scheduled, _logs, _clock = _rebind_app(monkeypatch)
    app._request_portal_rebind()

    app._portal_stop_event.set.assert_called_once()
    assert app._portal_rebind_pending is True
    assert len(scheduled) == 1


def test_second_click_while_pending_is_refused(monkeypatch):
    """Overlapping poll chains made the first click report a bogus failure
    against the second click's healthy listener (Codex review)."""
    app, scheduled, logs, _clock = _rebind_app(monkeypatch)
    app._request_portal_rebind()
    app._request_portal_rebind()

    assert len(scheduled) == 1
    assert any("Already asking" in m for m in logs)


def test_button_reads_asking_while_pending(monkeypatch):
    app, _scheduled, _logs, _clock = _rebind_app(monkeypatch)
    app._request_portal_rebind()

    kwargs = app._detect_set_shortcut_btn.configure.call_args.kwargs
    assert kwargs["state"] == "disabled"
    assert kwargs["text"] == "Asking…"


def test_restart_happens_once_the_listener_is_down(monkeypatch):
    app, scheduled, _logs, _clock = _rebind_app(monkeypatch)
    app._request_portal_rebind()
    app._portal_listener_started = False

    scheduled[0]()  # the 1200ms callback

    app._start_portal_listener.assert_called_once()
    assert app._portal_rebind_pending is False


def test_a_listener_started_after_the_request_counts_as_done(monkeypatch):
    """The 10s supervisor may restart the portal first. That IS the rebind —
    polling on against a healthy replacement and then logging failure was the
    defect (Codex review)."""
    app, scheduled, logs, clock = _rebind_app(monkeypatch)
    app._request_portal_rebind()
    clock["t"] += 2.0
    app._portal_started_at = clock["t"]   # supervisor got there first

    scheduled[0]()

    assert app._portal_rebind_pending is False
    app._start_portal_listener.assert_not_called()
    assert not any("still busy" in m for m in logs)


def test_a_stale_poll_chain_is_dropped(monkeypatch):
    app, scheduled, logs, _clock = _rebind_app(monkeypatch)
    app._request_portal_rebind()
    stale = scheduled[0]
    app._portal_rebind_pending = False
    app._request_portal_rebind()          # supersedes: generation bumps

    stale()

    assert app._portal_rebind_pending is True   # untouched by the stale chain
    assert not any("still busy" in m for m in logs)


def test_pending_always_clears_so_the_button_cannot_stick(monkeypatch):
    """A permanently disabled "Set shortcut…" would be a worse dead button
    than the one this whole change removes."""
    app, scheduled, logs, _clock = _rebind_app(monkeypatch)
    app._request_portal_rebind()
    cb = scheduled[0]
    for _ in range(wayfinder_main._PORTAL_REBIND_POLLS + 2):
        if not scheduled:
            break
        cb = scheduled.pop()
        cb()

    assert app._portal_rebind_pending is False
    assert any("still busy" in m for m in logs)


def test_a_raising_start_still_releases_the_button(monkeypatch):
    """A permanently "Asking…" button would be a worse dead control than the
    Detect button this change removes (Codex review)."""
    app, scheduled, _logs, _clock = _rebind_app(monkeypatch)
    app._start_portal_listener = MagicMock(side_effect=RuntimeError("no bus"))
    app._request_portal_rebind()
    app._portal_listener_started = False

    with pytest.raises(RuntimeError):
        scheduled[0]()

    assert app._portal_rebind_pending is False
    kwargs = app._detect_set_shortcut_btn.configure.call_args.kwargs
    assert kwargs["state"] == "normal"
    assert kwargs["text"] == "Set shortcut…"
