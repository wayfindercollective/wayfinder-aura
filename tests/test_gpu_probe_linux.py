"""Linux: the startup GPU probe (it loads a Whisper model) runs in GPU mode only."""

import os
import re
import subprocess

import pytest

from wayfinder.utils import gpu_simple
from wayfinder.utils import platform as platform_utils

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


def test_turning_gpu_back_on_still_picks_the_discrete_gpu(linux, monkeypatch, tmp_path):
    # GPU on picks device 1 and leaves GGML_VK_VISIBLE_DEVICES=1 set; GPU off
    # then on probes again. Under that mask ggml lists only the chosen GPU, as
    # device 0, so a masked probe would move dictation to the integrated GPU.
    cli = tmp_path / "whisper-cli"
    cli.write_text("")
    (tmp_path / "ggml-tiny.en.bin").write_bytes(b"")
    monkeypatch.setattr(platform_utils, "get_whisper_model_search_dirs", lambda: [tmp_path])
    devices = _INIT.splitlines()[1:]

    def ggml_probe(argv, **kwargs):
        mask = kwargs.get("env", os.environ).get("GGML_VK_VISIBLE_DEVICES")
        shown = devices if mask is None else [devices[int(i)] for i in mask.split(",")]
        lines = [re.sub(r"ggml_vulkan: \d+ =", f"ggml_vulkan: {n} =", line) for n, line in enumerate(shown)]
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="\n".join(lines))

    monkeypatch.setattr(gpu_simple.subprocess, "run", ggml_probe)
    config = {"use_gpu": True, "whisper_binary": str(cli)}
    for _ in ("first switch to GPU", "GPU off, then on again"):
        assert gpu_simple.setup_gpu_environment(config) == {"GGML_VK_VISIBLE_DEVICES": "1"}
        assert os.environ["GGML_VK_VISIBLE_DEVICES"] == "1"
