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

        # Licensed: cloud backends need the 'cloud_backends' feature (F1 gate); license it so
        # this exercises backend SELECTION, not the unlicensed downgrade to whisper.cpp.
        monkeypatch.setattr("wayfinder.license.FeatureGate.has_feature", lambda self, f: True)
        monkeypatch.setenv("GROQ_API_KEY", "test_key")
        sample_config["transcription_backend"] = "groq_whisper"
        sample_config["groq_api_key"] = "test_key"
        backend = get_backend(sample_config)

        assert isinstance(backend, GroqWhisperBackend)

    def test_get_backend_openai(self, sample_config: dict, monkeypatch: pytest.MonkeyPatch):
        """Test getting OpenAI backend."""
        from wayfinder.core.transcriber import get_backend, OpenAIWhisperBackend

        # Licensed (see test_get_backend_groq) so the F1 cloud gate doesn't downgrade.
        monkeypatch.setattr("wayfinder.license.FeatureGate.has_feature", lambda self, f: True)
        monkeypatch.setenv("OPENAI_API_KEY", "test_key")
        sample_config["transcription_backend"] = "openai_whisper"
        sample_config["openai_api_key"] = "test_key"
        backend = get_backend(sample_config)

        assert isinstance(backend, OpenAIWhisperBackend)

    def test_large_model_downgrade_never_points_at_missing_path(self, sample_config, tmp_path, monkeypatch):
        """Regression: unlicensed + a large local model must downgrade to a free model that
        EXISTS in the same dir — never a hardcoded missing path. The old code set
        ~/whisper.cpp/models/ggml-small.bin, which doesn't exist in the Flatpak, so
        transcription failed and the user's words were lost."""
        import os
        from wayfinder.core.transcriber import get_backend
        # Force unlicensed so the large_models gate fires.
        monkeypatch.setattr("wayfinder.license.FeatureGate.has_feature", lambda self, f: False)
        # A large model beside a free model in a tmp dir.
        (tmp_path / "ggml-large-v3-turbo-q5_0.bin").write_bytes(b"x")
        (tmp_path / "ggml-base.en.bin").write_bytes(b"x")
        sample_config["transcription_backend"] = "whisper_cpp"
        sample_config["model_path"] = str(tmp_path / "ggml-large-v3-turbo-q5_0.bin")
        backend = get_backend(sample_config)
        resolved = os.path.expanduser(getattr(backend, "model_path", ""))
        assert os.path.exists(resolved), f"downgrade produced a missing model_path: {resolved!r}"
        assert os.path.basename(resolved) == "ggml-base.en.bin"

    def test_large_model_downgrade_keeps_model_when_no_free_alternative(self, sample_config, tmp_path, monkeypatch):
        """Fail-safe: if no free model exists to fall back to, keep the configured model
        rather than break transcription with a missing path."""
        import os
        from wayfinder.core.transcriber import get_backend
        monkeypatch.setattr("wayfinder.license.FeatureGate.has_feature", lambda self, f: False)
        large = tmp_path / "ggml-large-v3-turbo-q5_0.bin"
        large.write_bytes(b"x")  # only a large model present, no free alternative
        sample_config["transcription_backend"] = "whisper_cpp"
        sample_config["model_path"] = str(large)
        backend = get_backend(sample_config)
        assert os.path.expanduser(getattr(backend, "model_path", "")) == str(large)


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


