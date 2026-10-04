"""macOS: is the hotkey actually reaching Aura, and does the app say so?

A Mac with every permission switch on could still leave Right Option dead:
pynput's listener ended silently when macOS refused its event tap, a tap made
before Input Monitoring applied heard nothing, and the banner's buttons could
appear to do nothing. These tests pin the listener's own check
(DarwinTapHealth), its retry, and the app's banner and checklist responses.
Every test fakes Quartz and pynput, so nothing touches the real keyboard.
"""
from __future__ import annotations

import threading
import time
from queue import Queue
from types import ModuleType, SimpleNamespace

import pytest

import wayfinder_main
from wayfinder.hotkeys import pynput_listener
from wayfinder.hotkeys.pynput_listener import DarwinTapHealth
from wayfinder.ui.welcome import relaunch_aware_requests
from wayfinder_main import WayfinderApp

needs_pynput = pytest.mark.skipif(pynput_listener.keyboard is None,
                                  reason="pynput unavailable here")

ALL_ON = {"microphone": True, "accessibility": True, "input_monitoring": True}


# --------------------------------------------------------------------------
# DarwinTapHealth: the state machine
# --------------------------------------------------------------------------

class TestTapHealth:
    def test_a_refused_tap_says_so(self):
        assert DarwinTapHealth(counter=lambda: 100).started(False) == "refused"

    def test_listening_until_a_key_arrives_then_live(self):
        typed = [100]
        health = DarwinTapHealth(counter=lambda: typed[0])
        assert health.started(True) == "listening"
        typed[0] = 110
        assert health.check(secure_input=False) == "listening"
        health.saw_event()
        assert health.check(secure_input=False) == "live"

    def test_many_typed_keys_and_none_arriving_is_deaf(self):
        typed = [100]
        health = DarwinTapHealth(counter=lambda: typed[0])
        health.started(True)
        typed[0] = 100 + DarwinTapHealth.DEAF_AFTER_KEYS - 1
        assert health.check(secure_input=False) == "listening"
        typed[0] += 1
        assert health.check(secure_input=False) == "deaf"

    def test_keys_typed_under_secure_input_prove_nothing(self):
        typed = [100]
        health = DarwinTapHealth(counter=lambda: typed[0])
        health.started(True)
        typed[0] = 500  # a password field: macOS hides these from every tap
        assert health.check(secure_input=True) == "listening"
        typed[0] = 500 + DarwinTapHealth.DEAF_AFTER_KEYS - 1
        assert health.check(secure_input=False) == "listening"

    def test_an_unreadable_counter_never_claims_deaf(self):
        health = DarwinTapHealth(counter=lambda: None)
        health.started(True)
        for _ in range(5):
            assert health.check(secure_input=False) == "listening"

    def test_a_new_tap_starts_from_scratch(self):
        health = DarwinTapHealth(counter=lambda: 1)
        health.started(True)
        health.saw_event()
        assert health.check(secure_input=False) == "live"
        assert health.started(True) == "listening"
        assert health.seen == 0


# --------------------------------------------------------------------------
# The listener loop: retry a refused tap, replace a paused one
# --------------------------------------------------------------------------

def _fake_quartz():
    quartz = ModuleType("Quartz")
    quartz.kCGEventFlagMaskSecondaryFn = 0x800000
    quartz.kCGEventFlagMaskAlternate = 0x080000
    quartz.kCGEventFlagMaskCommand = 0x100000
    quartz.kCGEventFlagMaskControl = 0x040000
    quartz.kCGEventFlagMaskShift = 0x020000
    quartz.kCGEventKeyDown = 10
    quartz.kCGEventKeyUp = 11
    quartz.kCGEventFlagsChanged = 12
    quartz.kCGEventTapDisabledByTimeout = 0xFFFFFFFE
    quartz.kCGEventTapDisabledByUserInput = 0xFFFFFFFF
    quartz.kCGKeyboardEventKeycode = 9
    quartz.kCGEventSourceUnixProcessID = 41
    quartz.CGEventGetFlags = lambda event: event["flags"]
    quartz.CGEventGetIntegerValueField = lambda event, field: (
        event.get("source_pid", 0) if field == 41 else event["keycode"]
    )
    return quartz


