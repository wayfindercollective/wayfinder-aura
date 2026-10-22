"""Sol PR 2 r7: a dictation in flight at Quit fell back to whisper-cli or
llama-simple after the terminal shutdown, and os._exit() orphaned the child
(its Python timeout gone with the parent). One-shot helpers now run through
child_supervisor's tracked registry: a terminal shutdown stops the running
ones and refuses the rest, and neither pipeline falls back after it."""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from wayfinder.utils import child_supervisor

ROOT = Path(__file__).resolve().parents[1]
SLEEPER = [sys.executable, "-c", "import time; time.sleep(30)"]


def _wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.02)
    return True


def _gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


class TestTheRegistry:
    def test_a_run_in_flight_is_stopped_by_a_terminal_shutdown(self):
        outcome = {}

        def run():
            try:
                child_supervisor.run_tracked(SLEEPER, capture_output=True, timeout=60)
            except child_supervisor.HelperStopped as exc:
                outcome["stopped"] = exc

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        assert _wait_for(lambda: child_supervisor._tracked)
        (proc,) = list(child_supervisor._tracked)
        started = time.monotonic()
        child_supervisor.close_tracked()
        thread.join(10)
        assert not thread.is_alive() and time.monotonic() - started < 6
        # Its output says nothing about the machine: the run raises, never returns.
        assert proc.poll() is not None and "stopped" in outcome
        assert not child_supervisor._tracked

    def test_a_stopped_run_whose_timeout_fires_first_is_still_stopped(self):
        # A stop that has not landed yet when the run's own timeout fires: the
        # caller must still see Quit, not a timeout it would count as a failure.
        outcome = {}

        def run():
            try:
                child_supervisor.run_tracked(SLEEPER, timeout=1.0, stop=lambda proc: None)
            except child_supervisor.HelperStopped:
                outcome["stopped"] = True
            except subprocess.TimeoutExpired:
                outcome["timed out"] = True

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        assert _wait_for(lambda: child_supervisor._tracked)
        child_supervisor.close_tracked(timeout=0.1)
        thread.join(10)
        assert outcome == {"stopped": True}

    def test_no_helper_starts_after_a_terminal_shutdown(self):
        child_supervisor.close_tracked()
        with patch("subprocess.Popen") as popen:
            with pytest.raises(child_supervisor.HelpersClosed):
                child_supervisor.run_tracked(SLEEPER)
        popen.assert_not_called()

    def test_reopen_admits_helpers_again(self):
        child_supervisor.close_tracked()
        child_supervisor.reopen_tracked()
        assert child_supervisor.run_tracked([sys.executable, "-c", "pass"]).returncode == 0

    def test_a_timeout_kills_the_helper_and_forgets_it(self):
        with pytest.raises(subprocess.TimeoutExpired):
            child_supervisor.run_tracked(SLEEPER, timeout=0.2)
        assert not child_supervisor._tracked

    def test_it_behaves_like_subprocess_run(self):
        script = [sys.executable, "-c",
                  "import sys; print(sys.stdin.read().upper()); sys.exit(3)"]
        result = child_supervisor.run_tracked(script, input="hi", capture_output=True, text=True)
        assert (result.returncode, result.stdout.strip()) == (3, "HI")
        with pytest.raises(subprocess.CalledProcessError):
            child_supervisor.run_tracked(script, input="hi", capture_output=True, text=True,
                                         check=True)

    @pytest.mark.parametrize("quit_call", [
        "WhisperServerBackend.shutdown(terminal=True)",
        "LlamaServerManager.close()",
    ])
    def test_no_helper_survives_the_app_after_quit(self, tmp_path, quit_call):
        # A real app process: a helper runs, Quit's terminal stop, then os._exit().
        pid_file = tmp_path / "helper.pid"
        helper = [sys.executable, "-c",
                  f"import os, time; open({str(pid_file)!r}, 'w').write(str(os.getpid())); "
                  "time.sleep(60)"]
        app = textwrap.dedent(f"""
            import os, threading, time
            from wayfinder.utils import child_supervisor
            from wayfinder.core.llama_server import LlamaServerManager
            from wayfinder.core.transcriber import WhisperServerBackend
            threading.Thread(target=child_supervisor.run_tracked, args=({helper!r},),
                             daemon=True).start()
            while not (os.path.exists({str(pid_file)!r}) and open({str(pid_file)!r}).read()):
                time.sleep(0.02)
            {quit_call}
            os._exit(0)
        """)
        env = {**os.environ, "PYTHONPATH": f"{ROOT / 'src'}{os.pathsep}{ROOT}"}
        subprocess.run([sys.executable, "-c", app], env=env, timeout=60, check=True)
        assert _wait_for(lambda: _gone(int(pid_file.read_text())))


