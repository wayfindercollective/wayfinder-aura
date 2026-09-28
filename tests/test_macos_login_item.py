"""Open-at-login (SMAppService) wrapper: safe off macOS and outside a bundle."""
from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace

from wayfinder.utils import macos_login_item as L


def test_not_available_off_macos(monkeypatch):
    monkeypatch.setattr(L.sys, "platform", "linux")
    assert L.available() is False
    assert L.status() is None
    assert L.set_enabled(True) == (False, "Available in the installed app.")


def test_source_run_is_not_a_login_item(monkeypatch):
    foundation = ModuleType("Foundation")
    foundation.NSBundle = SimpleNamespace(
        mainBundle=lambda: SimpleNamespace(bundlePath=lambda: "/usr/local/bin"))
    monkeypatch.setitem(sys.modules, "Foundation", foundation)
    monkeypatch.setattr(L.sys, "platform", "darwin")
    assert L.available() is False


def test_enable_reports_approval_needed(monkeypatch):
    svc = SimpleNamespace(registerAndReturnError_=lambda _e: (True, None),
                          status=lambda: L.REQUIRES_APPROVAL)
    monkeypatch.setattr(L, "available", lambda: True)
    monkeypatch.setattr(L, "_service", lambda: svc)
    ok, msg = L.set_enabled(True)
    assert ok is True and "Login Items" in msg


def test_refusal_is_reported_not_raised(monkeypatch):
    err = SimpleNamespace(localizedDescription=lambda: "Operation not permitted")
    svc = SimpleNamespace(registerAndReturnError_=lambda _e: (False, err),
                          status=lambda: L.NOT_REGISTERED)
    monkeypatch.setattr(L, "available", lambda: True)
    monkeypatch.setattr(L, "_service", lambda: svc)
    assert L.set_enabled(True) == (False, "macOS refused: Operation not permitted")


def test_disable_unregisters(monkeypatch):
    calls = []
    svc = SimpleNamespace(unregisterAndReturnError_=lambda _e: calls.append("off") or (True, None),
                          status=lambda: L.NOT_REGISTERED)
    monkeypatch.setattr(L, "available", lambda: True)
    monkeypatch.setattr(L, "_service", lambda: svc)
    assert L.set_enabled(False) == (True, None)
    assert calls == ["off"]


def test_setup_guide_applies_the_login_choice_only_when_ticked(monkeypatch):
    from wayfinder.ui.welcome import WelcomePane

    calls = []
    monkeypatch.setattr(L, "set_enabled", lambda on: calls.append(on) or (True, None))
    pane = WelcomePane.__new__(WelcomePane)
    pane.app = SimpleNamespace(log=lambda m: None)
    pane._login_item_var = SimpleNamespace(get=lambda: False)
    pane._apply_login_item_choice()
    assert calls == []
    pane._login_item_var = SimpleNamespace(get=lambda: True)
    pane._apply_login_item_choice()
    assert calls == [True]
    pane._login_item_var = None  # not offered (Linux, source run, already on)
    pane._apply_login_item_choice()
    assert calls == [True]