def _fake_listener_class(refusals):
    """pynput's darwin Listener: its thread ends at once when the tap is refused."""

    class FakeListener(threading.Thread):
        made: list = []
        kwargs: dict = {}

        def __init__(self, **kwargs):
            super().__init__(daemon=True)
            FakeListener.kwargs = kwargs
            self.refused = len(FakeListener.made) < refusals
            self.stopped = threading.Event()
            FakeListener.made.append(self)

        def run(self):
            if not self.refused:
                self.stopped.wait(5)

        def stop(self):
            self.stopped.set()

    return FakeListener


def _run_listener(monkeypatch, refusals, typed=None):
    monkeypatch.setitem(pynput_listener.sys.modules, "Quartz", _fake_quartz())
    monkeypatch.setattr(pynput_listener.sys, "platform", "darwin")
    listener_cls = _fake_listener_class(refusals)
    monkeypatch.setattr(pynput_listener.keyboard, "Listener", listener_cls)
    monkeypatch.setattr(pynput_listener, "_DARWIN_TAP_RETRY_S", (0.0, 0.0))
    monkeypatch.setattr(pynput_listener, "_DARWIN_IDLE_TICK_S", 0.01)
    monkeypatch.setattr(pynput_listener, "_darwin_secure_input_enabled", lambda: False)
    monkeypatch.setattr(pynput_listener, "_darwin_fn_pressed", lambda: False)
    counter = typed if typed is not None else [0]
    monkeypatch.setattr(pynput_listener, "_darwin_hid_key_events", lambda: counter[0])
    stop, health, logs = threading.Event(), [], []
    thread = threading.Thread(
        target=pynput_listener.pynput_hotkey_listener,
        kwargs=dict(event_queue=Queue(), hotkey_key=57, hotkey_modifiers=["fn"],
                    stop_event=stop, log_callback=logs.append, on_health=health.append),
        daemon=True,
    )
    thread.start()
    return SimpleNamespace(stop=stop, health=health, logs=logs, thread=thread,
                           listener_cls=listener_cls, typed=counter)


def _wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _finish(run):
    run.stop.set()
    run.thread.join(5)
    assert not run.thread.is_alive()


@needs_pynput
def test_a_refused_tap_is_retried_until_macos_allows_it(monkeypatch):
    run = _run_listener(monkeypatch, refusals=2)
    try:
        assert _wait_for(lambda: run.health[-1:] == ["listening"])
        assert run.health == ["refused", "listening"]
        assert any("hasn't given Wayfinder Aura keyboard access" in m for m in run.logs)
        assert any("allowed keyboard access" in m for m in run.logs)
        # The first key through the tap proves the hotkey works.
        run.listener_cls.kwargs["darwin_intercept"](10, {"flags": 0, "keycode": 0x24})
        assert _wait_for(lambda: run.health[-1:] == ["live"])
    finally:
        _finish(run)
    assert sum(listener.is_alive() for listener in run.listener_cls.made) == 0


@needs_pynput
def test_a_paused_tap_is_replaced_and_never_doubled(monkeypatch):
    run = _run_listener(monkeypatch, refusals=0)
    try:
        assert _wait_for(lambda: run.health == ["listening"])
        intercept = run.listener_cls.kwargs["darwin_intercept"]
        intercept(10, {"flags": 0, "keycode": 0x24})
        assert _wait_for(lambda: run.health[-1:] == ["live"])
        first = run.listener_cls.made[0]
        event = {"flags": 0, "keycode": 0}
        assert intercept(0xFFFFFFFE, event) is event  # macOS: tap disabled by timeout
        assert _wait_for(lambda: len(run.listener_cls.made) == 2)
        assert first.stopped.is_set()
        assert _wait_for(lambda: run.health[-1:] == ["listening"])
        assert any("paused the hotkey's event tap" in m for m in run.logs)
        assert sum(listener.is_alive() for listener in run.listener_cls.made) == 1
    finally:
        _finish(run)


@needs_pynput
def test_typed_keys_that_never_reach_the_tap_are_reported_as_deaf(monkeypatch):
    run = _run_listener(monkeypatch, refusals=0, typed=[1000])
    try:
        assert _wait_for(lambda: run.health == ["listening"])
        run.typed[0] += DarwinTapHealth.DEAF_AFTER_KEYS
        assert _wait_for(lambda: run.health[-1:] == ["deaf"])
        assert any("relaunched after Input Monitoring" in m for m in run.logs)
    finally:
        _finish(run)


# --------------------------------------------------------------------------
# App: banner, buttons, checklist
# --------------------------------------------------------------------------