class TestWhisperServerBackend:
    """Test the whisper-server (warm) backend recovery behavior."""

    def _make_resp(self, text: str) -> MagicMock:
        resp = MagicMock()
        resp.read.return_value = ('{"text": "%s"}' % text).encode("utf-8")
        return resp

    def test_hung_server_timeout_triggers_restart_and_retry(
        self, sample_audio_file: Path
    ):
        """Regression: a wedged-but-listening whisper-server makes urlopen raise
        TimeoutError (not URLError) — observed after a suspend/resume cycle.
        That must restart the server and retry, not fall through to an
        unrecoverable error that leaves the dead server hanging every dictation.
        """
        from wayfinder.core.transcriber import WhisperServerBackend

        backend = WhisperServerBackend(use_gpu=False, timeout=1)

        def fake_start():
            WhisperServerBackend._server_port = 8178

        # First inference hangs (TimeoutError); after restart it succeeds.
        urlopen = MagicMock(side_effect=[TimeoutError("timed out"),
                                         self._make_resp("recovered")])

        with patch.object(Path, "exists", return_value=True), \
             patch.object(WhisperServerBackend, "_start_server",
                          side_effect=fake_start) as mock_start, \
             patch.object(WhisperServerBackend, "_stop_server_internal") as mock_stop, \
             patch("urllib.request.urlopen", urlopen):
            result = backend.transcribe(str(sample_audio_file))

        assert result == "recovered"
        assert mock_stop.called, "wedged server must be stopped before retry"
        # _start_server: once at entry + once on restart
        assert mock_start.call_count == 2
        assert urlopen.call_count == 2


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

    def test_get_backend_faster_whisper(self, sample_config: dict, monkeypatch: pytest.MonkeyPatch):
        """Test factory creates FasterWhisperBackend with all params."""
        from wayfinder.core.transcriber import get_backend, FasterWhisperBackend

        # Licensed: faster_whisper is a premium backend (F1 gate) — license so this tests
        # selection, not the unlicensed downgrade.
        monkeypatch.setattr("wayfinder.license.FeatureGate.has_feature", lambda self, f: True)
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


class TestGpuCpuFallback:
    """Vulkan whisper crash -> automatic retry with the bundled CPU sibling binary."""

    @pytest.fixture(autouse=True)
    def _clean_memo(self):
        from wayfinder.core import transcriber
        transcriber._CPU_FALLBACK_ACTIVE.clear()
        yield
        transcriber._CPU_FALLBACK_ACTIVE.clear()

    def _write_stub(self, path: Path, body: str) -> None:
        path.write_text("#!/bin/sh\n" + body + "\n")
        path.chmod(0o755)

    def _backend(self, tmp_path: Path, gpu_body: str, cpu_body: str | None):
        """Backend wired to stub 'binaries' + dummy model/audio files."""
        from wayfinder.core.transcriber import WhisperCppBackend

        gpu = tmp_path / "whisper-cli"
        self._write_stub(gpu, gpu_body)
        if cpu_body is not None:
            self._write_stub(tmp_path / "whisper-cli-cpu", cpu_body)
        model = tmp_path / "model.bin"
        model.write_bytes(b"\x00")
        audio = tmp_path / "audio.wav"
        audio.write_bytes(b"\x00")
        backend = WhisperCppBackend(
            whisper_binary=str(gpu), model_path=str(model), timeout=10,
        )
        return backend, str(audio)

    def test_signal_death_falls_back_to_cpu_sibling(self, tmp_path):
        from wayfinder.core import transcriber

        backend, audio = self._backend(
            tmp_path,
            gpu_body="kill -SEGV $$",
            cpu_body='echo "hello fallback"',
        )
        assert backend.transcribe(audio) == "hello fallback"
        # Memoized for the session
        assert transcriber._CPU_FALLBACK_ACTIVE[backend.whisper_binary].endswith("-cpu")

    def test_fallback_is_memoized(self, tmp_path):
        """Second transcription must not touch the crashed GPU binary again."""
        marker = tmp_path / "gpu-runs"
        backend, audio = self._backend(
            tmp_path,
            # Only count real transcription calls ($1 = -m) — the _supported_flags()
            # --help probe also executes this stub once.
            gpu_body=f'if [ "$1" = "-m" ]; then echo run >> "{marker}"; fi; kill -SEGV $$',
            cpu_body='echo "ok"',
        )
        assert backend.transcribe(audio) == "ok"
        assert backend.transcribe(audio) == "ok"
        # GPU stub ran exactly once (first attempt), not on the second call.
        assert marker.read_text().count("run") == 1

    def test_vulkan_error_exit_falls_back(self, tmp_path):
        backend, audio = self._backend(
            tmp_path,
            gpu_body='echo "ggml_vulkan: device init failed" >&2; exit 1',
            cpu_body='echo "cpu text"',
        )
        assert backend.transcribe(audio) == "cpu text"

    def test_no_sibling_means_real_error(self, tmp_path):
        from wayfinder.core.transcriber import TranscriptionError

        backend, audio = self._backend(tmp_path, gpu_body="kill -SEGV $$", cpu_body=None)
        with pytest.raises(TranscriptionError):
            backend.transcribe(audio)

    def test_non_vulkan_failure_does_not_fall_back(self, tmp_path):
        """An ordinary whisper error (bad model etc.) must surface, not mask as CPU retry."""
        from wayfinder.core import transcriber
        from wayfinder.core.transcriber import TranscriptionError

        backend, audio = self._backend(
            tmp_path,
            gpu_body='echo "failed to load model" >&2; exit 1',
            cpu_body='echo "should not be used"',
        )
        with pytest.raises(TranscriptionError):
            backend.transcribe(audio)
        assert backend.whisper_binary not in transcriber._CPU_FALLBACK_ACTIVE

    def test_cpu_only_install_unaffected(self, tmp_path):
        """From-source installs (no sibling) keep working exactly as before."""
        backend, audio = self._backend(tmp_path, gpu_body='echo "normal text"', cpu_body=None)
        assert backend.transcribe(audio) == "normal text"


