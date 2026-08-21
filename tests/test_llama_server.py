"""Unit tests for the resident llama-server lifecycle manager.

Fully mocked — no real server is started. The behaviours locked here are the
ones whose failure modes are silent in production: serving a request from a
server configured for a DIFFERENT model, claiming GPU after falling back to
CPU, and letting an unread stdout pipe wedge the process.
"""
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from wayfinder.core.llama_server import (  # noqa: E402
    LlamaServerError, LlamaServerManager, resolve_server_binary,
)


@pytest.fixture(autouse=True)
def _clean_state():
    LlamaServerManager.reset_for_tests()
    yield
    LlamaServerManager.reset_for_tests()


class _FakeProc:
    """Popen stand-in. `rc` None means alive."""

    def __init__(self, rc=None):
        self._rc = rc
        self.stdout = _FakeStream()
        self.terminated = False
        self.killed = False

    def poll(self):
        return self._rc

    def terminate(self):
        self.terminated = True
        self._rc = 0

    def kill(self):
        self.killed = True
        self._rc = -9

    def wait(self, timeout=None):
        return self._rc


class _FakeStream:
    def __init__(self, lines=()):
        self._lines = list(lines)
        self.closed = False

    def readline(self):
        return self._lines.pop(0) if self._lines else b""

    def close(self):
        self.closed = True


class TestBinaryResolution:
    def test_finds_server_beside_the_configured_cli(self, tmp_path):
        (tmp_path / "llama-simple").touch()
        (tmp_path / "llama-server").touch()
        got = resolve_server_binary(str(tmp_path / "llama-simple"), use_gpu=True)
        assert got == str(tmp_path / "llama-server")

    def test_cpu_mode_prefers_the_cpu_twin(self, tmp_path):
        """A Vulkan-linked binary can SIGSEGV during device enumeration before
        -ngl 0 is honored, so CPU mode must not pick it when a twin exists."""
        (tmp_path / "llama-simple").touch()
        (tmp_path / "llama-server").touch()
        (tmp_path / "llama-server-cpu").touch()
        got = resolve_server_binary(str(tmp_path / "llama-simple"), use_gpu=False)
        assert got == str(tmp_path / "llama-server-cpu")

    def test_gpu_mode_prefers_the_vulkan_binary(self, tmp_path):
        (tmp_path / "llama-simple").touch()
        (tmp_path / "llama-server").touch()
        (tmp_path / "llama-server-cpu").touch()
        got = resolve_server_binary(str(tmp_path / "llama-simple"), use_gpu=True)
        assert got == str(tmp_path / "llama-server")

    def test_missing_server_returns_none_rather_than_a_bad_path(self, tmp_path):
        """A build without llama-server must fall through to the CLI, not point
        at a file that does not exist."""
        (tmp_path / "llama-simple").touch()
        assert resolve_server_binary(str(tmp_path / "llama-simple")) is None

    def test_empty_binary_is_handled(self):
        assert resolve_server_binary("") is None


class TestIdentityAndReuse:
    """The identity check is what stops a request being answered by a server
    holding a different model or running in the wrong CPU/GPU mode."""

    @pytest.mark.parametrize("field,value", [
        ("model_path", "/models/other.gguf"),
        ("n_ctx", 4096),
        ("n_threads", 8),
        ("use_gpu", False),
    ])
    def test_every_identity_field_forces_a_respawn(self, field, value):
        base = dict(binary="/b/llama-server", model_path="/models/a.gguf",
                    n_ctx=2048, n_threads=4, use_gpu=True)
        first = LlamaServerManager._identity_of(**base)
        second = LlamaServerManager._identity_of(**{**base, field: value})
        assert first != second

    def test_identical_config_reuses_the_running_server(self, tmp_path):
        binary, model = tmp_path / "llama-server", tmp_path / "m.gguf"
        binary.touch(); model.touch()
        LlamaServerManager._process = _FakeProc()
        LlamaServerManager._port = 8179
        LlamaServerManager._identity = LlamaServerManager._identity_of(
            str(binary), str(model), 2048, 4, True)
        with patch("subprocess.Popen") as popen:
            port = LlamaServerManager.ensure(str(binary), str(model), 2048, 4, True)
        assert port == 8179
        popen.assert_not_called()

    def test_a_dead_process_is_not_reused(self, tmp_path):
        binary, model = tmp_path / "llama-server", tmp_path / "m.gguf"
        binary.touch(); model.touch()
        LlamaServerManager._process = _FakeProc(rc=1)  # exited
        LlamaServerManager._identity = LlamaServerManager._identity_of(
            str(binary), str(model), 2048, 4, True)
        with patch.object(LlamaServerManager, "_find_port", return_value=(8179, False)), \
             patch.object(LlamaServerManager, "_wait_ready", return_value=True), \
             patch("subprocess.Popen", return_value=_FakeProc()) as popen:
            LlamaServerManager.ensure(str(binary), str(model), 2048, 4, True)
        assert popen.called