class _Widget:
    def __init__(self, managed=""):
        self.managed = managed
        self.options = {}

    def configure(self, **kwargs):
        self.options.update(kwargs)

    def winfo_manager(self):
        return self.managed

    def winfo_exists(self):
        return True

    def pack(self, **kwargs):
        self.managed = "pack"

    def pack_forget(self):
        self.managed = ""


def _banner_app(monkeypatch, *, state=(True, True), **extra):
    from wayfinder.utils import macos_permissions as mp

    monkeypatch.setattr(wayfinder_main, "IS_MACOS", True)
    monkeypatch.setattr(mp, "microphone_authorization", lambda: mp.MIC_AUTHORIZED)
    monkeypatch.setattr(mp, "macos_install_location_ready", lambda: True)
    scheduled, logs = [], []
    app = SimpleNamespace(
        macos_permission_banner=_Widget(),
        macos_permission_label=_Widget(),
        macos_permission_open_btn=_Widget(),
        macos_permission_recheck_btn=_Widget(),
        _dictate_banner_anchor=_Widget("pack"),
        get_hotkey_display=lambda: "Right Option",
        after=lambda ms, fn: scheduled.append((ms, fn)),
        log=logs.append,
        scheduled=scheduled,
        logs=logs,
    )
    app._macos_permission_state = lambda: state
    app._log_macos_permission_state = (
        lambda *a: WayfinderApp._log_macos_permission_state(app, *a))
    app._refresh_macos_permission_banner = (
        lambda: WayfinderApp._refresh_macos_permission_banner(app))
    app.__dict__.update(extra)
    return app


def test_every_switch_on_but_no_keys_offers_a_relaunch(monkeypatch):
    app = _banner_app(monkeypatch, _macos_hotkey_health="deaf")
    WayfinderApp._refresh_macos_permission_banner(app)
    assert app._missing_macos_permission == "hotkey_relaunch"
    assert "isn't reaching Wayfinder Aura" in app.macos_permission_label.options["text"]
    assert app.macos_permission_open_btn.options["text"] == "Relaunch Aura"
    assert app.macos_permission_banner.managed == "pack"


def test_keys_reaching_aura_cancel_a_pending_relaunch(monkeypatch):
    app = _banner_app(monkeypatch, _macos_hotkey_health="live",
                      _macos_input_relaunch_required=True)
    app.macos_permission_banner.managed = "pack"
    WayfinderApp._refresh_macos_permission_banner(app)
    assert app._missing_macos_permission is None
    assert app.macos_permission_banner.managed == ""


def test_a_missing_switch_still_outranks_the_hotkey_check(monkeypatch):
    app = _banner_app(monkeypatch, state=(False, False), _macos_hotkey_health="refused")
    WayfinderApp._refresh_macos_permission_banner(app)
    assert app._missing_macos_permission == "accessibility"


def test_relaunch_button_relaunches(monkeypatch):
    relaunched = []
    app = _banner_app(monkeypatch, _macos_hotkey_health="refused",
                      relaunch_app=lambda: relaunched.append(True) or True)
    WayfinderApp._refresh_macos_permission_banner(app)
    WayfinderApp._open_missing_macos_permission(app)
    assert relaunched == [True]


def test_live_health_from_the_listener_clears_the_relaunch_flag(monkeypatch):
    app = _banner_app(monkeypatch, _macos_input_relaunch_required=True,
                      event_queue=Queue())
    app._on_macos_hotkey_health = lambda state: WayfinderApp._on_macos_hotkey_health(app, state)
    WayfinderApp._queue_macos_hotkey_health(app, "live")  # the listener's thread
    kind, callback = app.event_queue.get_nowait()
    assert kind == wayfinder_main.EventType.UI_CALLBACK
    callback()  # the Tk thread
    assert app._macos_hotkey_health == "live"
    assert app._macos_input_relaunch_required is False


def test_recheck_answers_visibly_when_nothing_changed(monkeypatch):
    app = _banner_app(monkeypatch, state=(True, False))
    WayfinderApp._refresh_macos_permission_banner(app)
    WayfinderApp._recheck_macos_permissions(app)
    assert app.macos_permission_recheck_btn.options["text"] == "Still off"
    assert app.scheduled and app.scheduled[-1][0] == 2000
    app.scheduled[-1][1]()
    assert app.macos_permission_recheck_btn.options["text"] == "Recheck"


def test_recheck_stays_quiet_once_fixed(monkeypatch):
    app = _banner_app(monkeypatch, state=(True, False))
    WayfinderApp._refresh_macos_permission_banner(app)
    app._macos_permission_state = lambda: (True, True)
    WayfinderApp._recheck_macos_permissions(app)
    assert "text" not in app.macos_permission_recheck_btn.options
    assert app.macos_permission_banner.managed == ""