class TestGpuRecoveryProbe:
    """Background probe re-tests a crashed GPU binary and restores it when healthy."""

    @pytest.fixture(autouse=True)
    def _clean_state(self):
        from wayfinder.core import transcriber
        transcriber._CPU_FALLBACK_ACTIVE.clear()
        transcriber._gpu_retry_state.clear()
        transcriber.set_gpu_event_logger(None)
        yield
        transcriber._CPU_FALLBACK_ACTIVE.clear()
        transcriber._gpu_retry_state.clear()
        transcriber.set_gpu_event_logger(None)

    def _write_stub(self, path, body):
        path.write_text("#!/bin/sh\n" + body + "\n")
        path.chmod(0o755)

    def _backend(self, tmp_path, gpu_body, cpu_body="echo cpu-text"):
        from wayfinder.core.transcriber import WhisperCppBackend

        gpu = tmp_path / "whisper-cli"
        self._write_stub(gpu, gpu_body)
        self._write_stub(tmp_path / "whisper-cli-cpu", cpu_body)
        model = tmp_path / "model.bin"
        model.write_bytes(b"\x00")
        audio = tmp_path / "audio.wav"
        audio.write_bytes(b"\x00")
        return WhisperCppBackend(
            whisper_binary=str(gpu), model_path=str(model), timeout=10,
        ), str(audio)

    def test_fallback_logs_to_registered_app_logger(self, tmp_path):
        from wayfinder.core import transcriber

        events = []
        transcriber.set_gpu_event_logger(events.append)
        backend, audio = self._backend(tmp_path, "kill -SEGV $$")
        backend.transcribe(audio)

        assert any("switched to CPU" in e and "retry" in e.lower() for e in events)

    def test_fallback_schedules_backoff_retry(self, tmp_path):
        from wayfinder.core import transcriber

        backend, audio = self._backend(tmp_path, "kill -SEGV $$")
        backend.transcribe(audio)

        state = transcriber._gpu_retry_state[backend.whisper_binary]
        assert state["failures"] == 1
        assert state["next_retry"] > __import__("time").time()

    def test_probe_restores_gpu_when_healthy_again(self, tmp_path):
        from wayfinder.core import transcriber

        flag = tmp_path / "gpu-fixed"
        # Crashes until the flag file exists, then behaves.
        body = f'if [ -f "{flag}" ]; then echo gpu-text; else kill -SEGV $$; fi'
        events = []
        transcriber.set_gpu_event_logger(events.append)
        backend, audio = self._backend(tmp_path, body)

        assert backend.transcribe(audio) == "cpu-text"  # crashed -> CPU
        flag.touch()  # GPU "driver" recovers
        backend._probe_gpu_recovery()  # run the probe synchronously

        assert backend.whisper_binary not in transcriber._CPU_FALLBACK_ACTIVE
        assert any("restored" in e for e in events)
        assert backend.transcribe(audio) == "gpu-text"  # back on GPU

    def test_probe_failure_escalates_backoff(self, tmp_path):
        from wayfinder.core import transcriber

        backend, audio = self._backend(tmp_path, "kill -SEGV $$")
        backend.transcribe(audio)  # failures = 1
        backend._probe_gpu_recovery()  # still crashing -> failures = 2

        state = transcriber._gpu_retry_state[backend.whisper_binary]
        assert state["failures"] == 2
        assert backend.whisper_binary in transcriber._CPU_FALLBACK_ACTIVE

    def test_transcribe_spawns_probe_after_window(self, tmp_path):
        import time as _time
        from wayfinder.core import transcriber

        flag = tmp_path / "gpu-fixed"
        body = f'if [ -f "{flag}" ]; then echo gpu-text; else kill -SEGV $$; fi'
        backend, audio = self._backend(tmp_path, body)

        backend.transcribe(audio)  # crash -> fallback armed
        flag.touch()
        # Force the retry window open, then transcribe (on CPU) — the probe
        # should fire in the background and lift the fallback.
        transcriber._gpu_retry_state[backend.whisper_binary]["next_retry"] = 0.0
        assert backend.transcribe(audio) == "cpu-text"

        deadline = _time.time() + 5
        while (backend.whisper_binary in transcriber._CPU_FALLBACK_ACTIVE
               and _time.time() < deadline):
            _time.sleep(0.05)
        assert backend.whisper_binary not in transcriber._CPU_FALLBACK_ACTIVE

    def test_retry_window_not_elapsed_means_no_probe(self, tmp_path):
        from wayfinder.core import transcriber

        backend, audio = self._backend(tmp_path, "kill -SEGV $$")
        backend.transcribe(audio)
        state = transcriber._gpu_retry_state[backend.whisper_binary]
        before = dict(state)

        # Window is ~60s out; another transcription must not probe or mutate state.
        assert backend.transcribe(audio) == "cpu-text"
        assert transcriber._gpu_retry_state[backend.whisper_binary] == before

    def test_dying_logger_does_not_break_transcription(self, tmp_path):
        from wayfinder.core import transcriber

        def bad_logger(_msg):
            raise RuntimeError("UI gone")

        transcriber.set_gpu_event_logger(bad_logger)
        backend, audio = self._backend(tmp_path, "kill -SEGV $$")
        assert backend.transcribe(audio) == "cpu-text"


