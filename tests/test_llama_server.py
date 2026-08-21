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
        ("n_gpu_layers", 0),
        # Partial offload is a DIFFERENT server from full offload: collapsing
        # this to a bool made a deliberate 10-layer setting mean "all layers".
        ("n_gpu_layers", 10),
    ])
    def test_every_identity_field_forces_a_respawn(self, field, value):
        base = dict(binary="/b/llama-server", model_path="/models/a.gguf",
                    n_ctx=2048, n_threads=4, n_gpu_layers=99)
        first = LlamaServerManager._identity_of(**base)
        second = LlamaServerManager._identity_of(**{**base, field: value})
        assert first != second

    def test_identical_config_reuses_the_running_server(self, tmp_path):
        binary, model = tmp_path / "llama-server", tmp_path / "m.gguf"
        binary.touch(); model.touch()
        LlamaServerManager._process = _FakeProc()
        LlamaServerManager._port = 8179
        ident = LlamaServerManager._identity_of(str(binary), str(model), 2048, 4, 99)
        LlamaServerManager._identity = ident
        LlamaServerManager._requested = ident
        with patch("subprocess.Popen") as popen:
            port = LlamaServerManager.ensure(str(binary), str(model), 2048, 4, 99)
        assert port == 8179
        popen.assert_not_called()

    def test_a_dead_process_is_not_reused(self, tmp_path):
        binary, model = tmp_path / "llama-server", tmp_path / "m.gguf"
        binary.touch(); model.touch()
        LlamaServerManager._process = _FakeProc(rc=1)  # exited
        ident = LlamaServerManager._identity_of(str(binary), str(model), 2048, 4, 99)
        LlamaServerManager._identity = ident
        LlamaServerManager._requested = ident
        with patch.object(LlamaServerManager, "_find_port", return_value=8179), \
             patch.object(LlamaServerManager, "_wait_ready", return_value=True), \
             patch.object(LlamaServerManager, "_serving_model", return_value=True), \
             patch("subprocess.Popen", return_value=_FakeProc()) as popen:
            LlamaServerManager.ensure(str(binary), str(model), 2048, 4, 99)
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

        with patch.object(LlamaServerManager, "_find_port", return_value=8179), \
             patch.object(LlamaServerManager, "_wait_ready", side_effect=fake_ready), \
             patch.object(LlamaServerManager, "_serving_model", return_value=True), \
             patch("subprocess.Popen", side_effect=fake_popen):
            LlamaServerManager.ensure(str(binary), str(model), 2048, 4, n_gpu_layers=99)

        assert calls[0][calls[0].index("-ngl") + 1] == "99"
        assert calls[1][calls[1].index("-ngl") + 1] == "0"
        assert LlamaServerManager._identity[-1] == 0, "must not claim GPU"
        assert LlamaServerManager.spawned_gpu_layers() == 0

    def test_all_rungs_failing_disables_the_server(self, tmp_path):
        binary, model = tmp_path / "llama-server", tmp_path / "m.gguf"
        binary.touch(); model.touch()
        with patch.object(LlamaServerManager, "_find_port", return_value=8179), \
             patch.object(LlamaServerManager, "_wait_ready", return_value=False), \
             patch("subprocess.Popen", return_value=_FakeProc()):
            with pytest.raises(LlamaServerError):
                LlamaServerManager.ensure(str(binary), str(model), 2048, 4, 99)
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