def test_permission_changes_are_logged_once_each(monkeypatch):
    app = _banner_app(monkeypatch, state=(True, False))
    WayfinderApp._refresh_macos_permission_banner(app)
    WayfinderApp._refresh_macos_permission_banner(app)
    assert len(app.logs) == 1
    assert "Accessibility on, Input Monitoring off" in app.logs[0]
    assert "macOS " in app.logs[0]  # the first line names the macOS version
    app._macos_permission_state = lambda: (True, True)
    WayfinderApp._refresh_macos_permission_banner(app)
    assert len(app.logs) == 2 and "Input Monitoring on" in app.logs[1]


class TestChecklistNeverOpensIntoAGonePane:
    def _app(self, monkeypatch, **extra):
        import wayfinder.ui.welcome as welcome
        import wayfinder.utils.macos_permissions as mp

        created = []

        class FakePane:
            def __init__(self, parent, app, *, only_permissions=False):
                created.append(only_permissions)

        monkeypatch.setattr(wayfinder_main, "IS_MACOS", True)
        monkeypatch.setattr(mp, "permission_snapshot", lambda: dict(ALL_ON))
        monkeypatch.setattr(welcome, "WelcomePane", FakePane)
        tabs = []
        app = SimpleNamespace(tab_content_container=object(), _switch_tab=tabs.append,
                              log=lambda m: None, **extra)
        return app, created, tabs

    def test_a_destroyed_checklist_is_replaced(self, monkeypatch):
        app, created, tabs = self._app(
            monkeypatch, _permissions_pane=SimpleNamespace(_destroyed=True))
        assert WayfinderApp.show_permissions_setup(app, force=True) is True
        assert created == [True] and tabs == ["dictate"]

    def test_a_finished_tour_falls_back_to_the_checklist(self, monkeypatch):
        tour = SimpleNamespace(show_permissions_step=lambda: False)
        app, created, _ = self._app(monkeypatch, _welcome_active=True, _welcome_pane=tour)
        assert WayfinderApp.show_permissions_setup(app, force=True) is True
        assert created == [True]


class TestRelaunchAwareRequests:
    def test_live_keys_owe_no_relaunch(self):
        assert relaunch_aware_requests({"input_monitoring"}, ALL_ON, "live") == set()

    def test_no_keys_with_every_switch_on_owes_one(self):
        for health in ("refused", "deaf"):
            assert relaunch_aware_requests(set(), ALL_ON, health) == {"input_monitoring"}

    def test_a_missing_switch_is_asked_for_first(self):
        snapshot = dict(ALL_ON, input_monitoring=False)
        assert relaunch_aware_requests(set(), snapshot, "refused") == set()

    def test_unknown_health_changes_nothing(self):
        assert relaunch_aware_requests({"accessibility"}, ALL_ON, None) == {"accessibility"}


class TestChecklistFollowsTheHotkeyCheck:
    """A tap that recovers by itself (the 3 s retry) must clear the checklist's
    "relaunch" button even though no permission switch changed."""

    def _pane(self, monkeypatch, drawn_with, now):
        import wayfinder.utils.macos_permissions as mp
        from wayfinder.ui.welcome import WelcomePane

        monkeypatch.setattr(mp, "permission_snapshot", lambda: dict(ALL_ON))
        renders, polls = [], []
        pane = SimpleNamespace(
            _perm_poll_id=None, _destroyed=False,
            flow=SimpleNamespace(current="permissions"),
            _perm_snapshot=dict(ALL_ON), _perm_health=drawn_with,
            app=SimpleNamespace(_macos_hotkey_health=now),
            _render_step=lambda: renders.append(True),
            card=SimpleNamespace(after=lambda ms, fn: polls.append(ms) or "id"),
            _PERMISSION_POLL_MS=1000, _poll_permissions=lambda: None,
        )
        WelcomePane._poll_permissions(pane)
        return renders, polls

    def test_a_health_change_redraws_the_rows(self, monkeypatch):
        renders, polls = self._pane(monkeypatch, drawn_with="refused", now="listening")
        assert renders == [True] and polls == []

    def test_no_change_just_polls_again(self, monkeypatch):
        renders, polls = self._pane(monkeypatch, drawn_with="live", now="live")
        assert renders == [] and polls == [1000]