class TestFallbackLadder:
    def test_gpu_request_falls_back_to_cpu_and_records_cpu_identity(self, tmp_path):
        """The bug this prevents: a GPU request that lands on a CPU rung must not
        record GPU identity, or the next GPU request reuses the CPU server."""
        binary, model = tmp_path / "llama-server", tmp_path / "m.gguf"
        binary.touch(); model.touch()
        calls = []

        def fake_ready(proc, port, deadline):
            return len(calls) > 1  # first (GPU) rung fails, second (-ngl 0) works

        def fake_popen(cmd, **kw):
            calls.append(cmd)
            return _FakeProc()

        with patch.object(LlamaServerManager, "_find_port", return_value=(8179, False)), \
             patch.object(LlamaServerManager, "_wait_ready", side_effect=fake_ready), \
             patch("subprocess.Popen", side_effect=fake_popen):
            LlamaServerManager.ensure(str(binary), str(model), 2048, 4, use_gpu=True)

        assert "99" in calls[0] and "0" in calls[1]
        assert LlamaServerManager._identity[-1] is False, "must not claim GPU"

    def test_all_rungs_failing_disables_the_server(self, tmp_path):
        binary, model = tmp_path / "llama-server", tmp_path / "m.gguf"
        binary.touch(); model.touch()
        with patch.object(LlamaServerManager, "_find_port", return_value=(8179, False)), \
             patch.object(LlamaServerManager, "_wait_ready", return_value=False), \
             patch("subprocess.Popen", return_value=_FakeProc()):
            with pytest.raises(LlamaServerError):
                LlamaServerManager.ensure(str(binary), str(model), 2048, 4, True)
        assert LlamaServerManager._disabled is True

    def test_once_disabled_it_stops_paying_startup_cost(self, tmp_path):
        binary, model = tmp_path / "llama-server", tmp_path / "m.gguf"
        binary.touch(); model.touch()
        LlamaServerManager._disabled = True
        with patch("subprocess.Popen") as popen:
            with pytest.raises(LlamaServerError):
                LlamaServerManager.ensure(str(binary), str(model))
        popen.assert_not_called()

    def test_missing_binary_raises_without_spawning(self, tmp_path):
        model = tmp_path / "m.gguf"; model.touch()
        with pytest.raises(LlamaServerError, match="binary not found"):
            LlamaServerManager.ensure(str(tmp_path / "nope"), str(model))

    def test_missing_model_raises_without_spawning(self, tmp_path):
        binary = tmp_path / "llama-server"; binary.touch()
        with pytest.raises(LlamaServerError, match="model not found"):
            LlamaServerManager.ensure(str(binary), str(tmp_path / "nope.gguf"))


class TestAdoption:
    def test_adopted_server_records_unknown_identity(self, tmp_path):
        """An externally started server's real spawn flags are unknowable, so the
        next ensure() must re-probe instead of trusting an unverified match."""
        binary, model = tmp_path / "llama-server", tmp_path / "m.gguf"
        binary.touch(); model.touch()
        with patch.object(LlamaServerManager, "_find_port", return_value=(8179, True)), \
             patch("subprocess.Popen") as popen:
            port = LlamaServerManager.ensure(str(binary), str(model), 2048, 4, True)
        assert port == 8179
        popen.assert_not_called()
        assert LlamaServerManager._identity is None

    def test_serving_model_rejects_a_different_model(self):
        with patch.object(LlamaServerManager, "_props",
                          return_value={"model_path": "/models/other.gguf"}):
            assert LlamaServerManager._serving_model(8179, "/models/mine.gguf") is False

    def test_serving_model_accepts_the_same_model(self, tmp_path):
        m = tmp_path / "m.gguf"; m.touch()
        with patch.object(LlamaServerManager, "_props",
                          return_value={"model_path": str(m)}):
            assert LlamaServerManager._serving_model(8179, str(m)) is True

    def test_unreachable_port_is_not_ours(self):
        with patch.object(LlamaServerManager, "_props", return_value=None):
            assert LlamaServerManager._serving_model(8179, "/models/m.gguf") is False