class TestNeverAdoptsAStranger:
    """/props is unauthenticated, so a local process could claim to be serving
    our model and receive every dictation. We only ever use servers we spawned."""

    def test_an_occupied_port_is_skipped_never_adopted(self):
        import socket as _s
        real = _s.socket

        class Occupied(real):
            def connect_ex(self, addr):
                return 0 if addr[1] == 8179 else 1  # 8179 busy, 8180 free

        with patch("socket.socket", Occupied):
            assert LlamaServerManager._find_port() == 8180

    def test_all_ports_occupied_fails_rather_than_adopting(self, tmp_path):
        binary, model = tmp_path / "llama-server", tmp_path / "m.gguf"
        binary.touch(); model.touch()
        with patch.object(LlamaServerManager, "_find_port", return_value=None), \
             patch("subprocess.Popen") as popen:
            with pytest.raises(LlamaServerError, match="no free port"):
                LlamaServerManager.ensure(str(binary), str(model))
        popen.assert_not_called()

    def test_a_spawn_serving_the_wrong_model_is_rejected(self, tmp_path):
        """Post-spawn self-check: a child that loaded a different file would
        otherwise clean every dictation with the wrong model, invisibly."""
        binary, model = tmp_path / "llama-server", tmp_path / "m.gguf"
        binary.touch(); model.touch()
        with patch.object(LlamaServerManager, "_find_port", return_value=8179), \
             patch.object(LlamaServerManager, "_wait_ready", return_value=True), \
             patch.object(LlamaServerManager, "_serving_model", return_value=False), \
             patch("subprocess.Popen", return_value=_FakeProc()):
            with pytest.raises(LlamaServerError, match="unexpected model"):
                LlamaServerManager.ensure(str(binary), str(model), 2048, 4, 99)

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
            n_threads=4, n_gpu_layers=99, port=8179, **kw)[0]

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
            "/b/llama-server", "/m/x.gguf", 2048, 4, n_gpu_layers=0, port=8179)[0]
        assert cmd[cmd.index("-ngl") + 1] == "0"

    def test_cpu_request_has_no_rescue_rungs(self):
        """Already on CPU — there is nothing to degrade to."""
        attempts = LlamaServerManager._spawn_attempts(
            "/b/llama-server", "/m/x.gguf", 2048, 4, n_gpu_layers=0, port=8179)
        assert len(attempts) == 1


class TestWedgedServerRecovery:
    """A server that accepts the connection but never answers must not make every
    future dictation pay the same timeout."""

    def _backend(self, tmp_path):
        from wayfinder.core.postprocessor import LlamaCppCliBackend
        (tmp_path / "llama-simple").touch()
        (tmp_path / "llama-server").touch()
        model = tmp_path / "m.gguf"; model.touch()
        return LlamaCppCliBackend(
            llama_binary=str(tmp_path / "llama-simple"),
            model_path=str(model), residency="auto")

    def test_timeout_restarts_the_server_and_falls_back(self, tmp_path):
        import socket as _socket
        b = self._backend(tmp_path)
        with patch.object(LlamaServerManager, "ensure", return_value=8179), \
             patch.object(LlamaServerManager, "complete", side_effect=_socket.timeout()), \
             patch.object(LlamaServerManager, "shutdown") as shut:
            assert b._server_generate("prompt", 64) is None
        shut.assert_called_once()

    def test_a_non_timeout_error_does_not_restart(self, tmp_path):
        """Restarting on every transient error would thrash the model load."""
        b = self._backend(tmp_path)
        with patch.object(LlamaServerManager, "ensure", return_value=8179), \
             patch.object(LlamaServerManager, "complete", side_effect=ValueError("bad json")), \
             patch.object(LlamaServerManager, "shutdown") as shut:
            assert b._server_generate("prompt", 64) is None
        shut.assert_not_called()

    def test_start_failure_degrades_to_the_cli(self, tmp_path):
        b = self._backend(tmp_path)
        with patch.object(LlamaServerManager, "ensure",
                          side_effect=LlamaServerError("no server")):
            assert b._server_generate("prompt", 64) is None

    def test_request_timeout_is_shorter_than_a_frozen_dictation(self):
        assert LlamaServerManager.REQUEST_TIMEOUT <= 30.0

    def test_the_port_ensure_returned_is_the_one_used(self, tmp_path):
        """Reading cls._port inside complete() would race a concurrent shutdown."""
        b = self._backend(tmp_path)
        seen = {}
        def cap(**kw):
            seen.update(kw); return {"content": "x", "stop_type": "eos"}
        with patch.object(LlamaServerManager, "ensure", return_value=8181), \
             patch.object(LlamaServerManager, "complete", side_effect=cap):
            b._server_generate("prompt", 64)
        assert seen["port"] == 8181