class TestNoCliAfterQuit:
    @staticmethod
    def _audio(tmp_path):
        audio = tmp_path / "clip.wav"
        audio.write_bytes(b"RIFF")
        return str(audio)

    def test_a_dictation_after_quit_runs_no_whisper_cli(self, tmp_path):
        from wayfinder.core.transcriber import TranscriptionError, WhisperServerBackend
        WhisperServerBackend._server_disabled = False
        backend = WhisperServerBackend(use_gpu=False, timeout=1)
        WhisperServerBackend.shutdown(terminal=True)
        with patch("subprocess.Popen") as popen, patch("subprocess.run") as run, \
             patch.object(WhisperServerBackend, "_cli_fallback") as fallback:
            with pytest.raises(TranscriptionError, match="Aura is shutting down"):
                backend.transcribe(self._audio(tmp_path))
        fallback.assert_not_called()
        popen.assert_not_called()
        run.assert_not_called()

    def test_the_cli_backend_after_quit_runs_nothing(self, tmp_path):
        from wayfinder.core.transcriber import (
            TranscriptionError, WhisperCppBackend, WhisperServerBackend,
        )
        WhisperServerBackend.shutdown(terminal=True)
        with patch("subprocess.Popen") as popen, patch("subprocess.run") as run:
            with pytest.raises(TranscriptionError, match="Aura is shutting down"):
                WhisperCppBackend(use_gpu=False).transcribe(self._audio(tmp_path))
        popen.assert_not_called()
        run.assert_not_called()

    def test_a_settings_switch_still_falls_back_to_whisper_cli(self, tmp_path):
        from wayfinder.core.transcriber import ServerStartSuperseded, WhisperServerBackend
        WhisperServerBackend._server_disabled = False
        backend = WhisperServerBackend(use_gpu=False, timeout=1)
        cli = MagicMock()
        cli.transcribe.return_value = "words"
        with patch.object(WhisperServerBackend, "_start_server",
                          side_effect=ServerStartSuperseded("switched")), \
             patch.object(WhisperServerBackend, "_cli_fallback", return_value=cli):
            assert backend.transcribe(self._audio(tmp_path)) == "words"

    @staticmethod
    def _cleanup_backend(tmp_path):
        from wayfinder.core.postprocessor import LlamaCppCliBackend
        for name in ("llama-simple", "llama-server", "m.gguf"):
            (tmp_path / name).touch()
        return LlamaCppCliBackend(llama_binary=str(tmp_path / "llama-simple"),
                                  model_path=str(tmp_path / "m.gguf"), n_gpu_layers=0,
                                  residency="auto")

    def test_a_cleanup_after_quit_runs_no_llama_simple(self, tmp_path):
        from wayfinder.core.llama_server import LlamaServerManager
        from wayfinder.core.postprocessor import PostProcessingError
        backend = self._cleanup_backend(tmp_path)
        LlamaServerManager.close()
        with patch.object(backend, "_resident_model", return_value=None), \
             patch("subprocess.Popen") as popen, patch("subprocess.run") as run:
            # post_process() hands the caller its original text on this error.
            with pytest.raises(PostProcessingError, match="Aura is shutting down"):
                backend.process("some words to clean up", "")
        popen.assert_not_called()
        run.assert_not_called()

    def test_a_settings_switch_still_falls_back_to_llama_simple(self, tmp_path):
        from wayfinder.core.llama_server import LlamaServerManager, LlamaServerSuperseded
        backend = self._cleanup_backend(tmp_path)
        cleaned = MagicMock(returncode=0, stdout="Cleaned text: some words to clean up.",
                            stderr="")
        with patch.object(backend, "_resident_model", return_value=None), \
             patch.object(backend, "_subprocess_target",
                          return_value=(str(tmp_path / "llama-simple"), 0)), \
             patch.object(LlamaServerManager, "ensure",
                          side_effect=LlamaServerSuperseded("switched")), \
             patch("subprocess.run", return_value=cleaned) as run:
            backend.process("some words to clean up", "")
        assert run.called


