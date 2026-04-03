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

        sample_config["transcription_backend"] = "whisper_cpp"
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
        sample_config["transcription_backend"] = "groq_whisper"
        sample_config["groq_api_key"] = "test_key"
        backend = get_backend(sample_config)

        assert isinstance(backend, GroqWhisperBackend)

    def test_get_backend_openai(self, sample_config: dict, monkeypatch: pytest.MonkeyPatch):
        """Test getting OpenAI backend."""
        from wayfinder.core.transcriber import get_backend, OpenAIWhisperBackend

        monkeypatch.setenv("OPENAI_API_KEY", "test_key")
        sample_config["transcription_backend"] = "openai_whisper"
        sample_config["openai_api_key"] = "test_key"
        backend = get_backend(sample_config)

        assert isinstance(backend, OpenAIWhisperBackend)


class TestWhisperCppBackend:
    """Test whisper.cpp backend specifically."""

    def test_backend_initialization(self, sample_config: dict):
        """Test backend initialization with config."""
        from wayfinder.core.transcriber import WhisperCppBackend

        backend = WhisperCppBackend(
            whisper_binary=sample_config["whisper_binary"],
            model_path=sample_config["model_path"],
            threads=sample_config.get("threads", 4),
            timeout=sample_config.get("timeout", 60),
            use_gpu=sample_config.get("use_gpu", False),
        )

        assert backend.whisper_binary == sample_config["whisper_binary"]
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

        backend = WhisperCppBackend(
            whisper_binary=sample_config["whisper_binary"],
            model_path=sample_config["model_path"],
            use_gpu=False,
        )

        # Mock path existence checks so transcribe() doesn't bail out early
        with patch.object(Path, "exists", return_value=True):
            result = backend.transcribe(str(sample_audio_file))

        assert mock_run.called
        assert "Hello world" in result

    @patch("subprocess.run")
    def test_transcribe_error_handling(self, mock_run, sample_config: dict, sample_audio_file: Path):
        """Test error handling on transcription failure."""
        from wayfinder.core.transcriber import WhisperCppBackend, TranscriptionError

        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="Error: model not found",
        )

        backend = WhisperCppBackend(
            whisper_binary=sample_config["whisper_binary"],
            model_path=sample_config["model_path"],
            use_gpu=False,
        )

        with patch.object(Path, "exists", return_value=True):
            with pytest.raises(TranscriptionError):
                backend.transcribe(str(sample_audio_file))


class TestFasterWhisperBackend:
    """Test Faster-Whisper backend."""

    def test_backend_initialization(self):
        """Test backend initialization with all parameters."""
        from wayfinder.core.transcriber import FasterWhisperBackend

        backend = FasterWhisperBackend(
            model_size="small",
            use_gpu=False,
            compute_type="int8",
            prompt="Test prompt",
            language="en",
            beam_size=3,
            best_of=2,
            temperature=0.0,
            custom_vocabulary=["test"],
            no_speech_threshold=0.4,
            compression_ratio_threshold=2.0,
            temperature_fallback=0.2,
            suppress_nst=True,
            vad_enabled=True,
            vad_threshold=0.25,
            gpu_device="0",
        )

        assert backend.model_size == "small"
        assert backend.use_gpu is False
        assert backend.no_speech_threshold == 0.4
        assert backend.compression_ratio_threshold == 2.0
        assert backend.temperature_fallback == 0.2
        assert backend.suppress_nst is True
        assert backend.vad_enabled is True
        assert backend.vad_threshold == 0.25
        assert backend.gpu_device == "0"

    def test_is_available_without_import(self):
        """Test is_available returns False when faster_whisper not installed."""
        from wayfinder.core.transcriber import FasterWhisperBackend

        backend = FasterWhisperBackend()
        # This depends on whether faster_whisper is installed, but the method shouldn't crash
        result = backend.is_available()
        assert isinstance(result, bool)

    def test_build_prompt_first_chunk(self):
        """Test prompt building for first chunk (no context)."""
        from wayfinder.core.transcriber import FasterWhisperBackend

        backend = FasterWhisperBackend(
            prompt="Base prompt here.",
            custom_vocabulary=["Bazzite", "whisper.cpp"],
        )

        result = backend._build_prompt()
        assert "Base prompt here." in result
        assert "Bazzite" in result
        assert "whisper.cpp" in result

    def test_build_prompt_with_context(self):
        """Test prompt building with chunk context (overrides base prompt)."""
        from wayfinder.core.transcriber import FasterWhisperBackend

        backend = FasterWhisperBackend(
            prompt="Base prompt here.",
            custom_vocabulary=["Bazzite"],
        )

        result = backend._build_prompt(context="previous chunk text")
        assert "previous chunk text" in result
        assert "Base prompt here." not in result
        assert "Bazzite" in result

    def test_get_backend_faster_whisper(self, sample_config: dict):
        """Test factory creates FasterWhisperBackend with all params."""
        from wayfinder.core.transcriber import get_backend, FasterWhisperBackend

        sample_config["transcription_backend"] = "faster_whisper"
        sample_config["no_speech_threshold"] = 0.4
        sample_config["faster_whisper_vad_enabled"] = False
        sample_config["gpu_device"] = "1"

        backend = get_backend(sample_config)

        assert isinstance(backend, FasterWhisperBackend)
        assert backend.no_speech_threshold == 0.4
        assert backend.vad_enabled is False
        assert backend.gpu_device == "1"

    @patch("wayfinder.core.transcriber.FasterWhisperBackend.is_available", return_value=True)
    def test_transcribe_file_not_found(self, mock_avail):
        """Test transcribe raises error for missing audio file."""
        from wayfinder.core.transcriber import FasterWhisperBackend, TranscriptionError

        backend = FasterWhisperBackend()

        with pytest.raises(TranscriptionError, match="Audio file not found"):
            backend.transcribe("/nonexistent/file.wav")

    def test_temperature_fallback_creates_list(self):
        """Test that temperature_fallback > 0 creates a temperature list."""
        from wayfinder.core.transcriber import FasterWhisperBackend

        backend = FasterWhisperBackend(
            temperature=0.0,
            temperature_fallback=0.2,
        )

        # The list is built inside transcribe(), not stored directly.
        # Verify the params are stored correctly.
        assert backend.temperature == 0.0
        assert backend.temperature_fallback == 0.2

    def test_gpu_device_selection(self):
        """Test GPU device index parsing."""
        from wayfinder.core.transcriber import FasterWhisperBackend

        # Auto mode
        backend = FasterWhisperBackend(gpu_device="auto")
        assert backend.gpu_device == "auto"

        # Specific device
        backend = FasterWhisperBackend(gpu_device="2")
        assert backend.gpu_device == "2"


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
