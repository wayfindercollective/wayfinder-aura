"""macOS-only ggml Metal environment for whisper.cpp children."""

from wayfinder.utils import macos_ggml_env as env


def test_cpu_mode_registers_no_metal_device(monkeypatch):
    monkeypatch.setattr(env.sys, "platform", "darwin")
    assert env.whisper_metal_env(["whisper-server", "-m", "m.bin", "-ng"]) == {"GGML_METAL_DEVICES": "0"}
    assert env.whisper_metal_env(["whisper-cli", "--no-gpu"]) == {"GGML_METAL_DEVICES": "0"}
    assert env.whisper_metal_env(gpu=False) == {"GGML_METAL_DEVICES": "0"}


def test_gpu_mode_keeps_metal_without_residency_heartbeat(monkeypatch):
    monkeypatch.setattr(env.sys, "platform", "darwin")
    assert env.whisper_metal_env(["whisper-server", "-m", "m.bin"]) == {"GGML_METAL_NO_RESIDENCY": "1"}


def test_other_platforms_are_untouched(monkeypatch):
    for platform_name in ("linux", "win32"):
        monkeypatch.setattr(env.sys, "platform", platform_name)
        assert env.whisper_metal_env(["whisper-server", "-ng"]) == {}
        assert env.whisper_metal_env(gpu=True) == {}
