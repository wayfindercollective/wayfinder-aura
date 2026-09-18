from types import ModuleType

from wayfinder.utils import macos_permissions


def test_non_macos_is_always_ready(monkeypatch):
    monkeypatch.setattr(macos_permissions.sys, "platform", "linux")

    assert macos_permissions.request_accessibility_permission() is True


def test_frozen_macos_permissions_require_applications_copy(monkeypatch):
    monkeypatch.setattr(macos_permissions.sys, "platform", "darwin")
    monkeypatch.setattr(macos_permissions.sys, "frozen", True, raising=False)
    monkeypatch.setattr(
        macos_permissions.sys,
        "executable",
        "/Volumes/Aura/Wayfinder Aura.app/Contents/MacOS/Wayfinder Aura",
    )
    assert macos_permissions.macos_install_location_ready() is False

    monkeypatch.setattr(
        macos_permissions.sys,
        "executable",
        "/Applications/Wayfinder Aura.app/Contents/MacOS/Wayfinder Aura",
    )
    assert macos_permissions.macos_install_location_ready() is True


def test_macos_passes_prompt_choice_to_native_api(monkeypatch):
    calls = []
    native = ModuleType("ApplicationServices")
    native.kAXTrustedCheckOptionPrompt = "prompt"
    native.AXIsProcessTrustedWithOptions = lambda options: calls.append(options) or False
    monkeypatch.setitem(macos_permissions.sys.modules, "ApplicationServices", native)
    monkeypatch.setattr(macos_permissions.sys, "platform", "darwin")

    assert macos_permissions.request_accessibility_permission(prompt=False) is False
    assert calls == [{"prompt": False}]


def test_macos_native_api_failure_is_unknown(monkeypatch):
    native = ModuleType("ApplicationServices")
    native.kAXTrustedCheckOptionPrompt = "prompt"

    def fail(_options):
        raise RuntimeError("native failure")

    native.AXIsProcessTrustedWithOptions = fail
    monkeypatch.setitem(macos_permissions.sys.modules, "ApplicationServices", native)
    monkeypatch.setattr(macos_permissions.sys, "platform", "darwin")

    assert macos_permissions.request_accessibility_permission() is None


def test_input_monitoring_preflight_does_not_prompt_when_already_allowed(monkeypatch):
    calls = []
    quartz = ModuleType("Quartz")
    quartz.CGPreflightListenEventAccess = lambda: True
    quartz.CGRequestListenEventAccess = lambda: calls.append("request") or True
    monkeypatch.setitem(macos_permissions.sys.modules, "Quartz", quartz)
    monkeypatch.setattr(macos_permissions.sys, "platform", "darwin")

    assert macos_permissions.request_input_monitoring_permission(prompt=True) is True
    assert calls == []


def test_input_monitoring_can_request_access(monkeypatch):
    calls = []
    quartz = ModuleType("Quartz")
    quartz.CGPreflightListenEventAccess = lambda: False
    quartz.CGRequestListenEventAccess = lambda: calls.append("request") or False
    monkeypatch.setitem(macos_permissions.sys.modules, "Quartz", quartz)
    monkeypatch.setattr(macos_permissions.sys, "platform", "darwin")

    assert macos_permissions.request_input_monitoring_permission(prompt=True) is False
    assert calls == ["request"]


def test_startup_does_not_stack_input_prompt_behind_accessibility(monkeypatch):
    prompts = []
    monkeypatch.setattr(macos_permissions.sys, "platform", "darwin")
    monkeypatch.setattr(
        macos_permissions,
        "request_accessibility_permission",
        lambda *, prompt: prompts.append(("accessibility", prompt)) or False,
    )
    monkeypatch.setattr(
        macos_permissions,
        "request_input_monitoring_permission",
        lambda *, prompt: prompts.append(("input", prompt)) or False,
    )
    config = {"macos_input_permissions_prompted_v1": True}

    status = macos_permissions.request_startup_input_permissions(config)

    assert prompts == [("accessibility", True), ("input", False)]
    assert config["macos_accessibility_request_attempted_v2"] is True
    assert "macos_input_monitoring_request_attempted_v2" not in config
    assert status.config_changed is True


def test_input_monitoring_gets_its_own_request_after_accessibility(monkeypatch):
    prompts = []
    monkeypatch.setattr(macos_permissions.sys, "platform", "darwin")
    monkeypatch.setattr(
        macos_permissions,
        "request_accessibility_permission",
        lambda *, prompt: prompts.append(("accessibility", prompt)) or True,
    )
    monkeypatch.setattr(
        macos_permissions,
        "request_input_monitoring_permission",
        lambda *, prompt: prompts.append(("input", prompt)) or False,
    )
    config = {"macos_accessibility_request_attempted_v2": True}

    status = macos_permissions.request_startup_input_permissions(config)

    assert prompts == [("accessibility", False), ("input", True)]
    assert config["macos_input_monitoring_request_attempted_v2"] is True
    assert status.accessibility is True
    assert status.input_monitoring is False
    assert status.config_changed is True


def test_open_input_monitoring_settings_uses_listen_event_pane(monkeypatch):
    calls = []
    monkeypatch.setattr(macos_permissions.sys, "platform", "darwin")
    monkeypatch.setattr(
        macos_permissions.subprocess,
        "run",
        lambda args, **kwargs: calls.append((args, kwargs))
        or type("Result", (), {"returncode": 0})(),
    )

    assert macos_permissions.open_macos_privacy_settings("input_monitoring") is True
    assert "Privacy_ListenEvent" in calls[0][0][1]