class TestGpuModeIsRecordedNotAssumed:
    def test_a_numeric_argument_of_99_does_not_imply_gpu(self):
        """`"99" in cmd` scanned the whole argv, so n_threads=99 made a CPU
        server record itself as GPU and be reused for GPU requests."""
        cmd = LlamaServerManager._spawn_attempts(
            "/b/llama-server", "/m/x.gguf", n_ctx=2048, n_threads=99,
            n_gpu_layers=0, port=8179)[0]
        assert "99" in cmd, "precondition: 99 appears as a non-ngl argument"
        assert cmd[cmd.index("-ngl") + 1] == "0"


class TestCpuRescueIsReused:
    """A GPU request that legitimately degraded to CPU must be REUSED by the next
    identical request. Keying reuse off the spawned layer count instead of the
    requested one killed the healthy CPU server and retried the broken GPU ladder
    on every dictation — on a broken-Vulkan machine that reloaded the model each
    time and made the startup warm-up worthless."""

    def _degrade_to_cpu(self, tmp_path):
        binary, model = tmp_path / "llama-server", tmp_path / "m.gguf"
        binary.touch(); model.touch()
        calls = []

        def ready(proc, port, deadline):
            return len(calls) > 1  # GPU rung fails, -ngl 0 rung works

        with patch.object(LlamaServerManager, "_find_port", return_value=8179), \
             patch.object(LlamaServerManager, "_wait_ready", side_effect=ready), \
             patch.object(LlamaServerManager, "_serving_model", return_value=True), \
             patch("subprocess.Popen", side_effect=lambda cmd, **kw: (calls.append(cmd), _FakeProc())[1]):
            LlamaServerManager.ensure(str(binary), str(model), 2048, 4, n_gpu_layers=99)
        return binary, model, calls

    def test_the_degraded_server_is_reused_not_respawned(self, tmp_path):
        binary, model, _ = self._degrade_to_cpu(tmp_path)
        with patch("subprocess.Popen") as popen:
            port = LlamaServerManager.ensure(str(binary), str(model), 2048, 4, n_gpu_layers=99)
        assert port == 8179
        popen.assert_not_called(), "a working CPU rescue must not be killed and retried"

    def test_it_is_still_labelled_cpu(self, tmp_path):
        self._degrade_to_cpu(tmp_path)
        assert LlamaServerManager.spawned_gpu_layers() == 0

    def test_a_genuinely_different_request_still_respawns(self, tmp_path):
        binary, model, _ = self._degrade_to_cpu(tmp_path)
        with patch.object(LlamaServerManager, "_find_port", return_value=8179), \
             patch.object(LlamaServerManager, "_wait_ready", return_value=True), \
             patch.object(LlamaServerManager, "_serving_model", return_value=True), \
             patch("subprocess.Popen", return_value=_FakeProc()) as popen:
            LlamaServerManager.ensure(str(binary), str(model), 4096, 4, n_gpu_layers=99)
        assert popen.called


