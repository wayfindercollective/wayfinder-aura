"""GPU-based local transcription backend auto-select."""

from __future__ import annotations

from wayfinder.utils.gpu import (
    GPUInfo,
    apply_auto_transcription_backend,
    recommend_local_transcription_backend,
)


def test_recommend_amd_is_whisper_cpp():
    rec, reason = recommend_local_transcription_backend(
        ctranslate2_cuda=False,
        gpu=GPUInfo("amd", "Radeon RX", "amdgpu"),
        faster_whisper_installed=True,
        allow_faster_whisper=True,
    )
    assert rec == "whisper_cpp"
    assert "AMD" in reason or "Vulkan" in reason


def test_recommend_nvidia_cuda_auto_still_whisper_cpp():
    """Sol: Auto prefers whisper.cpp even when CT2 CUDA is present."""
    rec, reason = recommend_local_transcription_backend(
        ctranslate2_cuda=True,
        gpu=GPUInfo("nvidia", "RTX 4080", "nvidia"),
    )
    assert rec == "whisper_cpp"
    assert "Manual" in reason or "whisper.cpp" in reason


def test_recommend_nvidia_default_whisper_cpp():
    rec, reason = recommend_local_transcription_backend(
        gpu=GPUInfo("nvidia", "RTX", "nvidia"),
    )
    assert rec == "whisper_cpp"
    assert "NVIDIA" in reason


def test_apply_auto_recovers_from_faster_whisper():
    cfg = {
        "transcription_backend_auto": True,
        "processing_mode": "local",
        "transcription_backend": "faster_whisper",
        "use_gpu": True,
    }
    changed, msg = apply_auto_transcription_backend(
        cfg, allow_faster_whisper=True
    )
    assert changed is True
    assert cfg["transcription_backend"] == "whisper_cpp"
    assert "whisper_cpp" in msg


def test_apply_auto_noop_when_already_cpp():
    cfg = {
        "transcription_backend_auto": True,
        "processing_mode": "local",
        "transcription_backend": "whisper_cpp",
        "use_gpu": True,
    }
    changed, _ = apply_auto_transcription_backend(cfg, allow_faster_whisper=False)
    assert changed is False
    assert cfg["transcription_backend"] == "whisper_cpp"


def test_apply_auto_noop_when_manual():
    cfg = {
        "transcription_backend_auto": False,
        "processing_mode": "local",
        "transcription_backend": "faster_whisper",
    }
    changed, _ = apply_auto_transcription_backend(cfg)
    assert changed is False
    assert cfg["transcription_backend"] == "faster_whisper"


def test_apply_auto_skips_cloud_without_probe():
    cfg = {
        "transcription_backend_auto": True,
        "processing_mode": "local",
        "transcription_backend": "groq_whisper",
    }
    changed, msg = apply_auto_transcription_backend(cfg)
    assert changed is False
    assert msg == ""
    assert cfg["transcription_backend"] == "groq_whisper"


def test_apply_auto_skips_remote_mode():
    cfg = {
        "transcription_backend_auto": True,
        "processing_mode": "remote",
        "transcription_backend": "faster_whisper",
    }
    changed, _ = apply_auto_transcription_backend(cfg)
    assert changed is False
    assert cfg["transcription_backend"] == "faster_whisper"


def test_default_config_has_auto_flag():
    from wayfinder.config import DEFAULT_CONFIG

    assert DEFAULT_CONFIG.get("transcription_backend_auto") is True
    assert DEFAULT_CONFIG.get("faster_whisper_cuda_device") == "auto"


def test_fw_cuda_index_helper_defaults_zero():
    from wayfinder.core.transcriber import FasterWhisperBackend

    b = FasterWhisperBackend(model_size="tiny", use_gpu=True, cuda_device="auto")
    assert b._cuda_device_index() == 0
    b.cuda_device = "2"
    assert b._cuda_device_index() == 2