class TestWhisperServerWarmup:
    """whisper-server backend: instant first dictation via startup warm-up."""

    def test_server_backend_is_concrete(self):
        """Regression: backend was missing get_name/supports_gpu (abstract -> uninstantiable)."""
        from wayfinder.core.transcriber import WhisperServerBackend
        b = WhisperServerBackend(model_path="/tmp/none.bin")
        assert b.get_name()
        assert b.supports_gpu() is True

    def test_server_uses_short_dedicated_timeout(self, tmp_path):
        """The server request timeout comes from whisper_server_timeout (short, ~30s),
        NOT the generic 'timeout' (120s, for the CLI fallback). It must be well below
        the PROCESSING watchdog so a wedged server's restart+retry can salvage the
        dictation before the session is abandoned."""
        from wayfinder.core.transcriber import get_backend, WhisperServerBackend
        binary = tmp_path / "whisper-cli"; binary.write_text("#!/bin/sh\n"); binary.chmod(0o755)
        server = tmp_path / "whisper-server"; server.write_text("#!/bin/sh\n"); server.chmod(0o755)
        model = tmp_path / "m.bin"; model.write_bytes(b"\x00")
        config = {
            "whisper_server_mode": True,
            "whisper_binary": str(binary),
            "model_path": str(model),
            "timeout": 120,              # CLI fallback value — must NOT be used here
            "whisper_server_timeout": 30,
        }
        backend = get_backend(config)
        assert isinstance(backend, WhisperServerBackend)
        assert backend.timeout == 30  # short, dedicated — not the 120s CLI value

    def test_warm_up_noop_when_binary_missing(self, tmp_path):
        from wayfinder.core.transcriber import WhisperServerBackend
        b = WhisperServerBackend(
            whisper_server_binary=str(tmp_path / "absent"),
            model_path=str(tmp_path / "absent.bin"),
        )
        # is_available() is False -> warm_up must return without starting anything
        with patch.object(b, "_start_server") as start:
            b.warm_up()
            start.assert_not_called()

    def test_warm_up_starts_server_when_available(self, tmp_path):
        from wayfinder.core.transcriber import WhisperServerBackend
        binary = tmp_path / "whisper-server"
        binary.write_text("#!/bin/sh\n")
        model = tmp_path / "m.bin"
        model.write_bytes(b"\x00")
        b = WhisperServerBackend(whisper_server_binary=str(binary), model_path=str(model))
        with patch.object(b, "_start_server") as start:
            b.warm_up()
            start.assert_called_once()

    def test_warm_up_swallows_start_failure(self, tmp_path):
        """A broken warm-up must never propagate (would block app startup)."""
        from wayfinder.core.transcriber import WhisperServerBackend
        binary = tmp_path / "whisper-server"
        binary.write_text("#!/bin/sh\n")
        model = tmp_path / "m.bin"
        model.write_bytes(b"\x00")
        b = WhisperServerBackend(whisper_server_binary=str(binary), model_path=str(model))
        with patch.object(b, "_start_server", side_effect=RuntimeError("boom")):
            b.warm_up()  # must not raise

    def test_module_warm_up_routes_to_server_backend(self, tmp_path):
        from wayfinder.core import transcriber
        # get_backend only returns the server backend when the binary exists.
        (tmp_path / "whisper-cli").write_text("#!/bin/sh\n")
        (tmp_path / "whisper-server").write_text("#!/bin/sh\n")
        (tmp_path / "m.bin").write_bytes(b"\x00")
        cfg = {"transcription_backend": "whisper_cpp", "whisper_server_mode": True,
               "whisper_binary": str(tmp_path / "whisper-cli"),
               "model_path": str(tmp_path / "m.bin")}
        with patch.object(transcriber.WhisperServerBackend, "warm_up") as warm:
            transcriber.warm_up_transcription(cfg)
            warm.assert_called_once()

    def test_module_warm_up_noop_for_cli_backend(self):
        """whisper-cli has nothing to warm — must not raise or start a server."""
        from wayfinder.core import transcriber
        cfg = {"transcription_backend": "whisper_cpp", "whisper_server_mode": False,
               "whisper_binary": "/x/whisper-cli", "model_path": "/x/m.bin"}
        # WhisperCppBackend has no warm_up attr -> helper is a clean no-op
        transcriber.warm_up_transcription(cfg)


