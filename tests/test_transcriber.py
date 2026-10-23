"""
Tests for the transcription module.
"""

import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.mark.usefixtures("no_host_binaries")
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

    @pytest.mark.parametrize(
        "backend_id",
        ["groq_whisper", "openai_whisper", "faster_whisper"],
    )
    def test_free_config_cannot_execute_paid_backend(
        self, backend_id, sample_config, monkeypatch
    ):
        from wayfinder.core.transcriber import WhisperCppBackend, get_backend

        monkeypatch.setattr(
            "wayfinder.license.FeatureGate.has_feature", lambda self, f: False
        )
        sample_config.update({
            "transcription_backend": backend_id,
            "model_path": "/models/ggml-base.en.bin",
            "whisper_server_mode": False,
        })

        backend = get_backend(sample_config)

        assert isinstance(backend, WhisperCppBackend)
        assert backend.use_gpu is False

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

    def test_large_model_downgrade_fails_closed_when_no_free_alternative(
        self, sample_config, tmp_path, monkeypatch
    ):
        """Unlicensed Ultra weights must not run: when no free fallback file exists,
        leave the Ultra path (point at conventional free basename) rather than keep
        the large model."""
        import os
        from wayfinder.core.transcriber import get_backend
        monkeypatch.setattr("wayfinder.license.FeatureGate.has_feature", lambda self, f: False)
        large = tmp_path / "ggml-large-v3-turbo-q5_0.bin"
        large.write_bytes(b"x")  # only a large model present, no free alternative
        sample_config["transcription_backend"] = "whisper_cpp"
        sample_config["model_path"] = str(large)
        backend = get_backend(sample_config)
        resolved = os.path.expanduser(getattr(backend, "model_path", ""))
        assert os.path.basename(resolved) == "ggml-base.en.bin"
        assert "large" not in os.path.basename(resolved).lower()

    def test_free_gpu_disabled_for_base_model(self, sample_config, monkeypatch):
        """Free Base transcription remains CPU-only even if config requests GPU."""
        from wayfinder.core.transcriber import get_backend

        monkeypatch.setattr("wayfinder.license.FeatureGate.has_feature", lambda self, f: False)
        sample_config["transcription_backend"] = "whisper_cpp"
        sample_config["model_path"] = "/models/ggml-base.en.bin"
        sample_config["use_gpu"] = True
        sample_config["whisper_server_mode"] = False
        backend = get_backend(sample_config)
        assert getattr(backend, "use_gpu", None) is False

    def test_free_gpu_disabled_for_small_model(self, sample_config, monkeypatch):
        """Free tier must not get GPU on Small — that is Ultra."""
        from wayfinder.core.transcriber import get_backend

        monkeypatch.setattr("wayfinder.license.FeatureGate.has_feature", lambda self, f: False)
        sample_config["transcription_backend"] = "whisper_cpp"
        sample_config["model_path"] = "/models/ggml-small.en.bin"
        sample_config["use_gpu"] = True
        sample_config["whisper_server_mode"] = False
        backend = get_backend(sample_config)
        assert getattr(backend, "use_gpu", None) is False

    def test_ultra_gpu_enabled_for_small_model(self, sample_config, monkeypatch):
        """Ultra (gpu_acceleration feature) may use GPU on Small."""
        from wayfinder.core.transcriber import get_backend

        monkeypatch.setattr(
            "wayfinder.license.FeatureGate.has_feature",
            lambda self, f: f == "gpu_acceleration" or f == "large_models",
        )
        sample_config["transcription_backend"] = "whisper_cpp"
        sample_config["model_path"] = "/models/ggml-small.en.bin"
        sample_config["use_gpu"] = True
        sample_config["whisper_server_mode"] = False
        backend = get_backend(sample_config)
        assert getattr(backend, "use_gpu", None) is True

    def test_free_config_cannot_inject_custom_vocabulary(
        self, sample_config, monkeypatch
    ):
        from wayfinder.core.transcriber import get_backend

        monkeypatch.setattr(
            "wayfinder.license.FeatureGate.has_feature", lambda self, f: False
        )
        sample_config.update({
            "model_path": "/models/ggml-base.en.bin",
            "whisper_server_mode": False,
            "custom_vocabulary": ["PaidTerm"],
        })

        backend = get_backend(sample_config)

        assert backend.custom_vocabulary == []

    @pytest.mark.parametrize(
        "tone", ["minimal", "professional", "casual", "dev", "personal"]
    )
    def test_ultra_custom_vocabulary_reaches_backend_in_every_style(
        self, sample_config, monkeypatch, tone
    ):
        from wayfinder.core.transcriber import get_backend

        monkeypatch.setattr(
            "wayfinder.license.FeatureGate.has_feature",
            lambda self, f: f == "custom_vocabulary",
        )
        sample_config.update({
            "model_path": "/models/ggml-base.en.bin",
            "whisper_server_mode": False,
            "output_tone": tone,
            "custom_vocabulary": ["Wayfinder"],
        })

        backend = get_backend(sample_config)

        assert backend.custom_vocabulary == ["Wayfinder"]

    def test_dev_builtin_vocabulary_belongs_to_tone_feature(
        self, sample_config, monkeypatch
    ):
        from wayfinder.core.transcriber import DEV_VOCABULARY, get_backend

        monkeypatch.setattr(
            "wayfinder.license.FeatureGate.has_feature",
            lambda self, f: f == "tone_system",
        )
        sample_config.update({
            "model_path": "/models/ggml-base.en.bin",
            "whisper_server_mode": False,
            "output_tone": "dev",
            "custom_vocabulary": ["UnlicensedCustomTerm"],
        })

        backend = get_backend(sample_config)

        assert DEV_VOCABULARY[0] in backend.custom_vocabulary
        assert "UnlicensedCustomTerm" not in backend.custom_vocabulary


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

    def test_blank_binary_is_not_available(self):
        """Regression: Path('') is cwd, not a whisper executable."""
        from wayfinder.core.transcriber import WhisperCppBackend

        backend = WhisperCppBackend(whisper_binary="")

        assert backend.whisper_binary == ""
        assert backend.is_available() is False

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

    def test_server_output_pipe_is_continuously_drained_and_tail_is_bounded(self):
        """Regression: whisper-server writes several KB of logs per request. Keeping
        stdout=PIPE without a reader eventually fills the pipe and blocks the server
        inside write(), presenting as a random inference hang after many dictations.

        Emit substantially more than a normal pipe can hold; the child can exit only
        if Aura consumes concurrently. Diagnostics retain just the bounded last lines.
        """
        import subprocess
        import sys

        from wayfinder.core.transcriber import WhisperServerBackend

        script = (
            "import sys\n"
            "for i in range(400):\n"
            "    sys.stdout.write(f'line-{i:04d} ' + ('x' * 4096) + '\\n')\n"
            "sys.stdout.flush()\n"
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        try:
            WhisperServerBackend._start_server_output_drain(proc)
            assert proc.wait(timeout=5) == 0, (
                "child blocked because its stdout pipe was not drained"
            )
            WhisperServerBackend._server_log_thread.join(timeout=2)
            retained = list(WhisperServerBackend._server_log_tail)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=2)

        assert len(retained) == 200
        assert retained[-1].startswith("line-0399 ")
        assert all(not line.startswith("line-0000 ") for line in retained)

    def test_hung_server_timeout_triggers_restart_and_retry(
        self, sample_audio_file: Path
    ):
        """Regression: a wedged-but-listening whisper-server makes urlopen raise
        TimeoutError (not URLError) — observed after a suspend/resume cycle. That
        must restart the server and retry on the fresh one, never fall through to an
        unrecoverable error that leaves the dead server hanging every dictation.
        We do NOT re-POST to the same (possibly still-busy) server first — that would
        queue behind the wedged request — so the whole recovery is one restart + retry.
        """
        from wayfinder.core.transcriber import WhisperServerBackend

        WhisperServerBackend._server_disabled = False
        backend = WhisperServerBackend(use_gpu=False, timeout=1)

        def fake_start(*a, **k):
            WhisperServerBackend._server_port = 8178

        # First inference hangs (TimeoutError); after the restart it succeeds.
        urlopen = MagicMock(side_effect=[TimeoutError("timed out"),
                                         self._make_resp("recovered")])

        with patch.object(Path, "exists", return_value=True), \
             patch.object(WhisperServerBackend, "_start_server",
                          side_effect=fake_start) as mock_start, \
             patch.object(WhisperServerBackend, "_stop_server_internal") as mock_stop, \
             patch("urllib.request.urlopen", urlopen):
            result = backend.transcribe(str(sample_audio_file))

        assert result == "recovered"
        # The stop now happens INSIDE _start_server(force=True); recovery evidences the
        # restart by force-restarting (which replaces a wedged-but-listening server).
        assert mock_start.call_count == 2  # entry + restart
        assert mock_start.call_args_list[1].kwargs.get("force") is True, \
            "recovery must force-restart the wedged server"
        # POST(initial) + POST(post-restart) = 2 — no duplicate warm re-POST
        assert urlopen.call_count == 2

    def test_dead_server_urlerror_restarts_and_retries(
        self, sample_audio_file: Path
    ):
        """A URLError (connection refused: the server PROCESS is gone, not merely
        slow) must also restart + retry, recovering on the fresh server."""
        from wayfinder.core.transcriber import WhisperServerBackend
        import urllib.error

        WhisperServerBackend._server_disabled = False
        backend = WhisperServerBackend(use_gpu=False, timeout=1)

        def fake_start(*a, **k):
            WhisperServerBackend._server_port = 8178

        urlopen = MagicMock(side_effect=[urllib.error.URLError("connection refused"),
                                         self._make_resp("recovered")])

        with patch.object(Path, "exists", return_value=True), \
             patch.object(WhisperServerBackend, "_start_server",
                          side_effect=fake_start) as mock_start, \
             patch.object(WhisperServerBackend, "_stop_server_internal") as mock_stop, \
             patch("urllib.request.urlopen", urlopen):
            result = backend.transcribe(str(sample_audio_file))

        assert result == "recovered"
        assert mock_start.call_count == 2
        assert mock_start.call_args_list[1].kwargs.get("force") is True  # recovery force-restarts
        assert urlopen.call_count == 2

    def test_startup_failure_during_recovery_salvages_via_cli(
        self, sample_audio_file: Path
    ):
        """Finding-3 regression: a _start_server() failure DURING recovery must route
        to whisper-cli salvage, not escape and drop the chunk's words. _start_server()
        now sits INSIDE the salvage try (the old code left it outside)."""
        from wayfinder.core.transcriber import WhisperServerBackend, TranscriptionError

        WhisperServerBackend._server_disabled = False
        backend = WhisperServerBackend(use_gpu=False, timeout=1)

        calls = {"n": 0}

        def start_side(*a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                WhisperServerBackend._server_port = 8178      # entry start succeeds
            else:
                raise TranscriptionError("restart startup failed")  # restart fails

        cli = MagicMock()
        cli.transcribe.return_value = "salvaged words"
        urlopen = MagicMock(side_effect=[TimeoutError("wedged")])

        with patch.object(Path, "exists", return_value=True), \
             patch.object(WhisperServerBackend, "_start_server", side_effect=start_side), \
             patch.object(WhisperServerBackend, "_stop_server_internal"), \
             patch.object(WhisperServerBackend, "_cli_fallback", return_value=cli), \
             patch("urllib.request.urlopen", urlopen):
            result = backend.transcribe(str(sample_audio_file))

        assert result == "salvaged words"
        assert cli.transcribe.called, "startup failure must fall through to whisper-cli salvage"

    def test_malformed_response_after_restart_salvages_via_cli(
        self, sample_audio_file: Path
    ):
        """Finding-3 regression: a malformed/short response on the post-restart retry
        (a JSON parse error) must route to salvage, not escape the except and drop
        words. The JSON parse now sits INSIDE the salvage try."""
        from wayfinder.core.transcriber import WhisperServerBackend

        WhisperServerBackend._server_disabled = False
        backend = WhisperServerBackend(use_gpu=False, timeout=1)

        def fake_start(*a, **k):
            WhisperServerBackend._server_port = 8178

        bad = MagicMock()
        bad.read.return_value = b"not json"          # post-restart retry returns garbage
        cli = MagicMock()
        cli.transcribe.return_value = "salvaged words"
        urlopen = MagicMock(side_effect=[TimeoutError("wedged"), bad])

        with patch.object(Path, "exists", return_value=True), \
             patch.object(WhisperServerBackend, "_start_server", side_effect=fake_start), \
             patch.object(WhisperServerBackend, "_stop_server_internal"), \
             patch.object(WhisperServerBackend, "_cli_fallback", return_value=cli), \
             patch("urllib.request.urlopen", urlopen):
            result = backend.transcribe(str(sample_audio_file))

        assert result == "salvaged words"
        assert cli.transcribe.called, "malformed post-restart response must fall through to salvage"

    def test_server_retry_exhaustion_still_leaves_fresh_cli_salvage_budget(
        self, sample_audio_file: Path
    ):
        """A retry that consumes the server deadline must not starve CLI salvage.

        This is the live failure from 2026-08-03: two 5s server attempts used the
        entire recovery window, then CLI was handed that expired deadline and
        returned zero text despite seven seconds of captured speech.
        """
        from wayfinder.core.transcriber import WhisperServerBackend

        WhisperServerBackend._server_disabled = False
        backend = WhisperServerBackend(use_gpu=True, timeout=5)
        now = [1000.0]

        def fake_start(*_args, **_kwargs):
            WhisperServerBackend._server_port = 8178

        calls = [0]

        def fake_urlopen(*_args, **_kwargs):
            calls[0] += 1
            if calls[0] == 2:
                # The post-restart request consumes more than the 5s server
                # recovery deadline before failing.
                now[0] += 6.0
            raise TimeoutError("wedged")

        cli = MagicMock()

        def salvage(_audio, _context, deadline=None):
            assert deadline is not None and deadline > now[0] + 40.0
            return "rescued dictation"

        cli.transcribe.side_effect = salvage

        with patch.object(Path, "exists", return_value=True), \
             patch.object(WhisperServerBackend, "_start_server", side_effect=fake_start), \
             patch.object(WhisperServerBackend, "_stop_server_internal"), \
             patch.object(WhisperServerBackend, "_cli_fallback", return_value=cli), \
             patch("urllib.request.urlopen", side_effect=fake_urlopen), \
             patch("time.monotonic", side_effect=lambda: now[0]):
            result = backend.transcribe(str(sample_audio_file))

        assert result == "rescued dictation"
        assert cli.transcribe.called

    def test_recovery_retry_is_clamped_to_watchdog_budget(self, sample_audio_file: Path):
        """Finding-1 regression (retry leg): the post-restart retry timeout is DERIVED
        from the monotonic recovery deadline (now + self.timeout), not the full
        self.timeout. Here the clock advances during the restart, leaving only ~2s of
        budget, so the retry must be clamped to that. (This pins the retry-timeout
        derivation only; the salvage leg's clamping is covered by
        test_cli_salvage_attempt_is_clamped_to_deadline.)"""
        from wayfinder.core.transcriber import WhisperServerBackend

        WhisperServerBackend._server_disabled = False
        backend = WhisperServerBackend(use_gpu=False, timeout=30)

        # monotonic(): deadline set at t=1000 (+30 -> 1030); by the retry the clock is
        # at 1028, leaving ~2s. (Extra calls, if any, stay at 1028.)
        clock = iter([1000.0, 1028.0])

        def fake_monotonic():
            try:
                return next(clock)
            except StopIteration:
                return 1028.0

        def fake_start(*a, **k):
            WhisperServerBackend._server_port = 8178

        urlopen = MagicMock(side_effect=[TimeoutError("wedged"), self._make_resp("ok")])
        with patch.object(Path, "exists", return_value=True), \
             patch.object(WhisperServerBackend, "_start_server", side_effect=fake_start), \
             patch.object(WhisperServerBackend, "_stop_server_internal"), \
             patch("time.monotonic", fake_monotonic), \
             patch("urllib.request.urlopen", urlopen):
            result = backend.transcribe(str(sample_audio_file))

        assert result == "ok"
        # The post-restart retry (2nd urlopen) must be clamped to the ~2s remaining,
        # never the full 30s — proving the retry timeout tracks the recovery deadline.
        retry_timeout = urlopen.call_args_list[1].kwargs.get("timeout")
        assert 0 < retry_timeout <= 3, \
            f"retry timeout should be clamped to remaining budget, got {retry_timeout}"

    def test_cli_salvage_attempt_is_clamped_to_deadline(self, tmp_path):
        """Finding-1 regression (salvage leg): WhisperCppBackend.transcribe(deadline=…)
        must clamp each subprocess attempt to the time remaining, so the whisper-cli
        salvage cascade cannot overrun the recovery budget / the 120s watchdog."""
        import time as _time
        import subprocess as _sp  # noqa: F401 (patched by name below)
        from wayfinder.core.transcriber import WhisperCppBackend

        binary = tmp_path / "whisper-cli"; binary.write_text("#!/bin/sh\n"); binary.chmod(0o755)
        model = tmp_path / "m.bin"; model.write_bytes(b"\x00")
        audio = tmp_path / "a.wav"; audio.write_bytes(b"\x00")
        backend = WhisperCppBackend(whisper_binary=str(binary), model_path=str(model),
                                    use_gpu=False, timeout=30)

        seen = []

        def fake_run(cmd, **kw):
            seen.append((cmd, kw.get("timeout")))
            m = MagicMock(); m.returncode = 0; m.stdout = "hello world"; m.stderr = ""
            return m

        deadline = _time.monotonic() + 2.0   # only ~2s of budget remains
        with patch("subprocess.run", side_effect=fake_run):
            backend.transcribe(str(audio), deadline=deadline)

        # Ignore the cached `--help` flag probe; the transcription attempt(s) must each
        # be clamped to the ~2s remaining, never the full 30s configured timeout.
        attempt_timeouts = [t for cmd, t in seen if "--help" not in cmd]
        assert attempt_timeouts, "no transcription attempt observed"
        assert all(0 < t <= 3 for t in attempt_timeouts), \
            f"CLI salvage attempt must be clamped to remaining budget, got {attempt_timeouts}"

    def test_start_server_bails_when_deadline_passed(self, tmp_path):
        """Finding-1 regression: _start_server(deadline=…) must abandon its readiness
        wait once the budget is spent, instead of blocking its full ~30s (up to ~150s)
        loop on a listening-but-unresponsive child and blowing the 120s watchdog."""
        import time
        from wayfinder.core.transcriber import WhisperServerBackend, TranscriptionError

        binary = tmp_path / "whisper-server"; binary.write_text("#!/bin/sh\n"); binary.chmod(0o755)
        model = tmp_path / "m.bin"; model.write_bytes(b"\x00")
        backend = WhisperServerBackend(whisper_server_binary=str(binary),
                                       model_path=str(model), use_gpu=False)
        WhisperServerBackend._server_process = None

        fake_proc = MagicMock(); fake_proc.poll.return_value = None  # alive, never ready
        with patch("subprocess.Popen", return_value=fake_proc) as mock_popen, \
             patch.object(WhisperServerBackend, "_is_our_server", return_value=False), \
             patch.object(WhisperServerBackend, "_find_available_port", return_value=8178), \
             patch.object(WhisperServerBackend, "_stop_server_internal"):
            with pytest.raises(TranscriptionError) as exc:
                backend._start_server(deadline=time.monotonic() - 1)  # already expired
        assert "deadline" in str(exc.value).lower(), \
            "startup must abandon its wait when the recovery deadline has passed"
        mock_popen.assert_not_called()  # must NOT spawn a doomed child past the deadline

    def test_cli_salvage_bails_before_flag_probe_when_expired(self, tmp_path):
        """Finding-2 regression: WhisperCppBackend.transcribe(deadline=…) with an
        already-expired deadline must bail BEFORE the (up to 5s) --help flag probe, so
        preprocessing can't overrun an exhausted budget."""
        import time
        from wayfinder.core.transcriber import WhisperCppBackend, TranscriptionError

        binary = tmp_path / "whisper-cli"; binary.write_text("#!/bin/sh\n"); binary.chmod(0o755)
        model = tmp_path / "m.bin"; model.write_bytes(b"\x00")
        audio = tmp_path / "a.wav"; audio.write_bytes(b"\x00")
        backend = WhisperCppBackend(whisper_binary=str(binary), model_path=str(model),
                                    use_gpu=False, timeout=30)
        ran = []

        def fake_run(cmd, **kw):
            ran.append(cmd)
            m = MagicMock(); m.returncode = 0; m.stdout = ""; m.stderr = ""
            return m

        with patch("subprocess.run", side_effect=fake_run):
            with pytest.raises(TranscriptionError) as exc:
                backend.transcribe(str(audio), deadline=time.monotonic() - 1)  # expired
        assert "exhausted before start" in str(exc.value)
        assert ran == [], "must not run the --help probe (or any subprocess) when already expired"

    def test_deadline_clamped_timeout_does_not_poison_gpu_health(self, tmp_path):
        """Finding-3 regression: a subprocess timeout that fires because the salvage
        DEADLINE (not the GPU) ran out must NOT memoize CPU fallback — that would
        poison GPU health for every future dictation."""
        import time
        import subprocess as _sp
        from wayfinder.core import transcriber as _T
        from wayfinder.core.transcriber import WhisperCppBackend, TranscriptionError

        binary = tmp_path / "whisper-cli"; binary.write_text("#!/bin/sh\n"); binary.chmod(0o755)
        # A CPU sibling makes the GPU binary the FIRST-of-two attempt (not the last),
        # which is exactly when the OLD code would _activate_fallback() on a timeout —
        # so this genuinely exercises the regression, not the is_last shortcut.
        cpu = tmp_path / "whisper-cli-cpu"; cpu.write_text("#!/bin/sh\n"); cpu.chmod(0o755)
        model = tmp_path / "m.bin"; model.write_bytes(b"\x00")
        audio = tmp_path / "a.wav"; audio.write_bytes(b"\x00")
        _T._CPU_FALLBACK_ACTIVE.pop(str(binary), None)
        backend = WhisperCppBackend(whisper_binary=str(binary), model_path=str(model),
                                    use_gpu=True, timeout=30)

        def fake_run(cmd, **kw):
            if "--help" in cmd:
                m = MagicMock(); m.returncode = 0; m.stdout = ""; m.stderr = ""
                return m
            raise _sp.TimeoutExpired(cmd, kw.get("timeout"))  # GPU attempt "times out"

        # deadline ~2s out -> attempt clamped to ~2 < 30 -> a timeout here is the
        # budget expiring, NOT the GPU hanging.
        with patch("subprocess.run", side_effect=fake_run):
            with pytest.raises(TranscriptionError):
                backend.transcribe(str(audio), deadline=time.monotonic() + 2)
        assert str(binary) not in _T._CPU_FALLBACK_ACTIVE, \
            "a deadline-clamped timeout must NOT memoize CPU fallback"

    def test_a_pre_spawn_expiry_does_not_poison_gpu_health_either(self, tmp_path):
        """Sol mac r21 (Finding 1 caller audit): HelperNotStarted — the deadline
        passing before this attempt's helper was ever spawned — is the same
        budget-vs-failure gap as the deadline-clamped TimeoutExpired case right
        above, just one layer earlier. It must bail the same way, never reach
        _activate_fallback's memoized CPU fallback."""
        import time
        from wayfinder.core import transcriber as _T
        from wayfinder.core.transcriber import WhisperCppBackend, TranscriptionError
        from wayfinder.utils.child_supervisor import HelperNotStarted

        binary = tmp_path / "whisper-cli"; binary.write_text("#!/bin/sh\n"); binary.chmod(0o755)
        cpu = tmp_path / "whisper-cli-cpu"; cpu.write_text("#!/bin/sh\n"); cpu.chmod(0o755)
        model = tmp_path / "m.bin"; model.write_bytes(b"\x00")
        audio = tmp_path / "a.wav"; audio.write_bytes(b"\x00")
        _T._CPU_FALLBACK_ACTIVE.pop(str(binary), None)
        backend = WhisperCppBackend(whisper_binary=str(binary), model_path=str(model),
                                    use_gpu=True, timeout=30)

        def fake_run_tracked(cmd, **kw):
            if "--help" in cmd:
                m = MagicMock(); m.returncode = 0; m.stdout = ""; m.stderr = ""
                return m
            raise HelperNotStarted(cmd, kw.get("timeout"))   # nothing ran

        with patch("wayfinder.core.transcriber.run_tracked", side_effect=fake_run_tracked):
            with pytest.raises(TranscriptionError):
                backend.transcribe(str(audio), deadline=time.monotonic() + 2)
        assert str(binary) not in _T._CPU_FALLBACK_ACTIVE, \
            "a pre-spawn expiry must NOT memoize CPU fallback either"

    def test_start_server_lock_acquire_bounded_by_deadline(self, tmp_path):
        """Round-4 Finding-1: a competing holder of _server_lock must not make a
        deadline-bounded startup block past its budget — it raises instead of hanging."""
        import time
        from wayfinder.core.transcriber import WhisperServerBackend, TranscriptionError

        binary = tmp_path / "whisper-server"; binary.write_text("#!/bin/sh\n"); binary.chmod(0o755)
        model = tmp_path / "m.bin"; model.write_bytes(b"\x00")
        backend = WhisperServerBackend(whisper_server_binary=str(binary),
                                       model_path=str(model), use_gpu=False)

        WhisperServerBackend._server_lock.acquire()  # hold it so the bounded acquire can't
        try:
            t0 = time.monotonic()
            with pytest.raises(TranscriptionError) as exc:
                backend._start_server(deadline=time.monotonic() + 0.3)  # 0.3s budget
            elapsed = time.monotonic() - t0
        finally:
            WhisperServerBackend._server_lock.release()
        assert "lock" in str(exc.value).lower()
        assert elapsed < 2.0, "must give up near the deadline, not block indefinitely"

    def test_force_restart_replaces_a_running_server(self, tmp_path):
        """Round-4 Finding-1: force=True must NOT early-return on a server that looks
        'already running with the right model' — recovery uses it to replace a
        wedged-but-listening server."""
        from wayfinder.core.transcriber import WhisperServerBackend

        binary = tmp_path / "whisper-server"; binary.write_text("#!/bin/sh\n"); binary.chmod(0o755)
        model = tmp_path / "m.bin"; model.write_bytes(b"\x00")
        backend = WhisperServerBackend(whisper_server_binary=str(binary),
                                       model_path=str(model), use_gpu=False)

        alive = MagicMock(); alive.poll.return_value = None       # looks healthy
        WhisperServerBackend._server_process = alive
        WhisperServerBackend._server_model_path = str(model)      # ...and same model
        stopped = []
        new_proc = MagicMock(); new_proc.poll.return_value = None
        try:
            with patch.object(WhisperServerBackend, "_stop_server_internal",
                              side_effect=lambda *a, **k: stopped.append(True)), \
                 patch.object(WhisperServerBackend, "_find_available_port", return_value=8178), \
                 patch.object(WhisperServerBackend, "_is_our_server", side_effect=[False, True]), \
                 patch("subprocess.Popen", return_value=new_proc), \
                 patch("time.sleep"):
                backend._start_server(force=True)
        finally:
            WhisperServerBackend._server_process = None
        assert stopped, "force=True must stop the existing server, not reuse it as-is"

    def test_deadline_bail_in_readiness_does_not_disable_server(self, tmp_path):
        """Round-4 Finding-1/3: a startup abandoned because the recovery DEADLINE passed
        is a budget bail, NOT a broken server — it must NOT set _server_disabled (which
        would send every future dictation to the slow CLI fallback)."""
        import time
        from wayfinder.core.transcriber import WhisperServerBackend, TranscriptionError

        binary = tmp_path / "whisper-server"; binary.write_text("#!/bin/sh\n"); binary.chmod(0o755)
        model = tmp_path / "m.bin"; model.write_bytes(b"\x00")
        backend = WhisperServerBackend(whisper_server_binary=str(binary),
                                       model_path=str(model), use_gpu=False)
        WhisperServerBackend._server_process = None
        WhisperServerBackend._server_disabled = False

        never_ready = MagicMock(); never_ready.poll.return_value = None
        try:
            with patch.object(WhisperServerBackend, "_stop_server_internal"), \
                 patch.object(WhisperServerBackend, "_find_available_port", return_value=8178), \
                 patch.object(WhisperServerBackend, "_is_our_server", return_value=False), \
                 patch("subprocess.Popen", return_value=never_ready):
                with pytest.raises(TranscriptionError) as exc:
                    backend._start_server(deadline=time.monotonic() + 0.15)  # tiny budget
        finally:
            WhisperServerBackend._server_process = None
        assert "deadline" in str(exc.value).lower()
        assert WhisperServerBackend._server_disabled is False, \
            "a deadline bail must NOT disable the server (poisoning guard)"

    def test_stop_server_clamps_sigterm_wait_to_deadline(self, tmp_path):
        """Round-4 Finding-1: _stop_server_internal(deadline=near) clamps its SIGTERM
        wait to the remaining budget instead of blocking the full 5s."""
        import time
        from wayfinder.core.transcriber import WhisperServerBackend

        proc = MagicMock(); proc.wait.return_value = 0  # exits promptly on SIGTERM
        WhisperServerBackend._server_process = proc
        WhisperServerBackend._stop_server_internal(deadline=time.monotonic() + 1.0)  # ~1s

        term_wait = proc.wait.call_args.kwargs.get("timeout")
        assert term_wait is not None and 0 < term_wait <= 1.5, \
            f"SIGTERM wait must be clamped to remaining budget, got {term_wait}"
        assert WhisperServerBackend._server_process is None  # cleaned up

    def test_cli_flag_probe_clamped_to_small_budget(self, tmp_path):
        """Round-4 Finding-2: with a small positive salvage budget, the --help flag
        probe timeout is clamped, not left at the full 5s."""
        import time
        from wayfinder.core.transcriber import WhisperCppBackend

        binary = tmp_path / "whisper-cli"; binary.write_text("#!/bin/sh\n"); binary.chmod(0o755)
        model = tmp_path / "m.bin"; model.write_bytes(b"\x00")
        audio = tmp_path / "a.wav"; audio.write_bytes(b"\x00")
        backend = WhisperCppBackend(whisper_binary=str(binary), model_path=str(model),
                                    use_gpu=False, timeout=30)
        seen = []

        def fake_run(cmd, **kw):
            seen.append((cmd, kw.get("timeout")))
            m = MagicMock(); m.returncode = 0; m.stdout = "hello"; m.stderr = ""
            return m

        with patch("subprocess.run", side_effect=fake_run):
            backend.transcribe(str(audio), deadline=time.monotonic() + 1.0)  # ~1s budget

        help_timeout = next((t for cmd, t in seen if "--help" in cmd), None)
        assert help_timeout is not None and 0 < help_timeout <= 1.5, \
            f"--help probe must be clamped to the small remaining budget, got {help_timeout}"

    def test_force_recovery_does_not_reuse_orphaned_listener(self, tmp_path):
        """Round-5 Finding-3: when _server_process is None (orphaned) but a wedged
        server is still LISTENING on the port, forced recovery must NOT re-adopt it —
        port discovery gets reuse_ok=False and the reuse branch is disabled, so a fresh
        server is spawned instead."""
        from wayfinder.core.transcriber import WhisperServerBackend

        binary = tmp_path / "whisper-server"; binary.write_text("#!/bin/sh\n"); binary.chmod(0o755)
        model = tmp_path / "m.bin"; model.write_bytes(b"\x00")
        backend = WhisperServerBackend(whisper_server_binary=str(binary),
                                       model_path=str(model), use_gpu=False)
        WhisperServerBackend._server_process = None  # orphaned — nothing tracked to stop
        reuse_ok_seen = {}

        def fake_find(probe_timeout=2, reuse_ok=True):
            reuse_ok_seen["v"] = reuse_ok
            return 8200

        new_proc = MagicMock(); new_proc.poll.return_value = None
        # A wedged server WOULD answer "our server" — the code must not reuse it.
        try:
            with patch.object(WhisperServerBackend, "_stop_server_internal"), \
                 patch.object(WhisperServerBackend, "_find_available_port", side_effect=fake_find), \
                 patch.object(WhisperServerBackend, "_is_our_server", return_value=True), \
                 patch("subprocess.Popen", return_value=new_proc) as mock_popen, \
                 patch("time.sleep"):
                backend._start_server(force=True)
        finally:
            WhisperServerBackend._server_process = None
        assert reuse_ok_seen.get("v") is False, "forced recovery must pass reuse_ok=False"
        assert mock_popen.called, "forced recovery must spawn a fresh server, not reuse the wedge"

    def test_deadline_truncated_flag_probe_drops_fragile_flags(self, tmp_path):
        """Round-5 Finding-2: if the --help probe times out under a salvage deadline,
        the version-fragile flags must NOT be emitted (fail-open would make bundled
        v1.7.2 transcribe nothing), and the truncated probe must not be cached."""
        import time
        import subprocess as _sp
        from wayfinder.core.transcriber import WhisperCppBackend

        binary = tmp_path / "whisper-cli"; binary.write_text("#!/bin/sh\n"); binary.chmod(0o755)
        model = tmp_path / "m.bin"; model.write_bytes(b"\x00")
        audio = tmp_path / "a.wav"; audio.write_bytes(b"\x00")
        backend = WhisperCppBackend(whisper_binary=str(binary), model_path=str(model),
                                    use_gpu=False, timeout=30, suppress_nst=True,
                                    no_speech_threshold=0.5)
        got = {}

        def fake_run(cmd, **kw):
            if "--help" in cmd:
                raise _sp.TimeoutExpired(cmd, kw.get("timeout"))  # probe truncated
            got["cmd"] = cmd
            m = MagicMock(); m.returncode = 0; m.stdout = "hello"; m.stderr = ""
            return m

        with patch("subprocess.run", side_effect=fake_run):
            backend.transcribe(str(audio), deadline=time.monotonic() + 1.0)

        cmd = got["cmd"]
        assert "--no-speech-thold" not in cmd and "--suppress-nst" not in cmd, \
            "a truncated probe must NOT fail-open on fragile flags under a deadline"
        assert "--no-gpu" in cmd, "non-fragile flags (e.g. --no-gpu) keep fail-open behavior"
        assert getattr(backend, "_supported_flags_cache", None) is None, \
            "a truncated probe must not be cached (a later unhurried call can re-probe)"

    def test_gpu_adaptive_timeout_capped_below_short_configured_timeout(self, tmp_path):
        """Finding-2 regression: with a configured timeout below the 15s floor, the
        adaptive GPU timeout must still be CAPPED at self.timeout — the cap wins over
        the floor. `max(15, min(t, …))` would wrongly return 15 > t."""
        import wave as _wave
        from wayfinder.core.transcriber import WhisperServerBackend

        wav = tmp_path / "clip.wav"
        with _wave.open(str(wav), "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
            w.writeframes(b"\x00\x00" * 16000)  # 1s

        WhisperServerBackend._server_disabled = False
        backend = WhisperServerBackend(use_gpu=True, timeout=5)  # below the 15s floor

        def fake_start(*a, **k):
            WhisperServerBackend._server_port = 8178

        urlopen = MagicMock(return_value=self._make_resp("ok"))
        with patch.object(Path, "exists", return_value=True), \
             patch.object(WhisperServerBackend, "_start_server", side_effect=fake_start), \
             patch("urllib.request.urlopen", urlopen):
            backend.transcribe(str(wav))

        assert urlopen.call_args.kwargs.get("timeout") == 5.0, \
            "adaptive GPU timeout must be capped at self.timeout even when it is below 15s"

    def test_malformed_initial_response_routes_to_recovery(self, sample_audio_file: Path):
        """Finding-3 regression: a malformed/undecodable response on the FIRST attempt
        (not just post-restart) must route into recovery, not escape as a generic error
        and drop the chunk. Here the initial POST returns garbage; the restart+retry
        then succeeds."""
        from wayfinder.core.transcriber import WhisperServerBackend

        WhisperServerBackend._server_disabled = False
        backend = WhisperServerBackend(use_gpu=False, timeout=5)

        def fake_start(*a, **k):
            WhisperServerBackend._server_port = 8178

        bad = MagicMock(); bad.read.return_value = b"not json"    # initial: garbage
        urlopen = MagicMock(side_effect=[bad, self._make_resp("recovered")])

        with patch.object(Path, "exists", return_value=True), \
             patch.object(WhisperServerBackend, "_start_server",
                          side_effect=fake_start) as mock_start, \
             patch.object(WhisperServerBackend, "_stop_server_internal") as mock_stop, \
             patch("urllib.request.urlopen", urlopen):
            result = backend.transcribe(str(sample_audio_file))

        assert result == "recovered", "malformed initial response must recover, not drop"
        assert mock_start.call_count == 2 and mock_start.call_args_list[1].kwargs.get("force") is True, \
            "must force-restart on a bad initial response"

    def test_retry_uses_fresh_port_after_restart(self, sample_audio_file: Path):
        """Finding-4 regression: if _start_server() selects a NEW port on restart, the
        post-restart retry must POST to that new port, not the stale original one."""
        from wayfinder.core.transcriber import WhisperServerBackend

        WhisperServerBackend._server_disabled = False
        backend = WhisperServerBackend(use_gpu=False, timeout=5)

        ports = iter([8178, 8180])   # entry start -> 8178, restart -> 8180

        def start_side(*a, **k):
            WhisperServerBackend._server_port = next(ports)

        urlopen = MagicMock(side_effect=[TimeoutError("wedged"), self._make_resp("ok")])
        with patch.object(Path, "exists", return_value=True), \
             patch.object(WhisperServerBackend, "_start_server", side_effect=start_side), \
             patch.object(WhisperServerBackend, "_stop_server_internal"), \
             patch("urllib.request.urlopen", urlopen):
            result = backend.transcribe(str(sample_audio_file))

        assert result == "ok"
        # The 2nd urlopen (post-restart) Request must target the NEW port 8180.
        retry_req = urlopen.call_args_list[1].args[0]
        assert ":8180/" in retry_req.full_url, \
            f"retry must use the fresh port, got {retry_req.full_url}"

    def test_retry_failure_salvages_words_via_cli(self, sample_audio_file: Path):
        """Regression: a chunk whose request fails even after restart+retry must be
        salvaged through the per-call whisper-cli backend, not dropped. Dropping it
        stores "[error]" upstream and silently loses the user's words — observed on
        the free/CPU path when a long multi-chunk dictation wedged the server.
        """
        from wayfinder.core.transcriber import WhisperServerBackend

        backend = WhisperServerBackend(use_gpu=False, timeout=1)

        def fake_start(*a, **k):
            WhisperServerBackend._server_port = 8178

        # Initial request AND post-restart retry both fail — the server is gone.
        urlopen = MagicMock(side_effect=[TimeoutError("timed out"),
                                         TimeoutError("gone after restart")])
        cli = MagicMock()
        cli.transcribe.return_value = "salvaged words"

        with patch.object(Path, "exists", return_value=True), \
             patch.object(WhisperServerBackend, "_start_server", side_effect=fake_start), \
             patch.object(WhisperServerBackend, "_stop_server_internal"), \
             patch.object(WhisperServerBackend, "_cli_fallback", return_value=cli), \
             patch("urllib.request.urlopen", urlopen):
            result = backend.transcribe(str(sample_audio_file))

        assert result == "salvaged words"
        assert cli.transcribe.called, "words must be salvaged via whisper-cli, not dropped"

    def test_cli_fallback_resolves_existing_binary_when_sibling_missing(self, tmp_path):
        """H2 regression: on a server-only install the derived whisper-cli sibling
        is absent. The salvage backend must still point at a whisper-cli that
        EXISTS (here the CPU twin) — not a missing path that raises 'binary not
        found' and drops the chunk's words. (The old code blindly used the derived
        sibling; this exercises the resolver, which that path never did.)"""
        from wayfinder.core.transcriber import WhisperServerBackend

        server = tmp_path / "whisper-server"
        server.write_text("#!/bin/sh\n")            # server binary present
        cpu_cli = tmp_path / "whisper-cli-cpu"      # only the CPU twin is present
        cpu_cli.write_text("#!/bin/sh\n")
        # tmp_path/"whisper-cli" (the derived sibling) is deliberately absent.

        backend = WhisperServerBackend(whisper_server_binary=str(server), use_gpu=False)
        resolved = backend._cli_fallback().whisper_binary
        assert Path(resolved).exists(), "salvage backend must point at an existing binary"
        assert resolved == str(cpu_cli)

    def test_cli_fallback_uses_derived_sibling_when_present(self, tmp_path):
        """When the normal whisper-cli sibling IS present, resolve to it (not the
        CPU twin or PATH) — the common from-source layout stays unchanged."""
        from wayfinder.core.transcriber import WhisperServerBackend

        server = tmp_path / "whisper-server"
        server.write_text("#!/bin/sh\n")
        cli = tmp_path / "whisper-cli"
        cli.write_text("#!/bin/sh\n")

        backend = WhisperServerBackend(whisper_server_binary=str(server), use_gpu=False)
        assert backend._cli_fallback().whisper_binary == str(cli)

    def test_salvage_missing_cli_everywhere_raises_clear_error(
        self, tmp_path, sample_audio_file, monkeypatch
    ):
        """When NO whisper-cli exists anywhere (no sibling, no CPU twin, no bundle,
        nothing on PATH), a wedged server that also fails its retry must raise a
        CLEAR combined error — never a silent drop."""
        from wayfinder.core.transcriber import WhisperServerBackend, TranscriptionError

        server = tmp_path / "whisper-server"
        server.write_text("#!/bin/sh\n")
        model = tmp_path / "model.bin"
        model.write_text("x")
        monkeypatch.setattr("shutil.which", lambda *a, **k: None)  # nothing on PATH

        WhisperServerBackend._server_disabled = False
        backend = WhisperServerBackend(
            whisper_server_binary=str(server), model_path=str(model),
            use_gpu=False, timeout=1,
        )

        def fake_start(*a, **k):
            WhisperServerBackend._server_port = 8178

        urlopen = MagicMock(side_effect=[TimeoutError("wedged"),
                                         TimeoutError("gone after restart")])
        with patch.object(WhisperServerBackend, "_start_server", side_effect=fake_start), \
             patch.object(WhisperServerBackend, "_stop_server_internal"), \
             patch("urllib.request.urlopen", urlopen):
            with pytest.raises(TranscriptionError) as exc:
                backend.transcribe(str(sample_audio_file))
        assert "salvage also failed" in str(exc.value)

    def test_cpu_path_keeps_full_request_timeout(self, sample_audio_file: Path):
        """On the CPU path the adaptive request-timeout curve (which assumes GPU
        speed) must NOT apply. The 1s fixture would otherwise be sized to the GPU floor
        (15s), which a cold restart + base.en load can exceed — timing out and dropping
        the final chunk's words. CPU keeps the full configured budget.
        """
        from wayfinder.core.transcriber import WhisperServerBackend

        backend = WhisperServerBackend(use_gpu=False, timeout=30)

        def fake_start(*a, **k):
            WhisperServerBackend._server_port = 8178

        urlopen = MagicMock(return_value=self._make_resp("ok"))
        with patch.object(Path, "exists", return_value=True), \
             patch.object(WhisperServerBackend, "_start_server", side_effect=fake_start), \
             patch("urllib.request.urlopen", urlopen):
            backend.transcribe(str(sample_audio_file))

        assert urlopen.call_args.kwargs.get("timeout") == 30, \
            "CPU path must use the full timeout, not the GPU-optimistic shrink"

    def test_gpu_path_still_shrinks_request_timeout(self, sample_audio_file: Path):
        """Guard the inverse: on GPU the adaptive cap is still applied, but with a
        GENEROUS floor so a slow-but-completing chunk under GPU contention finishes on
        the first try instead of timing out into a model reload. The 1s fixture floors
        at 15s (min(30, max(15, 1*4+10))).
        """
        from wayfinder.core.transcriber import WhisperServerBackend

        backend = WhisperServerBackend(use_gpu=True, timeout=30)

        def fake_start(*a, **k):
            WhisperServerBackend._server_port = 8178

        urlopen = MagicMock(return_value=self._make_resp("ok"))
        with patch.object(Path, "exists", return_value=True), \
             patch.object(WhisperServerBackend, "_start_server", side_effect=fake_start), \
             patch("urllib.request.urlopen", urlopen):
            backend.transcribe(str(sample_audio_file))

        assert urlopen.call_args.kwargs.get("timeout") == 15.0, \
            "GPU path should use the generous adaptive floor (15s for the 1s fixture)"

    @pytest.mark.parametrize("dur_s,expected_timeout", [
        (1.0, 15.0),   # below floor: 1*4+10=14 -> clamped up to the 15s floor
        (2.0, 18.0),   # interior above floor: 2*4+10=18
        (3.0, 22.0),   # interior: 3*4+10=22
        (5.0, 30.0),   # cap knee: 5*4+10=30 == self.timeout
        (20.0, 30.0),  # above cap: 20*4+10=90 -> capped at self.timeout (30)
    ])
    def test_gpu_adaptive_timeout_knees(self, tmp_path, dur_s, expected_timeout):
        """M2: GPU request timeout = min(self.timeout, max(15, dur*4+10)). Exercise
        the floor, the interior, and the cap knee so a change to the adaptive formula
        can't silently over-shrink (needless restarts/reloads under contention) or
        exceed the self.timeout cap (recovery blowing the watchdog). Characterization
        guard — passes today."""
        import wave as _wave
        from wayfinder.core.transcriber import WhisperServerBackend

        wav = tmp_path / f"clip_{dur_s}.wav"
        with _wave.open(str(wav), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(b"\x00\x00" * int(16000 * dur_s))

        WhisperServerBackend._server_disabled = False
        backend = WhisperServerBackend(use_gpu=True, timeout=30)

        def fake_start(*a, **k):
            WhisperServerBackend._server_port = 8178

        urlopen = MagicMock(return_value=self._make_resp("ok"))
        with patch.object(WhisperServerBackend, "_start_server", side_effect=fake_start), \
             patch("urllib.request.urlopen", urlopen):
            backend.transcribe(str(wav))

        assert urlopen.call_args.kwargs.get("timeout") == expected_timeout

    def test_gpu_retry_state_is_thread_safe(self):
        """M1: _schedule_gpu_retry mutates the module-global _gpu_retry_state, which
        is touched by both chunk-transcription workers and the background GPU-probe
        thread. The read-modify-write must be serialized so concurrent GPU failures
        don't lose failure-count increments (which would mis-time the retry backoff)."""
        import threading as _t
        from wayfinder.core import transcriber as _T

        binary = "/fake/whisper-cli-m1"
        _T._gpu_retry_state.pop(binary, None)

        n_threads, per_thread = 8, 250
        barrier = _t.Barrier(n_threads)

        def hammer():
            barrier.wait()  # release all threads together to maximize contention
            for _ in range(per_thread):
                _T._schedule_gpu_retry(binary)

        threads = [_t.Thread(target=hammer) for _ in range(n_threads)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()

        try:
            assert _T._gpu_retry_state[binary]["failures"] == n_threads * per_thread, \
                "concurrent GPU failures lost increments — the RMW is not serialized"
        finally:
            _T._gpu_retry_state.pop(binary, None)  # don't leak into other tests


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

    def test_whisper_cpp_and_server_prompt_strategy_match(self):
        """CLI + server backends must prefer prior-chunk context over the base prompt.

        Chunked dictation feeds context from the previous segment; stacking base
        prompt + context blew the prompt budget and diluted continuity.
        """
        from wayfinder.core.transcriber import WhisperCppBackend, WhisperServerBackend

        backends = [
            WhisperCppBackend(
                whisper_binary="/nonexistent",
                model_path="/nonexistent",
                prompt="Generic style seed.",
                custom_vocabulary=["Wayfinder"],
            ),
            WhisperServerBackend(
                prompt="Generic style seed.",
                custom_vocabulary=["Wayfinder"],
            ),
        ]
        for backend in backends:
            first = backend._build_prompt()
            assert "Generic style seed." in first, backend.get_name()
            assert "Wayfinder" in first
            assert "Vocabulary:" not in first

            later = backend._build_prompt(
                context="and then we continued speaking about the release"
            )
            assert "continued speaking" in later, backend.get_name()
            assert "Generic style seed." not in later, backend.get_name()
            assert "Wayfinder" in later

    def test_get_backend_faster_whisper(self, sample_config: dict, monkeypatch: pytest.MonkeyPatch):
        """Test factory creates FasterWhisperBackend with all params."""
        from wayfinder.core.transcriber import get_backend, FasterWhisperBackend

        # Licensed: faster_whisper is a premium backend (F1 gate) — license so this tests
        # selection, not the unlicensed downgrade.
        monkeypatch.setattr("wayfinder.license.FeatureGate.has_feature", lambda self, f: True)
        sample_config["transcription_backend"] = "faster_whisper"
        sample_config["no_speech_threshold"] = 0.4
        sample_config["faster_whisper_vad_enabled"] = False
        # Vulkan gpu_device must NOT become the CUDA ordinal.
        sample_config["gpu_device"] = "1"
        sample_config["faster_whisper_cuda_device"] = "2"

        backend = get_backend(sample_config)

        assert isinstance(backend, FasterWhisperBackend)
        assert backend.no_speech_threshold == 0.4
        assert backend.vad_enabled is False
        assert backend._cuda_device_index() == 2
        # Vulkan index must not be used as CUDA device_index
        assert backend._cuda_device_index() != int(sample_config["gpu_device"])

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

    def test_supports_gpu_uses_ctranslate2_not_torch(self, monkeypatch):
        """Regression: torch.cuda (True on ROCm/AMD) must not claim FW GPU support.

        Faster-Whisper runs on CTranslate2. Stock CT2 wheels need NVIDIA CUDA;
        AMD ROCm torch reporting True previously made us open device='cuda' and
        fail, then silently CPU-fall-back after a confusing error.
        """
        from wayfinder.core.transcriber import FasterWhisperBackend

        FasterWhisperBackend._gpu_unavailable_logged = False
        backend = FasterWhisperBackend(use_gpu=True)
        backend._gpu_supported = None

        monkeypatch.setattr(backend, "is_available", lambda: True)
        monkeypatch.setattr(
            FasterWhisperBackend, "ctranslate2_cuda_device_count", staticmethod(lambda: 0)
        )
        # Even if someone reintroduces a torch check, CT2 count wins.
        import sys
        fake_torch = type(sys)("torch")
        fake_torch.cuda = type(sys)("cuda")
        fake_torch.cuda.is_available = lambda: True
        monkeypatch.setitem(sys.modules, "torch", fake_torch)

        assert backend.supports_gpu() is False

        backend._gpu_supported = None
        monkeypatch.setattr(
            FasterWhisperBackend, "ctranslate2_cuda_device_count", staticmethod(lambda: 1)
        )
        assert backend.supports_gpu() is True

    def test_get_model_stays_on_cpu_when_ct2_has_no_cuda(self, monkeypatch):
        """use_gpu=True + zero CT2 devices must load CPU int8 without trying CUDA."""
        import sys
        import types
        from wayfinder.core.transcriber import FasterWhisperBackend

        FasterWhisperBackend._model_cache.clear()
        backend = FasterWhisperBackend(model_size="tiny", use_gpu=True, compute_type="float16")
        backend._gpu_supported = None
        monkeypatch.setattr(backend, "is_available", lambda: True)
        monkeypatch.setattr(
            FasterWhisperBackend, "ctranslate2_cuda_device_count", staticmethod(lambda: 0)
        )

        calls = []

        class FakeWhisperModel:
            def __init__(self, model_size, device="cpu", device_index=0, compute_type="default"):
                calls.append(
                    {"model_size": model_size, "device": device, "compute_type": compute_type}
                )

        fake_fw = types.ModuleType("faster_whisper")
        fake_fw.WhisperModel = FakeWhisperModel
        monkeypatch.setitem(sys.modules, "faster_whisper", fake_fw)

        model = backend._get_model()
        assert model is not None
        assert calls == [{"model_size": "tiny", "device": "cpu", "compute_type": "int8"}]
        assert backend._active_device == "cpu"


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


class TestWhisperArtifactCleanup:
    """Regression coverage for stock Whisper silence/noise hallucinations."""

    def test_repetition_cleanup_defaults_on_but_can_be_disabled(self):
        from wayfinder.core.transcriber import clean_whisper_artifacts

        alphabet = "A, B, C, D, E, F, G, H, I, J, K, L, M, N, O, P, Q, R, S, T, U, V, W, X, Y, and Z."
        assert clean_whisper_artifacts(alphabet) != alphabet  # established default
        assert clean_whisper_artifacts(alphabet, collapse_repetitions=False) == alphabet
        # The switch affects only loop removal, not other basic artifact guards.
        assert clean_whisper_artifacts(
            "Hello [MUSIC] world.", collapse_repetitions=False
        ) == "Hello world."

    def test_a_sentence_length_repetition_loop_is_collapsed(self):
        from wayfinder.core.transcriber import clean_whisper_artifacts

        # _collapse_whisper_repetitions alone only tries 2-4 word windows, so
        # a stuck decode looping a whole sentence needs the sibling collapse
        # that clean_whisper_artifacts also runs under the same switch. Five
        # copies clears LOOP_MIN_RUN, the one bar cleanup and the chunk-level
        # re-hear now share (Sol loop r4) — three intentional copies must
        # survive cleanup; see test_three_ordinary_paced_copies_survive_
        # cleanup_in_full below.
        pre = "Let's go over the plan first."
        loop = "If the player is level one they're going to be level one."
        looped_text = " ".join([pre, loop, loop, loop, loop, loop])
        assert clean_whisper_artifacts(looped_text) == f"{pre} {loop}"
        assert clean_whisper_artifacts(looped_text, collapse_repetitions=False) == looped_text

    def test_three_ordinary_paced_copies_survive_cleanup_in_full(self):
        from wayfinder.core.transcriber import clean_whisper_artifacts

        # Sol loop r3: three intentional, ordinary-paced repeats of a short
        # sentence used to collapse to one copy even with no chunk timing to
        # suggest a stuck decode. clean_whisper_artifacts runs at LOOP_MIN_RUN
        # (5) now, so 3-4 copies survive; only a longer run (5+, see
        # test_a_sentence_length_repetition_loop_is_collapsed above) is
        # collapsed outright.
        sentence = "We need to go home."
        text = " ".join([sentence] * 3)
        assert clean_whisper_artifacts(text) == text

    def test_an_unpunctuated_phrase_x3_survives_cleanup_in_full(self):
        from wayfinder.core.transcriber import clean_whisper_artifacts

        phrase = "we need to go home"
        text = " ".join([phrase] * 3)
        assert clean_whisper_artifacts(text) == text

    def test_a_closing_quote_at_a_sentence_boundary_survives_cleanup(self):
        from wayfinder.core.transcriber import clean_whisper_artifacts

        # Sol loop r3, finding 1: a closing quote right after the period used
        # to defeat sentence splitting and let a window scan delete real
        # words at the far sentence's end.
        text = (
            '"We need to go home." "We need to go home." '
            '"We need to go home before dinner."'
        )
        assert clean_whisper_artifacts(text) == text

    def test_a_phrase_loop_inside_one_sentence_collapses_despite_terminal_punctuation(self):
        from wayfinder.core.transcriber import clean_whisper_artifacts

        # A single terminal period hides the loop from collapse_sentence_loops
        # (it's all one "sentence"); collapse_phrase_window_loops used to skip
        # any punctuated text entirely, so this slipped through both. Five
        # copies clears the one bar, LOOP_MIN_RUN (Sol loop r4).
        phrase = "we need to update the database before lunch"
        looped_text = " ".join([phrase] * 5) + "."
        cleaned = clean_whisper_artifacts(looped_text)
        assert cleaned != looped_text
        assert cleaned.count("database") == 1

    def test_prefix_sentences_do_not_inflate_three_full_copies_past_the_bar(self):
        from wayfinder.core.transcriber import clean_whisper_artifacts

        # Sol loop r4, finding 2: three full copies plus two shorter,
        # distinct sentences that happen to be prefixes of the anchor used
        # to collapse all five — the prefixes inflated the run's length past
        # the bar even though full_copies alone (3) never reached it.
        anchor = "We need to leave this building right now."
        shorter = "We need to leave this building."
        shortest = "We need to leave."
        text = " ".join([anchor] * 3 + [shorter, shortest])
        assert clean_whisper_artifacts(text) == text

    def test_decimal_sentences_keep_their_numeric_meaning_through_cleanup(self):
        from wayfinder.core.transcriber import clean_whisper_artifacts

        # Sol loop r4, finding 4: _norm stripped a leading decimal point,
        # equating ".5" and "5" and collapsing distinct sentences into one.
        decimal = "Please set the scale to .5."
        whole = "Please set the scale to 5."
        alternating = " ".join([decimal, whole, decimal, whole, decimal])
        assert clean_whisper_artifacts(alternating) == alternating
        assert clean_whisper_artifacts(" ".join([decimal] * 5)) == decimal

    def test_the_reported_shape_collapses_fully_except_the_intentional_copies(self):
        from wayfinder.core.transcriber import clean_whisper_artifacts

        # The user's real case (Sol loop r4): 3 intentional copies of a
        # "level 5" sentence, then a stuck decode — 8 full copies of a
        # "level 1" sentence, one cut short, then one more full copy.
        level5 = "If the player is level 5 they're going to be level 1."
        level1 = "If the player is level 1 they're going to be level 1."
        level1_cut = "If the player is level 1."
        text = " ".join([level5] * 3 + [level1] * 8 + [level1_cut, level1])
        assert clean_whisper_artifacts(text) == " ".join([level5] * 3 + [level1])

    @pytest.mark.parametrize("text", [
        "I'll see you next time.",
        "We'll see you next time.",
        "We'll be right back.",
        "I'm going to talk to you about what's going on today.",
        (
            "I'm going to talk to you about what's going on today. "
            "I'm going to talk to you about what's going on today."
        ),
    ])
    def test_drops_known_whole_output_silence_hallucinations(self, text):
        from wayfinder.core.transcriber import clean_whisper_artifacts

        assert clean_whisper_artifacts(text) == ""

    def test_drop_print_never_includes_the_dropped_text(self, capsys):
        """Finding 4 (Sol pr67 r2, scoped): the phrase-hallucination drop
        print must say it happened without repeating what was heard."""
        from wayfinder.core.transcriber import clean_whisper_artifacts

        secret = "We'll be right back."
        assert clean_whisper_artifacts(secret) == ""
        out = capsys.readouterr().out
        assert "Dropped" in out
        assert secret not in out and "right back" not in out

    def test_keeps_known_phrase_inside_real_sentence(self):
        from wayfinder.core.transcriber import clean_whisper_artifacts

        text = "I'll see you next time after the build finishes."

        assert clean_whisper_artifacts(text) == text

    def test_keeps_legitimate_repeated_sentence(self):
        from wayfinder.core.transcriber import clean_whisper_artifacts

        text = "Ship it today. Ship it today."

        assert clean_whisper_artifacts(text) == text

    # Sol pr67 r3, finding 2's scope guard: the safety copy gets a
    # stricter, SEPARATE asterisk rule precisely so normal cleanup's own
    # output never has to change. These pin that down literally, and (when
    # a `base` checkout of this review's starting commit is available as a
    # sibling of this repo) also replay them against ITS clean_whisper_
    # artifacts in a subprocess, so a future edit that drifts the two
    # apart fails loudly instead of only matching a value written by hand.
    _CLEANUP_PARITY_INPUTS = (
        "<|endoftext|> Okay.",
        "<|0.00|> So,",
        "Thank you for watching…",
        "Hello [MUSIC] world.",
        "Ship it today. Ship it today.",
        "I'll see you next time after the build finishes.",
        "*laughing* Okay, let's go.",
        "Hello *world*, this is *emphasis* right.",
    )

    def test_normal_cleanup_preserves_unclosed_code_arguments(self):
        from wayfinder.core.transcriber import clean_whisper_artifacts
        # An unmatched run is not a completed annotation. The old regex
        # paired unrelated widths and deleted "args and" as though wrapped.
        assert clean_whisper_artifacts("*args and **kwargs") == "args and kwargs"

    @pytest.mark.parametrize("marks", ["*", "**", "***"])
    @pytest.mark.parametrize("body_length", [40, 41])
    def test_annotation_limit_counts_the_body_without_delimiters(self, marks, body_length):
        from wayfinder.core.transcriber import clean_whisper_artifacts
        body = "loud sounds of somebody opening the door".ljust(body_length, "x")
        assert len(body) == body_length
        text = f"{marks}{body}{marks} please come inside"
        expected = "please come inside" if body_length <= 40 else f"{body} please come inside"
        assert clean_whisper_artifacts(text) == expected

    def test_normal_cleanup_retains_its_long_annotation_limit(self):
        from wayfinder.core.transcriber import clean_whisper_artifacts
        speech = "these are the longer spoken words that must remain in the transcript"
        assert clean_whisper_artifacts(f"*{speech}*") == speech

    def test_normal_cleanup_output_is_unchanged_for_safety_copy_fix_inputs(self):
        """These inputs exercise findings 2, 4 and 5's machinery
        (asterisk handling, special-token stripping, punctuation folding),
        all of which had to change the SAFETY COPY without changing what
        normal cleanup returns -- pinned here so a future refactor that
        merges the two paths can't silently do so."""
        from wayfinder.core.transcriber import clean_whisper_artifacts

        expected = {
            "<|endoftext|> Okay.": "Okay.",
            "<|0.00|> So,": "So,",
            "Thank you for watching…": "",
            "Hello [MUSIC] world.": "Hello world.",
            "Ship it today. Ship it today.": "Ship it today. Ship it today.",
            "I'll see you next time after the build finishes.":
                "I'll see you next time after the build finishes.",
            "*laughing* Okay, let's go.": "Okay, let's go.",
            "Hello *world*, this is *emphasis* right.": "Hello , this is right.",
        }
        for text in self._CLEANUP_PARITY_INPUTS:
            assert clean_whisper_artifacts(text) == expected[text], text

    def test_normal_cleanup_matches_the_pre_fix_base_commit(self):
        """Best-effort cross-check against this review round's starting
        commit (base/, a sibling of this checkout under the same pr67-r3
        scratch work dir) -- skipped where that sibling doesn't exist, e.g.
        once this patch has landed and base/ is gone."""
        import json
        import subprocess
        import sys
        from pathlib import Path

        from wayfinder.core.transcriber import clean_whisper_artifacts

        base_root = Path(__file__).resolve().parents[2] / "base"
        if not (base_root / "src" / "wayfinder" / "core" / "transcriber.py").exists():
            pytest.skip("no sibling base/ checkout for this review round")

        script = (
            "import sys, json\n"
            "sys.path.insert(0, sys.argv[1] + '/src')\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "from wayfinder.core.transcriber import clean_whisper_artifacts\n"
            "texts = json.loads(sys.argv[2])\n"
            "print(json.dumps([clean_whisper_artifacts(t) for t in texts], ensure_ascii=False))\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", script, str(base_root),
             json.dumps(list(self._CLEANUP_PARITY_INPUTS))],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, result.stderr
        base_outputs = json.loads(result.stdout.strip().splitlines()[-1])
        tree_outputs = [clean_whisper_artifacts(t) for t in self._CLEANUP_PARITY_INPUTS]
        assert tree_outputs == base_outputs

    # Sol pr67 r4, finding 1: the noise path's new punctuation/quote fold
    # and generic-repeat rejection leaked into normal cleanup. Normal
    # cleanup's SPEECH-verdict output must match 2dad2af (orig/) exactly
    # for each of these, including the two it used to wrongly drop.
    _SPEECH_PATH_PARITY_INPUTS = (
        "“Thank you”",             # “Thank you” -- curly quotes
        "Please subscribe",
        "We’ll be right back.",          # curly apostrophe
        "Thank you for watching…",       # trailing ellipsis
        "*args and **kwargs",
        "*loud noise* Okay.",
        "Thank you. Thank you, 李明。",         # mixed CJK name
        "谢谢观看。谢谢观看。",  # mixed CJK repeat
    )

    def test_normal_cleanup_matches_orig_for_the_noise_check_scope_guard(self):
        """Finding 1 (Sol pr67 r4): replayed against orig/ (2dad2af, this
        review round's starting commit) in a subprocess, the same way the
        base/ parity check above runs; skipped where that sibling doesn't
        exist, e.g. once this patch has landed and orig/ is gone."""
        import json
        import subprocess
        import sys
        from pathlib import Path

        from wayfinder.core.transcriber import clean_whisper_artifacts

        orig_root = Path(__file__).resolve().parents[2] / "orig"
        if not (orig_root / "src" / "wayfinder" / "core" / "transcriber.py").exists():
            pytest.skip("no sibling orig/ checkout for this review round")

        script = (
            "import sys, json\n"
            "sys.path.insert(0, sys.argv[1] + '/src')\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "from wayfinder.core.transcriber import clean_whisper_artifacts\n"
            "texts = json.loads(sys.argv[2])\n"
            "print(json.dumps([clean_whisper_artifacts(t) for t in texts], ensure_ascii=False))\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", script, str(orig_root),
             json.dumps(list(self._SPEECH_PATH_PARITY_INPUTS))],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, result.stderr
        orig_outputs = json.loads(result.stdout.strip().splitlines()[-1])
        tree_outputs = [clean_whisper_artifacts(t) for t in self._SPEECH_PATH_PARITY_INPUTS]
        assert tree_outputs == orig_outputs

    def test_drops_observed_vocabulary_prompt_leak_when_hints_are_active(self):
        from wayfinder.core.transcriber import drop_whisper_prompt_leak

        leaked = (
            "Vocabulary: We're in the front of the audience. We're taking the alarm, "
            "and now they're in the moment."
        )
        assert drop_whisper_prompt_leak(leaked, ["daan"]) == ""

    def test_keeps_vocabulary_word_when_it_is_not_the_internal_marker(self):
        from wayfinder.core.transcriber import drop_whisper_prompt_leak

        text = "We should improve the vocabulary in this paragraph."
        assert drop_whisper_prompt_leak(text, ["daan"]) == text


class TestTranscribeWithConfig:
    """Test the high-level transcribe_with_config function."""

    @patch("wayfinder.core.transcriber.get_backend")
    def test_raw_transcript_bypasses_every_aura_text_pass(
        self, mock_get_backend, sample_config: dict, sample_audio_file: Path
    ):
        from wayfinder.core.transcriber import transcribe_with_config

        raw = "  A, B, C, D, E, F, G, H, I, J, K, L, M, N, O, P, Q, R, S, T, U, V, W, X, Y, and Z.\n"
        backend = MagicMock()
        backend.transcribe.return_value = raw
        mock_get_backend.return_value = backend
        sample_config.update({
            "raw_transcription": True,
            "collapse_whisper_repetitions": True,
            "post_processing_enabled": True,
            "ensure_punctuation": True,
        })

        with patch("wayfinder.core.transcriber.clean_whisper_artifacts") as cleanup, patch(
            "wayfinder.core.transcriber.normalize_whisper_caps"
        ) as caps, patch("wayfinder.core.postprocessor.process_with_config") as llm:
            result = transcribe_with_config(str(sample_audio_file), sample_config)

        assert result == raw
        cleanup.assert_not_called()
        caps.assert_not_called()
        llm.assert_not_called()

    @patch("wayfinder.core.transcriber.get_backend")
    def test_only_repetition_switch_off_keeps_alphabet_with_other_cleanup_on(
        self, mock_get_backend, sample_config: dict, sample_audio_file: Path
    ):
        from wayfinder.core.transcriber import transcribe_with_config

        backend = MagicMock()
        backend.transcribe.return_value = "A, B, C, D, E, F, G, H, I, J, K, L, M, N, O, P, Q, R, S, T, U, V, W, X, Y, and Z."
        mock_get_backend.return_value = backend
        sample_config.update({
            "raw_transcription": False,
            "collapse_whisper_repetitions": False,
            "post_processing_enabled": False,
            "output_tone": "professional",
            "ensure_punctuation": False,
        })

        result = transcribe_with_config(str(sample_audio_file), sample_config)

        assert "c, d, e" in result.lower()
        assert "x, y, and z" in result.lower()

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

    @patch("wayfinder.core.transcriber.get_backend")
    def test_transcribe_with_config_drops_vocabulary_prompt_leak(
        self, mock_get_backend, sample_config: dict, sample_audio_file: Path
    ):
        from wayfinder.core.transcriber import transcribe_with_config

        sample_config["custom_vocabulary"] = ["daan"]
        mock_backend = MagicMock()
        mock_backend.transcribe.return_value = "Vocabulary: invented words from silence"
        mock_get_backend.return_value = mock_backend

        assert transcribe_with_config(str(sample_audio_file), sample_config) == ""

    @patch("wayfinder.core.transcriber.get_backend")
    def test_raw_out_captures_the_backends_text_before_cleanup(
        self, mock_get_backend, sample_config: dict, sample_audio_file: Path
    ):
        from wayfinder.core.transcriber import transcribe_with_config

        # With the default config, cleanup collapses a 13-copy sentence loop
        # before returning. A caller deciding "did this chunk loop?" off the
        # return value alone would never see it — raw_out hands back the
        # backend's text untouched (Sol loop r1).
        loop = "if the player is level one they are going to be level one."
        raw = " ".join([loop] * 13)
        backend = MagicMock()
        backend.transcribe.return_value = raw
        mock_get_backend.return_value = backend
        sample_config.update({"post_processing_enabled": False})

        raw_out = []
        result = transcribe_with_config(str(sample_audio_file), sample_config, raw_out=raw_out)

        assert raw_out == [raw]
        assert len(raw_out[0].split()) == len(raw.split())
        assert len(result.split()) < len(raw.split())  # the default cleanup collapsed it

    @patch("wayfinder.core.transcriber.get_backend")
    def test_a_phrase_loop_with_terminal_punctuation_collapses_under_ensure_punctuation(
        self, mock_get_backend, sample_config: dict, sample_audio_file: Path
    ):
        from wayfinder.core.transcriber import transcribe_with_config

        # End to end through the ensure_punctuation=True path (the default):
        # the loop is one "sentence" by punctuation, so it must be caught by
        # word-window detection, not sentence-level detection. Five copies to
        # clear the one bar (LOOP_MIN_RUN; Sol loop r4).
        phrase = "we need to update the database before lunch"
        backend = MagicMock()
        backend.transcribe.return_value = " ".join([phrase] * 5) + "."
        mock_get_backend.return_value = backend
        sample_config.update({"ensure_punctuation": True, "post_processing_enabled": False})

        result = transcribe_with_config(str(sample_audio_file), sample_config)

        assert result.lower().count("database") == 1


@pytest.mark.linux_only
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
            use_gpu=True,
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

    def test_a_reset_during_the_gpu_attempt_runs_no_cpu_attempt(self, tmp_path, monkeypatch):
        # Sol mac r6: a dictation retired while its GPU attempt ran still
        # launched the CPU attempt and memoized CPU for the session.
        from wayfinder.core import transcriber
        cls = transcriber.WhisperServerBackend
        monkeypatch.setattr(cls, "_abandoned", cls._abandoned)
        marker = tmp_path / "cpu-runs"
        backend, audio = self._backend(
            tmp_path, gpu_body="kill -SEGV $$",
            cpu_body=f'if [ "$1" = "-m" ]; then echo run >> "{marker}"; fi; echo "words nobody will type"',
        )
        run = transcriber.run_tracked

        def reset_mid_attempt(cmd, **kwargs):
            result = run(cmd, **kwargs)
            if cmd[0] == backend.whisper_binary and cmd[1] == "-m":
                cls.abandon_requests()   # the sleep handler's reset
            return result

        monkeypatch.setattr(transcriber, "run_tracked", reset_mid_attempt)
        with pytest.raises(transcriber.RequestAbandoned):
            backend.transcribe(audio)
        assert not marker.exists()
        assert backend.whisper_binary not in transcriber._CPU_FALLBACK_ACTIVE

    def test_a_retired_dictation_starts_no_whisper_cli(self, tmp_path, monkeypatch):
        from wayfinder.core import transcriber
        cls = transcriber.WhisperServerBackend
        monkeypatch.setattr(cls, "_abandoned", cls._abandoned)
        backend, audio = self._backend(tmp_path, gpu_body='echo "words"', cpu_body=None)
        backend._supported_flags = lambda **_k: pytest.fail("a retired dictation probed whisper-cli")
        monkeypatch.setattr(transcriber, "run_tracked",
                            lambda *_a, **_k: pytest.fail("a retired dictation ran whisper-cli"))
        token = cls.request_token()
        cls.abandon_requests()
        with transcriber.dictation_requests(token):
            with pytest.raises(transcriber.RequestAbandoned):
                backend.transcribe(audio)

    def test_cpu_only_install_unaffected(self, tmp_path):
        """From-source installs (no sibling) keep working exactly as before."""
        backend, audio = self._backend(tmp_path, gpu_body='echo "normal text"', cpu_body=None)
        backend.use_gpu = False
        assert backend.transcribe(audio) == "normal text"

    def test_explicit_cpu_uses_packaged_cpu_binary_only(self, tmp_path):
        """Unchecked GPU must not even probe the packaged Vulkan executable."""
        from wayfinder.core.transcriber import WhisperCppBackend

        gpu_marker = tmp_path / "gpu-started"
        gpu = tmp_path / "whisper-cli"
        self._write_stub(gpu, f'touch "{gpu_marker}"; echo gpu-text')
        cpu = tmp_path / "whisper-cli-cpu"
        self._write_stub(
            cpu,
            'if [ "$1" = "--help" ]; then echo "--no-gpu --no-timestamps"; '
            'exit 0; fi; echo cpu-text',
        )
        model = tmp_path / "model.bin"; model.write_bytes(b"x")
        audio = tmp_path / "audio.wav"; audio.write_bytes(b"x")
        backend = WhisperCppBackend(
            whisper_binary=str(gpu), model_path=str(model), use_gpu=False
        )

        assert backend.transcribe(str(audio)) == "cpu-text"
        assert not gpu_marker.exists()

    def test_explicit_gpu_uses_gpu_binary(self, tmp_path):
        backend, audio = self._backend(
            tmp_path, gpu_body="echo gpu-text", cpu_body="echo cpu-text"
        )

        assert backend.use_gpu is True
        assert backend.transcribe(audio) == "gpu-text"


class TestPackagedServerCpuRouting:
    def _backend(self, tmp_path, *, use_gpu):
        from wayfinder.core.transcriber import WhisperServerBackend

        gpu = tmp_path / "whisper-server"; gpu.write_text("#!/bin/sh\n"); gpu.chmod(0o755)
        cpu = tmp_path / "whisper-server-cpu"; cpu.write_text("#!/bin/sh\n"); cpu.chmod(0o755)
        model = tmp_path / "ggml-base.en.bin"; model.write_bytes(b"x")
        return WhisperServerBackend(
            whisper_server_binary=str(gpu), model_path=str(model), use_gpu=use_gpu
        )

    def test_cpu_mode_starts_cpu_server_without_gpu_flag(self, tmp_path):
        attempts = self._backend(tmp_path, use_gpu=False)._server_cmd_attempts(8178)

        assert attempts[0][0].endswith("whisper-server-cpu")
        assert "-ng" not in attempts[0]

    def test_gpu_mode_starts_gpu_server_first(self, tmp_path):
        attempts = self._backend(tmp_path, use_gpu=True)._server_cmd_attempts(8178)

        assert attempts[0][0].endswith("whisper-server")
        assert "-ng" not in attempts[0]


@pytest.mark.linux_only
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
            use_gpu=True,
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

    def test_probe_succeeds_returns_none_on_a_pre_spawn_expiry(self, tmp_path):
        """Sol mac r21: HelperNotStarted during the probe itself is no verdict,
        the same as _probe_gpu_ok's None on the llama side."""
        from wayfinder.utils.child_supervisor import HelperNotStarted
        backend, _audio = self._backend(tmp_path, "echo gpu-text")
        with patch("wayfinder.core.transcriber.run_tracked",
                   side_effect=HelperNotStarted(["x"], 60.0)):
            assert backend._gpu_probe_succeeds() is None

    def test_recovery_probe_is_a_no_op_on_a_pre_spawn_expiry(self, tmp_path):
        """Neither a recovery nor a backoff on no-verdict: the next scheduled
        retry gets an unspent try instead of this one counting as a failure
        it never ran (Sol mac r21)."""
        from wayfinder.core import transcriber
        backend, audio = self._backend(tmp_path, "kill -SEGV $$")
        backend.transcribe(audio)   # failures = 1, CPU fallback active
        before = dict(transcriber._gpu_retry_state[backend.whisper_binary])
        with patch.object(backend, "_gpu_probe_succeeds", return_value=None):
            backend._probe_gpu_recovery()
        assert transcriber._gpu_retry_state[backend.whisper_binary] == before
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


@pytest.mark.usefixtures("no_host_binaries")
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
        model = tmp_path / "ggml-base.en.bin"; model.write_bytes(b"\x00")
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

    def test_blank_server_binary_is_not_available(self, tmp_path):
        """Regression for live crash: Path('') used to make cwd look like a server."""
        from wayfinder.core.transcriber import WhisperServerBackend

        model = tmp_path / "m.bin"
        model.write_bytes(b"\x00")
        b = WhisperServerBackend(whisper_server_binary="", model_path=str(model))

        assert b.is_available() is False
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
        # Executable, so discovery keeps the configured binary instead of a
        # whisper-cli the host happens to have on PATH.
        for name in ("whisper-cli", "whisper-server"):
            (tmp_path / name).write_text("#!/bin/sh\n")
            (tmp_path / name).chmod(0o755)
        (tmp_path / "ggml-base.en.bin").write_bytes(b"\x00")
        cfg = {"transcription_backend": "whisper_cpp", "whisper_server_mode": True,
               "whisper_binary": str(tmp_path / "whisper-cli"),
               "model_path": str(tmp_path / "ggml-base.en.bin")}
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


@pytest.mark.usefixtures("no_host_binaries")
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
        # Executable, so discovery keeps the configured binary instead of a
        # whisper-cli the host happens to have on PATH.
        server.chmod(0o755)
        cli.chmod(0o755)
        model = tmp_path / "ggml-base.en.bin"
        model.write_bytes(b"\x00")
        cfg = {"whisper_server_mode": True, "whisper_binary": str(cli),
               "model_path": str(model)}
        assert isinstance(get_backend(cfg), WhisperServerBackend)

    def test_get_backend_falls_back_to_cli_when_server_missing(self, tmp_path):
        from wayfinder.core.transcriber import get_backend, WhisperCppBackend
        # Only whisper-cli exists, no whisper-server next to it.
        cli = tmp_path / "whisper-cli"
        cli.write_text("#!/bin/sh\n")
        # A Free-tier model name, so the result doesn't hinge on whatever
        # license gate an earlier test left cached: a gated name gets swapped
        # for a missing Base path, which disables server mode for the wrong reason.
        model = tmp_path / "ggml-base.en.bin"
        model.write_bytes(b"\x00")
        cfg = {"whisper_server_mode": True, "whisper_binary": str(cli),
               "model_path": str(model)}
        backend = get_backend(cfg)
        assert isinstance(backend, WhisperCppBackend)
        assert not hasattr(backend, "whisper_server_binary")

    def test_blank_whisper_binary_does_not_select_empty_server(
        self, tmp_path, monkeypatch
    ):
        """The live config had whisper_binary='', which must not become Popen([''])."""
        from wayfinder.core import transcriber
        from wayfinder.core.transcriber import get_backend, WhisperCppBackend

        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setattr(transcriber.shutil, "which", lambda name: None)
        model = tmp_path / "m.bin"
        model.write_bytes(b"\x00")

        backend = get_backend({
            "whisper_server_mode": True,
            "whisper_binary": "",
            "model_path": str(model),
        })

        assert isinstance(backend, WhisperCppBackend)
        assert backend.whisper_binary
        assert backend.whisper_binary != ""

    @pytest.mark.linux_only
    def test_blank_whisper_binary_resolves_existing_home_cli(
        self, tmp_path, monkeypatch
    ):
        from wayfinder.core import transcriber
        from wayfinder.core.transcriber import get_backend, WhisperCppBackend

        home_cli = tmp_path / "whisper.cpp" / "build" / "bin" / "whisper-cli"
        home_cli.parent.mkdir(parents=True)
        home_cli.write_text("#!/bin/sh\n")
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setattr(transcriber.shutil, "which", lambda name: None)
        model = tmp_path / "m.bin"
        model.write_bytes(b"\x00")

        backend = get_backend({
            "whisper_server_mode": True,
            "whisper_binary": "",
            "model_path": str(model),
        })

        assert isinstance(backend, WhisperCppBackend)
        assert backend.whisper_binary == str(home_cli)

    @pytest.mark.linux_only
    def test_source_mode_ignores_flatpak_path_candidates(
        self, tmp_path, monkeypatch
    ):
        """Source/test runs must not silently select bundled /app binaries."""
        from wayfinder.core import transcriber
        from wayfinder.core.transcriber import get_backend, WhisperCppBackend

        home_cli = tmp_path / "whisper.cpp" / "build" / "bin" / "whisper-cli"
        home_cli.parent.mkdir(parents=True)
        home_cli.write_text("#!/bin/sh\n")
        model = tmp_path / "m.bin"
        model.write_bytes(b"\x00")

        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setattr(transcriber, "IS_FLATPAK", False)
        monkeypatch.setattr(transcriber.shutil, "which", lambda name: f"/app/bin/{name}")

        backend = get_backend({
            "whisper_server_mode": True,
            "whisper_binary": "/app/bin/whisper-cli",
            "model_path": str(model),
        })

        assert isinstance(backend, WhisperCppBackend)
        assert backend.whisper_binary == str(home_cli)

    def test_flatpak_mode_allows_flatpak_path_candidates(
        self, tmp_path, monkeypatch
    ):
        """The source-mode guard must not disable real Flatpak bundled binaries."""
        from wayfinder.core import transcriber

        monkeypatch.setattr(transcriber, "IS_FLATPAK", True)
        monkeypatch.setattr(transcriber, "_existing_file", lambda path: path == "/app/bin/whisper-cli")

        assert transcriber._resolve_whisper_cli_binary("") == "/app/bin/whisper-cli"


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


class TestServerReuseIdentity:
    """The resident server's identity for reuse = (alive, model path, CPU/GPU
    mode). A use_gpu flip — e.g. the toggle a user hits right after activating
    Ultra — must FAIL reuse so the next dictation respawns with GPU flags,
    even if nothing calls shutdown() explicitly. Previously only toggle_gpu()'s
    explicit shutdown enforced this (single-write-site invariant)."""

    MODEL = "/models/ggml-base.en.bin"

    def _set_state(self, alive=True, model=MODEL, gpu=False, threads=4, vk=None):
        from wayfinder.core.transcriber import WhisperServerBackend
        proc = MagicMock()
        proc.poll.return_value = None if alive else 1
        WhisperServerBackend._server_process = proc
        WhisperServerBackend._server_model_path = model
        WhisperServerBackend._server_use_gpu = gpu
        WhisperServerBackend._server_vk_devices = vk
        # macOS/Windows also match -t (the backend's default is 4); set it
        # rather than inherit whatever an earlier test left on the class.
        WhisperServerBackend._server_threads = threads
        WhisperServerBackend._server_binary = None

    def _backend(self, gpu=False, model=MODEL):
        from wayfinder.core.transcriber import WhisperServerBackend
        return WhisperServerBackend(model_path=model, use_gpu=gpu, timeout=1)

    def teardown_method(self):
        from wayfinder.core.transcriber import WhisperServerBackend
        WhisperServerBackend._server_process = None
        WhisperServerBackend._server_model_path = ""
        WhisperServerBackend._server_use_gpu = None
        WhisperServerBackend._server_threads = None
        WhisperServerBackend._server_binary = None
        WhisperServerBackend._server_vk_devices = None

    @pytest.fixture
    def linux(self, monkeypatch):
        from wayfinder.core import transcriber
        monkeypatch.setattr(transcriber.sys, "platform", "linux")
        monkeypatch.delenv("GGML_VK_VISIBLE_DEVICES", raising=False)

    def test_vulkan_device_change_fails_reuse(self, linux, monkeypatch):
        # GPU just turned on: a dictation started the Vulkan server before the
        # device selection set GGML_VK_VISIBLE_DEVICES, so it runs on device 0.
        self._set_state(gpu=True, vk=None)
        monkeypatch.setenv("GGML_VK_VISIBLE_DEVICES", "1")
        assert self._backend(gpu=True)._server_reusable() is False
        self._set_state(gpu=True, vk="1")
        assert self._backend(gpu=True)._server_reusable() is True

    def test_a_cpu_server_ignores_the_vulkan_device(self, linux, monkeypatch):
        self._set_state(gpu=False, vk=None)
        monkeypatch.setenv("GGML_VK_VISIBLE_DEVICES", "1")
        assert self._backend(gpu=False)._server_reusable() is True

    def test_spawn_records_the_vulkan_device_it_started_on(self, linux, monkeypatch, tmp_path):
        from wayfinder.core.transcriber import WhisperServerBackend

        binary = tmp_path / "whisper-server"; binary.write_text("#!/bin/sh\n"); binary.chmod(0o755)
        model = tmp_path / "m.bin"; model.write_bytes(b"\x00")
        backend = WhisperServerBackend(whisper_server_binary=str(binary),
                                       model_path=str(model), use_gpu=True)
        WhisperServerBackend._server_process = None
        monkeypatch.setenv("GGML_VK_VISIBLE_DEVICES", "1")
        new_proc = MagicMock(); new_proc.poll.return_value = None
        with patch.object(WhisperServerBackend, "_stop_server_internal"), \
             patch.object(WhisperServerBackend, "_find_available_port", return_value=8200), \
             patch.object(WhisperServerBackend, "_is_our_server", return_value=True), \
             patch("subprocess.Popen", return_value=new_proc) as mock_popen, \
             patch("time.sleep"):
            backend._start_server(force=True)
        assert mock_popen.call_args.kwargs["env"]["GGML_VK_VISIBLE_DEVICES"] == "1"
        assert backend._server_reusable() is True
        monkeypatch.setenv("GGML_VK_VISIBLE_DEVICES", "0")  # the user picked another GPU
        assert backend._server_reusable() is False

    def test_same_model_same_mode_is_reusable(self):
        self._set_state(gpu=False)
        assert self._backend(gpu=False)._server_reusable() is True

    def test_gpu_flip_fails_reuse(self):
        # CPU server resident from the pre-activation warm-up; the user just
        # enabled GPU → the server must NOT be mistaken for suitable.
        self._set_state(gpu=False)
        assert self._backend(gpu=True)._server_reusable() is False

    def test_gpu_to_cpu_flip_also_fails_reuse(self):
        self._set_state(gpu=True)
        assert self._backend(gpu=False)._server_reusable() is False

    def test_model_change_fails_reuse(self):
        self._set_state()
        assert self._backend(model="/models/ggml-large-v3-turbo.bin")._server_reusable() is False

    def test_dead_process_fails_reuse(self):
        self._set_state(alive=False)
        assert self._backend()._server_reusable() is False

    def test_unknown_mode_fails_reuse(self):
        # _server_use_gpu None (adopted server / reset state) must never match
        # a request — not even a None/None coincidence.
        self._set_state()
        from wayfinder.core.transcriber import WhisperServerBackend
        WhisperServerBackend._server_use_gpu = None
        assert self._backend(gpu=False)._server_reusable() is False
        backend = self._backend(gpu=False)
        backend.use_gpu = None
        assert backend._server_reusable() is False

    def test_stop_clears_identity_even_without_local_process(self):
        # Adopted-server identity has no local Popen; shutdown must still
        # clear it so stale identity can't satisfy a future reuse check.
        from wayfinder.core.transcriber import WhisperServerBackend
        WhisperServerBackend._server_process = None
        WhisperServerBackend._server_model_path = self.MODEL
        WhisperServerBackend._server_use_gpu = True
        WhisperServerBackend._stop_server_internal()
        assert WhisperServerBackend._server_model_path == ""
        assert WhisperServerBackend._server_use_gpu is None


class TestServerAdoptionPerPlatform:
    """Linux keeps main's reuse of its own running server; macOS never adopts
    (its supervised child cannot outlive the app, and adopting an unowned
    listener would hand it recorded audio)."""

    @pytest.mark.parametrize("platform_name, expected", [
        ("linux", 8178), ("darwin", 8179), ("win32", 8179)])
    def test_occupied_port_reuse(self, monkeypatch, platform_name, expected):
        import socket as socket_module

        import wayfinder.core.transcriber as transcriber

        class FakeSocket:
            def __init__(self, *a, **k):
                pass

            def connect_ex(self, address):
                return 0 if address[1] == 8178 else 1  # only 8178 is occupied

            def close(self):
                pass

        monkeypatch.setattr(transcriber.sys, "platform", platform_name)
        monkeypatch.setattr(socket_module, "socket", FakeSocket)
        backend = transcriber.WhisperServerBackend.__new__(transcriber.WhisperServerBackend)
        backend.port = 8178
        monkeypatch.setattr(backend, "_is_our_server", lambda port, timeout=2: True, raising=False)

        assert backend._find_available_port() == expected


class TestServerReuseThreadsOnMacOS:
    """The resident server's -t is fixed at spawn; macOS respawns when it changes."""

    @pytest.mark.parametrize("platform_name, expected", [
        ("darwin", False), ("win32", False), ("linux", True)])
    def test_thread_change_breaks_reuse_only_on_macos(self, monkeypatch, platform_name, expected):
        import wayfinder.core.transcriber as transcriber

        class Alive:
            def poll(self):
                return None

        cls = transcriber.WhisperServerBackend
        monkeypatch.setattr(transcriber.sys, "platform", platform_name)
        monkeypatch.setattr(cls, "_server_process", Alive())
        monkeypatch.setattr(cls, "_server_model_path", "/m.bin")
        monkeypatch.setattr(cls, "_server_use_gpu", False)
        monkeypatch.setattr(cls, "_server_threads", 4)
        backend = cls.__new__(cls)
        backend.model_path, backend.use_gpu, backend.threads = "/m.bin", False, 12

        assert backend._server_reusable() is expected



class TestMacServerRequestFields:
    """macOS asks whisper-server for text only (no per-token timestamps)."""

    def _body_for(self, platform, sample_audio_file, monkeypatch, use_gpu=False):
        import json as _json
        from unittest.mock import MagicMock, patch
        from wayfinder.core import transcriber
        from wayfinder.core.transcriber import WhisperServerBackend

        monkeypatch.setattr(transcriber.sys, "platform", platform)
        WhisperServerBackend._server_disabled = False
        backend = WhisperServerBackend(use_gpu=use_gpu, timeout=5)
        resp = MagicMock()
        resp.read.return_value = _json.dumps({"text": "hello"}).encode()
        urlopen = MagicMock(return_value=resp)

        def fake_start(*a, **k):
            WhisperServerBackend._server_port = 8178

        with patch.object(Path, "exists", return_value=True), \
             patch.object(WhisperServerBackend, "_start_server", side_effect=fake_start), \
             patch("urllib.request.urlopen", urlopen):
            backend.transcribe(str(sample_audio_file))
        return urlopen.call_args[0][0].data

    def test_macos_disables_timestamps(self, sample_audio_file, monkeypatch):
        body = self._body_for("darwin", sample_audio_file, monkeypatch)
        assert b'name="no_timestamps"\r\n\r\ntrue' in body

    def test_windows_disables_timestamps(self, sample_audio_file, monkeypatch):
        body = self._body_for("win32", sample_audio_file, monkeypatch)
        assert b'name="no_timestamps"\r\n\r\ntrue' in body

    def test_linux_request_is_unchanged(self, sample_audio_file, monkeypatch):
        body = self._body_for("linux", sample_audio_file, monkeypatch)
        assert b"no_timestamps" not in body


@pytest.mark.usefixtures("no_host_binaries")
class TestGreedyDecoding:
    """docs/EVAL-2026-09-24.md: beam search never beat greedy on whisper.cpp."""

    def test_greedy_even_when_a_saved_config_carries_old_beam_keys(self):
        from wayfinder.core.transcriber import whisper_decoding
        assert whisper_decoding({}) == (1, 1)
        assert whisper_decoding({"accuracy_mode": "high", "beam_size": 8, "best_of": 5}) == (1, 1)

    @pytest.mark.parametrize("value,expected", [
        (5, (5, 5)), (8, (8, 5)), (12, (8, 5)), (0, (1, 1)), (-3, (1, 1)), ("x", (1, 1)), (None, (1, 1)),
    ])
    def test_hidden_override_is_clamped_to_what_whisper_cpp_accepts(self, value, expected):
        from wayfinder.core.transcriber import whisper_decoding
        assert whisper_decoding({"whisper_beam_size": value}) == expected

    @pytest.mark.parametrize("server_mode", [True, False])
    def test_server_and_cli_backends_get_greedy(self, server_mode, tmp_path):
        from wayfinder.core.transcriber import WhisperServerBackend, get_backend
        # Its own binaries: without a whisper-server, server mode quietly falls
        # back to the CLI and the server case would test the CLI twice.
        for name in ("whisper-cli", "whisper-server"):
            (tmp_path / name).write_text("#!/bin/sh\n")
            (tmp_path / name).chmod(0o755)
        (tmp_path / "ggml-base.en.bin").write_bytes(b"\x00")
        cfg = {"transcription_backend": "whisper_cpp", "accuracy_mode": "high",
               "beam_size": 8, "best_of": 5, "whisper_server_mode": server_mode,
               "whisper_binary": str(tmp_path / "whisper-cli"),
               "model_path": str(tmp_path / "ggml-base.en.bin")}
        backend = get_backend(cfg)
        assert isinstance(backend, WhisperServerBackend) is server_mode
        assert (backend.beam_size, backend.best_of) == (1, 1)


class TestServerRequestDecoding(TestMacServerRequestFields):
    """Decoding params ride on every request, so a change needs no restart."""

    @pytest.mark.parametrize("platform", ["darwin", "linux"])
    def test_every_request_carries_beam_and_best_of(self, platform, sample_audio_file, monkeypatch):
        body = self._body_for(platform, sample_audio_file, monkeypatch)
        assert b'name="beam_size"\r\n\r\n' in body
        assert b'name="best_of"\r\n\r\n' in body

    @pytest.mark.parametrize("platform", ["darwin", "linux", "win32"])
    @pytest.mark.parametrize("use_gpu", [False, True])
    def test_no_temperature_retries_is_explicit_in_request(
        self, platform, use_gpu, sample_audio_file, monkeypatch
    ):
        """The native server accepts -nf but ignores it; its HTTP default is
        temperature_inc=0.2. Explicit zero matches Aura's existing CLI policy
        and avoids repeatedly decoding low-confidence speech such as A–Z.
        """
        body = self._body_for(platform, sample_audio_file, monkeypatch, use_gpu=use_gpu)
        assert body.count(b'name="temperature_inc"') == 1
        assert b'name="temperature_inc"\r\n\r\n0.0\r\n' in body

    def test_restart_retry_keeps_no_temperature_retries(self, sample_audio_file, monkeypatch):
        from wayfinder.core import transcriber

        cls = transcriber.WhisperServerBackend
        monkeypatch.setattr(cls, "_server_disabled", False)
        backend = cls(use_gpu=True)
        response = MagicMock()
        response.read.return_value = b'{"text":"hello"}'
        urlopen = MagicMock(side_effect=[TimeoutError("stalled"), response])

        def fake_start(*args, **kwargs):
            cls._server_port = 8179 if kwargs.get("force") else 8178

        with patch.object(cls, "_start_server", side_effect=fake_start), \
             patch("urllib.request.urlopen", urlopen):
            assert backend.transcribe(str(sample_audio_file)) == "hello"

        assert urlopen.call_count == 2
        requests = [call.args[0] for call in urlopen.call_args_list]
        assert [request.full_url for request in requests] == [
            "http://127.0.0.1:8178/inference", "http://127.0.0.1:8179/inference",
        ]
        assert all(b'name="temperature_inc"\r\n\r\n0.0\r\n' in request.data
                   for request in requests)

    def test_inference_timing_does_not_log_transcript(self, sample_audio_file, monkeypatch, capsys):
        self._body_for("darwin", sample_audio_file, monkeypatch)
        output = capsys.readouterr().out
        assert "[Whisper Server] Inference completed in " in output
        assert "hello" not in output


class TestSoundAnnotations:
    """base.en typed "[sound of wind]" on noise-only audio (beam eval, 2026-09-24)."""

    @pytest.mark.parametrize("raw,clean", [
        ("[sound of wind]", ""),
        ("Hello (sound of the rain) there.", "Hello there."),
        ("Ship it [audience noise] now", "Ship it now"),
    ])
    def test_noise_annotations_with_joiners_are_removed(self, raw, clean):
        from wayfinder.core.transcriber import clean_whisper_artifacts
        assert clean_whisper_artifacts(raw) == clean

    @pytest.mark.parametrize("text", [
        "See [of course] here", "I said (and the wind) okay", "Tag [sound of] stays",
    ])
    def test_dictated_brackets_survive(self, text):
        from wayfinder.core.transcriber import clean_whisper_artifacts
        assert clean_whisper_artifacts(text) == text


class TestOneTokenPerDictation:
    """Sol mac r6: a request nested in a dictation (the Metal twin, whisper-cli,
    a re-heard chunk) took the abandonment counter afresh, so a reset before
    it began went unseen."""

    @pytest.fixture(autouse=True)
    def _counter(self, monkeypatch):
        from wayfinder.core.transcriber import WhisperServerBackend
        monkeypatch.setattr(WhisperServerBackend, "_abandoned", WhisperServerBackend._abandoned)

    def test_requests_inside_a_dictation_keep_its_token(self):
        import threading
        from wayfinder.core.transcriber import WhisperServerBackend as cls, dictation_requests
        token = cls.request_token()
        other = {}
        with dictation_requests(token):
            cls.abandon_requests()
            assert cls.request_token() == token
            with dictation_requests(cls._abandoned):   # a nested scope cannot renew it
                assert cls.request_token() == token
            thread = threading.Thread(target=lambda: other.update(token=cls.request_token()))
            thread.start()
            thread.join()
        assert other["token"] == cls._abandoned   # only this thread's requests
        assert cls.request_token() == cls._abandoned

    def test_whisper_server_hands_its_token_to_whisper_cli(self, tmp_path, monkeypatch):
        from wayfinder.core.transcriber import WhisperServerBackend as cls
        audio = tmp_path / "audio.wav"
        audio.write_bytes(b"\x00")
        backend = cls(whisper_server_binary=str(tmp_path / "whisper-server"),
                      model_path=str(tmp_path / "model.bin"))
        monkeypatch.setattr(cls, "_disabled_now", classmethod(lambda c: True))
        token = cls.request_token()
        seen = []

        class Cli:
            def transcribe(self, audio_path, context="", deadline=None):
                cls.abandon_requests()   # a reset as whisper-cli starts
                seen.append(cls.request_token())
                return "words"

        backend._cli_fallback = lambda: Cli()
        assert backend.transcribe(str(audio)) == "words"
        assert seen == [token]

    def test_a_dictation_retired_before_it_reaches_the_backend_builds_none(self, monkeypatch):
        from wayfinder.core import transcriber
        cls = transcriber.WhisperServerBackend
        monkeypatch.setattr(transcriber, "get_backend",
                            lambda _config: pytest.fail("a retired dictation built a backend"))
        token = cls.request_token()
        cls.abandon_requests()
        with pytest.raises(transcriber.RequestAbandoned):
            transcriber.transcribe_with_config("chunk.wav", {}, request_token=token)

    def test_a_reset_as_the_request_begins_reaches_its_backend(self, monkeypatch):
        from wayfinder.core import transcriber
        cls = transcriber.WhisperServerBackend
        seen = []

        class Backend:
            custom_vocabulary = []

            def transcribe(self, audio_path, context=""):
                cls.abandon_requests()             # lands as the request begins
                seen.append(cls.request_token())   # what whisper-server's own start checks
                return "words"

        monkeypatch.setattr(transcriber, "get_backend", lambda _config: Backend())
        token = cls.request_token()
        transcriber.transcribe_with_config("chunk.wav", {"raw_transcription": True}, request_token=token)
        assert seen == [token]

    def test_a_dictation_retired_during_asr_gets_no_cleanup(self, monkeypatch):
        from wayfinder.core import transcriber
        from wayfinder.core import postprocessor
        cls = transcriber.WhisperServerBackend

        class Backend:
            custom_vocabulary = []

            def transcribe(self, audio_path, context=""):
                cls.abandon_requests()
                return "words nobody will type"

        monkeypatch.setattr(transcriber, "get_backend", lambda _config: Backend())
        monkeypatch.setattr(postprocessor, "process_with_config",
                            lambda *_a, **_k: pytest.fail("a retired dictation was cleaned up"))
        with pytest.raises(transcriber.RequestAbandoned):
            transcriber.transcribe_with_config(
                "chunk.wav", {"post_processing_enabled": True}, request_token=cls.request_token())


class TestRetiredDictationGapsSolR7:
    """Sol mac r7: stages a retired dictation still reached."""

    @pytest.fixture(autouse=True)
    def _counter(self, monkeypatch):
        from wayfinder.core.transcriber import WhisperServerBackend
        monkeypatch.setattr(WhisperServerBackend, "_abandoned", WhisperServerBackend._abandoned)

    def test_a_reset_during_the_flag_probe_runs_no_attempt(self, tmp_path, monkeypatch):
        from wayfinder.core import transcriber
        cls = transcriber.WhisperServerBackend
        backend, audio = TestGpuCpuFallback()._backend(tmp_path, gpu_body='echo "words"', cpu_body=None)

        def probe(**_kwargs):
            cls.abandon_requests()   # Escape while --help ran
            return set()

        backend._supported_flags = probe
        monkeypatch.setattr(transcriber, "run_tracked",
                            lambda *_a, **_k: pytest.fail("a retired dictation ran whisper-cli"))
        with transcriber.dictation_requests(cls.request_token()):
            with pytest.raises(transcriber.RequestAbandoned):
                backend.transcribe(audio)

    def test_a_reset_while_the_backend_is_built_sends_it_nothing(self, monkeypatch):
        from wayfinder.core import transcriber
        cls = transcriber.WhisperServerBackend

        class Backend:
            def transcribe(self, *_a, **_k):
                pytest.fail("a retired dictation reached the backend")

        def build(_config):
            cls.abandon_requests()
            return Backend()

        monkeypatch.setattr(transcriber, "get_backend", build)
        with pytest.raises(transcriber.RequestAbandoned):
            transcriber.transcribe_with_config("chunk.wav", {}, request_token=cls.request_token())

    @pytest.mark.parametrize("caller", ["no dictation", "an enclosing dictation"])
    def test_a_call_without_a_token_is_cancelled_too(self, monkeypatch, caller):
        # The Welcome test and a benchmark pass no token: a reset during ASR
        # still started cleanup and returned the words.
        from wayfinder.core import postprocessor, transcriber
        cls = transcriber.WhisperServerBackend

        class Backend:
            custom_vocabulary = []

            def transcribe(self, audio_path, context=""):
                cls.abandon_requests()
                return "words nobody will type"

        monkeypatch.setattr(transcriber, "get_backend", lambda _config: Backend())
        monkeypatch.setattr(postprocessor, "process_with_config",
                            lambda *_a, **_k: pytest.fail("a retired call was cleaned up"))
        if caller == "no dictation":
            with pytest.raises(transcriber.RequestAbandoned):
                transcriber.transcribe_with_config("a.wav", {"post_processing_enabled": True})
        else:
            token = cls.request_token()
            cls.abandon_requests()   # retired before this call began
            monkeypatch.setattr(transcriber, "get_backend",
                                lambda _config: pytest.fail("a retired dictation built a backend"))
            with transcriber.dictation_requests(token):
                with pytest.raises(transcriber.RequestAbandoned):
                    transcriber.transcribe_with_config("a.wav", {"post_processing_enabled": True})


class TestWhisperCliOutputIsUtf8:
    """whisper.cpp prints UTF-8 whatever the system code page. Decoded with
    Windows' default (cp1252), "it’s café" was typed as "itâ€™s cafÃ©", and a
    byte cp1252 lacks dropped the whole transcript."""

    SPOKEN = "it’s café, naïve — “quoted” Ásgeir"

    def test_non_ascii_text_survives_the_cli(self, tmp_path, monkeypatch):
        import subprocess
        import sys
        from wayfinder.core import transcriber

        binary = tmp_path / "whisper-cli"; binary.write_text("#!/bin/sh\n"); binary.chmod(0o755)
        model = tmp_path / "m.bin"; model.write_bytes(b"\x00")
        audio = tmp_path / "a.wav"; audio.write_bytes(b"\x00")
        backend = transcriber.WhisperCppBackend(whisper_binary=str(binary), model_path=str(model),
                                                use_gpu=False, timeout=30)
        real_run = transcriber.run_tracked
        speak = f"import sys; sys.stdout.buffer.write({self.SPOKEN.encode()!r})"

        def run(cmd, **kwargs):
            if "--help" in cmd:
                return subprocess.CompletedProcess(cmd, 0, "", "")
            # The real decode, with the kwargs the backend passes. They
            # must name it: CI sets PYTHONUTF8, the shipped app does not.
            assert kwargs.get("encoding") == "utf-8"
            return real_run([sys.executable, "-c", speak], **kwargs)

        monkeypatch.setattr(transcriber, "run_tracked", run)
        assert backend.transcribe(str(audio)) == self.SPOKEN


class TestARetiredDictationsHelpers:
    """Sol mac r10: a tracked helper is refused under the spawn lock once its
    dictation is retired. whisper-cli's probes then cache nothing, and its
    transcription ends as RequestAbandoned, not as a shutdown."""

    @staticmethod
    def _backend(tmp_path):
        from wayfinder.core import transcriber
        binary = tmp_path / "whisper-cli"; binary.write_text("#!/bin/sh\n"); binary.chmod(0o755)
        model = tmp_path / "m.bin"; model.write_bytes(b"\x00")
        return transcriber.WhisperCppBackend(whisper_binary=str(binary), model_path=str(model),
                                             use_gpu=False, timeout=30)

    def test_probes_refused_for_a_retired_dictation_cache_nothing(self, tmp_path, monkeypatch):
        from wayfinder.core import transcriber
        cls = transcriber.WhisperServerBackend
        monkeypatch.setattr(cls, "_abandoned", cls._abandoned)
        backend = self._backend(tmp_path)
        with transcriber.dictation_requests(cls.request_token()):
            cls.abandon_requests()
            with pytest.raises(transcriber.RequestAbandoned):
                backend._supported_flags()
            assert backend.supports_gpu() is False
        assert getattr(backend, "_supported_flags_cache", None) is None   # not "no flags"
        assert backend._gpu_supported is None                              # not "no GPU"

    def test_a_gpu_probe_that_runs_out_of_time_caches_nothing(self, tmp_path, monkeypatch):
        # Sol mac r19: the probe's time now counts its watcher's start too,
        # and a slow start says nothing about the binary.
        import subprocess
        from wayfinder.core import transcriber

        def timed_out(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))

        monkeypatch.setattr(transcriber, "run_tracked", timed_out)
        backend = self._backend(tmp_path)
        assert backend.supports_gpu() is False
        assert backend._gpu_supported is None                              # not "no GPU"

    @pytest.mark.parametrize("at", ["flag-probe", "transcription"])
    def test_a_transcription_refused_at_the_spawn_lock_is_abandoned(self, tmp_path, monkeypatch, at):
        import subprocess
        import threading
        from wayfinder.core import transcriber
        from wayfinder.utils import child_supervisor
        cls = transcriber.WhisperServerBackend
        monkeypatch.setattr(cls, "_abandoned", cls._abandoned)
        backend = self._backend(tmp_path)
        if at == "transcription":   # else the --help probe is the first spawn
            monkeypatch.setattr(backend, "_supported_flags", lambda *a, **k: set())
        audio = tmp_path / "a.wav"; audio.write_bytes(b"\x00")
        spawned = []
        real_popen = subprocess.Popen
        monkeypatch.setattr(subprocess, "Popen",
                            lambda *a, **k: spawned.append(a) or real_popen(*a, **k))
        token = cls.request_token()
        outcome = {}

        def dictation():
            with transcriber.dictation_requests(token):
                try:
                    outcome["result"] = backend.transcribe(str(audio))
                except BaseException as exc:
                    outcome["error"] = exc

        with child_supervisor._tracked_lock:   # another helper's spawn
            worker = threading.Thread(target=dictation, daemon=True)
            worker.start()
            worker.join(0.3)
            assert worker.is_alive(), outcome
            cls.abandon_requests()
        worker.join(10)
        assert isinstance(outcome.get("error"), transcriber.RequestAbandoned), outcome
        assert spawned == []


class TestARetiredDictationsActions:
    """Sol mac r11: each action is refused at the point of action, after
    every wait in front of it: the server POST, the CLI salvage, a cloud
    request, a model load, a timed worker, the GPU recovery probe."""

    @pytest.fixture
    def cls(self, monkeypatch):
        from wayfinder.core import transcriber
        cls = transcriber.WhisperServerBackend
        monkeypatch.setattr(cls, "_abandoned", cls._abandoned)
        return cls

    @staticmethod
    def _serving(tmp_path, monkeypatch, cls):
        """A backend whose server is up on 8178: no start, no spawn."""
        monkeypatch.setattr(cls, "_server_port", 8178)
        monkeypatch.setattr(cls, "_server_process", None)
        monkeypatch.setattr(cls, "_disabled_now", staticmethod(lambda: False))
        backend = cls(use_gpu=False, timeout=5)
        audio = tmp_path / "a.wav"; audio.write_bytes(b"\x00")
        return backend, str(audio)

    @pytest.mark.parametrize("at", ["initial", "recovery"])
    def test_no_server_request_after_a_retirement(self, tmp_path, monkeypatch, cls, at):
        # The initial POST waited through the audio read after its check.
        from wayfinder.core import transcriber
        backend, audio = self._serving(tmp_path, monkeypatch, cls)
        posts, starts = [], []

        def post(req, timeout):
            posts.append(req)
            raise ConnectionResetError("connection reset")

        def start(**_kwargs):
            starts.append(_kwargs)
            if at == "recovery" and len(starts) == 2:
                cls.abandon_requests()   # retired during the restart

        monkeypatch.setattr(transcriber, "urlopen_loopback", post)
        backend._start_server = start
        if at == "initial":
            def prompt(context):
                cls.abandon_requests()   # anything after the start's check
                return ""
            backend._build_prompt = prompt
        backend._cli_fallback = lambda: pytest.fail("a retired dictation ran whisper-cli")
        with pytest.raises(transcriber.RequestAbandoned):
            backend.transcribe(audio)
        assert len(posts) == (0 if at == "initial" else 1)

    def test_a_salvage_refused_at_its_spawn_is_abandoned(self, tmp_path, monkeypatch, cls):
        # It was wrapped in "whisper-cli salvage also failed".
        from wayfinder.core import transcriber
        backend, audio = self._serving(tmp_path, monkeypatch, cls)
        starts = []

        def start(**_kwargs):
            starts.append(_kwargs)
            if len(starts) == 2:
                raise transcriber.TranscriptionError("still dead")

        class Cli:
            def transcribe(self, *_args, **_kwargs):
                cls.abandon_requests()
                raise transcriber.HelperRefused("the dictation was retired: no helper for it")

        monkeypatch.setattr(transcriber, "urlopen_loopback",
                            lambda *_a, **_k: (_ for _ in ()).throw(ConnectionResetError("reset")))
        backend._start_server = start
        backend._cli_fallback = Cli
        with pytest.raises(transcriber.RequestAbandoned) as err:
            backend.transcribe(audio)
        assert "salvage" not in str(err.value)

    @pytest.mark.parametrize("cloud", ["groq", "openai"])
    def test_no_cloud_transcription_for_a_dictation_retired_during_its_setup(
            self, tmp_path, cls, cloud):
        from wayfinder.core import transcriber
        backend = (transcriber.GroqWhisperBackend(api_key="gsk_test") if cloud == "groq"
                   else transcriber.OpenAIWhisperBackend(api_key="sk-test"))
        client = MagicMock()

        def get_client():
            cls.abandon_requests()   # retired while the client was built
            return client

        backend.is_available = lambda: True
        backend._get_client = get_client
        audio = tmp_path / "a.wav"; audio.write_bytes(b"\x00")
        with transcriber.dictation_requests(cls.request_token()):
            with pytest.raises(transcriber.RequestAbandoned):
                backend.transcribe(str(audio))
        client.audio.transcriptions.create.assert_not_called()

    @pytest.mark.parametrize("at", ["cuda-discovery", "failed-gpu-load"])
    def test_no_faster_whisper_load_for_a_retired_dictation(self, monkeypatch, cls, at):
        import sys
        import types
        from wayfinder.core import transcriber
        loads = []

        def model(size, device="cpu", **_kwargs):
            loads.append(device)
            if device == "cuda":
                cls.abandon_requests()   # retired during the failed load
                raise RuntimeError("CUDA out of memory")
            return object()

        monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=model))
        monkeypatch.setattr(transcriber.FasterWhisperBackend, "_model_cache", {})
        backend = transcriber.FasterWhisperBackend(model_size="tiny", use_gpu=True)

        def supports_gpu():
            if at == "cuda-discovery":
                cls.abandon_requests()
                return False
            return True

        backend.supports_gpu = supports_gpu
        backend._cuda_device_index = lambda: 0
        with transcriber.dictation_requests(cls.request_token()):
            with pytest.raises(transcriber.RequestAbandoned):
                backend._get_model()
        assert loads == ([] if at == "cuda-discovery" else ["cuda"])
        assert transcriber.FasterWhisperBackend._model_cache == {}

    def test_a_timed_worker_runs_under_its_callers_dictation(self, cls):
        from wayfinder.core import transcriber
        from wayfinder.utils.timeout import run_with_timeout
        token = cls.request_token()
        seen = lambda: getattr(transcriber._dictation, "token", None)   # noqa: E731
        with transcriber.dictation_requests(token):
            assert run_with_timeout(seen, 5) == token
        assert run_with_timeout(seen, 5) is None   # outside a dictation: none

    @pytest.mark.parametrize("timeout", [5, 0])
    def test_a_worker_that_starts_after_a_retirement_runs_nothing(self, monkeypatch, cls, timeout):
        # The resident and in-process models and Faster-Whisper run on a
        # run_with_timeout worker, after the caller's last check.
        import threading
        import types
        from wayfinder.core import transcriber
        from wayfinder.utils import timeout as timeout_module
        ran = []

        class LateThread(threading.Thread):
            def start(self):
                cls.abandon_requests()   # while the worker waits to run
                super().start()

        monkeypatch.setattr(timeout_module, "threading", types.SimpleNamespace(Thread=LateThread))
        with transcriber.dictation_requests(cls.request_token()):
            if not timeout:   # inline, on the caller's thread
                cls.abandon_requests()
            with pytest.raises(transcriber.RequestAbandoned):
                timeout_module.run_with_timeout(lambda: ran.append(True), timeout)
        assert ran == []
        assert timeout_module.run_with_timeout(lambda: ran.append(True) or "ran", 5) == "ran"

    @staticmethod
    def _gpu_backend(tmp_path, monkeypatch):
        from wayfinder.core import transcriber
        backend = TestARetiredDictationsHelpers._backend(tmp_path)
        monkeypatch.setattr(transcriber, "_gpu_retry_state",
                            {backend.whisper_binary: {"next_retry": 0.0, "failures": 2}})
        monkeypatch.setattr(transcriber, "_CPU_FALLBACK_ACTIVE", {backend.whisper_binary: True})
        monkeypatch.setattr(transcriber, "_gpu_probe_inflight", False)
        return backend

    def test_no_gpu_recovery_probe_for_a_dictation_retired_at_its_lock(self, tmp_path, monkeypatch, cls):
        import threading
        from wayfinder.core import transcriber
        backend = self._gpu_backend(tmp_path, monkeypatch)
        probes = []
        backend._probe_gpu_recovery = lambda *a: probes.append(a)
        token = cls.request_token()

        def dictation():
            with transcriber.dictation_requests(token):
                backend._maybe_probe_gpu_recovery()

        with transcriber._gpu_probe_lock:   # another probe's admission
            worker = threading.Thread(target=dictation, daemon=True)
            worker.start()
            worker.join(0.2)
            assert worker.is_alive()
            cls.abandon_requests()
        worker.join(5)
        assert probes == [] and transcriber._gpu_probe_inflight is False

    @pytest.mark.parametrize("found", [True, False])
    def test_a_gpu_recovery_probe_retired_meanwhile_sets_nothing(self, tmp_path, monkeypatch, cls, found):
        from wayfinder.core import transcriber
        backend = self._gpu_backend(tmp_path, monkeypatch)
        seen = []

        def probe():
            seen.append(getattr(transcriber._dictation, "token", None))
            cls.abandon_requests()
            return found

        backend._gpu_probe_succeeds = probe
        token = cls.request_token()
        with transcriber.dictation_requests(token):
            backend._maybe_probe_gpu_recovery()
        for thread in threading_enumerate("wayfinder-gpu-probe"):
            thread.join(5)
        assert seen == [token]   # the probe ran under the dictation's token
        assert transcriber._gpu_retry_state[backend.whisper_binary] == {"next_retry": 0.0, "failures": 2}
        assert transcriber._CPU_FALLBACK_ACTIVE == {backend.whisper_binary: True}
        assert transcriber._gpu_probe_inflight is False

    @staticmethod
    def _gated_state_lock(monkeypatch):
        """_gpu_state_lock, held by the test until it releases gate.lock;
        gate.waiting says a thread now waits for it."""
        import threading
        from wayfinder.core import transcriber

        class Gate:
            lock = threading.Lock()
            waiting = threading.Event()

            def __enter__(self):
                self.waiting.set()
                self.lock.acquire()
                return self

            def __exit__(self, *_a):
                self.lock.release()

        gate = Gate()
        monkeypatch.setattr(transcriber, "_gpu_state_lock", gate)
        gate.lock.acquire()
        return gate

    @pytest.mark.parametrize("retire", [True, False])
    @pytest.mark.parametrize("found", [True, False])
    def test_a_gpu_recovery_verdict_asks_under_the_state_lock(self, tmp_path, monkeypatch, cls,
                                                              found, retire):
        # Sol mac r12: the probe left its dictation's scope before the state
        # lock; a retirement while it waited there still set its verdict.
        from wayfinder.core import transcriber
        backend = self._gpu_backend(tmp_path, monkeypatch)
        backend._gpu_probe_succeeds = lambda: found
        gate = self._gated_state_lock(monkeypatch)
        with transcriber.dictation_requests(cls.request_token()):
            backend._maybe_probe_gpu_recovery()
        assert gate.waiting.wait(5)
        if retire:
            cls.abandon_requests()
        gate.lock.release()
        for thread in threading_enumerate("wayfinder-gpu-probe"):
            thread.join(5)
        state = transcriber._gpu_retry_state[backend.whisper_binary]
        if retire:
            assert state == {"next_retry": 0.0, "failures": 2}
            assert transcriber._CPU_FALLBACK_ACTIVE == {backend.whisper_binary: True}
        elif found:
            assert state["failures"] == 1 and transcriber._CPU_FALLBACK_ACTIVE == {}
        else:
            assert state["failures"] == 3
        assert transcriber._gpu_probe_inflight is False

    @pytest.mark.skipif(os.name == "nt", reason="the whisper-cli stand-ins are shell scripts")
    @pytest.mark.parametrize("retire", [True, False])
    def test_a_cpu_fallback_retired_at_the_state_lock_memoizes_nothing(self, tmp_path, monkeypatch,
                                                                       cls, retire):
        # Sol mac r12, the CLI's twin of the probe: a crashed GPU run memoized
        # the CPU fallback and counted a failure for a dictation retired while
        # it waited for the state lock.
        import threading
        from wayfinder.core import transcriber
        gpu = tmp_path / "whisper-cli"
        gpu.write_text("#!/bin/sh\nkill -SEGV $$\n"); gpu.chmod(0o755)
        cpu = tmp_path / "whisper-cli-cpu"
        cpu.write_text('#!/bin/sh\necho "cpu words"\n'); cpu.chmod(0o755)
        model = tmp_path / "model.bin"; model.write_bytes(b"\x00")
        audio = tmp_path / "audio.wav"; audio.write_bytes(b"\x00")
        backend = transcriber.WhisperCppBackend(whisper_binary=str(gpu), model_path=str(model),
                                                timeout=10, use_gpu=True)
        monkeypatch.setattr(transcriber, "_CPU_FALLBACK_ACTIVE", {})
        monkeypatch.setattr(transcriber, "_gpu_retry_state", {})
        gate = self._gated_state_lock(monkeypatch)
        result = {}

        def run():
            try:
                with transcriber.dictation_requests(cls.request_token()):
                    result["text"] = backend.transcribe(str(audio))
            except Exception as e:   # the test inspects it
                result["err"] = e

        worker = threading.Thread(target=run)
        worker.start()
        assert gate.waiting.wait(10)
        if retire:
            cls.abandon_requests()
        gate.lock.release()
        worker.join(10)
        if retire:
            assert isinstance(result.get("err"), transcriber.RequestAbandoned)
            assert transcriber._CPU_FALLBACK_ACTIVE == {} and transcriber._gpu_retry_state == {}
        else:
            assert result.get("text") == "cpu words"
            assert transcriber._CPU_FALLBACK_ACTIVE[str(gpu)].endswith("-cpu")
            assert transcriber._gpu_retry_state[str(gpu)]["failures"] == 1


def threading_enumerate(name):
    import threading
    return [t for t in threading.enumerate() if t.name == name]


def test_recovery_probe_does_not_condemn_gpu_after_shortened_admission(monkeypatch):
    from types import SimpleNamespace
    from wayfinder.core import transcriber
    error = subprocess.TimeoutExpired(["whisper-cli"], 0.25)
    error.execution_budget = 0.25
    monkeypatch.setattr(transcriber, "run_tracked", MagicMock(side_effect=error))
    backend = SimpleNamespace(whisper_binary="unused", model_path="unused")
    assert transcriber.WhisperCppBackend._gpu_probe_succeeds(backend) is None


def test_cli_admission_timeout_never_memoizes_cpu_fallback(tmp_path, monkeypatch):
    from wayfinder.core import transcriber
    binary, cpu, model, audio = [tmp_path / name for name in
                                ("whisper-cli", "whisper-cli-cpu", "model.bin", "audio.wav")]
    for path in (binary, cpu, model, audio):
        path.touch()
    monkeypatch.setattr(transcriber, "_CPU_FALLBACK_ACTIVE", {})
    backend = transcriber.WhisperCppBackend(whisper_binary=str(binary), model_path=str(model),
                                            use_gpu=True, timeout=30)

    def run(cmd, **kwargs):
        if "--help" in cmd:
            return subprocess.CompletedProcess(cmd, 0, "", "")
        assert cmd[0] != str(cpu), "admission delay must not select the CPU fallback"
        error = subprocess.TimeoutExpired(cmd, kwargs["timeout"])
        error.execution_budget = 0.25
        raise error

    monkeypatch.setattr(transcriber, "run_tracked", run)
    with pytest.raises(transcriber.TranscriptionError):
        backend.transcribe(str(audio))
    assert str(binary) not in transcriber._CPU_FALLBACK_ACTIVE


@pytest.mark.parametrize("remaining,expected", [(59.99, False), (0.25, None)])
def test_recovery_timeout_distinguishes_launch_overhead_from_admission(monkeypatch, remaining, expected):
    from types import SimpleNamespace
    from wayfinder.core import transcriber
    error = subprocess.TimeoutExpired(["whisper-cli"], remaining)
    error.execution_budget = remaining
    monkeypatch.setattr(transcriber, "run_tracked", MagicMock(side_effect=error))
    backend = SimpleNamespace(whisper_binary="unused", model_path="unused")
    assert transcriber.WhisperCppBackend._gpu_probe_succeeds(backend) is expected


def test_normal_gpu_timeout_still_tries_and_remembers_cpu_fallback(tmp_path, monkeypatch):
    from wayfinder.core import transcriber
    binary, cpu, model, audio = [tmp_path / name for name in
                                ("whisper-cli", "whisper-cli-cpu", "model.bin", "audio.wav")]
    for path in (binary, cpu, model, audio):
        path.touch()
    monkeypatch.setattr(transcriber, "_CPU_FALLBACK_ACTIVE", {})
    backend = transcriber.WhisperCppBackend(whisper_binary=str(binary), model_path=str(model),
                                            use_gpu=True, timeout=30)
    attempts = []

    def run(cmd, **kwargs):
        if "--help" in cmd:
            return subprocess.CompletedProcess(cmd, 0, "", "")
        attempts.append(cmd[0])
        if cmd[0] == str(binary):
            error = subprocess.TimeoutExpired(cmd, kwargs["timeout"])
            error.execution_budget = 29.99
            raise error
        return subprocess.CompletedProcess(cmd, 0, "These spoken words survived the fallback.", "")

    monkeypatch.setattr(transcriber, "run_tracked", run)
    assert "spoken words" in backend.transcribe(str(audio))
    assert attempts == [str(binary), str(cpu)]
    assert str(binary) in transcriber._CPU_FALLBACK_ACTIVE

class TestUnpromptedTranscription:
    """Noise-verdict audio is heard with no prompt: primed with the dictation
    prompt, large-v3-turbo turned fan/hum/hiss into invented sentences (2026-10-13)."""

    @pytest.fixture
    def prompts(self, monkeypatch, tmp_path):
        from types import SimpleNamespace

        import wayfinder.license as lic
        from wayfinder.core import transcriber as T

        gate = SimpleNamespace(is_premium=True, has_feature=lambda f: True)
        monkeypatch.setattr(lic, "get_feature_gate", lambda *a, **k: gate)
        heard = []

        def transcribe(backend, audio_path, context="", deadline=None):
            heard.append(backend._build_prompt(context))
            return "hello there"

        monkeypatch.setattr(T.WhisperCppBackend, "transcribe", transcribe)
        model = tmp_path / "ggml-base.en.bin"
        model.write_bytes(b"\0")
        self.config = {"transcription_backend": "whisper_cpp", "model_path": str(model),
                       "whisper_server_mode": False, "post_processing_enabled": False,
                       "output_tone": "professional", "custom_vocabulary": ["Wayfinder"]}
        self.audio = str(tmp_path / "a.wav")
        return heard

    def test_dictation_is_primed_with_prompt_vocabulary_and_context(self, prompts):
        from wayfinder.core.transcriber import transcribe_with_config

        transcribe_with_config(self.audio, self.config)
        transcribe_with_config(self.audio, self.config, context="earlier words")
        assert "Use proper punctuation" in prompts[0] and "Wayfinder" in prompts[0]
        assert prompts[1].startswith("earlier words")

    def test_unprompted_reaches_the_core_ml_servers_metal_stand_in(self, prompts, monkeypatch):
        from types import SimpleNamespace

        from wayfinder.core import transcriber as T

        heard = []
        stand_in = SimpleNamespace(prompt="Use proper punctuation", custom_vocabulary=["Wayfinder"])
        stand_in.transcribe = lambda audio_path, context="": heard.append(
            (stand_in.prompt, stand_in.custom_vocabulary, context)) or "hello there"
        server = SimpleNamespace(prompt="Use proper punctuation", custom_vocabulary=["Wayfinder"],
                                 _ready_only_fallback=stand_in)
        # Core ML not loaded yet: the dictation goes to the Metal stand-in.
        server.transcribe = lambda audio_path, context="": stand_in.transcribe(audio_path, context)
        monkeypatch.setattr(T, "get_backend", lambda config: server)
        T.transcribe_with_config(self.audio, self.config, context="earlier words", unprompted=True)
        assert heard == [("", [], "")]

    def test_unprompted_sends_no_prompt_vocabulary_or_context(self, prompts):
        from wayfinder.core.transcriber import transcribe_with_config

        text = transcribe_with_config(self.audio, self.config, context="earlier words",
                                      unprompted=True)
        assert prompts == [""]
        assert text == "Hello there."

    # -- Sol review of the PR #67 port: the two-word rule must run on the
    # recognizer's OWN words, before vocabulary replacement or LLM cleanup can
    # rewrite them, and a known hallucination phrase must be rejected even
    # under Raw Transcript (whose early return used to skip that check). --

    def test_unprompted_two_word_rule_precedes_vocabulary_correction(self, prompts, monkeypatch):
        """A one-word noise artifact ("Okay.") must stay rejected even though a
        configured correction would turn it into two words ("Sounds good."):
        the rule has to see what the recognizer actually said."""
        from wayfinder.core import transcriber as T

        monkeypatch.setattr(T.WhisperCppBackend, "transcribe",
                            lambda backend, audio_path, context="", deadline=None: "Okay.")
        self.config["vocabulary_replacements"] = [["okay", "sounds good"]]
        text = T.transcribe_with_config(self.audio, self.config, unprompted=True)
        assert text == ""

    def test_unprompted_raw_transcription_still_rejects_a_hallucination_phrase(
        self, prompts, monkeypatch
    ):
        """Raw Transcript's early return happens AFTER the noise checks now: a
        known Whisper silence/noise catch-phrase has enough words to pass the
        two-word rule, so it needs its own rejection — matching what normal
        (non-raw) mode already does via clean_whisper_artifacts."""
        from wayfinder.core import transcriber as T

        monkeypatch.setattr(
            T.WhisperCppBackend, "transcribe",
            lambda backend, audio_path, context="", deadline=None: "Thank you for watching.")
        self.config["raw_transcription"] = True
        text = T.transcribe_with_config(self.audio, self.config, unprompted=True)
        assert text == ""

    @pytest.mark.parametrize("heard", [
        # Sol pr67 r2: the two-word rule counted a bracketed
        # annotation Whisper itself emits as a "word", so noise output with
        # exactly one real word behind an annotation passed it; normal
        # cleanup then strips the annotation and leaves the lone filler word
        # for Aura to type.
        "[Music] Okay.",
        "[BLANK_AUDIO] So,",
        "*music* Okay.",
    ])
    def test_unprompted_two_word_rule_ignores_non_speech_annotations(
        self, prompts, monkeypatch, heard
    ):
        from wayfinder.core import transcriber as T

        monkeypatch.setattr(T.WhisperCppBackend, "transcribe",
                            lambda backend, audio_path, context="", deadline=None: heard)
        text = T.transcribe_with_config(self.audio, self.config, unprompted=True)
        assert text == ""

    def test_unprompted_two_word_rule_still_keeps_real_speech_behind_an_annotation(
        self, prompts, monkeypatch
    ):
        """A real two-word phrase must still survive once the annotation in
        front of it is set aside — the fix must not reject on sight of any
        annotation."""
        from wayfinder.core import transcriber as T

        monkeypatch.setattr(
            T.WhisperCppBackend, "transcribe",
            lambda backend, audio_path, context="", deadline=None:
                "[Music] Thanks for the update.")
        text = T.transcribe_with_config(self.audio, self.config, unprompted=True)
        assert text == "Thanks for the update."

    @pytest.mark.parametrize("heard", [
        # Sol pr67 r2: the early phrase match lacked the whitespace
        # canonicalisation normal cleanup does before its own final
        # hallucination check, so repeated whitespace defeated it.
        "Thank  you for watching.",
        # ... and surrounding non-speech markers must not defeat it either.
        "[Music] Thank you  for watching.",
        "Thank you for watching.  [Applause]",
    ])
    def test_unprompted_hallucination_match_normalizes_whitespace_and_markers(
        self, prompts, monkeypatch, heard
    ):
        """Raw Transcript on, so the EARLY phrase match is the only thing
        standing between this and typing the hallucinated phrase — normal
        cleanup's own (already whitespace-safe) check never runs."""
        from wayfinder.core import transcriber as T

        monkeypatch.setattr(T.WhisperCppBackend, "transcribe",
                            lambda backend, audio_path, context="", deadline=None: heard)
        self.config["raw_transcription"] = True
        text = T.transcribe_with_config(self.audio, self.config, unprompted=True)
        assert text == ""

    def test_unprompted_hallucination_match_is_never_a_substring_match(
        self, prompts, monkeypatch
    ):
        """A real sentence that merely CONTAINS a known catch-phrase must be
        kept — only the entire (whitespace-canonicalised) text is compared."""
        from wayfinder.core import transcriber as T

        monkeypatch.setattr(
            T.WhisperCppBackend, "transcribe",
            lambda backend, audio_path, context="", deadline=None:
                "I want to thank you for watching the show")
        text = T.transcribe_with_config(self.audio, self.config, unprompted=True)
        assert text != ""

    # -- Sol pr67 r3: six more findings against the safety copy itself. --

    def test_unprompted_raw_transcription_returns_accepted_text_untouched(
        self, prompts, monkeypatch
    ):
        """Finding 2, redone (Sol pr67 r6): wrapped text never counts towards
        the two-word rule, but it only decides accept or reject. An accepted
        recording comes back from Raw Transcript exactly as heard, wrappers
        included."""
        from wayfinder.core import transcriber as T

        heard = "*Really* we need **two** more chairs (for later)."
        monkeypatch.setattr(
            T.WhisperCppBackend, "transcribe",
            lambda backend, audio_path, context="", deadline=None: heard)
        self.config["raw_transcription"] = True
        text = T.transcribe_with_config(self.audio, self.config, unprompted=True)
        assert text == heard

    def test_unprompted_raw_transcription_keeps_mixed_script_sentences_distinct(
        self, prompts, monkeypatch
    ):
        """Finding 3: the repetition helper's ASCII-only key
        discarded "李明", so "Thank you." and "Thank you, 李明。" hashed to
        the SAME key, were wrongly treated as one repeated sentence, and
        collapsed down to "Thank you." -- a blacklisted phrase -- throwing
        away the real sentence (with the name) along with it. A Unicode-
        aware key must keep these two different sentences different."""
        from wayfinder.core import transcriber as T

        monkeypatch.setattr(
            T.WhisperCppBackend, "transcribe",
            lambda backend, audio_path, context="", deadline=None:
                "Thank you. Thank you, 李明。")
        self.config["raw_transcription"] = True
        text = T.transcribe_with_config(self.audio, self.config, unprompted=True)
        assert text == "Thank you. Thank you, 李明。"

    @pytest.mark.parametrize("heard", [
        # Finding 4: the special-token leak regex lived only in
        # clean_whisper_artifacts, AFTER the point where normal mode's own
        # cleanup runs, so the safety copy's two-word rule counted the
        # token itself as a word and let a single real filler word
        # ("Okay.") through. Once the token is stripped before counting,
        # "Okay." alone is one word and must be rejected -- in BOTH modes,
        # since the early check runs before Raw Transcript's return.
        "<|endoftext|> Okay.",
        "<|0.00|> So,",   # a timestamp token, not just <|endoftext|>
    ])
    @pytest.mark.parametrize("raw", [False, True])
    def test_unprompted_two_word_rule_ignores_special_tokens(
        self, prompts, monkeypatch, heard, raw
    ):
        from wayfinder.core import transcriber as T

        monkeypatch.setattr(T.WhisperCppBackend, "transcribe",
                            lambda backend, audio_path, context="", deadline=None: heard)
        self.config["raw_transcription"] = raw
        text = T.transcribe_with_config(self.audio, self.config, unprompted=True)
        assert text == ""

    @pytest.mark.parametrize("heard", [
        # Finding 5: the early phrase match strips ASCII punctuation
        # only; normal cleanup folds a Unicode ellipsis down to a plain "."
        # well before its OWN hallucination check, so it correctly rejects
        # this, but the early safety-copy check (the only thing standing
        # between this and Raw Transcript) did not and let it through.
        "Thank you for watching…",
        # Curly quotes and full-width CJK punctuation render the same marks
        # and must canonicalise the same way, in the comparison copy only.
        "“Thank you for watching”",
        "Thank you for watching。",
    ])
    def test_unprompted_raw_transcription_rejects_punctuation_variant_hallucinations(
        self, prompts, monkeypatch, heard
    ):
        from wayfinder.core import transcriber as T

        monkeypatch.setattr(T.WhisperCppBackend, "transcribe",
                            lambda backend, audio_path, context="", deadline=None: heard)
        self.config["raw_transcription"] = True
        text = T.transcribe_with_config(self.audio, self.config, unprompted=True)
        assert text == ""

    # -- Sol pr67 r4: three more findings, all noise-path only. --

    @pytest.mark.parametrize("raw", [False, True])
    @pytest.mark.parametrize("heard", [
        # Finding 2: a descriptive annotation built from a noise word and
        # plain filler words ("loud", "soft") still describes non-speech
        # audio even though those filler words aren't noise vocabulary.
        "*loud noise* Okay.",
        "*soft music* So,",
        # Sol pr67 r5: a closing mark inside, and the bracketed shapes.
        "*loud noise.* Okay.",
        "*loud noise!* Okay.",
        "(loud music) Okay.",
        "[soft music] So,",
    ])
    def test_unprompted_noise_rejects_descriptive_sound_annotations(
        self, prompts, monkeypatch, heard, raw
    ):
        from wayfinder.core import transcriber as T

        monkeypatch.setattr(T.WhisperCppBackend, "transcribe",
                            lambda backend, audio_path, context="", deadline=None: heard)
        self.config["raw_transcription"] = raw
        text = T.transcribe_with_config(self.audio, self.config, unprompted=True)
        assert text == ""

    @pytest.mark.parametrize("raw", [False, True])
    @pytest.mark.parametrize("heard", [
        # Sol pr67 r6: nothing Whisper wrapped counts as a spoken word on the
        # noise path, whatever it says inside. Telling an annotation from
        # emphasis or code by its words failed both ways ("*a loud noise*
        # Okay." passed, "Turn **music**" lost a word). A wrong rejection
        # drops the recording as 1.3.0 already did; a wrong acceptance types
        # something never said.
        "*a loud noise* Okay.",
        "*loud crashing noise* Okay.",
        "Close *the door*",
        "Turn **music**",
        "`*phone rings*` twice",
        "(door opens) Alice",
        "*door and **kwargs",
        "*args and **kwargs",
        # An opener left unmatched runs to the end, a closer from the start.
        "Okay [Music",
        "loud noise] Okay.",
        "♪ la la la",
        # Sol pr67 r7: nested asterisks can't expose an inner word, every
        # one of Whisper's music symbols counts, and the other bracket kinds.
        "*a loud **crashing** noise* Okay.",
        "**loud *crashing* noise** Okay.",
        "♫ la la ♫ Okay.",
        "♬ la la ♪ Okay.",
        "Okay ♫ la la",
        "{noise} Okay.",
        "（music） Okay.",
        "[loud (crashing] noise) Okay.",
        # Sol pr67 r8: what is wrapped is decided on the original text, so
        # removing one bracket kind can't expose another's contents.
        "[noise (] loud crashing [) noise] Okay.",
        "(noise [) loud crashing (] noise) Okay.",
        "<|nospeech|> [Music (] loud [|>] Okay.",
        "*a* Okay *crashing",
    ])
    def test_unprompted_noise_never_counts_wrapped_words(
        self, prompts, monkeypatch, heard, raw
    ):
        from wayfinder.core import transcriber as T

        monkeypatch.setattr(T.WhisperCppBackend, "transcribe",
                            lambda backend, audio_path, context="", deadline=None: heard)
        self.config["raw_transcription"] = raw
        text = T.transcribe_with_config(self.audio, self.config, unprompted=True)
        assert text == ""

    @pytest.mark.parametrize("raw", [False, True])
    @pytest.mark.parametrize("heard", [
        # Sol pr67 r7: a repeat with no space after the stop is still one
        # known phrase said twice.
        "Thank you.Thank you.",
        "Thanks for watching!Thanks for watching!",
        # Sol pr67 r8: a text made of catch-phrases and nothing else, however
        # they are punctuated or spaced.
        "Subtitles by the Amara.org community. Subtitles by the Amara.org community.",
        "Thank you, thank you.",
        "Thank you Thank you",
        "Thank you. Thanks for watching!",
    ])
    def test_unprompted_noise_rejects_repeats_with_no_space_between(
        self, prompts, monkeypatch, heard, raw
    ):
        from wayfinder.core import transcriber as T

        monkeypatch.setattr(T.WhisperCppBackend, "transcribe",
                            lambda backend, audio_path, context="", deadline=None: heard)
        self.config["raw_transcription"] = raw
        text = T.transcribe_with_config(self.audio, self.config, unprompted=True)
        assert text == ""

    @pytest.mark.parametrize("raw", [False, True])
    @pytest.mark.parametrize("heard", [
        "Close *the door* behind you please.",
        "(door opens) Alice, come in.",
        "We need [two] more chairs.",
    ])
    def test_unprompted_noise_keeps_two_words_outside_any_wrapper(
        self, prompts, monkeypatch, heard, raw
    ):
        """Two spoken words outside every wrapper still pass, Raw Transcript
        on or off; Raw returns them exactly as heard."""
        from wayfinder.core import transcriber as T

        monkeypatch.setattr(T.WhisperCppBackend, "transcribe",
                            lambda backend, audio_path, context="", deadline=None: heard)
        self.config["raw_transcription"] = raw
        text = T.transcribe_with_config(self.audio, self.config, unprompted=True)
        assert text == heard if raw else text.strip()

    @pytest.mark.parametrize("raw", [False, True])
    @pytest.mark.parametrize("heard", [
        # Finding 3: repetition matching ran before punctuation folding, so
        # a repeated catch-phrase separated by an ellipsis, a closing
        # quote, or a full-width stop evaded the noise check.
        "Thank you for watching… Thank you for watching…",
        '"Thank you." "Thank you."',
        "Thank you for watching。Thank you for watching。",
        # Sol pr67 r5: bracketed repeats, mixed wrappers too.
        "(Thank you.) (Thank you.)",
        "[Thank you.] [Thank you.]",
        "(Thank you.) [Thank you.]",
    ])
    def test_unprompted_noise_rejects_repeats_across_punctuation_variants(
        self, prompts, monkeypatch, heard, raw
    ):
        from wayfinder.core import transcriber as T

        monkeypatch.setattr(T.WhisperCppBackend, "transcribe",
                            lambda backend, audio_path, context="", deadline=None: heard)
        self.config["raw_transcription"] = raw
        text = T.transcribe_with_config(self.audio, self.config, unprompted=True)
        assert text == ""

    @pytest.mark.parametrize("raw", [False, True])
    @pytest.mark.parametrize("heard", ["Turn it down. Turn it down.", "谢谢你。谢谢你。"])
    def test_unprompted_noise_keeps_a_repeated_sentence_that_is_no_catch_phrase(
        self, prompts, monkeypatch, heard, raw
    ):
        # A sentence said twice over a fan is speech: only a repeated
        # catch-phrase is the hallucination.
        from wayfinder.core import transcriber as T

        monkeypatch.setattr(T.WhisperCppBackend, "transcribe",
                            lambda backend, audio_path, context="", deadline=None: heard)
        self.config["raw_transcription"] = raw
        text = T.transcribe_with_config(self.audio, self.config, unprompted=True)
        assert text.strip()

    @pytest.mark.parametrize("raw", [False, True])
    def test_unprompted_noise_rejects_curly_quoted_known_phrase(
        self, prompts, monkeypatch, raw
    ):
        """The noise path's own quote fold (finding 1 scopes it OUT of
        normal cleanup, not out of the noise check) must still catch a
        curly-quoted catch-phrase on its own, no repeat involved."""
        from wayfinder.core import transcriber as T

        monkeypatch.setattr(
            T.WhisperCppBackend, "transcribe",
            lambda backend, audio_path, context="", deadline=None:
                "“Thank you”")
        self.config["raw_transcription"] = raw
        text = T.transcribe_with_config(self.audio, self.config, unprompted=True)
        assert text == ""

    @pytest.mark.parametrize("raw", [False, True])
    @pytest.mark.parametrize("heard", [
        "*cough* please open the door *cough*",
        "♫ la la ♫ please open the door ♬ la la ♬",
        "**loud *crashing* noise** please open the door *cough*",
    ])
    def test_separate_annotations_never_erase_the_speech_between_them(
        self, prompts, monkeypatch, heard, raw
    ):
        from wayfinder.core import transcriber as T
        monkeypatch.setattr(T.WhisperCppBackend, "transcribe",
                            lambda backend, audio_path, context="", deadline=None: heard)
        self.config["raw_transcription"] = raw
        text = T.transcribe_with_config(self.audio, self.config, unprompted=True)
        assert text == heard if raw else "please open the door" in text.casefold()

    def test_symmetric_wrapper_runs_preserve_every_outside_word(self):
        from wayfinder.core.transcriber import _noise_check_safety_copy
        annotations = ["*cough*", "**noise**", "*a **loud** noise*",
                       "**loud *crashing* noise**", "♫ la la ♬"]
        for annotation in annotations:
            for count in (2, 3, 5, 20, 100):
                outside = [f"spoken{n}" for n in range(count)]
                text = " ".join(f"{annotation} {word}" for word in outside) + " " + annotation
                assert _noise_check_safety_copy(text).split() == outside

    def test_unprompted_rejection_never_logs_the_recognized_text(
        self, prompts, monkeypatch, capsys
    ):
        """Finding 4 (Sol pr67 r2, scoped): the speech-guard's own
        diagnostics must say a transcript was dropped without printing what
        it said, on every rejection path this change touches."""
        from wayfinder.core import transcriber as T

        for heard in ("Secretword.", "[Music] Secretword.", "Thank  you for watching."):
            monkeypatch.setattr(
                T.WhisperCppBackend, "transcribe",
                lambda backend, audio_path, context="", deadline=None, heard=heard: heard)
            text = T.transcribe_with_config(self.audio, self.config, unprompted=True)
            assert text == ""

        out = capsys.readouterr().out
        assert "Secretword" not in out
        assert "Thank" not in out and "watching" not in out
