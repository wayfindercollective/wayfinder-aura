"""Linux: the startup GPU probe (it loads a Whisper model) runs in GPU mode only."""

import pytest

from wayfinder.utils import gpu_simple

# ggml's device lines, integrated GPU listed first (a hybrid laptop).
_INIT = (
    "ggml_vulkan: Found 2 Vulkan devices:\n"
    "ggml_vulkan: 0 = AMD Radeon 780M Graphics (RADV PHOENIX) (radv) | uma: 1 | fp16: 1 | "
    "matrix cores: KHR_coopmat\n"
    "ggml_vulkan: 1 = AMD Radeon RX 7900 XTX (RADV NAVI31) (radv) | uma: 0 | fp16: 1 | "
    "matrix cores: KHR_coopmat\n"
)


@pytest.fixture
def linux(monkeypatch):
    monkeypatch.setattr(gpu_simple.sys, "platform", "linux")
    monkeypatch.delenv("GGML_VK_VISIBLE_DEVICES", raising=False)


def test_cpu_mode_never_probes(linux, monkeypatch):
    monkeypatch.setattr(gpu_simple, "detect_gpu_devices",
                        lambda config=None: pytest.fail("probed in CPU mode"))
    assert gpu_simple.setup_gpu_environment({"use_gpu": False}) == {}
    assert gpu_simple.setup_gpu_environment(None) == {}


def test_gpu_mode_picks_the_discrete_gpu(linux, monkeypatch):
    monkeypatch.setattr(gpu_simple, "detect_gpu_devices",
                        lambda config=None: gpu_simple.parse_ggml_vulkan_devices(_INIT))
    assert gpu_simple.setup_gpu_environment({"use_gpu": True}) == {"GGML_VK_VISIBLE_DEVICES": "1"}


def test_benchmark_can_force_the_probe_in_cpu_mode(linux, monkeypatch):
    monkeypatch.setattr(gpu_simple, "detect_gpu_devices",
                        lambda config=None: gpu_simple.parse_ggml_vulkan_devices(_INIT))
    env = gpu_simple.setup_gpu_environment({"use_gpu": False}, force=True)
    assert env == {"GGML_VK_VISIBLE_DEVICES": "1"}


def test_a_manual_device_still_wins_without_a_probe(linux, monkeypatch):
    monkeypatch.setattr(gpu_simple, "detect_gpu_devices",
                        lambda config=None: pytest.fail("probed with a manual device"))
    env = gpu_simple.setup_gpu_environment({"use_gpu": True, "gpu_device": "0"})
    assert env == {"GGML_VK_VISIBLE_DEVICES": "0"}
