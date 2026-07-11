"""
Pytest configuration and fixtures for Wayfinder Aura tests.
"""

import json
import os
import struct
import sys
import tempfile
import wave
from pathlib import Path
from typing import Generator
from unittest.mock import MagicMock, patch

import pytest


def pytest_configure(config):
    """Register custom markers not declared in pyproject.toml.

    ``live`` gates the socket smoke tests (test_live_smoke.py): they need a
    running app instance and are skipped unless WAYFINDER_LIVE=1, so registering
    the marker here (rather than editing pyproject) keeps the sign-off battery
    self-contained and silences the unknown-marker warning.
    """
    config.addinivalue_line(
        "markers",
        "live: requires a running Wayfinder app (skipped unless WAYFINDER_LIVE=1)",
    )


# Add src to path for imports
project_root = Path(__file__).parent.parent
src_dir = project_root / "src"
if str(src_dir) not in sys.path:
    sys.path.insert(0, str(src_dir))
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))


# =============================================================================
# Directory Fixtures
# =============================================================================

@pytest.fixture
def temp_dir() -> Generator[Path, None, None]:
    """Provide a temporary directory for test files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def temp_config_dir(temp_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Provide a temporary config directory and patch HOME."""
    config_dir = temp_dir / ".config" / "wayfinder-aura"
    config_dir.mkdir(parents=True)

    # Patch home directory for config tests
    monkeypatch.setenv("HOME", str(temp_dir))

    # CONFIG_DIR/CONFIG_FILE are computed at import time from Path.home(), so if
    # wayfinder.config was already imported (any earlier test), they still point
    # at the REAL ~/.config/wayfinder-aura/config.json and the HOME patch above
    # does nothing — a full-suite run once clobbered the developer's real config
    # this way. Patch the module attributes directly so config tests can never
    # escape the temp dir, regardless of import order.
    from wayfinder import config as _cfg
    monkeypatch.setattr(_cfg, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(_cfg, "CONFIG_FILE", config_dir / "config.json")

    return config_dir


@pytest.fixture
def mock_online_license():
    """Make store_license / load_stored_license succeed without a network: activate_online returns
    a valid premium result + a token, and _verify_token accepts any token as a valid premium
    payload. These tests exercise the gating logic; the real online activation + Ed25519 verify
    path is covered by the licensing-service e2e tests."""
    import time
    from unittest.mock import patch
    from wayfinder.license import LicenseInfo, PREMIUM_FEATURES

    features = list(PREMIUM_FEATURES)

    def _activate(key, machine_id):
        return (
            LicenseInfo(
                is_valid=True,
                is_premium=True,
                license_key=key,
                machine_id=machine_id,
                plan="ultra",
                features=features,
                token="TEST.TOKEN",
            ),
            "TEST.TOKEN",
            True,
        )

    def _verify(token, machine_id=None):
        if not token:
            return None
        now = int(time.time())
        return {
            "plan": "ultra",
            "features": features,
            "machineId": machine_id,
            "iat": now,
            "exp": now + 10 ** 9,
            "v": 2,
        }

    with patch("wayfinder.license.activate_online", _activate), patch(
        "wayfinder.license._verify_token", _verify
    ):
        yield


# =============================================================================
# Configuration Fixtures
# =============================================================================

@pytest.fixture
def sample_config() -> dict:
    """Provide a sample configuration dictionary."""
    return {
        "whisper_binary": "/usr/bin/whisper-cli",
        "model_path": "/tmp/models/ggml-small.bin",
        "hotkey_key": 67,
        "hotkey_modifiers": [],
        "audio_device": None,
        "sample_rate": 16000,
        "threads": 4,
        "timeout": 60,
        "typing_speed": "instant",
        "use_gpu": False,
    }


@pytest.fixture
def full_config() -> dict:
    """Provide a full configuration dictionary matching DEFAULT_CONFIG."""
    from wayfinder.config import DEFAULT_CONFIG
    config = DEFAULT_CONFIG.copy()
    # Override paths for testing
    config["whisper_binary"] = "/usr/bin/whisper-cli"
    config["model_path"] = "/tmp/models/ggml-small.bin"
    config["llama_cpp_model_path"] = "/tmp/models/test-model.gguf"
    config["llama_cpp_binary"] = "/usr/bin/llama-cli"
    config["use_gpu"] = False
    return config


# =============================================================================
# Audio Fixtures
# =============================================================================

@pytest.fixture
def sample_audio_file(temp_dir: Path) -> Path:
    """Create a minimal valid WAV file for testing (1 second silence)."""
    audio_path = temp_dir / "test_audio.wav"

    sample_rate = 16000
    duration = 1.0
    num_samples = int(sample_rate * duration)

    with wave.open(str(audio_path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)  # 16-bit
        wav.setframerate(sample_rate)
        silence = struct.pack("<" + "h" * num_samples, *([0] * num_samples))
        wav.writeframes(silence)

    return audio_path


@pytest.fixture
def short_audio_file(temp_dir: Path) -> Path:
    """Create a very short WAV file (0.1 seconds) for edge case testing."""
    audio_path = temp_dir / "short_audio.wav"

    sample_rate = 16000
    num_samples = int(sample_rate * 0.1)

    with wave.open(str(audio_path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        silence = struct.pack("<" + "h" * num_samples, *([0] * num_samples))
        wav.writeframes(silence)

    return audio_path


# =============================================================================
# Mock Subprocess Fixtures
# =============================================================================

@pytest.fixture
def mock_whisper_success():
    """Mock subprocess.run to simulate successful whisper-cli transcription."""
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "Hello, this is a test transcription."
    mock_result.stderr = ""

    with patch("subprocess.run", return_value=mock_result) as mock_run:
        yield mock_run


@pytest.fixture
def mock_whisper_failure():
    """Mock subprocess.run to simulate failed whisper-cli transcription."""
    mock_result = MagicMock()
    mock_result.returncode = 1
    mock_result.stdout = ""
    mock_result.stderr = "Error: model file not found"

    with patch("subprocess.run", return_value=mock_result) as mock_run:
        yield mock_run


@pytest.fixture
def mock_ydotool_success():
    """Mock subprocess.run to simulate successful ydotool text injection.

    Forces Linux platform so inject_text dispatches to the ydotool backend,
    and mocks check_ydotool_ready so the pre-flight check passes on any OS.
    """
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = ""
    mock_result.stderr = ""

    with patch("wayfinder.core.injector.sys") as mock_sys, \
         patch("wayfinder.utils.platform.get_text_injector", return_value="ydotool"), \
         patch("wayfinder.core.injector.check_ydotool_ready", return_value=(True, "mocked")), \
         patch("wayfinder.core.injector.subprocess.run", return_value=mock_result) as mock_run:
        mock_sys.platform = "linux"
        yield mock_run


@pytest.fixture
def mock_ydotool_failure():
    """Mock subprocess.run to simulate failed ydotool text injection.

    Forces Linux platform so inject_text dispatches to the ydotool backend,
    and mocks check_ydotool_ready so the pre-flight check passes.
    """
    mock_result = MagicMock()
    mock_result.returncode = 1
    mock_result.stdout = ""
    mock_result.stderr = "Error: ydotool daemon not running"

    with patch("wayfinder.core.injector.sys") as mock_sys, \
         patch("wayfinder.utils.platform.get_text_injector", return_value="ydotool"), \
         patch("wayfinder.core.injector.check_ydotool_ready", return_value=(True, "mocked")), \
         patch("wayfinder.core.injector.subprocess.run", return_value=mock_result) as mock_run:
        mock_sys.platform = "linux"
        yield mock_run


@pytest.fixture
def mock_llama_success():
    """Mock subprocess.run to simulate successful llama.cpp post-processing."""
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "Cleaned text: This is the cleaned output."
    mock_result.stderr = ""

    with patch("subprocess.run", return_value=mock_result) as mock_run:
        yield mock_run


# =============================================================================
# Voice Profile Fixtures
# =============================================================================

@pytest.fixture
def voice_profile_dir(temp_dir: Path) -> Path:
    """Create a temporary directory for voice profile data."""
    profile_dir = temp_dir / ".config" / "wayfinder-aura"
    profile_dir.mkdir(parents=True)
    return profile_dir


@pytest.fixture
def sample_voice_profile_data() -> dict:
    """Provide sample voice profile data.

    The history repeats the distinctive terms (``frontend``, ``refactoring``,
    ``endpoint``, ``TypeScript``) so they survive the distinctiveness extractor
    (which requires MIN_OCCURRENCES>=2 and is re-run on load to self-heal stale
    lists). ``branch``/``main`` are ordinary English (STOP_WORDS) and are meant
    to be filtered out.
    """
    return {
        "history": [
            {"text": "I'm working on the frontend refactoring today.", "timestamp": 1700000000, "word_count": 7},
            {"text": "Let me push the frontend branch to main.", "timestamp": 1700000100, "word_count": 8},
            {"text": "The API endpoint needs refactoring for TypeScript.", "timestamp": 1700000200, "word_count": 7},
            {"text": "Can you review my TypeScript endpoint changes?", "timestamp": 1700000300, "word_count": 7},
            {"text": "I should finish the frontend refactoring in TypeScript.", "timestamp": 1700000400, "word_count": 8},
        ],
        "profile": {
            "summary": "Technical speaker who discusses software development, git workflows, and code reviews.",
            "vocabulary": ["frontend", "refactoring", "TypeScript", "endpoint"],
            "generated_at": 1700000500,
            "samples_used": 5,
        },
        "transcriptions_since_regen": 0,
    }


# =============================================================================
# Environment Fixtures
# =============================================================================

@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch):
    """Clean up environment for each test."""
    # Remove any API keys that might affect tests
    for key in ["ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GROQ_API_KEY"]:
        monkeypatch.delenv(key, raising=False)

    # Remove AppImage/Flatpak env vars to ensure clean test state
    for key in ["APPIMAGE", "APPDIR", "FLATPAK_ID", "WAYFINDER_FLATPAK"]:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def appimage_env(monkeypatch: pytest.MonkeyPatch, temp_dir: Path):
    """Set up environment variables to simulate running from an AppImage."""
    appdir = temp_dir / "AppDir"
    appdir.mkdir()
    (appdir / "usr" / "bin").mkdir(parents=True)
    (appdir / "usr" / "lib").mkdir(parents=True)
    (appdir / "usr" / "share" / "whisper-models").mkdir(parents=True)
    (appdir / "usr" / "share" / "llm-models").mkdir(parents=True)
    (appdir / "usr" / "bin" / "wayfinder-aura").write_text("#!/bin/sh\n")

    monkeypatch.setenv("APPIMAGE", str(temp_dir / "WayfinderAura.AppImage"))
    monkeypatch.setenv("APPDIR", str(appdir))

    return appdir


@pytest.fixture
def flatpak_env(monkeypatch: pytest.MonkeyPatch):
    """Set up environment variables to simulate running in Flatpak."""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("FLATPAK_ID", "io.wayfindercollective.WayfinderAura")
    monkeypatch.setenv("WAYFINDER_FLATPAK", "1")


@pytest.fixture
def wayland_env(monkeypatch: pytest.MonkeyPatch):
    """Set up environment variables to simulate Wayland session."""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "KDE")


@pytest.fixture
def x11_env(monkeypatch: pytest.MonkeyPatch):
    """Set up environment variables to simulate X11 session."""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "GNOME")


# =============================================================================
# Reset global state between tests
# =============================================================================

@pytest.fixture(autouse=True)
def reset_voice_profile():
    """Reset the global voice profile singleton between tests."""
    yield
    try:
        from wayfinder.core.voice_profile import reset_voice_profile
        reset_voice_profile()
    except (ImportError, OSError):
        pass
