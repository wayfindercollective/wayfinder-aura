"""macOS permissions checklist wiring in the app (setup guide, banner, Ultra).

Uses plain namespaces with the real WayfinderApp methods, so no Tk window is
created. Every macOS path has a Linux-unchanged counterpart.
"""
from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

import wayfinder_main
from wayfinder_main import WayfinderApp


def _ns(**extra):
    ns = SimpleNamespace(
        stop_event=threading.Event(),
        logs=[],
        config={},
        event_queue=None,
    )
    ns.log = ns.logs.append
    ns.__dict__.update(extra)
    return ns


class TestShowPermissionsSetup:
    def test_linux_never_shows_it(self, monkeypatch):
        monkeypatch.setattr(wayfinder_main, "IS_MACOS", False)
        assert WayfinderApp.show_permissions_setup(_ns(), force=True) is False

    def test_mac_with_everything_granted_stays_quiet_unless_forced(self, monkeypatch):
        import wayfinder.utils.macos_permissions as mp

        monkeypatch.setattr(wayfinder_main, "IS_MACOS", True)
        monkeypatch.setattr(mp, "permission_snapshot", lambda: {
            "microphone": True, "accessibility": True, "input_monitoring": True})
        assert WayfinderApp.show_permissions_setup(_ns()) is False

    def test_mac_routes_into_the_running_setup_guide(self, monkeypatch):
        import wayfinder.utils.macos_permissions as mp

        monkeypatch.setattr(wayfinder_main, "IS_MACOS", True)
        monkeypatch.setattr(mp, "permission_snapshot", lambda: {
            "microphone": True, "accessibility": False, "input_monitoring": False})
        shown = []
        pane = SimpleNamespace(show_permissions_step=lambda: shown.append(True))
        ns = _ns(_welcome_active=True, _welcome_pane=pane)
        assert WayfinderApp.show_permissions_setup(ns) is True
        assert shown == [True]

    def test_mac_reuses_an_open_checklist(self, monkeypatch):
        import wayfinder.utils.macos_permissions as mp

        monkeypatch.setattr(wayfinder_main, "IS_MACOS", True)
        monkeypatch.setattr(mp, "permission_snapshot", lambda: {
            "microphone": False, "accessibility": True, "input_monitoring": True})
        tabs = []
        ns = _ns(_permissions_pane=object(), _switch_tab=tabs.append)
        assert WayfinderApp.show_permissions_setup(ns) is True
        assert tabs == ["dictate"]


class TestPermissionGrantRestartsHotkeys:
    def test_linux_grant_callback_is_a_noop(self, monkeypatch):
        monkeypatch.setattr(wayfinder_main, "IS_MACOS", False)
        restarted = []
        ns = _ns(_restart_pynput_listener=lambda: restarted.append(True),
                 _refresh_macos_permission_banner=lambda: None)
        WayfinderApp._macos_permissions_granted(ns, ["accessibility"])
        assert restarted == []

    def test_mac_input_grant_restarts_listener_and_refreshes_banner(self, monkeypatch):
        monkeypatch.setattr(wayfinder_main, "IS_MACOS", True)
        calls = []
        ns = _ns(_restart_pynput_listener=lambda: calls.append("restart"),
                 _refresh_macos_permission_banner=lambda: calls.append("banner"))
        WayfinderApp._macos_permissions_granted(ns, ["input_monitoring"])
        assert calls == ["restart", "banner"]

    def test_mac_microphone_grant_only_refreshes_banner(self, monkeypatch):
        monkeypatch.setattr(wayfinder_main, "IS_MACOS", True)
        calls = []
        ns = _ns(_restart_pynput_listener=lambda: calls.append("restart"),
                 _refresh_macos_permission_banner=lambda: calls.append("banner"))
        WayfinderApp._macos_permissions_granted(ns, ["microphone"])
        assert calls == ["banner"]

    def test_restart_stops_old_listener_without_touching_shared_stop(self, monkeypatch):
        monkeypatch.setattr(wayfinder_main, "IS_MACOS", True)
        old_restart = threading.Event()
        old_thread = threading.Thread(target=old_restart.wait, args=(5,), daemon=True)
        old_thread.start()
        started = threading.Event()
        ns = _ns(_pynput_restart_event=old_restart, _pynput_thread=old_thread,
                 _pynput_listener_started=True)
        ns._start_pynput_listener = lambda: (
            started.set() if ns._pynput_listener_started is False else None)

        WayfinderApp._restart_pynput_listener(ns)

        assert started.wait(3), "a fresh listener was not started"
        assert old_restart.is_set()
        assert not old_thread.is_alive()
        assert not ns.stop_event.is_set()  # socket listener etc. keep running

    def test_restart_does_nothing_while_shutting_down(self, monkeypatch):
        monkeypatch.setattr(wayfinder_main, "IS_MACOS", True)
        started = []
        ns = _ns(_start_pynput_listener=lambda: started.append(True))
        ns.stop_event.set()
        WayfinderApp._restart_pynput_listener(ns)
        assert started == []


