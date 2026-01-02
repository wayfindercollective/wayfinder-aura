"""
Pytest configuration and fixtures for Wayfinder Aura tests.
"""

import os
import sys
import tempfile
from pathlib import Path
from typing import Generator

import pytest

# Add src to path for imports
project_root = Path(__file__).parent.parent
src_dir = project_root / "src"
if str(src_dir) not in sys.path:
    sys.path.insert(0, str(src_dir))
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))


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
    
    return config_dir


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
def sample_audio_file(temp_dir: Path) -> Path:
    """Create a minimal valid WAV file for testing."""
    import struct
    import wave
    
    audio_path = temp_dir / "test_audio.wav"
    
    # Create a simple 1-second silent WAV file
    sample_rate = 16000
    duration = 1.0
    num_samples = int(sample_rate * duration)
    
    with wave.open(str(audio_path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)  # 16-bit
        wav.setframerate(sample_rate)
        
        # Write silence (all zeros)
        silence = struct.pack("<" + "h" * num_samples, *([0] * num_samples))
        wav.writeframes(silence)
    
    return audio_path


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch):
    """Clean up environment for each test."""
    # Remove any API keys that might affect tests
    for key in ["ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GROQ_API_KEY"]:
        monkeypatch.delenv(key, raising=False)
    
    # Set a development license secret for tests
    monkeypatch.setenv("WAYFINDER_LICENSE_SECRET", "test_secret_for_unit_tests_only")