class TestShutdownReachesAnUnpublishedChild:
    """The gap sol found: ensure() holds the lock for its WHOLE readiness wait,
    shutdown() gives up on the lock after 5s, and the app then calls os._exit(0).
    A child spawned but not yet published was reachable by nobody."""

    def test_the_in_flight_child_is_tracked_where_shutdown_can_see_it(self, tmp_path):
        binary, model = tmp_path / "llama-server", tmp_path / "m.gguf"
        binary.touch(); model.touch()
        proc = _FakeProc()
        seen = {}

        def ready(p, port, deadline):
            # Mid-load: this is exactly the window in which a Quit lands.
            seen["starting"] = LlamaServerManager._starting
            return True

        with patch.object(LlamaServerManager, "_find_port", return_value=8179), \
             patch.object(LlamaServerManager, "_wait_ready", side_effect=ready), \
             patch.object(LlamaServerManager, "_owns_listener", return_value=True), \
             patch.object(LlamaServerManager, "_serving_model", return_value=True), \
             patch("subprocess.Popen", return_value=proc):
            LlamaServerManager.ensure(str(binary), str(model), 2048, 4, 99)
        assert seen["starting"] is proc, "shutdown() could not have reached it"

    def test_shutdown_without_the_lock_kills_the_unpublished_child(self):
        """The no-lock branch used to kill only _process, which is still None
        while the child is loading — so the child survived the app."""
        proc = _FakeProc()
        LlamaServerManager._starting = proc
        LlamaServerManager._lock.acquire()
        try:
            LlamaServerManager.shutdown()   # cannot take the lock
        finally:
            LlamaServerManager._lock.release()
            LlamaServerManager._starting = None
        assert proc.terminated or proc.killed, "the unpublished child was orphaned"

    def test_the_handle_is_cleared_once_the_ladder_is_done(self, tmp_path):
        """A stale handle would let a LATER shutdown kill a healthy server."""
        binary, model = tmp_path / "llama-server", tmp_path / "m.gguf"
        binary.touch(); model.touch()
        with patch.object(LlamaServerManager, "_find_port", return_value=8179), \
             patch.object(LlamaServerManager, "_wait_ready", return_value=True), \
             patch.object(LlamaServerManager, "_owns_listener", return_value=True), \
             patch.object(LlamaServerManager, "_serving_model", return_value=True), \
             patch("subprocess.Popen", return_value=_FakeProc()):
            LlamaServerManager.ensure(str(binary), str(model), 2048, 4, 99)
        assert LlamaServerManager._starting is None

    def test_a_shutdown_stops_the_ladder_from_spawning_more_rungs(self, tmp_path):
        """Without a per-rung epoch check, a shutdown partway down the ladder
        still spawned every remaining fallback rung."""
        binary, model = tmp_path / "llama-server", tmp_path / "m.gguf"
        binary.touch(); model.touch()
        spawned = []

        def popen(cmd, **kw):
            spawned.append(cmd)
            LlamaServerManager._epoch += 1   # a shutdown lands after rung 1
            return _FakeProc()

        with patch.object(LlamaServerManager, "_find_port", return_value=8179), \
             patch.object(LlamaServerManager, "_wait_ready", return_value=False), \
             patch("subprocess.Popen", side_effect=popen):
            with pytest.raises(LlamaServerError, match="shut down while starting"):
                LlamaServerManager.ensure(str(binary), str(model), 2048, 4, 99)
        assert len(spawned) == 1, f"kept laddering after a shutdown: {spawned}"


