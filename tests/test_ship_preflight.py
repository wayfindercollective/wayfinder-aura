"""Tests for the release hardware preflight helper."""

from __future__ import annotations

import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _load_preflight():
    script = REPO / "scripts" / "ship_preflight.py"
    spec = importlib.util.spec_from_file_location("ship_preflight", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
