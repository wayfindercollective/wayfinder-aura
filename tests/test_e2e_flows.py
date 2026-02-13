"""
End-to-end flow tests for Wayfinder Aura.

These complement test_integration.py with additional pipeline scenarios.
All hardware (sounddevice, subprocess for whisper/ydotool) is mocked.
"""

import json
import os
import struct
import subprocess
import tempfile
import wave
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

from wayfinder.state import AppState, get_next_state
from wayfinder.config import DEFAULT_CONFIG, load_config, save_config


# =============================================================================
# Helpers
# =============================================================================

def _make_wav(path: Path, duration: float = 1.0, sample_rate: int = 16000) -> Path:
    """Create a minimal valid WAV file."""
    num_samples = int(sample_rate * duration)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(struct.pack("<" + "h" * num_samples, *([0] * num_samples)))
    return path


def _mock_subprocess_whisper(text: str = "Hello world"):
    """Return a mock subprocess result simulating whisper-cli output."""
    return subprocess.CompletedProcess(
        args=["whisper-cli"], returncode=0, stdout=text, stderr=""
    )


def _mock_subprocess_ydotool():
    """Return a mock subprocess result simulating ydotool success."""
    return subprocess.CompletedProcess(
        args=["ydotool"], returncode=0, stdout="", stderr=""
    )


def _mock_subprocess_llama(text: str = "Cleaned text."):
    """Return a mock subprocess result simulating llama.cpp output."""
    return subprocess.CompletedProcess(
        args=["llama-cli"], returncode=0, stdout=text, stderr=""
    )


# =============================================================================
# Full Dictation Pipeline
# =============================================================================


class TestFullDictationPipeline:
    """Test the complete IDLE -> RECORDING -> PROCESSING -> PASTING -> IDLE flow."""

    def test_state_machine_full_cycle(self):
        """State machine should traverse the full dictation lifecycle."""
        state = AppState.IDLE

        state = get_next_state(state, "hotkey")
        assert state == AppState.RECORDING

        state = get_next_state(state, "hotkey")
        assert state == AppState.PROCESSING

        state = get_next_state(state, "transcribed")
        assert state == AppState.PASTING

        state = get_next_state(state, "pasted")
        assert state == AppState.IDLE

    @patch("subprocess.run")
    def test_transcribe_then_inject(self, mock_run,
                                     temp_dir: Path, sample_audio_file: Path, sample_config: dict):
        """Full flow: transcribe audio, then inject the result text."""
        from wayfinder.core.transcriber import transcribe_with_config
        from wayfinder.core.injector import inject_text

        # First call: whisper transcription; second call: ydotool injection
        mock_run.side_effect = [
            _mock_subprocess_whisper("This is a test."),
            _mock_subprocess_ydotool(),
        ]

        # Create fake binary and model files so path existence checks pass
        fake_binary = temp_dir / "whisper-cli"
        fake_binary.touch()
        fake_model = temp_dir / "model.bin"
        fake_model.touch()

        # Disable post-processing so the transcribed text is returned directly
        config = sample_config.copy()
        config["whisper_binary"] = str(fake_binary)
        config["model_path"] = str(fake_model)
        config["post_processing_enabled"] = False
        config["output_tone"] = "professional"  # non-minimal to skip regex path too

        state = AppState.IDLE
        state = get_next_state(state, "hotkey")  # -> RECORDING
        state = get_next_state(state, "hotkey")  # -> PROCESSING

        result = transcribe_with_config(str(sample_audio_file), config)
        assert len(result) > 0

        state = get_next_state(state, "transcribed")  # -> PASTING
        assert state == AppState.PASTING

        inject_text(result, typing_speed="instant")

        state = get_next_state(state, "pasted")  # -> IDLE
        assert state == AppState.IDLE

    @patch("subprocess.run")
    def test_stop_event_also_transitions_to_processing(self, mock_run, sample_audio_file, sample_config):
        """The 'stop' event should also move RECORDING -> PROCESSING."""
        state = get_next_state(AppState.RECORDING, "stop")
        assert state == AppState.PROCESSING


# =============================================================================
# Chunked Recording Pipeline
# =============================================================================


