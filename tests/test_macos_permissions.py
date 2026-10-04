import sys
from types import ModuleType, SimpleNamespace

import pytest

from wayfinder.utils import macos_permissions


def test_non_macos_is_always_ready(monkeypatch):
    monkeypatch.setattr(macos_permissions.sys, "platform", "linux")

    assert macos_permissions.request_accessibility_permission() is True


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX bundle paths")
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


def test_iohid_request_registers_current_app_for_input_monitoring(monkeypatch):
    calls = []

    class Request:
        def __call__(self, kind):
            calls.append(kind)
            return True

    request = Request()
    monkeypatch.setattr(macos_permissions.sys, "platform", "darwin")
    monkeypatch.setattr(macos_permissions.ctypes, "CDLL",
                        lambda path: SimpleNamespace(IOHIDRequestAccess=request))

    assert macos_permissions.request_input_monitoring_registration() is True
    assert calls == [1]  # kIOHIDRequestTypeListenEvent
    assert request.argtypes == [macos_permissions.ctypes.c_int]
    assert request.restype == macos_permissions.ctypes.c_bool


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
    config = {"welcome_completed": True, "macos_input_permissions_prompted_v1": True}

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
    config = {"welcome_completed": True, "macos_accessibility_request_attempted_v2": True}

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


def test_permission_snapshot_is_all_granted_off_macos(monkeypatch):
    monkeypatch.setattr(macos_permissions.sys, "platform", "linux")
    assert macos_permissions.permission_snapshot() == {
        "microphone": True, "accessibility": True, "input_monitoring": True,
    }


def test_first_run_startup_never_prompts_before_the_setup_guide(monkeypatch):
    prompts = []
    monkeypatch.setattr(macos_permissions.sys, "platform", "darwin")
    monkeypatch.setattr(
        macos_permissions, "request_accessibility_permission",
        lambda *, prompt: prompts.append(("accessibility", prompt)) or False,
    )
    monkeypatch.setattr(
        macos_permissions, "request_input_monitoring_permission",
        lambda *, prompt: prompts.append(("input", prompt)) or False,
    )
    config = {}

    status = macos_permissions.request_startup_input_permissions(config)

    assert all(prompt is False for _, prompt in prompts)
    assert status.config_changed is False
    assert config == {}


def test_repair_refuses_any_bundle_but_aura(monkeypatch):
    calls = []
    monkeypatch.setattr(macos_permissions.sys, "platform", "darwin")
    monkeypatch.setattr(macos_permissions, "own_bundle_identifier", lambda: "org.python.python")
    monkeypatch.setattr(macos_permissions.subprocess, "run",
                        lambda *a, **k: calls.append(a))

    assert macos_permissions.repair_permission("accessibility") is False
    assert calls == []


def test_repair_resets_only_auras_own_entry_then_asks_again(monkeypatch):
    calls, asked = [], []
    monkeypatch.setattr(macos_permissions.sys, "platform", "darwin")
    monkeypatch.setattr(macos_permissions, "own_bundle_identifier",
                        lambda: macos_permissions.AURA_BUNDLE_ID)
    monkeypatch.setattr(macos_permissions.subprocess, "run",
                        lambda args, **k: calls.append(args))
    monkeypatch.setattr(macos_permissions, "ask_for_permission",
                        lambda name: asked.append(name) or True)

    assert macos_permissions.repair_permission("input_monitoring") is True
    assert calls == [["/usr/bin/tccutil", "reset", "ListenEvent",
                      macos_permissions.AURA_BUNDLE_ID]]
    assert asked == ["input_monitoring"]


def test_repair_is_a_noop_off_macos(monkeypatch):
    monkeypatch.setattr(macos_permissions.sys, "platform", "linux")
    assert macos_permissions.repair_permission("accessibility") is False


def _ask_env(monkeypatch, *, trusted, bundle):
    calls, prompts = [], []
    monkeypatch.setattr(macos_permissions.sys, "platform", "darwin")
    monkeypatch.setattr(macos_permissions, "own_bundle_identifier", lambda: bundle)
    monkeypatch.setattr(macos_permissions.subprocess, "run",
                        lambda args, **k: calls.append(args)
                        or type("R", (), {"returncode": 0})())
    monkeypatch.setattr(macos_permissions, "request_accessibility_permission",
                        lambda *, prompt: prompts.append(prompt) or trusted)
    return calls, prompts


def test_allow_clears_auras_stale_entry_before_prompting(monkeypatch):
    # An entry from an older signature makes the Settings switch grant that
    # copy instead of this one; "allow" resets Aura's own entry, then asks.
    calls, prompts = _ask_env(monkeypatch, trusted=False,
                              bundle=macos_permissions.AURA_BUNDLE_ID)
    assert macos_permissions.ask_for_permission("accessibility") is True
    assert calls[0] == ["/usr/bin/tccutil", "reset", "Accessibility",
                        macos_permissions.AURA_BUNDLE_ID]
    assert "Privacy_Accessibility" in calls[1][1]  # then the Settings pane
    assert prompts == [False, True]


def test_allow_never_resets_a_working_grant(monkeypatch):
    calls, _ = _ask_env(monkeypatch, trusted=True, bundle=macos_permissions.AURA_BUNDLE_ID)
    macos_permissions.ask_for_permission("accessibility")
    assert not any("tccutil" in str(c) for c in calls)