class TestListenerRestartEventWiring:
    def _start(self, monkeypatch, is_mac):
        import wayfinder.hotkeys as hk

        seen = {}
        done = threading.Event()

        def fake_listener(*args, **kwargs):
            seen.update(kwargs)
            done.set()

        monkeypatch.setattr(wayfinder_main, "IS_MACOS", is_mac)
        monkeypatch.setattr(hk, "pynput_hotkey_listener", fake_listener)
        monkeypatch.setattr(hk, "is_pynput_available", lambda: True)
        ns = _ns()
        WayfinderApp._start_pynput_listener(ns)
        assert done.wait(3)
        ns._pynput_thread.join(3)
        return ns, seen

    def test_mac_listener_gets_its_own_restart_event(self, monkeypatch):
        ns, seen = self._start(monkeypatch, True)
        assert isinstance(seen.get("restart_event"), threading.Event)
        assert seen["restart_event"] is ns._pynput_restart_event
        assert seen["restart_event"] is not ns.stop_event

    def test_linux_listener_call_is_unchanged(self, monkeypatch):
        ns, seen = self._start(monkeypatch, False)
        assert "restart_event" not in seen
        assert ns._pynput_restart_event is None

    def test_finished_listener_clears_started_flag(self, monkeypatch):
        ns, _ = self._start(monkeypatch, False)
        assert ns._pynput_listener_started is False


@pytest.mark.posix_only
class TestRelaunch:
    def test_linux_never_relaunches(self, monkeypatch):
        monkeypatch.setattr(wayfinder_main, "IS_MACOS", False)
        assert WayfinderApp.relaunch_app(_ns()) is False

    def test_source_run_has_no_bundle_to_reopen(self, monkeypatch):
        import Foundation

        monkeypatch.setattr(wayfinder_main, "IS_MACOS", True)
        quit_calls = []
        bundle = SimpleNamespace(bundlePath=lambda: "/usr/local/bin")
        monkeypatch.setattr(Foundation, "NSBundle",
                            SimpleNamespace(mainBundle=lambda: bundle))
        ns = _ns(quit_app=lambda: quit_calls.append(True))
        assert WayfinderApp.relaunch_app(ns) is False
        assert quit_calls == []

    def test_bundle_relaunch_waits_for_this_pid_then_opens(self, monkeypatch):
        import Foundation

        monkeypatch.setattr(wayfinder_main, "IS_MACOS", True)
        bundle = SimpleNamespace(bundlePath=lambda: "/Applications/Wayfinder Aura.app")
        monkeypatch.setattr(Foundation, "NSBundle",
                            SimpleNamespace(mainBundle=lambda: bundle))
        spawned, quit_calls = [], []
        monkeypatch.setattr(wayfinder_main.subprocess, "Popen",
                            lambda args, **k: spawned.append((args, k)))
        ns = _ns(quit_app=lambda: quit_calls.append(True))
        assert WayfinderApp.relaunch_app(ns) is True
        args, kwargs = spawned[0]
        assert args[0] == "/bin/sh" and "kill -0" in args[2] and "/usr/bin/open" in args[2]
        assert args[-1] == "/Applications/Wayfinder Aura.app"
        assert args[-2] == str(wayfinder_main.os.getpid())
        assert kwargs.get("start_new_session") is True
        assert quit_calls == [True]
