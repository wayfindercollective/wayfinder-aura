"""
Integration tests for end-to-end voice dictation flow.

These tests verify that the major components work together correctly.
They use mocks for hardware-dependent functionality (audio, display).
"""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class TestE2ETranscriptionFlow:
    """Test end-to-end transcription workflow."""

    @patch("wayfinder.core.recorder.sd")
    @patch("subprocess.run")
    def test_record_and_transcribe_flow(self, mock_run, mock_sd, sample_audio_file: Path, sample_config: dict):
        """Test recording audio and transcribing it."""
        from wayfinder.core.recorder import AudioRecorder
        from wayfinder.core.transcriber import transcribe_with_config

        # This test exercises the whisper-cli (subprocess) path — pin it to CLI mode
        # so it doesn't route through a real whisper-server (server mode is now the
        # config default).
        sample_config = dict(sample_config)
        sample_config["whisper_server_mode"] = False

        # Mock subprocess for whisper-cli
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="This is a test transcription",
            stderr="",
        )

        # Create recorder
        recorder = AudioRecorder()
        assert recorder is not None

        # Mock Path.exists() so WhisperCppBackend doesn't fail on binary/model checks
        original_exists = Path.exists
        def mock_exists(self):
            path_str = str(self)
            if "whisper" in path_str or "ggml" in path_str:
                return True
            return original_exists(self)

        with patch.object(Path, "exists", mock_exists):
            result = transcribe_with_config(str(sample_audio_file), sample_config)

        assert "transcription" in result.lower() or "test" in result.lower()

    @patch("wayfinder.core.recorder.sd")
    def test_state_transitions_during_recording(self, mock_sd):
        """Test that state transitions happen correctly during recording."""
        from wayfinder.state import AppState, get_next_state

        # Simulate the state machine flow
        state = AppState.IDLE

        # User presses hotkey to start recording
        state = get_next_state(state, "hotkey")
        assert state == AppState.RECORDING

        # User presses hotkey again to stop
        state = get_next_state(state, "hotkey")
        assert state == AppState.PROCESSING

        # Transcription completes
        state = get_next_state(state, "transcribed")
        assert state == AppState.PASTING

        # Text is pasted
        state = get_next_state(state, "pasted")
        assert state == AppState.IDLE


class TestConfigPersistence:
    """Test configuration persistence across sessions."""

    def test_config_survives_reload(self, temp_config_dir: Path):
        """Test that config changes persist across loads."""
        from wayfinder.config import load_config, save_config

        # Load and modify config
        config = load_config()
        config["threads"] = 16
        config["typing_speed"] = "slow"
        save_config(config)

        # Simulate app restart by reloading
        reloaded = load_config()

        assert reloaded["threads"] == 16
        assert reloaded["typing_speed"] == "slow"


class TestLicenseIntegration:
    """Test license system integration with features."""

    def test_feature_gate_singleton(self, temp_config_dir: Path):
        """Test that get_feature_gate returns singleton."""
        from wayfinder.license import get_feature_gate

        gate1 = get_feature_gate()
        gate2 = get_feature_gate()

        assert gate1 is gate2

    def test_license_activation_flow(self, temp_config_dir: Path, mock_online_license):
        """Test complete license activation flow."""
        from wayfinder.license import get_feature_gate, PREMIUM_FEATURES

        gate = get_feature_gate()

        # Initially not premium
        initial_premium = gate.is_premium

        key = "WF-TEST-ONLINE-ACTIVATION"
        result = gate.activate(key)

        assert result.is_valid
        assert gate.is_premium

        # Verify premium features work
        for feature_id in PREMIUM_FEATURES:
            assert gate.has_feature(feature_id)


class TestHotkeySocketCommunication:
    """Test socket-based hotkey communication."""

    def test_socket_path_defined(self):
        """Test that socket path is properly defined."""
        from wayfinder.config import SOCKET_PATH

        assert SOCKET_PATH is not None
        assert "/tmp/" in SOCKET_PATH or "wayfinder" in SOCKET_PATH.lower()


class TestAudioPreprocessingPipeline:
    """Test audio preprocessing pipeline."""

    def test_preprocessing_levels(self):
        """Test all preprocessing levels work."""
        import numpy as np
        from wayfinder.core.recorder import preprocess_audio

        # Create test audio
        audio = np.random.randn(16000).astype(np.float32) * 0.1

        levels = ["off", "light", "medium", "heavy"]
        for level in levels:
            result = preprocess_audio(audio, level)
            assert result is not None
            assert len(result) > 0


class TestErrorRecovery:
    """Test error recovery scenarios."""

    @patch("subprocess.run")
    def test_transcription_error_handled(self, mock_run, sample_audio_file: Path, sample_config: dict):
        """Test that transcription errors are properly raised."""
        from wayfinder.core.transcriber import transcribe_with_config, TranscriptionError

        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="CUDA out of memory",
        )

        with pytest.raises(TranscriptionError):
            transcribe_with_config(str(sample_audio_file), sample_config)

    def test_invalid_audio_path_handled(self, sample_config: dict):
        """Test handling of non-existent audio files."""
        from wayfinder.core.transcriber import transcribe_with_config, TranscriptionError

        with pytest.raises((TranscriptionError, FileNotFoundError, Exception)):
            transcribe_with_config("/nonexistent/audio.wav", sample_config)


class TestModuleCompatibility:
    """Test that modular structure works correctly."""

    def test_package_imports(self):
        """Test that package imports work."""
        from wayfinder import config, state
        from wayfinder.core import recorder, transcriber

        assert hasattr(config, "load_config")
        assert hasattr(state, "AppState")
        assert hasattr(recorder, "AudioRecorder")
        assert hasattr(transcriber, "transcribe_with_config")

    def test_core_module_exports(self):
        """Test that core module exports work."""
        from wayfinder.core import (
            AudioRecorder,
            ChunkedRecorder,
            transcribe_with_config,
            TranscriptionError,
        )

        assert AudioRecorder is not None
        assert ChunkedRecorder is not None
        assert transcribe_with_config is not None
        assert TranscriptionError is not None