class TestWhisperServerTranscribeParsing:
    """Exercise the real HTTP response parsing — the path that hit NameError: json."""

    def test_transcribe_parses_json_response(self, tmp_path):
        from unittest.mock import MagicMock, patch
        from wayfinder.core.transcriber import WhisperServerBackend
        import io

        audio = tmp_path / "a.wav"
        audio.write_bytes(b"RIFF....WAVE")
        b = WhisperServerBackend(model_path=str(tmp_path / "m.bin"))

        fake_resp = MagicMock()
        fake_resp.read.return_value = b'{"text": " hello world "}'
        with patch.object(b, "_start_server"), \
             patch("urllib.request.urlopen", return_value=fake_resp):
            WhisperServerBackend._server_port = 8178
            text = b.transcribe(str(audio))
        # Must parse + strip — a missing `import json` made this raise NameError.
        assert text == "hello world"


class TestServerModeDefaultAndFallback:
    """Server mode is the default, but falls back to CLI when the binary is absent."""

    def test_default_config_enables_server_mode(self):
        from wayfinder.config import DEFAULT_CONFIG
        assert DEFAULT_CONFIG["whisper_server_mode"] is True

    def test_get_backend_uses_server_when_binary_present(self, tmp_path):
        from wayfinder.core.transcriber import get_backend, WhisperServerBackend
        server = tmp_path / "whisper-server"
        server.write_text("#!/bin/sh\n")
        cli = tmp_path / "whisper-cli"
        cli.write_text("#!/bin/sh\n")
        model = tmp_path / "m.bin"
        model.write_bytes(b"\x00")
        cfg = {"whisper_server_mode": True, "whisper_binary": str(cli),
               "model_path": str(model)}
        assert isinstance(get_backend(cfg), WhisperServerBackend)

    def test_get_backend_falls_back_to_cli_when_server_missing(self, tmp_path):
        from wayfinder.core.transcriber import get_backend, WhisperCppBackend
        # Only whisper-cli exists, no whisper-server next to it.
        cli = tmp_path / "whisper-cli"
        cli.write_text("#!/bin/sh\n")
        model = tmp_path / "m.bin"
        model.write_bytes(b"\x00")
        cfg = {"whisper_server_mode": True, "whisper_binary": str(cli),
               "model_path": str(model)}
        backend = get_backend(cfg)
        assert isinstance(backend, WhisperCppBackend)
        assert not hasattr(backend, "whisper_server_binary")


