"""The shared CPU / GPU / RAM readout (Advanced + Basic Settings, benchmark)."""

import sys

import pytest

from wayfinder.utils import gpu, system_info


def test_readout_has_three_text_lines():
    info = system_info.get_system_info()
    assert set(info) == {"cpu", "gpu", "ram"}
    assert all(isinstance(value, str) and value for value in info.values())


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc")
def test_linux_reads_cpu_and_ram_from_proc():
    info = system_info.get_system_info()
    assert info["cpu"] != "Unknown"
    assert info["ram"].endswith(" GB")


def test_every_entry_point_returns_the_same_readout(monkeypatch):
    fixed = {"cpu": "Test CPU", "gpu": "Test GPU", "ram": "8 GB"}
    monkeypatch.setattr(system_info, "get_system_info", lambda: dict(fixed))
    assert gpu.get_system_info() == fixed

    import wayfinder_main

    assert wayfinder_main.BenchmarkRunner.get_system_info() == fixed