class TestAFallbackRacingQuitIsStopped:
    """The fallback started just before Quit: its helper is tracked, so the
    terminal shutdown stops it rather than leaving it to outlive the app."""

    @staticmethod
    def _hanging_helper(tmp_path, name):
        helper = tmp_path / name
        helper.write_text("#!/bin/sh\nexec sleep 30\n")   # one process, like the real binary
        helper.chmod(0o755)
        (tmp_path / "m.bin").touch()
        return str(helper), str(tmp_path / "m.bin")

    @staticmethod
    def _quit_while_running(work, quit_call):
        outcome = {}

        def run():
            try:
                outcome["value"] = work()
            except Exception as exc:  # recorded for the assertions
                outcome["error"] = exc

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        assert _wait_for(lambda: child_supervisor._tracked)
        helpers = list(child_supervisor._tracked)
        started = time.monotonic()
        quit_call()
        thread.join(10)
        assert not thread.is_alive() and time.monotonic() - started < 8
        assert all(proc.poll() is not None for proc in helpers)
        return outcome

    @pytest.mark.skipif(sys.platform == "win32", reason="shell-script helper")
    def test_whisper_cli_in_flight_at_quit_is_stopped(self, tmp_path):
        from wayfinder.core.transcriber import (
            TranscriptionError, WhisperCppBackend, WhisperServerBackend,
        )
        binary, model = self._hanging_helper(tmp_path, "whisper-cli")
        backend = WhisperCppBackend(whisper_binary=binary, model_path=model, use_gpu=False,
                                    timeout=60)
        audio = TestNoCliAfterQuit._audio(tmp_path)
        with patch.object(WhisperCppBackend, "_supported_flags", return_value=set()):
            outcome = self._quit_while_running(
                lambda: backend.transcribe(audio),
                lambda: WhisperServerBackend.shutdown(terminal=True))
        assert isinstance(outcome.get("error"), TranscriptionError)

    @pytest.mark.skipif(sys.platform == "win32", reason="shell-script helper")
    def test_llama_simple_in_flight_at_quit_is_stopped(self, tmp_path):
        from wayfinder.core.llama_server import LlamaServerManager
        from wayfinder.core.postprocessor import LlamaCppCliBackend, PostProcessingError
        binary, model = self._hanging_helper(tmp_path, "llama-simple")
        backend = LlamaCppCliBackend(llama_binary=binary, model_path=model, n_gpu_layers=0,
                                     residency="auto")
        with patch.object(backend, "_resident_model", return_value=None), \
             patch.object(backend, "_server_generate", return_value=None), \
             patch.object(backend, "_subprocess_target", return_value=(binary, 0)):
            outcome = self._quit_while_running(
                lambda: backend.process("some words to clean up", ""),
                LlamaServerManager.close)
        # Stopped mid-run: the dictation keeps its words, uncleaned.
        assert (isinstance(outcome.get("error"), PostProcessingError)
                or outcome.get("value") == "some words to clean up")