class TestListenerOwnership:
    """_find_port() only proves nobody was listening a MOMENT AGO. Between that
    probe and llama-server's bind, any local process can take the port, answer
    /health, and echo our model path back from the unauthenticated /props."""

    def test_a_port_held_by_a_stranger_is_refused(self, tmp_path):
        binary, model = tmp_path / "llama-server", tmp_path / "m.gguf"
        binary.touch(); model.touch()
        served = []

        with patch.object(LlamaServerManager, "_find_port", return_value=8179), \
             patch.object(LlamaServerManager, "_wait_ready", return_value=True), \
             patch.object(LlamaServerManager, "_owns_listener", return_value=False), \
             patch.object(LlamaServerManager, "_serving_model",
                          side_effect=lambda *a: served.append(a) or True), \
             patch("subprocess.Popen", return_value=_FakeProc()):
            with pytest.raises(LlamaServerError):
                LlamaServerManager.ensure(str(binary), str(model), 2048, 4, 99)
        assert LlamaServerManager._process is None, "published a stranger's listener"
        assert served == [], "asked a stranger to identify itself, and believed it"

    def test_ownership_is_checked_before_identity(self, tmp_path):
        """/props is unauthenticated, so _serving_model believes whatever the
        listener claims. It must only ever be asked of a proven-ours listener."""
        binary, model = tmp_path / "llama-server", tmp_path / "m.gguf"
        binary.touch(); model.touch()
        order = []
        with patch.object(LlamaServerManager, "_find_port", return_value=8179), \
             patch.object(LlamaServerManager, "_wait_ready", return_value=True), \
             patch.object(LlamaServerManager, "_owns_listener",
                          side_effect=lambda *a: order.append("own") or True), \
             patch.object(LlamaServerManager, "_serving_model",
                          side_effect=lambda *a: order.append("props") or True), \
             patch("subprocess.Popen", return_value=_FakeProc()):
            LlamaServerManager.ensure(str(binary), str(model), 2048, 4, 99)
        assert order == ["own", "props"]

    def test_an_undeterminable_kernel_does_not_disable_cleanup(self, tmp_path):
        """None means /proc could not tell us. Defence in depth, not a promise:
        failing closed here would kill cleanup on every kernel that hides this."""
        binary, model = tmp_path / "llama-server", tmp_path / "m.gguf"
        binary.touch(); model.touch()
        with patch.object(LlamaServerManager, "_find_port", return_value=8179), \
             patch.object(LlamaServerManager, "_wait_ready", return_value=True), \
             patch.object(LlamaServerManager, "_owns_listener", return_value=None), \
             patch.object(LlamaServerManager, "_serving_model", return_value=True), \
             patch("subprocess.Popen", return_value=_FakeProc()):
            assert LlamaServerManager.ensure(str(binary), str(model), 2048, 4, 99) == 8179

    def test_ownership_detection_works_on_this_kernel(self):
        """The helper is worthless if /proc parsing is wrong here: it would
        report None forever and silently restore the hole it was added to close."""
        import socket as _s, subprocess as _sp, sys as _sys, time as _t
        probe = _s.socket(); probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]; probe.close()
        child = _sp.Popen([_sys.executable, "-m", "http.server", str(port),
                           "--bind", "127.0.0.1"],
                          stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
        stranger = _sp.Popen([_sys.executable, "-c", "import time;time.sleep(30)"])
        try:
            for _ in range(50):
                _t.sleep(0.1)
                if LlamaServerManager._listener_inodes(port):
                    break
            assert LlamaServerManager._owns_listener(child, port) is True
            assert LlamaServerManager._owns_listener(stranger, port) is False
        finally:
            child.kill(); stranger.kill(); child.wait(); stranger.wait()


class TestShutdownDuringStartup:
    """Quit / GPU toggle / model switch can fire while a startup is still loading.
    Publishing that child afterwards leaves a multi-GB process behind."""

    def test_a_startup_that_finishes_after_a_shutdown_does_not_publish(self, tmp_path):
        binary, model = tmp_path / "llama-server", tmp_path / "m.gguf"
        binary.touch(); model.touch()
        proc = _FakeProc()

        def ready(p, port, deadline):
            LlamaServerManager._epoch += 1   # a shutdown lands mid-load
            return True

        with patch.object(LlamaServerManager, "_find_port", return_value=8179), \
             patch.object(LlamaServerManager, "_wait_ready", side_effect=ready), \
             patch.object(LlamaServerManager, "_serving_model", return_value=True), \
             patch("subprocess.Popen", return_value=proc):
            with pytest.raises(LlamaServerError, match="shut down while starting"):
                LlamaServerManager.ensure(str(binary), str(model), 2048, 4, 99)
        assert LlamaServerManager._process is None
        assert proc.terminated or proc.killed, "the orphan must be killed"

    def test_shutdown_without_the_lock_still_bumps_the_epoch(self):
        """The no-lock branch cannot write shared state, but it MUST invalidate
        an in-flight startup or the orphan it was racing survives."""
        before = LlamaServerManager._epoch
        LlamaServerManager._lock.acquire()
        try:
            LlamaServerManager.shutdown()   # cannot take the lock, times out
        finally:
            LlamaServerManager._lock.release()
        assert LlamaServerManager._epoch > before

    def test_our_own_stop_does_not_invalidate_our_own_startup(self, tmp_path):
        """_stop_locked() bumps the epoch too; capturing it too early made every
        startup abort itself."""
        binary, model = tmp_path / "llama-server", tmp_path / "m.gguf"
        binary.touch(); model.touch()
        with patch.object(LlamaServerManager, "_find_port", return_value=8179), \
             patch.object(LlamaServerManager, "_wait_ready", return_value=True), \
             patch.object(LlamaServerManager, "_serving_model", return_value=True), \
             patch("subprocess.Popen", return_value=_FakeProc()):
            assert LlamaServerManager.ensure(str(binary), str(model), 2048, 4, 99) == 8179


class TestStartupDeadline:
    """The ladder must fit inside the app's 120s PROCESSING watchdog, which
    DISCARDS the cleaned text rather than merely delaying it."""

    def test_lock_acquisition_respects_the_deadline(self, tmp_path):
        import time as _t
        binary, model = tmp_path / "llama-server", tmp_path / "m.gguf"
        binary.touch(); model.touch()
        LlamaServerManager._lock.acquire()
        try:
            with pytest.raises(LlamaServerError, match="deadline"):
                LlamaServerManager.ensure(str(binary), str(model), 2048, 4, 99,
                                          deadline=_t.monotonic() + 0.2)
        finally:
            LlamaServerManager._lock.release()

    def test_readiness_wait_is_clamped_to_the_deadline(self, tmp_path):
        import time as _t
        binary, model = tmp_path / "llama-server", tmp_path / "m.gguf"
        binary.touch(); model.touch()
        seen = []
        with patch.object(LlamaServerManager, "_find_port", return_value=8179), \
             patch.object(LlamaServerManager, "_wait_ready",
                          side_effect=lambda p, port, dl: (seen.append(dl), False)[1]), \
             patch("subprocess.Popen", return_value=_FakeProc()):
            with pytest.raises(LlamaServerError):
                LlamaServerManager.ensure(str(binary), str(model), 2048, 4, 99,
                                          deadline=_t.monotonic() + 1.0)
        assert seen, "readiness was never attempted"
        assert all(d <= _t.monotonic() + 1.1 for d in seen), \
            "an attempt was allowed to run past the caller's budget"

    def test_no_deadline_means_the_full_startup_timeout(self, tmp_path):
        import time as _t
        binary, model = tmp_path / "llama-server", tmp_path / "m.gguf"
        binary.touch(); model.touch()
        seen = []
        with patch.object(LlamaServerManager, "_find_port", return_value=8179), \
             patch.object(LlamaServerManager, "_wait_ready",
                          side_effect=lambda p, port, dl: (seen.append(dl), False)[1]), \
             patch("subprocess.Popen", return_value=_FakeProc()):
            with pytest.raises(LlamaServerError):
                LlamaServerManager.ensure(str(binary), str(model), 2048, 4, 99)
        assert seen[0] > _t.monotonic() + 60, "background warm-up should be unbounded"


class TestRepeatedFailurePolicy:
    def test_a_persistently_failing_server_is_eventually_restarted(self):
        LlamaServerManager._consecutive_failures = 0
        with patch.object(LlamaServerManager, "shutdown") as shut:
            results = [LlamaServerManager.note_failure()
                       for _ in range(LlamaServerManager.MAX_CONSECUTIVE_FAILURES)]
        assert results[-1] is True and not any(results[:-1])
        shut.assert_called_once()

    def test_a_one_off_failure_does_not_thrash_the_model_load(self):
        LlamaServerManager._consecutive_failures = 0
        with patch.object(LlamaServerManager, "shutdown") as shut:
            assert LlamaServerManager.note_failure() is False
        shut.assert_not_called()

    def test_success_resets_the_counter(self):
        LlamaServerManager._consecutive_failures = 0
        with patch.object(LlamaServerManager, "shutdown") as shut:
            LlamaServerManager.note_failure()
            LlamaServerManager.note_success()
            for _ in range(LlamaServerManager.MAX_CONSECUTIVE_FAILURES - 1):
                assert LlamaServerManager.note_failure() is False
        shut.assert_not_called()


class TestSaveMemoryReleasesEverything:
    def test_save_memory_disables_the_resident_wheel_too(self, tmp_path):
        """Checking only force_subprocess left llama-cpp-python loading and
        CACHING the model, so the setting freed nothing."""
        from wayfinder.core.postprocessor import LlamaCppCliBackend
        (tmp_path / "llama-simple").touch()
        model = tmp_path / "m.gguf"; model.touch()
        b = LlamaCppCliBackend(llama_binary=str(tmp_path / "llama-simple"),
                               model_path=str(model), residency="save_memory")
        assert b._resident_model() is None
        assert b._server_enabled() is False

    def test_auto_still_allows_the_wheel(self, tmp_path):
        from wayfinder.core.postprocessor import LlamaCppCliBackend
        (tmp_path / "llama-simple").touch()
        model = tmp_path / "m.gguf"; model.touch()
        b = LlamaCppCliBackend(llama_binary=str(tmp_path / "llama-simple"),
                               model_path=str(model), residency="auto")
        assert b._server_enabled() is True