class TestLogDrain:
    def test_drain_consumes_until_eof_and_closes(self):
        tail = __import__("collections").deque(maxlen=200)
        stream = _FakeStream([b"loading\n", b"ready\n"])
        LlamaServerManager._drain(stream, tail)
        assert list(tail) == ["loading", "ready"]
        assert stream.closed is True

    def test_non_bytes_is_treated_as_eof_not_an_infinite_loop(self):
        """A test double or corrupted stream must not spin forever waiting for a
        b"" sentinel it can never produce."""
        tail = __import__("collections").deque(maxlen=200)
        LlamaServerManager._drain(_FakeStream(["str-not-bytes\n"]), tail)
        assert list(tail) == []

    def test_tail_is_bounded(self):
        tail = __import__("collections").deque(maxlen=200)
        LlamaServerManager._drain(
            _FakeStream([b"line %d\n" % i for i in range(500)]), tail)
        assert len(tail) == 200
        assert tail[-1] == "line 499"

    def test_closed_pipe_does_not_propagate(self):
        class Boom(_FakeStream):
            def readline(self):
                raise ValueError("closed underneath us")
        LlamaServerManager._drain(Boom(), __import__("collections").deque())


class TestShutdown:
    def test_shutdown_terminates_and_clears_state(self):
        proc = _FakeProc()
        LlamaServerManager._process = proc
        LlamaServerManager._port = 8179
        LlamaServerManager._identity = ("a", "b", 1, 2, True)
        LlamaServerManager.shutdown()
        assert proc.terminated is True
        assert LlamaServerManager._process is None
        assert LlamaServerManager._identity is None
        assert LlamaServerManager._port == 0

    def test_shutdown_is_idempotent(self):
        LlamaServerManager.shutdown()
        LlamaServerManager.shutdown()

    def test_a_process_that_ignores_terminate_is_killed(self):
        class Stubborn(_FakeProc):
            def terminate(self):
                self.terminated = True  # stays alive
            def wait(self, timeout=None):
                if not self.killed:
                    raise subprocess.TimeoutExpired("llama-server", timeout)
                return -9
        proc = Stubborn()
        LlamaServerManager._process = proc
        LlamaServerManager.shutdown()
        assert proc.killed is True


class TestSpawnCommand:
    def _cmd(self, **kw):
        return LlamaServerManager._spawn_attempts(
            binary="/b/llama-server", model_path="/m/x.gguf", n_ctx=2048,
            n_threads=4, use_gpu=True, port=8179, **kw)[0]

    def test_binds_loopback_only(self):
        cmd = self._cmd()
        assert "127.0.0.1" in cmd
        assert "0.0.0.0" not in cmd

    def test_single_slot_keeps_the_kv_cache_whole(self):
        cmd = self._cmd()
        assert cmd[cmd.index("--parallel") + 1] == "1"

    def test_context_and_threads_are_passed_through(self):
        cmd = self._cmd()
        assert cmd[cmd.index("-c") + 1] == "2048"
        assert cmd[cmd.index("-t") + 1] == "4"

    def test_cpu_request_has_no_gpu_layers(self):
        cmd = LlamaServerManager._spawn_attempts(
            "/b/llama-server", "/m/x.gguf", 2048, 4, use_gpu=False, port=8179)[0]
        assert cmd[cmd.index("-ngl") + 1] == "0"

    def test_cpu_request_has_no_rescue_rungs(self):
        """Already on CPU — there is nothing to degrade to."""
        attempts = LlamaServerManager._spawn_attempts(
            "/b/llama-server", "/m/x.gguf", 2048, 4, use_gpu=False, port=8179)
        assert len(attempts) == 1