def test_allow_never_resets_from_a_source_run(monkeypatch):
    calls, _ = _ask_env(monkeypatch, trusted=False, bundle="org.python.python")
    macos_permissions.ask_for_permission("accessibility")
    assert not any("tccutil" in str(c) for c in calls)


def test_input_monitoring_request_waits_for_accessibility(monkeypatch):
    calls = []
    monkeypatch.setattr(macos_permissions.sys, "platform", "darwin")
    monkeypatch.setattr(
        macos_permissions, "request_accessibility_permission",
        lambda *, prompt: calls.append(("accessibility", prompt)) or False)
    monkeypatch.setattr(
        macos_permissions, "request_input_monitoring_permission",
        lambda *, prompt: calls.append(("input", prompt)) or False)
    monkeypatch.setattr(
        macos_permissions, "open_macos_privacy_settings",
        lambda name: calls.append(("settings", name)) or True)

    assert macos_permissions.ask_for_permission("input_monitoring") is True
    assert calls == [("accessibility", False), ("accessibility", True),
                     ("settings", "accessibility")]


def test_input_monitoring_allow_registers_without_resetting_a_working_grant(monkeypatch):
    calls = []
    monkeypatch.setattr(macos_permissions.sys, "platform", "darwin")
    monkeypatch.setattr(macos_permissions, "request_accessibility_permission",
                        lambda *, prompt: True)
    monkeypatch.setattr(macos_permissions, "request_input_monitoring_permission",
                        lambda *, prompt: calls.append(("preflight", prompt)) or True)
    monkeypatch.setattr(macos_permissions, "request_input_monitoring_registration",
                        lambda: calls.append("register") or True)
    monkeypatch.setattr(macos_permissions, "_reset_own_entry",
                        lambda name: calls.append(("reset", name)))
    monkeypatch.setattr(macos_permissions, "open_macos_privacy_settings",
                        lambda name: calls.append(("settings", name)) or True)

    assert macos_permissions.ask_for_permission("input_monitoring") is True
    assert calls == [("preflight", False), "register",
                     ("settings", "input_monitoring")]


def _patch_build(monkeypatch, build):
    monkeypatch.setattr(macos_permissions, "_build_identity", lambda: build)


def test_allow_resets_only_on_this_builds_first_ask(monkeypatch):
    # The second "allow" can come right after the user turned the switch on,
    # before macOS applied it to this process: resetting then wiped the grant.
    calls, _ = _ask_env(monkeypatch, trusted=False, bundle=macos_permissions.AURA_BUNDLE_ID)
    _patch_build(monkeypatch, "build-1")
    macos_permissions.ask_for_permission("accessibility")
    macos_permissions.ask_for_permission("accessibility")
    resets = [c for c in calls if "tccutil" in str(c)]
    assert resets == [["/usr/bin/tccutil", "reset", "Accessibility",
                       macos_permissions.AURA_BUNDLE_ID]]


def test_each_permission_gets_its_own_first_reset(monkeypatch):
    calls, _ = _ask_env(monkeypatch, trusted=False, bundle=macos_permissions.AURA_BUNDLE_ID)
    _patch_build(monkeypatch, "build-1")
    monkeypatch.setattr(macos_permissions, "microphone_authorization",
                        lambda: macos_permissions.MIC_DENIED)
    monkeypatch.setattr(macos_permissions, "request_microphone_access", lambda: True)
    macos_permissions.ask_for_permission("accessibility")
    macos_permissions.ask_for_permission("microphone")
    macos_permissions.ask_for_permission("microphone")
    services = [c[2] for c in calls if "tccutil" in str(c)]
    assert services == ["Accessibility", "Microphone"]


def test_a_newly_installed_build_may_clear_its_stale_entry_again(monkeypatch):
    calls, _ = _ask_env(monkeypatch, trusted=False, bundle=macos_permissions.AURA_BUNDLE_ID)
    _patch_build(monkeypatch, "build-1")
    macos_permissions.ask_for_permission("accessibility")
    _patch_build(monkeypatch, "build-2")
    macos_permissions.ask_for_permission("accessibility")
    macos_permissions.ask_for_permission("accessibility")
    assert sum("tccutil" in str(c) for c in calls) == 2


def test_a_failed_reset_is_not_recorded(monkeypatch):
    calls = []
    monkeypatch.setattr(macos_permissions.sys, "platform", "darwin")
    monkeypatch.setattr(macos_permissions, "own_bundle_identifier",
                        lambda: macos_permissions.AURA_BUNDLE_ID)
    results = iter([1, 0])
    monkeypatch.setattr(macos_permissions.subprocess, "run",
                        lambda args, **k: calls.append(args)
                        or type("R", (), {"returncode": next(results, 0)})())
    monkeypatch.setattr(macos_permissions, "request_accessibility_permission",
                        lambda *, prompt: False)
    _patch_build(monkeypatch, "build-1")
    macos_permissions.ask_for_permission("accessibility")  # tccutil failed
    macos_permissions.ask_for_permission("accessibility")  # so it may try again
    macos_permissions.ask_for_permission("accessibility")  # and then never
    assert sum("tccutil" in str(c) for c in calls) == 2

