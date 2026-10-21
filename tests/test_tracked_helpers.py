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
            outcome["result"] = child_supervisor.run_tracked(SLEEPER, capture_output=True, timeout=60)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        assert _wait_for(lambda: child_supervisor._tracked)
        (proc,) = list(child_supervisor._tracked)
        started = time.monotonic()
        child_supervisor.close_tracked()
        thread.join(10)
        assert not thread.is_alive() and time.monotonic() - started < 6
        assert proc.poll() is not None and outcome["result"].returncode != 0
        assert not child_supervisor._tracked

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