class TestChunkedRecordingPipeline:
    """Test chunked recording accumulation and transcription."""

    @patch("wayfinder.core.recorder.sd")
    @patch("subprocess.run")
    def test_multiple_chunks_transcribed(self, mock_run, mock_sd,
                                          temp_dir: Path, sample_config: dict):
        """Simulates recording multiple chunks and transcribing each."""
        from wayfinder.core.transcriber import transcribe_with_config

        chunk_texts = ["Hello", "world", "how are you"]
        chunks = []
        for i, text in enumerate(chunk_texts):
            path = _make_wav(temp_dir / f"chunk_{i}.wav", duration=0.5)
            chunks.append(path)

        # Create fake binary and model files
        fake_binary = temp_dir / "whisper-cli"
        fake_binary.touch()
        fake_model = temp_dir / "model.bin"
        fake_model.touch()

        # Disable post-processing for clean results
        config = sample_config.copy()
        config["whisper_binary"] = str(fake_binary)
        config["model_path"] = str(fake_model)
        config["post_processing_enabled"] = False
        config["output_tone"] = "professional"

        # Each call to subprocess.run returns next chunk transcription
        mock_run.side_effect = [_mock_subprocess_whisper(t) for t in chunk_texts]

        results = []
        for chunk_path in chunks:
            result = transcribe_with_config(str(chunk_path), config)
            results.append(result)

        combined = " ".join(results)
        # Case-insensitive check since clean_whisper_artifacts may adjust capitalization
        assert "hello" in combined.lower()
        assert "world" in combined.lower()

    def test_chunked_config_defaults(self):
        """DEFAULT_CONFIG should have chunked mode settings."""
        assert "chunked_mode" in DEFAULT_CONFIG
        assert "chunk_duration" in DEFAULT_CONFIG
        assert "chunk_overlap" in DEFAULT_CONFIG
        assert DEFAULT_CONFIG["chunked_mode"] is True
        assert DEFAULT_CONFIG["chunk_duration"] > 0


# =============================================================================
# Remote Mode Pipeline
# =============================================================================


class TestRemoteModePipeline:
    """Test cloud API transcription paths (Groq/OpenAI)."""

    def test_groq_config_defaults_present(self):
        """DEFAULT_CONFIG should have Groq API settings."""
        assert "groq_whisper_model" in DEFAULT_CONFIG
        assert "groq_api_key" in DEFAULT_CONFIG
        assert DEFAULT_CONFIG["groq_whisper_model"] == "whisper-large-v3"

    def test_openai_config_defaults_present(self):
        """DEFAULT_CONFIG should have OpenAI API settings."""
        assert "openai_api_key" in DEFAULT_CONFIG
        assert "openai_model" in DEFAULT_CONFIG
        assert "openai_base_url" in DEFAULT_CONFIG


# =============================================================================
# Style Change Mid-Session
# =============================================================================


class TestStyleChangeMidSession:
    """Test changing output_tone between dictations."""

    def test_tone_change_preserved_in_config(self, temp_config_dir: Path):
        """Changing output_tone should persist across save/load."""
        config = load_config()
        config["output_tone"] = "casual"
        save_config(config)

        reloaded = load_config()
        assert reloaded["output_tone"] == "casual"

    def test_all_tones_are_valid(self):
        """All 5 tone presets should be accepted by process_with_config."""
        from wayfinder.core.postprocessor import get_tone_options

        tones = get_tone_options()
        assert len(tones) == 5
        tone_ids = {t["id"] for t in tones}
        assert tone_ids == {"minimal", "professional", "casual", "dev", "personal"}

    def test_tone_options_have_required_fields(self):
        """Each tone option should have id, name, icon, description."""
        from wayfinder.core.postprocessor import get_tone_options

        for tone in get_tone_options():
            assert "id" in tone
            assert "name" in tone
            assert "icon" in tone
            assert "description" in tone
            assert isinstance(tone["id"], str)
            assert isinstance(tone["name"], str)


# =============================================================================
# Error Recovery
# =============================================================================


class TestErrorRecovery:
    """Test that errors return the state machine to IDLE."""

    def test_transcription_error_returns_to_idle(self):
        """PROCESSING + error -> IDLE."""
        state = get_next_state(AppState.PROCESSING, "error")
        assert state == AppState.IDLE

    def test_injection_error_returns_to_idle(self):
        """PASTING + error -> IDLE."""
        state = get_next_state(AppState.PASTING, "error")
        assert state == AppState.IDLE

    @patch("subprocess.run")
    def test_transcription_failure_raises(self, mock_run, sample_audio_file: Path, sample_config: dict):
        """A failed whisper process should raise TranscriptionError."""
        from wayfinder.core.transcriber import transcribe_with_config, TranscriptionError

        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="model not found")

        state = get_next_state(AppState.IDLE, "hotkey")   # -> RECORDING
        state = get_next_state(state, "hotkey")            # -> PROCESSING

        with pytest.raises(TranscriptionError):
            transcribe_with_config(str(sample_audio_file), sample_config)

        # After catching the error, transition to IDLE
        state = get_next_state(state, "error")
        assert state == AppState.IDLE

    @patch("wayfinder.core.injector.subprocess.run")
    def test_injection_failure_raises(self, mock_run):
        """A failed ydotool should raise InjectionError."""
        from wayfinder.core.injector import inject_text, InjectionError

        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="daemon not running")

        state = AppState.PASTING
        with pytest.raises(InjectionError):
            inject_text("Some text", typing_speed="instant")

        state = get_next_state(state, "error")
        assert state == AppState.IDLE


