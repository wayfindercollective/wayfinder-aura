"""GPU acceleration is a PREMIUM feature — enforced at the backend factory so a
config.json edit can't unlock it without a license. (Pairs with the UI gate.)"""
import pytest
from wayfinder.core.transcriber import get_backend, WhisperCppBackend
from wayfinder.license import LicenseInfo

BASE_CFG = {
    "transcription_backend": "whisper_cpp",
    "whisper_server_mode": False,   # -> WhisperCppBackend (no server binary needed)
    "use_gpu": True,
    "whisper_binary": "/x/whisper-cli",
    "model_path": "/x/m.bin",
}

@pytest.fixture(autouse=True)
def _free_license(monkeypatch):
    # Real license = non-premium unless a test overrides load_stored_license.
    monkeypatch.setattr("wayfinder.license.load_stored_license",
                        lambda: LicenseInfo(is_valid=False, is_premium=False))
    import wayfinder.license as L
    L._feature_gate = None  # force gate rebuild per test

def _reset_gate():
    import wayfinder.license as L
    L._feature_gate = None

def test_free_user_gpu_forced_off(monkeypatch, tmp_path):
    monkeypatch.setattr("wayfinder.config.CONFIG_FILE", tmp_path / "c.json")  # no dev flag
    _reset_gate()
    b = get_backend(dict(BASE_CFG))
    assert isinstance(b, WhisperCppBackend)
    assert b.use_gpu is False  # config asked for GPU, but free tier is gated to CPU

def test_premium_user_gpu_respected(monkeypatch):
    monkeypatch.setattr("wayfinder.license.load_stored_license",
                        lambda: LicenseInfo(is_valid=True, is_premium=True))
    _reset_gate()
    b = get_backend(dict(BASE_CFG))
    assert b.use_gpu is True

def test_premium_user_can_still_choose_cpu(monkeypatch):
    monkeypatch.setattr("wayfinder.license.load_stored_license",
                        lambda: LicenseInfo(is_valid=True, is_premium=True))
    _reset_gate()
    cfg = dict(BASE_CFG); cfg["use_gpu"] = False
    b = get_backend(cfg)
    assert b.use_gpu is False