class TestProbesAndDetectionAtQuit:
    """Sol PR 2 r8: the --help flag probe, the GPU probe and GPU detection
    launched whisper-cli untracked, so one could start after Quit or keep
    running through it. They now go through the same registry."""

    @staticmethod
    def _model_dir(tmp_path, monkeypatch):
        from wayfinder.utils import platform as wf_platform
        models = tmp_path / "models"
        models.mkdir()
        (models / "ggml-tiny.bin").touch()
        monkeypatch.setattr(wf_platform, "get_whisper_model_search_dirs", lambda: [models])

    def test_the_flag_probe_after_quit_runs_nothing(self, tmp_path):
        from wayfinder.core.transcriber import WhisperCppBackend
        binary, model = TestAFallbackRacingQuitIsStopped._hanging_helper(tmp_path, "whisper-cli")
        backend = WhisperCppBackend(whisper_binary=binary, model_path=model, use_gpu=False)
        child_supervisor.close_tracked()
        with patch("subprocess.Popen") as popen:
            assert backend._supported_flags() == set()
            assert backend.supports_gpu() is False
        popen.assert_not_called()
        # Nothing was learned, so nothing is cached for after a reopen.
        assert getattr(backend, "_supported_flags_cache", None) is None
        assert backend._gpu_supported is None

    @pytest.mark.skipif(sys.platform == "win32", reason="shell-script helper")
    @pytest.mark.parametrize("probe", ["_supported_flags", "supports_gpu"])
    def test_a_flag_probe_in_flight_at_quit_is_stopped(self, tmp_path, probe):
        from wayfinder.core.transcriber import WhisperCppBackend
        binary, model = TestAFallbackRacingQuitIsStopped._hanging_helper(tmp_path, "whisper-cli")
        backend = WhisperCppBackend(whisper_binary=binary, model_path=model, use_gpu=False)
        run = (lambda: backend._supported_flags(probe_timeout=60)) if probe == "_supported_flags" \
            else backend.supports_gpu
        with patch("wayfinder.core.transcriber.run_tracked",
                   lambda cmd, **kw: child_supervisor.run_tracked(cmd, **{**kw, "timeout": 60})):
            outcome = TestAFallbackRacingQuitIsStopped._quit_while_running(
                run, lambda: child_supervisor.close_tracked())
        assert outcome.get("value") in (set(), False)
        assert getattr(backend, "_supported_flags_cache", None) is None
        assert backend._gpu_supported is None

    def test_gpu_detection_after_quit_runs_nothing(self, tmp_path, monkeypatch):
        from wayfinder.utils.gpu_simple import _detect_windows_vulkan_devices, detect_gpu_devices
        binary, _ = TestAFallbackRacingQuitIsStopped._hanging_helper(tmp_path, "whisper-cli")
        self._model_dir(tmp_path, monkeypatch)
        monkeypatch.setattr("wayfinder.utils.gpu_simple.windows_vulkan_whisper", lambda c: binary)
        child_supervisor.close_tracked()
        with patch("subprocess.Popen") as popen:
            # No device list, which must not read as "no discrete GPU".
            with pytest.raises(child_supervisor.HelpersClosed):
                detect_gpu_devices({"whisper_binary": binary})
            with pytest.raises(child_supervisor.HelpersClosed):
                _detect_windows_vulkan_devices({})
        popen.assert_not_called()

    @pytest.mark.skipif(sys.platform == "win32", reason="shell-script helper")
    @pytest.mark.parametrize("path", ["linux", "windows"])
    def test_gpu_detection_in_flight_at_quit_is_stopped(self, tmp_path, monkeypatch, path):
        # The GPU-on worker runs this off the Tk thread, so Quit can land mid-probe.
        from wayfinder.utils import gpu_simple
        binary, _ = TestAFallbackRacingQuitIsStopped._hanging_helper(tmp_path, "whisper-cli")
        self._model_dir(tmp_path, monkeypatch)
        monkeypatch.setattr(gpu_simple, "windows_vulkan_whisper", lambda c: binary)
        monkeypatch.setattr(gpu_simple, "run_tracked",
                            lambda cmd, **kw: child_supervisor.run_tracked(cmd, **{**kw, "timeout": 60}))
        work = ((lambda: gpu_simple.detect_gpu_devices({"whisper_binary": binary})) if path == "linux"
                else (lambda: gpu_simple._detect_windows_vulkan_devices({})))
        outcome = TestAFallbackRacingQuitIsStopped._quit_while_running(
            work, lambda: child_supervisor.close_tracked())
        assert isinstance(outcome.get("error"), child_supervisor.HelperStopped)

    @pytest.mark.skipif(sys.platform == "win32", reason="shell-script helper")
    def test_ggml_detection_in_flight_at_quit_is_stopped(self, tmp_path, monkeypatch):
        from wayfinder.utils import gpu
        home = tmp_path / "home"
        (home / "whisper.cpp" / "build" / "bin").mkdir(parents=True)
        TestAFallbackRacingQuitIsStopped._hanging_helper(home / "whisper.cpp" / "build" / "bin",
                                                          "whisper-cli")
        monkeypatch.setenv("HOME", str(home))
        self._model_dir(tmp_path, monkeypatch)
        monkeypatch.setattr(gpu, "run_tracked",
                            lambda cmd, **kw: child_supervisor.run_tracked(cmd, **{**kw, "timeout": 60}))
        outcome = TestAFallbackRacingQuitIsStopped._quit_while_running(
            gpu.detect_ggml_devices, lambda: child_supervisor.close_tracked())
        assert isinstance(outcome.get("error"), child_supervisor.HelperStopped)

    @pytest.mark.skipif(sys.platform == "win32", reason="shell-script helper")
    def test_a_benchmark_cut_short_by_quit_is_not_a_result(self, tmp_path, monkeypatch):
        # Otherwise the device left standing would be cached as the fastest.
        from types import SimpleNamespace
        from wayfinder.utils import gpu
        binary, model = TestAFallbackRacingQuitIsStopped._hanging_helper(tmp_path, "whisper-cli")
        monkeypatch.setattr(gpu, "detect_ggml_devices", lambda: [SimpleNamespace(index=0)])
        outcome = TestAFallbackRacingQuitIsStopped._quit_while_running(
            lambda: gpu.benchmark_gpu_devices(whisper_cli=binary, model_path=model, timeout=60),
            lambda: child_supervisor.close_tracked())
        assert outcome.get("value", {}).get("fastest") == "error"

    def test_timing_and_setup_checks_after_quit_run_nothing(self, tmp_path):
        from wayfinder.core.setup import check_whisper_cpp
        from wayfinder.core.whisper_timings import measure_transcription
        binary, model = TestAFallbackRacingQuitIsStopped._hanging_helper(tmp_path, "whisper-cli")
        audio = TestNoCliAfterQuit._audio(tmp_path)
        child_supervisor.close_tracked()
        with patch("subprocess.Popen") as popen:
            assert measure_transcription([binary, "-m", model], audio, audio).ok is False
            check_whisper_cpp({"whisper_binary": binary})
        popen.assert_not_called()