# =============================================================================
# Multiple Consecutive Dictations
# =============================================================================


class TestConsecutiveDictations:
    """Test running several dictation cycles in sequence."""

    def test_three_consecutive_dictation_cycles(self):
        """State machine should handle three full cycles without getting stuck."""
        for cycle in range(3):
            state = AppState.IDLE

            state = get_next_state(state, "hotkey")
            assert state == AppState.RECORDING, f"Cycle {cycle}: failed IDLE->RECORDING"

            state = get_next_state(state, "hotkey")
            assert state == AppState.PROCESSING, f"Cycle {cycle}: failed RECORDING->PROCESSING"

            state = get_next_state(state, "transcribed")
            assert state == AppState.PASTING, f"Cycle {cycle}: failed PROCESSING->PASTING"

            state = get_next_state(state, "pasted")
            assert state == AppState.IDLE, f"Cycle {cycle}: failed PASTING->IDLE"

    def test_error_mid_cycle_then_restart(self):
        """After an error resets to IDLE, the next cycle should work fine."""
        # First cycle: error during processing
        state = AppState.IDLE
        state = get_next_state(state, "hotkey")   # RECORDING
        state = get_next_state(state, "hotkey")    # PROCESSING
        state = get_next_state(state, "error")     # IDLE (error recovery)
        assert state == AppState.IDLE

        # Second cycle: normal flow
        state = get_next_state(state, "hotkey")
        assert state == AppState.RECORDING
        state = get_next_state(state, "hotkey")
        assert state == AppState.PROCESSING
        state = get_next_state(state, "transcribed")
        assert state == AppState.PASTING
        state = get_next_state(state, "pasted")
        assert state == AppState.IDLE


# =============================================================================
# Config Persistence Round-Trip
# =============================================================================


class TestConfigPersistenceRoundTrip:
    """Test saving and reloading various config settings."""

    def test_custom_values_survive_round_trip(self, temp_config_dir: Path):
        """Custom settings should persist through save/load."""
        config = load_config()
        config["threads"] = 8
        config["beam_size"] = 3
        config["typing_speed"] = "slow"
        config["output_tone"] = "dev"
        config["use_gpu"] = False
        save_config(config)

        reloaded = load_config()
        assert reloaded["threads"] == 8
        assert reloaded["beam_size"] == 3
        assert reloaded["typing_speed"] == "slow"
        assert reloaded["output_tone"] == "dev"
        assert reloaded["use_gpu"] is False

    def test_new_keys_merged_with_defaults(self, temp_config_dir: Path):
        """Saved config with missing keys should be filled from defaults on load."""
        # Save a partial config
        partial = {"threads": 16}
        save_config(partial)

        reloaded = load_config()
        # The saved key is preserved
        assert reloaded["threads"] == 16
        # Missing keys come from DEFAULT_CONFIG
        assert "whisper_binary" in reloaded
        assert "hotkey_key" in reloaded
        assert reloaded["hotkey_key"] == DEFAULT_CONFIG["hotkey_key"]

    def test_config_file_is_valid_json(self, temp_dir: Path):
        """Saved config file should be valid, readable JSON."""
        import wayfinder.config as cfg_mod

        test_config_dir = temp_dir / "config_json_test"
        test_config_dir.mkdir(parents=True)
        test_config_file = test_config_dir / "config.json"

        original_dir = cfg_mod.CONFIG_DIR
        original_file = cfg_mod.CONFIG_FILE
        try:
            cfg_mod.CONFIG_DIR = test_config_dir
            cfg_mod.CONFIG_FILE = test_config_file

            config = load_config()
            config["test_key"] = "test_value"
            save_config(config)

            assert test_config_file.exists()

            with open(test_config_file) as f:
                data = json.load(f)
            assert isinstance(data, dict)
            assert data["test_key"] == "test_value"
        finally:
            cfg_mod.CONFIG_DIR = original_dir
            cfg_mod.CONFIG_FILE = original_file