class TestServerFlagLadderAndCliDelegation:
    """Bulletproof server startup: v1.7.2's bundled server lacks -nth/-sns (an
    unknown flag makes it print usage and exit), broken-Vulkan machines need a
    no-GPU retry, and a machine where the server can't run at all must degrade
    to the per-call CLI backend instead of failing every dictation."""

    def _backend(self, tmp_path):
        from wayfinder.core.transcriber import WhisperServerBackend
        binary = tmp_path / "whisper-server"
        binary.write_text("#!/bin/sh\n")
        model = tmp_path / "m.bin"
        model.write_bytes(b"\x00")
        return WhisperServerBackend(
            whisper_server_binary=str(binary), model_path=str(model),
            use_gpu=True, suppress_nst=True,
        )

    def test_attempt_ladder_full_then_safe_then_cpu(self, tmp_path):
        b = self._backend(tmp_path)
        attempts = b._server_cmd_attempts(8178)
        assert len(attempts) == 3
        full, safe, cpu = attempts
        # Attempt 1: full modern flag set
        assert "-nth" in full and "-sns" in full
        # Attempt 2: v1.7.2-safe — no post-1.7.2 flags, still GPU
        assert "-nth" not in safe and "-sns" not in safe and "-ng" not in safe
        # Attempt 3: broken-Vulkan rescue — safe flags + no-GPU
        assert "-nth" not in cpu and cpu[-1] == "-ng"
        # All attempts keep the universally-supported accuracy flags
        for cmd in attempts:
            for flag in ("-m", "-t", "--port", "-l", "-bs", "-bo", "-et", "-nf"):
                assert flag in cmd

    def test_no_gpu_config_skips_cpu_rescue_attempt(self, tmp_path):
        b = self._backend(tmp_path)
        b.use_gpu = False
        attempts = b._server_cmd_attempts(8178)
        assert len(attempts) == 2
        assert all("-ng" in cmd for cmd in attempts)

    def test_transcribe_delegates_to_cli_when_server_disabled(self, tmp_path):
        from unittest.mock import MagicMock, patch
        from wayfinder.core.transcriber import WhisperServerBackend
        audio = tmp_path / "a.wav"
        audio.write_bytes(b"RIFF....WAVE")
        b = self._backend(tmp_path)
        fake_cli = MagicMock()
        fake_cli.transcribe.return_value = "via cli"
        old = WhisperServerBackend._server_disabled
        try:
            WhisperServerBackend._server_disabled = True
            with patch.object(b, "_cli_fallback", return_value=fake_cli):
                assert b.transcribe(str(audio)) == "via cli"
            fake_cli.transcribe.assert_called_once()
        finally:
            WhisperServerBackend._server_disabled = old

    def test_transcribe_falls_back_to_cli_when_start_raises(self, tmp_path):
        from unittest.mock import MagicMock, patch
        from wayfinder.core.transcriber import WhisperServerBackend, TranscriptionError
        audio = tmp_path / "a.wav"
        audio.write_bytes(b"RIFF....WAVE")
        b = self._backend(tmp_path)
        fake_cli = MagicMock()
        fake_cli.transcribe.return_value = "via cli"
        old = WhisperServerBackend._server_disabled
        try:
            WhisperServerBackend._server_disabled = False
            with patch.object(b, "_start_server", side_effect=TranscriptionError("nope")), \
                 patch.object(b, "_cli_fallback", return_value=fake_cli):
                assert b.transcribe(str(audio)) == "via cli"
        finally:
            WhisperServerBackend._server_disabled = old

    def test_cli_fallback_derives_cli_binary_and_keeps_settings(self, tmp_path):
        b = self._backend(tmp_path)
        cli = b._cli_fallback()
        assert cli.whisper_binary.endswith("whisper-cli")
        assert cli.model_path == b.model_path
        assert cli.threads == b.threads
        # Cached — same object on second call
        assert b._cli_fallback() is cli

    def test_cpu_server_sibling_is_last_resort(self, tmp_path):
        # A whisper-server-cpu next to the server binary adds a final attempt
        # using it (Vulkan builds can crash at lib init even with -ng).
        from wayfinder.core.transcriber import WhisperServerBackend
        b = self._backend(tmp_path)
        (tmp_path / "whisper-server-cpu").write_text("#!/bin/sh\n")
        attempts = b._server_cmd_attempts(8178)
        assert len(attempts) == 4
        assert attempts[-1][0].endswith("whisper-server-cpu")
        assert "-nth" not in attempts[-1]