class TestAnInterpreterExit:
    @pytest.mark.parametrize("ending", ["", "import sys; sys.exit(0)"])
    def test_a_plain_exit_stops_a_tracked_helper(self, tmp_path, ending):
        # No server ever started, so no server exit hook exists (main.py,
        # CLI verbs): the registry's own hook stops the helper.
        pid_file = tmp_path / "helper.pid"
        helper = [sys.executable, "-c",
                  f"import os, time; open({str(pid_file)!r}, 'w').write(str(os.getpid())); "
                  "time.sleep(60)"]
        app = textwrap.dedent(f"""
            import os, threading, time
            from wayfinder.utils import child_supervisor
            threading.Thread(target=child_supervisor.run_tracked, args=({helper!r},),
                             daemon=True).start()
            while not (os.path.exists({str(pid_file)!r}) and open({str(pid_file)!r}).read()):
                time.sleep(0.02)
            {ending}
        """)
        env = {**os.environ, "PYTHONPATH": f"{ROOT / 'src'}{os.pathsep}{ROOT}"}
        subprocess.run([sys.executable, "-c", app], env=env, timeout=60, check=True)
        assert _wait_for(lambda: _gone(int(pid_file.read_text())))


class TestACutShortRunIsNeverAResult:
    """Sol PR 2 r9: the cut-short check read the global closed flag, so a
    worker resuming after a reopen (a failed restart re-exec) cached its
    interrupted result; and a stopped GPU run counted as a GPU failure. The
    shutdown now marks each child it stops, and that run raises HelperStopped
    however late its caller resumes."""

    @staticmethod
    def _resume_after_reopen(monkeypatch):
        """Hold run_tracked between the stop and its result until reopen."""
        real_forget = child_supervisor.forget_tracked
        reopened = threading.Event()

        def forget_after_reopen(proc):
            if getattr(proc, "_aura_cut_short", False):
                reopened.wait(5)
            real_forget(proc)

        monkeypatch.setattr(child_supervisor, "forget_tracked", forget_after_reopen)

        def quit_then_reopen():
            child_supervisor.close_tracked()
            child_supervisor.reopen_tracked()
            reopened.set()

        return quit_then_reopen

    @staticmethod
    def _long(module, monkeypatch):
        # The real registry with a long timeout, so only the stop ends the run.
        monkeypatch.setattr(module, "run_tracked",
                            lambda cmd, **kw: child_supervisor.run_tracked(cmd, **{**kw, "timeout": 60}))

    def test_a_stopped_run_raises_even_after_a_reopen(self, monkeypatch):
        quit_then_reopen = self._resume_after_reopen(monkeypatch)
        outcome = TestAFallbackRacingQuitIsStopped._quit_while_running(
            lambda: child_supervisor.run_tracked(SLEEPER, capture_output=True), quit_then_reopen)
        assert isinstance(outcome.get("error"), child_supervisor.HelperStopped)
        assert child_supervisor.run_tracked([sys.executable, "-c", "pass"]).returncode == 0

    @pytest.mark.skipif(sys.platform == "win32", reason="shell-script helper")
    @pytest.mark.parametrize("probe", ["_supported_flags", "supports_gpu"])
    def test_a_probe_resuming_after_a_reopen_caches_nothing(self, tmp_path, monkeypatch, probe):
        import wayfinder.core.transcriber as transcriber
        binary, model = TestAFallbackRacingQuitIsStopped._hanging_helper(tmp_path, "whisper-cli")
        backend = transcriber.WhisperCppBackend(whisper_binary=binary, model_path=model, use_gpu=False)
        self._long(transcriber, monkeypatch)
        TestAFallbackRacingQuitIsStopped._quit_while_running(
            getattr(backend, probe), self._resume_after_reopen(monkeypatch))
        assert getattr(backend, "_supported_flags_cache", None) is None
        assert backend._gpu_supported is None

    @pytest.mark.skipif(sys.platform == "win32", reason="shell-script helper")
    def test_a_stopped_gpu_transcription_is_not_a_gpu_failure(self, tmp_path, monkeypatch):
        import wayfinder.core.transcriber as transcriber
        binary, model = TestAFallbackRacingQuitIsStopped._hanging_helper(tmp_path, "whisper-cli")
        cpu, _ = TestAFallbackRacingQuitIsStopped._hanging_helper(tmp_path, "whisper-cli-cpu")
        monkeypatch.setattr(transcriber, "_cpu_fallback_binary", lambda b: cpu)
        monkeypatch.setattr(transcriber, "_CPU_FALLBACK_ACTIVE", {})
        monkeypatch.setattr(transcriber, "_gpu_retry_state", {})
        self._long(transcriber, monkeypatch)
        backend = transcriber.WhisperCppBackend(whisper_binary=binary, model_path=model,
                                                use_gpu=True, timeout=60)
        audio = TestNoCliAfterQuit._audio(tmp_path)
        with patch.object(transcriber.WhisperCppBackend, "_supported_flags", return_value=set()):
            outcome = TestAFallbackRacingQuitIsStopped._quit_while_running(
                lambda: backend.transcribe(audio), self._resume_after_reopen(monkeypatch))
        assert isinstance(outcome.get("error"), transcriber.TranscriptionError)
        assert transcriber._CPU_FALLBACK_ACTIVE == {} and transcriber._gpu_retry_state == {}

    @pytest.mark.skipif(sys.platform == "win32", reason="shell-script helper")
    def test_a_stopped_gpu_recovery_probe_changes_no_gpu_state(self, tmp_path, monkeypatch):
        import wayfinder.core.transcriber as transcriber
        binary, model = TestAFallbackRacingQuitIsStopped._hanging_helper(tmp_path, "whisper-cli")
        on_cpu = {binary: binary + "-cpu"}
        state = {binary: {"failures": 2, "next_retry": 0.0}}
        monkeypatch.setattr(transcriber, "_CPU_FALLBACK_ACTIVE", dict(on_cpu))
        monkeypatch.setattr(transcriber, "_gpu_retry_state", {binary: dict(state[binary])})
        self._long(transcriber, monkeypatch)
        backend = transcriber.WhisperCppBackend(whisper_binary=binary, model_path=model, use_gpu=True)
        outcome = TestAFallbackRacingQuitIsStopped._quit_while_running(
            backend._probe_gpu_recovery, self._resume_after_reopen(monkeypatch))
        assert "error" not in outcome   # its thread ends quietly
        assert transcriber._CPU_FALLBACK_ACTIVE == on_cpu and transcriber._gpu_retry_state == state

    @pytest.mark.skipif(sys.platform == "win32", reason="shell-script helper")
    def test_a_stopped_llama_gpu_probe_caches_no_verdict(self, tmp_path, monkeypatch):
        import wayfinder.core.postprocessor as postprocessor
        binary, model = TestAFallbackRacingQuitIsStopped._hanging_helper(tmp_path, "llama-simple")
        backend = postprocessor.LlamaCppCliBackend(llama_binary=binary, model_path=model,
                                                   n_gpu_layers=-1, residency="auto")
        monkeypatch.setattr(postprocessor.LlamaCppCliBackend, "_gpu_probe", {})
        self._long(postprocessor, monkeypatch)
        outcome = TestAFallbackRacingQuitIsStopped._quit_while_running(
            backend._subprocess_target, self._resume_after_reopen(monkeypatch))
        assert isinstance(outcome.get("error"), child_supervisor.HelperStopped)
        assert postprocessor.LlamaCppCliBackend._gpu_probe == {}

    @pytest.mark.skipif(sys.platform == "win32", reason="shell-script helper")
    @pytest.mark.parametrize("reopen", [False, True])
    def test_a_benchmark_with_one_device_measured_is_not_a_result(self, tmp_path, monkeypatch, reopen):
        # Device 0 finishes; Quit stops device 1. Device 0 must not be cached
        # as the fastest, whether or not the app reopens before the loop resumes.
        from types import SimpleNamespace
        from wayfinder.utils import gpu
        helper = tmp_path / "whisper-cli"
        marker = tmp_path / "device1"
        helper.write_text(f'#!/bin/sh\nif [ "$GGML_VK_VISIBLE_DEVICES" = 1 ]; then '
                          f'touch {marker}; exec sleep 30; fi\nexit 0\n')
        helper.chmod(0o755)
        (tmp_path / "m.bin").touch()
        monkeypatch.setattr(gpu, "detect_ggml_devices",
                            lambda: [SimpleNamespace(index=0), SimpleNamespace(index=1)])
        quit_call = (self._resume_after_reopen(monkeypatch) if reopen
                     else child_supervisor.close_tracked)
        outcome = {}
        thread = threading.Thread(target=lambda: outcome.update(value=gpu.benchmark_gpu_devices(
            whisper_cli=str(helper), model_path=str(tmp_path / "m.bin"), timeout=60)), daemon=True)
        thread.start()
        assert _wait_for(marker.exists)
        quit_call()
        thread.join(10)
        assert outcome.get("value", {}).get("fastest") == "error"

    def test_a_benchmark_whose_device_list_quit_stopped_is_not_a_result(self, tmp_path, monkeypatch):
        from wayfinder.utils import gpu
        binary, model = TestAFallbackRacingQuitIsStopped._hanging_helper(tmp_path, "whisper-cli")

        def stopped():
            raise child_supervisor.HelperStopped("stopped by a terminal shutdown")

        monkeypatch.setattr(gpu, "detect_ggml_devices", stopped)
        monkeypatch.setattr(gpu, "detect_vulkan_devices",
                            lambda: pytest.fail("fell back to vulkaninfo's ordering"))
        assert gpu.benchmark_gpu_devices(whisper_cli=binary, model_path=model)["fastest"] == "error"

    def test_gpu_setup_that_quit_cut_short_is_not_a_failed_setup(self, monkeypatch):
        # Otherwise a reopen after a failed restart sends every dictation to
        # the CPU path until GPU is toggled again.
        import wayfinder_main
        workers = []

        class DeferredThread:
            def __init__(self, target, **_kwargs):
                workers.append(target)

            def start(self):
                pass

        def cut_short(config):
            raise child_supervisor.HelperStopped("stopped by a terminal shutdown")

        monkeypatch.setattr(wayfinder_main, "IS_WINDOWS", False)
        monkeypatch.setattr(wayfinder_main, "IS_MACOS", False)
        monkeypatch.setattr(wayfinder_main.threading, "Thread", DeferredThread)
        monkeypatch.setattr("wayfinder.utils.gpu_simple.setup_gpu_environment", cut_short)
        monkeypatch.setattr("wayfinder.core.transcriber.WhisperServerBackend.shutdown",
                            lambda **_: None)
        monkeypatch.setattr(wayfinder_main, "_release_cleanup_residency", lambda **_: None)
        logs = []
        app = SimpleNamespace(config={"use_gpu": True, "whisper_server_mode": True,
                                      "transcription_backend": "whisper_cpp"},
                              log=logs.append)
        wayfinder_main.WayfinderApp._apply_transcription_hardware_change(app, True)
        workers[0]()
        assert app._gpu_setup_done.is_set() and app._gpu_setup_failed is False
        assert not any("GPU setup failed" in line for line in logs)
