"""Tests for the release hardware preflight helper."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def _load_preflight():
    script = REPO / "scripts" / "ship_preflight.py"
    spec = importlib.util.spec_from_file_location("ship_preflight", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.linux_only
def test_preflight_script_is_executable():
    script = REPO / "scripts" / "ship_preflight.py"

    assert script.stat().st_mode & 0o111


def test_parse_vulkan_devices_extracts_discrete_and_integrated_devices():
    preflight = _load_preflight()
    summary = """
GPU0:
    deviceType         = PHYSICAL_DEVICE_TYPE_DISCRETE_GPU
    deviceName         = AMD Radeon RX 9060 XT (RADV GFX1200)
    driverName         = radv
GPU1:
    deviceType         = PHYSICAL_DEVICE_TYPE_INTEGRATED_GPU
    deviceName         = AMD Ryzen Graphics
    driverName         = radv
"""

    devices = preflight.parse_vulkan_devices(summary)

    assert devices == [
        {
            "slot": "GPU0",
            "deviceType": "PHYSICAL_DEVICE_TYPE_DISCRETE_GPU",
            "deviceName": "AMD Radeon RX 9060 XT (RADV GFX1200)",
            "driverName": "radv",
        },
        {
            "slot": "GPU1",
            "deviceType": "PHYSICAL_DEVICE_TYPE_INTEGRATED_GPU",
            "deviceName": "AMD Ryzen Graphics",
            "driverName": "radv",
        },
    ]


def test_dedicated_gpu_prefers_vulkan_discrete_classification():
    preflight = _load_preflight()
    result = preflight.detect_dedicated_gpu(
        "",
        [{"deviceType": "PHYSICAL_DEVICE_TYPE_DISCRETE_GPU", "deviceName": "AMD Radeon RX 9060 XT"}],
    )

    assert result["present"] is True
    assert result["source"] == "vulkan"


def test_foreign_flatpak_parent_uses_host_spawn_when_available(monkeypatch):
    preflight = _load_preflight()
    monkeypatch.setenv("FLATPAK_ID", "com.visualstudio.code")
    monkeypatch.setattr(preflight.shutil, "which", lambda name: "/usr/bin/flatpak-spawn" if name == "flatpak-spawn" else None)

    assert preflight.should_use_flatpak_spawn() is True


def test_wayfinder_flatpak_does_not_require_host_spawn(monkeypatch):
    preflight = _load_preflight()
    monkeypatch.setenv("FLATPAK_ID", "io.wayfindercollective.WayfinderAura")
    monkeypatch.setattr(preflight.shutil, "which", lambda name: "/usr/bin/flatpak-spawn" if name == "flatpak-spawn" else None)

    assert preflight.should_use_flatpak_spawn() is False


@pytest.mark.linux_only
def test_collect_preflight_pings_socket_on_host_when_in_foreign_flatpak(monkeypatch):
    preflight = _load_preflight()
    monkeypatch.setenv("FLATPAK_ID", "com.visualstudio.code")
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
    monkeypatch.setattr(preflight.shutil, "which", lambda name: "/usr/bin/flatpak-spawn" if name == "flatpak-spawn" else None)
    monkeypatch.setattr(preflight, "command_exists", lambda name, host=False: f"/usr/bin/{name}" if host else "")
    monkeypatch.setattr(preflight, "command_stdout", lambda *args, **kwargs: "")
    monkeypatch.setattr(preflight, "host_socket_ping", lambda path: f"host:{path}")
    monkeypatch.setattr(preflight, "socket_ping", lambda path: f"sandbox:{path}")

    report = preflight.collect_preflight()

    assert report["wayfinder_socket"]["ping"].startswith("host:")


def test_parse_portal_device_types_reads_the_gdbus_reply():
    preflight = _load_preflight()

    assert preflight.parse_portal_device_types("(<uint32 7>,)\n") == 7
    assert preflight.parse_portal_device_types("") is None
    assert preflight.parse_portal_device_types("Error: GDBus.Error:org.freedesktop.DBus.Error.UnknownProperty") is None


def test_portal_approval_reads_state_without_revealing_the_token(tmp_path):
    preflight = _load_preflight()
    approved = tmp_path / "approved.json"
    approved.write_text('{"declined": false, "restore_token": "secret-token"}')
    declined = tmp_path / "declined.json"
    declined.write_text('{"declined": true, "restore_token": ""}')

    assert preflight.portal_approval([tmp_path / "missing.json"]) == "not asked yet"
    assert preflight.portal_approval([declined]) == "declined"
    assert preflight.portal_approval([declined, approved]) == "approved"


def _wayland_report(portal_types, approval="approved", ydotool_socket="missing"):
    return {
        "session": {"XDG_SESSION_TYPE": "wayland", "XDG_CURRENT_DESKTOP": "KDE"},
        "tools": {"wtype": "", "xdotool": "/usr/bin/xdotool", "ydotool": "", "flatpak": "/usr/bin/flatpak"},
        "ydotool_socket": ydotool_socket,
        "remote_desktop_portal": {"available_device_types": portal_types, "aura_approval": approval},
        "wayfinder_socket": {"ping": "pong"},
        "gpu": {"dedicated_gpu": {"present": False}, "vulkan_devices": []},
        "steam_deck": {"detected": False},
    }


def _rows(preflight, report):
    return {name: (status, detail) for status, name, detail in preflight.summarize(report)}


def test_wayland_with_portal_treats_wtype_and_ydotool_as_fallbacks():
    preflight = _load_preflight()
    rows = _rows(preflight, _wayland_report(7))

    assert rows["RemoteDesktop portal"][0] == "OK"
    assert rows["Aura portal approval"][0] == "OK"
    assert rows["host tool wtype"][0] == "INFO"
    assert rows["host tool ydotool"][0] == "INFO"
    assert rows["ydotool socket"][0] == "INFO"
    assert "secret" not in repr(rows)


def test_wayland_without_portal_keyboard_warns_and_needs_fallbacks():
    preflight = _load_preflight()
    rows = _rows(preflight, _wayland_report(None, approval="not asked yet"))

    assert rows["RemoteDesktop portal"][0] == "WARN"
    assert rows["Aura portal approval"][0] == "INFO"
    assert rows["host tool wtype"][0] == "WARN"
    assert rows["ydotool socket"][0] == "WARN"


def test_declined_portal_warns_with_the_settings_toggle():
    preflight = _load_preflight()
    status, detail = _rows(preflight, _wayland_report(7, approval="declined"))["Aura portal approval"]

    assert status == "WARN"
    assert "Type into every app" in detail


def test_x11_session_has_no_portal_rows():
    preflight = _load_preflight()
    report = _wayland_report(7)
    report["session"]["XDG_SESSION_TYPE"] = "x11"
    rows = _rows(preflight, report)

    assert "RemoteDesktop portal" not in rows
    assert rows["host tool wtype"][0] == "WARN"
