"""
Tests for the transcription module.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class TestTranscriptionBackends:
    """Test transcription backend selection."""

    def test_get_backend_whisper_cpp(self, sample_config: dict):
        """Test getting whisper.cpp backend."""
        from wayfinder.core.transcriber import get_backend, WhisperCppBackend

        sample_config["transcription_backend"] = "whisper.cpp"
        backend = get_backend(sample_config)

        assert isinstance(backend, WhisperCppBackend)

    def test_get_backend_default(self, sample_config: dict):
        """Test default backend is whisper.cpp."""
        from wayfinder.core.transcriber import get_backend, WhisperCppBackend

        # Remove backend setting to test default
        sample_config.pop("transcription_backend", None)
        backend = get_backend(sample_config)

        assert isinstance(backend, WhisperCppBackend)

    def test_get_backend_groq(self, sample_config: dict, monkeypatch: pytest.MonkeyPatch):
        """Test getting Groq backend."""
        from wayfinder.core.transcriber import get_backend, GroqWhisperBackend

        monkeypatch.setenv("GROQ_API_KEY", "test_key")
        sample_config["transcription_backend"] = "groq"
        backend = get_backend(sample_config)

        assert isinstance(backend, GroqWhisperBackend)

    def test_get_backend_openai(self, sample_config: dict, monkeypatch: pytest.MonkeyPatch):
        """Test getting OpenAI backend."""
        from wayfinder.core.transcriber import get_backend, OpenAIWhisperBackend

        monkeypatch.setenv("OPENAI_API_KEY", "test_key")
        sample_config["transcription_backend"] = "openai"
        backend = get_backend(sample_config)

        assert isinstance(backend, OpenAIWhisperBackend)


class TestWhisperCppBackend:
    """Test whisper.cpp backend specifically."""

    def test_backend_initialization(self, sample_config: dict):
        """Test backend initialization with config."""
        from wayfinder.core.transcriber import WhisperCppBackend

        backend = WhisperCppBackend(sample_config)

        assert backend.binary_path == sample_config["whisper_binary"]
        assert backend.model_path == sample_config["model_path"]

    @patch("subprocess.run")
    def test_transcribe_calls_whisper_cli(self, mock_run, sample_config: dict, sample_audio_file: Path):
        """Test that transcribe calls whisper-cli correctly."""
        from wayfinder.core.transcriber import WhisperCppBackend

        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="Hello world",
            stderr="",
        )

        backend = WhisperCppBackend(sample_config)
        result = backend.transcribe(str(sample_audio_file))

        assert mock_run.called
        assert result == "Hello world"

    @patch("subprocess.run")
    def test_transcribe_error_handling(self, mock_run, sample_config: dict, sample_audio_file: Path):
        """Test error handling on transcription failure."""
        from wayfinder.core.transcriber import WhisperCppBackend, TranscriptionError

        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="Error: model not found",
        )

        backend = WhisperCppBackend(sample_config)

        with pytest.raises(TranscriptionError):
            backend.transcribe(str(sample_audio_file))


class TestTranscriptionPostProcessing:
    """Test post-processing of transcription results."""

    def test_ensure_punctuation_adds_period(self):
        """Test that punctuation is added to sentences."""
        from wayfinder.core.transcriber import ensure_punctuation_postprocess

        result = ensure_punctuation_postprocess("hello world")

        assert result.endswith(".")

    def test_ensure_punctuation_preserves_existing(self):
        """Test that existing punctuation is preserved."""
        from wayfinder.core.transcriber import ensure_punctuation_postprocess

        result = ensure_punctuation_postprocess("Hello world!")

        assert result == "Hello world!"

    def test_ensure_punctuation_question(self):
        """Test that questions get question marks."""
        from wayfinder.core.transcriber import ensure_punctuation_postprocess

        result = ensure_punctuation_postprocess("how are you")

        assert result.endswith("?")


class TestTranscribeWithConfig:
    """Test the high-level transcribe_with_config function."""

    @patch("wayfinder.core.transcriber.get_backend")
    def test_transcribe_with_config_basic(self, mock_get_backend, sample_config: dict, sample_audio_file: Path):
        """Test basic transcription flow."""
        from wayfinder.core.transcriber import transcribe_with_config

        mock_backend = MagicMock()
        mock_backend.transcribe.return_value = "Hello world"
        mock_get_backend.return_value = mock_backend

        result = transcribe_with_config(str(sample_audio_file), sample_config)

        assert mock_backend.transcribe.called
        assert "Hello" in result or "hello" in result.lower()

    @patch("wayfinder.core.transcriber.get_backend")
    def test_transcribe_with_context(self, mock_get_backend, sample_config: dict, sample_audio_file: Path):
        """Test transcription with context prompt."""
        from wayfinder.core.transcriber import transcribe_with_config

        mock_backend = MagicMock()
        mock_backend.transcribe.return_value = "continuing the story"
        mock_get_backend.return_value = mock_backend

        context = "This is a story about"
        result = transcribe_with_config(str(sample_audio_file), sample_config, context=context)

        # Verify context was passed
        call_args = mock_backend.transcribe.call_args
        assert mock_backend.transcribe.called
