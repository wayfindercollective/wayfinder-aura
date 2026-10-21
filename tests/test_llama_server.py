"""Unit tests for the resident llama-server lifecycle manager.

Fully mocked — no real server is started. The behaviours locked here are the
ones whose failure modes are silent in production: serving a request from a
server configured for a DIFFERENT model, claiming GPU after falling back to
CPU, and letting an unread stdout pipe wedge the process.
"""
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from wayfinder.core.llama_server import (  # noqa: E402
    LlamaServerError, LlamaServerManager, LlamaServerSuperseded, resolve_server_binary,
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
        # A pid that cannot exist, so _owns_listener's /proc lookup fails fast
        # and returns "cannot determine" instead of consulting the host. Without
        # this the suite reads the REAL /proc/net/tcp: a live app serving on the
        # same port made unrelated tests find a stranger's socket and crash on
        # the missing attribute. Tests must not depend on what the host is running.
        self.pid = -1

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


def _ensure_serving(proc, port=8179):
    """Stub ensure() that, like the real one, hands this thread `proc`."""
    def ensure(*_a, **_kw):
        LlamaServerManager._served.proc = proc
        return port
    return patch.object(LlamaServerManager, "ensure", side_effect=ensure)


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

        def fake_ready(proc, port, deadline, epoch=None):
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
        import time as _time
        b = self._backend(tmp_path)
        wedged = LlamaServerManager._process = _FakeProc()
        with _ensure_serving(wedged), \
             patch.object(LlamaServerManager, "complete", side_effect=_socket.timeout()):
            assert b._server_generate("prompt", 64) is None
            # The restart moved off-thread so a wedged stop (lock wait +
            # child termination) cannot eat this dictation's budget — wait for
            # the spawned thread rather than asserting synchronously.
            deadline = _time.time() + 5
            while not wedged.terminated and _time.time() < deadline:
                _time.sleep(0.01)
        assert wedged.terminated and LlamaServerManager._process is None

    def test_a_non_timeout_error_does_not_restart(self, tmp_path):
        """Restarting on every transient error would thrash the model load."""
        b = self._backend(tmp_path)
        with patch.object(LlamaServerManager, "ensure", return_value=8179), \
             patch.object(LlamaServerManager, "complete", side_effect=ValueError("bad json")), \
             patch.object(LlamaServerManager, "restart") as restart:
            assert b._server_generate("prompt", 64) is None
        restart.assert_not_called()

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

        def ready(proc, port, deadline, epoch=None):
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


@pytest.fixture(autouse=True)
def _assume_we_own_the_listener(request):
    """Tests that are not ABOUT ownership must never consult the real /proc.

    _owns_listener reads /proc/net/tcp for EVERY listener on the port, so its
    answer depends on what the host happens to be running — and the check now
    fails closed, so a live server on the test port turned half this file red.
    Patching the seam (rather than relying on a fake pid making the lookup fail)
    is what actually decouples them: the ownership tests below opt out and
    exercise the real thing.
    """
    if request.cls is not None and request.cls.__name__ in (
            "TestListenerOwnership", "TestOwnershipIsRecheckedPerRequest",
            "TestARequestIsBoundToItsChild"):
        yield
        return
    with patch.object(LlamaServerManager, "_owns_listener", return_value=True), \
         patch.object(LlamaServerManager, "still_ours", return_value=True):
        yield


class TestShutdownReachesAnUnpublishedChild:
    """The gap sol found: ensure() holds the lock for its WHOLE readiness wait,
    shutdown() gives up on the lock after 5s, and the app then calls os._exit(0).
    A child spawned but not yet published was reachable by nobody."""

    def test_the_in_flight_child_is_tracked_where_shutdown_can_see_it(self, tmp_path):
        binary, model = tmp_path / "llama-server", tmp_path / "m.gguf"
        binary.touch(); model.touch()
        proc = _FakeProc()
        seen = {}

        def ready(p, port, deadline, epoch=None):
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

    def test_a_shutdown_landing_inside_popen_does_not_leave_a_live_child(self, tmp_path):
        """The window a follow-up review found and reproduced.

        shutdown() times out on the lock WHILE Popen is still running: it bumps
        the epoch, looks at _starting, finds None, and returns satisfied. Popen
        then returns a live child. Before the post-publish re-check the next
        epoch test was on the far side of the readiness wait — and Quit calls
        os._exit() long before that, so the child outlived the app."""
        binary, model = tmp_path / "llama-server", tmp_path / "m.gguf"
        binary.touch(); model.touch()
        proc = _FakeProc()

        def popen(cmd, **kw):
            LlamaServerManager._epoch += 1   # the shutdown lands *inside* Popen
            return proc

        waited = []

        def ready(p, port, deadline, epoch=None):
            waited.append(1)
            return True

        with patch.object(LlamaServerManager, "_find_port", return_value=8179), \
             patch.object(LlamaServerManager, "_wait_ready", side_effect=ready), \
             patch.object(LlamaServerManager, "_serving_model", return_value=True), \
             patch("subprocess.Popen", side_effect=popen):
            with pytest.raises(LlamaServerError, match="shut down while starting"):
                LlamaServerManager.ensure(str(binary), str(model), 2048, 4, 99)

        # The load-bearing assertion. A post-READINESS epoch check also kills the
        # child eventually, so "it died" passes with or without the fix. What the
        # fix changes is WHEN: readiness can take the whole STARTUP_TIMEOUT while
        # a model loads, and for all of it shutdown() has already returned
        # satisfied. Quit calls os._exit() inside that window, and the child
        # outlives the app. So: we must never have started waiting at all.
        assert not waited, (
            "waited for readiness on a child we had already been told to drop")
        assert proc.terminated or proc.killed, "child survived a shutdown mid-spawn"
        assert LlamaServerManager._process is None
        assert LlamaServerManager._starting is None

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

    def test_an_unprovable_listener_is_refused_not_trusted(self, tmp_path):
        """None means /proc could not tell us, and that must FAIL CLOSED.

        _wait_ready only proves something answered /health on the port — our own
        child may have failed to bind and exited — so an unproven listener is
        exactly the one that must not receive a dictation. The cost is the
        resident server, not cleanup: _server_generate falls through to the CLI.
        """
        binary, model = tmp_path / "llama-server", tmp_path / "m.gguf"
        binary.touch(); model.touch()
        served = []
        with patch.object(LlamaServerManager, "_find_port", return_value=8179), \
             patch.object(LlamaServerManager, "_wait_ready", return_value=True), \
             patch.object(LlamaServerManager, "_owns_listener", return_value=None), \
             patch.object(LlamaServerManager, "_serving_model",
                          side_effect=lambda *a: served.append(a) or True), \
             patch("subprocess.Popen", return_value=_FakeProc()):
            with pytest.raises(LlamaServerError):
                LlamaServerManager.ensure(str(binary), str(model), 2048, 4, 99)
        assert LlamaServerManager._process is None
        assert served == [], "asked an unproven listener to identify itself"

    @pytest.mark.linux_only
    def test_a_wildcard_listener_is_visible_to_the_scan(self):
        """0.0.0.0 and :: answer connections to 127.0.0.1, but their hex address
        is all zeroes. Filtering the scan to loopback made a stranger holding the
        wildcard INVISIBLE to the very check meant to catch it."""
        import socket as _s
        srv = _s.socket(_s.AF_INET, _s.SOCK_STREAM)
        srv.setsockopt(_s.SOL_SOCKET, _s.SO_REUSEADDR, 1)
        srv.bind(("0.0.0.0", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        try:
            assert LlamaServerManager._listener_inodes(port), (
                "a wildcard listener was invisible to the ownership scan")
        finally:
            srv.close()

    @pytest.mark.linux_only
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

        def ready(p, port, deadline, epoch=None):
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

    def test_a_supersede_mid_load_stops_the_child_without_disabling(self, tmp_path):
        # A CPU/GPU switch lands while the model loads: the load stops at the
        # next /health poll, and the ladder does not run on into _disabled.
        binary, model = tmp_path / "llama-server", tmp_path / "m.gguf"
        binary.touch(); model.touch()
        spawned = []

        def popen(cmd, **_kw):
            spawned.append(_FakeProc())
            return spawned[-1]

        def health(*_a, **_kw):
            LlamaServerManager.supersede()
            raise OSError("still loading")

        with patch.object(LlamaServerManager, "_find_port", return_value=8179), \
             patch("wayfinder.core.llama_server.urlopen_loopback", side_effect=health), \
             patch("subprocess.Popen", side_effect=popen):
            with pytest.raises(LlamaServerError, match="shut down while starting"):
                LlamaServerManager.ensure(str(binary), str(model), 2048, 4, 99)
        assert len(spawned) == 1 and spawned[0].terminated
        assert LlamaServerManager._disabled is False
        assert LlamaServerManager._process is None

    def test_old_mode_teardown_keeps_a_server_started_after_the_switch(self, tmp_path):
        binary, model = tmp_path / "llama-server", tmp_path / "m.gguf"
        binary.touch(); model.touch()

        def start(ngl):
            proc = _FakeProc()
            with patch.object(LlamaServerManager, "_find_port", return_value=8179), \
                 patch.object(LlamaServerManager, "_wait_ready", return_value=True), \
                 patch.object(LlamaServerManager, "_serving_model", return_value=True), \
                 patch("subprocess.Popen", return_value=proc):
                LlamaServerManager.ensure(str(binary), str(model), 2048, 4, ngl)
            return proc

        old = start(99)
        switch = LlamaServerManager.supersede()
        LlamaServerManager.shutdown(before_epoch=switch)
        assert old.terminated and LlamaServerManager._process is None

        switch = LlamaServerManager.supersede()
        new = start(0)  # a dictation starts the new mode before the teardown runs
        LlamaServerManager.shutdown(before_epoch=switch)
        assert not new.terminated and LlamaServerManager._process is new


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
                          side_effect=lambda p, port, dl, epoch=None: (seen.append(dl), False)[1]), \
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
                          side_effect=lambda p, port, dl, epoch=None: (seen.append(dl), False)[1]), \
             patch("subprocess.Popen", return_value=_FakeProc()):
            with pytest.raises(LlamaServerError):
                LlamaServerManager.ensure(str(binary), str(model), 2048, 4, 99)
        assert seen[0] > _t.monotonic() + 60, "background warm-up should be unbounded"


class TestRepeatedFailurePolicy:
    def test_a_persistently_failing_server_is_eventually_restarted(self):
        import time as _time
        LlamaServerManager._consecutive_failures = 0
        failing = LlamaServerManager._process = _FakeProc()
        results = [LlamaServerManager.note_failure(failing)
                   for _ in range(LlamaServerManager.MAX_CONSECUTIVE_FAILURES)]
        # The restart runs on its own thread now, so a wedged stop cannot
        # spend the caller's cleanup budget — wait for it.
        deadline = _time.time() + 5
        while not failing.terminated and _time.time() < deadline:
            _time.sleep(0.01)
        assert results[-1] is True and not any(results[:-1])
        assert failing.terminated and LlamaServerManager._process is None

    def test_a_one_off_failure_does_not_thrash_the_model_load(self):
        LlamaServerManager._consecutive_failures = 0
        with patch.object(LlamaServerManager, "restart") as restart:
            assert LlamaServerManager.note_failure() is False
        restart.assert_not_called()

    def test_success_resets_the_counter(self):
        LlamaServerManager._consecutive_failures = 0
        with patch.object(LlamaServerManager, "restart") as restart:
            LlamaServerManager.note_failure()
            LlamaServerManager.note_success()
            for _ in range(LlamaServerManager.MAX_CONSECUTIVE_FAILURES - 1):
                assert LlamaServerManager.note_failure() is False
        restart.assert_not_called()


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


class TestOwnershipIsRecheckedPerRequest:
    """Startup-time proof cannot speak for a connection opened later.

    The verified child can exit after /props, and the completion request travels
    over a NEW connection — so a process binding the freed port in between would
    receive the prompt. Ownership is a property of the moment you use it.
    """

    def teardown_method(self):
        LlamaServerManager._process = None

    def test_a_dead_child_is_no_longer_ours(self):
        LlamaServerManager._process = _FakeProc(rc=0)   # exited
        with patch.object(LlamaServerManager, "_owns_listener", return_value=True):
            assert LlamaServerManager.still_ours(8179) is False

    def test_no_child_at_all_is_no_longer_ours(self):
        LlamaServerManager._process = None
        assert LlamaServerManager.still_ours(8179) is False

    def test_a_live_child_that_lost_the_port_is_no_longer_ours(self):
        LlamaServerManager._process = _FakeProc()
        with patch.object(LlamaServerManager, "_owns_listener", return_value=False):
            assert LlamaServerManager.still_ours(8179) is False

    def test_unprovable_is_not_ours_either(self):
        """None means /proc could not answer — same fail-closed rule as startup."""
        LlamaServerManager._process = _FakeProc()
        with patch.object(LlamaServerManager, "_owns_listener", return_value=None):
            assert LlamaServerManager.still_ours(8179) is False

    def test_a_live_child_holding_the_port_is_ours(self):
        LlamaServerManager._process = _FakeProc()
        with patch.object(LlamaServerManager, "_owns_listener", return_value=True):
            assert LlamaServerManager.still_ours(8179) is True

    # -- the wiring, not just the method: _server_generate must actually consult
    # still_ours before handing over the prompt. Every other test class mocks
    # still_ours through the autouse fixture, so only THIS (exempt) class can
    # prove the call site exists — the fourth review round found exactly that
    # gap.

    def _backend(self, tmp_path):
        from wayfinder.core.postprocessor import LlamaCppCliBackend
        (tmp_path / "llama-simple").touch()
        (tmp_path / "llama-server").touch()
        model = tmp_path / "m.gguf"; model.touch()
        return LlamaCppCliBackend(
            llama_binary=str(tmp_path / "llama-simple"),
            model_path=str(model), residency="auto")

    def test_server_generate_refuses_when_ownership_is_lost(self, tmp_path):
        b = self._backend(tmp_path)
        with patch.object(LlamaServerManager, "ensure", return_value=8179), \
             patch.object(LlamaServerManager, "still_ours", return_value=False), \
             patch.object(LlamaServerManager, "complete",
                          side_effect=AssertionError(
                              "prompt handed to an unverified listener")):
            assert b._server_generate("prompt", 64) is None

    def test_server_generate_proceeds_when_ownership_holds(self, tmp_path):
        b = self._backend(tmp_path)
        child = _FakeProc()
        with _ensure_serving(child), \
             patch.object(LlamaServerManager, "still_ours", return_value=True) as so, \
             patch.object(LlamaServerManager, "complete",
                          return_value={"content": "x", "stop_type": "eos"}) as complete:
            assert b._server_generate("prompt", 64) is not None
        # Ownership and credentials both belong to the child ensure() handed out.
        so.assert_called_once_with(8179, child)
        assert complete.call_args.kwargs["child"] is child


class TestSwitchCancellation:
    """Sol PR 2 r2: a start configured before a CPU/GPU switch must not load
    the old mode, and a cancelled start is not a server failure."""

    @staticmethod
    def _files(tmp_path):
        for name in ("llama-simple", "llama-server"):
            (tmp_path / name).touch()
        model = tmp_path / "m.gguf"
        model.touch()
        return str(tmp_path / "llama-server"), str(model)

    def _backend(self, tmp_path, ngl):
        from wayfinder.core.postprocessor import LlamaCppCliBackend
        self._files(tmp_path)
        return LlamaCppCliBackend(
            llama_binary=str(tmp_path / "llama-simple"),
            model_path=str(tmp_path / "m.gguf"), n_gpu_layers=ngl, residency="auto")

    def test_a_request_configured_before_a_switch_leaves_the_new_server_alone(self, tmp_path):
        binary, model = self._files(tmp_path)
        stale = LlamaServerManager.load_epoch()
        LlamaServerManager.supersede()
        new = _FakeProc()
        with patch.object(LlamaServerManager, "_find_port", return_value=8179), \
             patch.object(LlamaServerManager, "_wait_ready", return_value=True), \
             patch.object(LlamaServerManager, "_serving_model", return_value=True):
            with patch("subprocess.Popen", return_value=new):
                LlamaServerManager.ensure(binary, model, 2048, 4, 0,
                                          load_epoch=LlamaServerManager.load_epoch())
            with patch("subprocess.Popen") as popen:
                with pytest.raises(LlamaServerSuperseded):
                    LlamaServerManager.ensure(binary, model, 2048, 4, 99, load_epoch=stale)
        popen.assert_not_called()
        assert not new.terminated and LlamaServerManager._process is new

    def test_a_switch_during_our_own_stop_still_cancels_the_start(self, tmp_path):
        # _epoch is captured after our own stop; a switch landing inside that
        # stop moved it first, so only the switch epoch can still tell.
        binary, model = self._files(tmp_path)
        real_stop = LlamaServerManager._stop_locked.__func__

        def stop_then_switch(cls):
            real_stop(cls)
            LlamaServerManager.supersede()

        with patch.object(LlamaServerManager, "_stop_locked", classmethod(stop_then_switch)), \
             patch.object(LlamaServerManager, "_find_port", return_value=8179), \
             patch.object(LlamaServerManager, "_wait_ready", return_value=True), \
             patch.object(LlamaServerManager, "_serving_model", return_value=True), \
             patch("subprocess.Popen", return_value=_FakeProc()) as popen:
            with pytest.raises(LlamaServerSuperseded):
                LlamaServerManager.ensure(binary, model, 2048, 4, 99,
                                          load_epoch=LlamaServerManager.load_epoch())
        popen.assert_not_called()
        assert LlamaServerManager._process is None

    def test_cancelled_starts_never_restart_the_new_modes_server(self, tmp_path):
        spawned, stops = [], []
        real_shutdown = LlamaServerManager.shutdown.__func__

        def popen(cmd, **_kw):
            spawned.append(_FakeProc())
            return spawned[-1]

        def switch_mid_load(*_a, **_kw):
            LlamaServerManager.supersede()
            return False

        def recording_shutdown(cls, before_epoch=None):
            stops.append(before_epoch)
            real_shutdown(cls, before_epoch)

        real_restart = LlamaServerManager.restart.__func__

        def recording_restart(cls, failed):
            stops.append(failed)
            real_restart(cls, failed)

        cancellations = LlamaServerManager.MAX_CONSECUTIVE_FAILURES + 1
        with patch.object(LlamaServerManager, "_find_port", return_value=8179), \
             patch.object(LlamaServerManager, "_serving_model", return_value=True), \
             patch.object(LlamaServerManager, "shutdown", classmethod(recording_shutdown)), \
             patch.object(LlamaServerManager, "restart", classmethod(recording_restart)), \
             patch("subprocess.Popen", side_effect=popen):
            with patch.object(LlamaServerManager, "_wait_ready", side_effect=switch_mid_load):
                for _ in range(cancellations):
                    assert self._backend(tmp_path, -1)._server_generate("prompt", 8) is None
            assert len(spawned) == cancellations
            assert all(proc.terminated or proc.killed for proc in spawned)
            assert LlamaServerManager._consecutive_failures == 0
            assert LlamaServerManager._disabled is False
            with patch.object(LlamaServerManager, "_wait_ready", return_value=True), \
                 patch.object(LlamaServerManager, "complete",
                              return_value={"content": "x", "stop_type": "eos"}):
                assert self._backend(tmp_path, 0)._server_generate("prompt", 8) is not None
            for thread in threading.enumerate():
                if thread.name == "llama-server-failure-restart":
                    thread.join(5)
        new = spawned[-1]
        assert stops == [], "a cancelled start was counted as a failure and restarted"
        assert not new.terminated and not new.killed
        assert LlamaServerManager._process is new

    def test_a_dictation_configured_before_a_switch_starts_nothing(self, tmp_path):
        backend = self._backend(tmp_path, -1)
        LlamaServerManager.supersede()
        with patch.object(LlamaServerManager, "_find_port", return_value=8179), \
             patch("subprocess.Popen", return_value=_FakeProc()) as popen:
            assert backend._server_generate("prompt", 8) is None
        popen.assert_not_called()
        assert LlamaServerManager._consecutive_failures == 0

    def test_a_dictation_snapshot_taken_before_a_switch_starts_no_cleanup_server(
            self, tmp_path):
        # The cleanup runs after the transcription, from the snapshot taken
        # when the dictation began; GPU off during the transcription must not
        # let it load the GPU server under the new epoch.
        from wayfinder.core import postprocessor
        from wayfinder.core.llama_server import LOAD_EPOCH_KEY
        self._files(tmp_path)
        config = {"post_processing_backend": "llama_cpp", "llama_cpp_use_cli": True,
                  "llama_cpp_binary": str(tmp_path / "llama-simple"),
                  "llama_cpp_model_path": str(tmp_path / "m.gguf")}
        snapshot = {**config, LOAD_EPOCH_KEY: LlamaServerManager.load_epoch()}
        LlamaServerManager.supersede()
        backend = postprocessor.get_backend(snapshot)
        with patch.object(LlamaServerManager, "_find_port", return_value=8179), \
             patch("subprocess.Popen", return_value=_FakeProc()) as popen:
            assert backend._server_generate("prompt", 8) is None
        popen.assert_not_called()
        # Unstamped, a backend belongs to the epoch it was built under.
        assert (postprocessor.get_backend(config)._llama_load_epoch
                == LlamaServerManager.load_epoch())

    @staticmethod
    def _hold_the_lock():
        """The new mode's start, holding _lock through its readiness wait."""
        held, done = threading.Event(), threading.Event()

        def start():
            with LlamaServerManager._lock:
                held.set()
                done.wait(15)

        holder = threading.Thread(target=start, daemon=True)
        holder.start()
        assert held.wait(5)
        return done, holder

    @staticmethod
    def _record_stops():
        """Every stop a counted failure could queue: shutdown() or restart()."""
        stops = []
        return stops, patch.multiple(
            LlamaServerManager,
            shutdown=classmethod(lambda cls, before_epoch=None: stops.append(before_epoch)),
            restart=classmethod(lambda cls, failed: stops.append(failed)))

    def test_stale_requests_queued_behind_the_new_modes_start_are_not_failures(
            self, tmp_path):
        # Sol PR 2 r3: they timed out on the lock before any staleness check,
        # and three such timeouts restarted the new mode's server.
        binary, model = self._files(tmp_path)
        done, holder = self._hold_the_lock()
        stops, recording = self._record_stops()
        try:
            with recording, patch("subprocess.Popen") as popen:
                with pytest.raises(LlamaServerError) as current:
                    LlamaServerManager.ensure(binary, model, 2048, 4, 0,
                                              deadline=time.monotonic() + 0.3,
                                              load_epoch=LlamaServerManager.load_epoch())
                assert not isinstance(current.value, LlamaServerSuperseded)
                for _ in range(LlamaServerManager.MAX_CONSECUTIVE_FAILURES + 1):
                    backend = self._backend(tmp_path, -1)
                    LlamaServerManager.supersede()
                    assert backend._server_generate(
                        "prompt", 8, deadline=time.monotonic() + 0.3) is None
            popen.assert_not_called()
            assert stops == [] and LlamaServerManager._consecutive_failures == 0
        finally:
            done.set()
            holder.join(5)

    def test_a_switch_during_the_lock_wait_cancels_the_request_promptly(self, tmp_path):
        done, holder = self._hold_the_lock()
        switch = threading.Timer(0.2, LlamaServerManager.supersede)
        try:
            backend = self._backend(tmp_path, -1)
            started = time.monotonic()
            switch.start()
            with patch("subprocess.Popen") as popen:
                assert backend._server_generate("prompt", 8, deadline=started + 10) is None
            assert time.monotonic() - started < 3, "waited out the deadline after a switch"
            popen.assert_not_called()
            assert LlamaServerManager._consecutive_failures == 0
        finally:
            switch.cancel()
            done.set()
            holder.join(5)

    def test_a_lock_wait_that_runs_out_after_a_switch_is_cancelled_not_failed(
            self, tmp_path):
        binary, model = self._files(tmp_path)
        done, holder = self._hold_the_lock()
        switch = threading.Timer(0.1, LlamaServerManager.supersede)
        try:
            # One slice covers the whole wait, so the switch is only seen once
            # the acquisition has already failed.
            with patch.object(LlamaServerManager, "_LOCK_POLL_S", 10.0):
                switch.start()
                with pytest.raises(LlamaServerSuperseded):
                    LlamaServerManager.ensure(binary, model, 2048, 4, 99,
                                              deadline=time.monotonic() + 0.4,
                                              load_epoch=LlamaServerManager.load_epoch())
        finally:
            switch.cancel()
            done.set()
            holder.join(5)

    @pytest.mark.parametrize("failure", [TimeoutError("timed out"),
                                         ConnectionResetError("reset by peer")])
    def test_a_request_a_switch_cut_off_is_not_a_failure(self, tmp_path, failure):
        # The request started before the switch; the stop that came with the
        # switch is what failed it, so neither counting nor a restart applies.
        def cut_off(**_kw):
            LlamaServerManager.supersede()
            raise failure

        stops, recording = self._record_stops()
        with recording, \
             patch.object(LlamaServerManager, "ensure", return_value=8179), \
             patch.object(LlamaServerManager, "still_ours", return_value=True), \
             patch.object(LlamaServerManager, "complete", side_effect=cut_off):
            for _ in range(LlamaServerManager.MAX_CONSECUTIVE_FAILURES + 1):
                assert self._backend(tmp_path, -1)._server_generate("prompt", 8) is None
            for thread in threading.enumerate():
                if thread.name.startswith("llama-server-") and thread.name.endswith("restart"):
                    thread.join(5)
        assert stops == [] and LlamaServerManager._consecutive_failures == 0

    def test_a_stale_request_for_a_removed_model_is_cancelled_not_failed(self, tmp_path):
        # The user switched models and deleted the old one: the request read
        # before the switch must not count its missing file as a failure.
        binary, model = self._files(tmp_path)
        stale = LlamaServerManager.load_epoch()
        LlamaServerManager.supersede()
        Path(model).unlink()
        with pytest.raises(LlamaServerSuperseded):
            LlamaServerManager.ensure(binary, model, 2048, 4, 99, load_epoch=stale)

    def test_a_switch_during_the_last_rungs_reap_does_not_disable_the_server(self, tmp_path):
        # Sol PR 2 r4: the ladder ran out after the switch and still set
        # _disabled, so the new mode could never start its server.
        binary, model = self._files(tmp_path)

        class _SwitchDuringReap(_FakeProc):
            def kill(self):
                LlamaServerManager.supersede()
                super().kill()

        with patch.object(LlamaServerManager, "_find_port", return_value=8179), \
             patch.object(LlamaServerManager, "_spawn_attempts",
                          return_value=[[binary, "-ngl", "99"]]), \
             patch.object(LlamaServerManager, "_wait_ready", return_value=False), \
             patch("subprocess.Popen", return_value=_SwitchDuringReap()):
            with pytest.raises(LlamaServerSuperseded):
                LlamaServerManager.ensure(binary, model, 2048, 4, 99,
                                          load_epoch=LlamaServerManager.load_epoch())
        assert LlamaServerManager._disabled is False

    def test_a_switch_behind_spawn_errors_does_not_disable_the_server(self, tmp_path):
        binary, model = self._files(tmp_path)

        def switch_then_fail(*_a, **_kw):
            LlamaServerManager.supersede()
            raise OSError("exec format error")

        with patch.object(LlamaServerManager, "_find_port", return_value=8179), \
             patch.object(LlamaServerManager, "_spawn_attempts",
                          return_value=[[binary, "-ngl", "99"]]), \
             patch("subprocess.Popen", side_effect=switch_then_fail):
            with pytest.raises(LlamaServerSuperseded):
                LlamaServerManager.ensure(binary, model, 2048, 4, 99,
                                          load_epoch=LlamaServerManager.load_epoch())
        assert LlamaServerManager._disabled is False

    def test_a_failure_disables_only_the_mode_it_was_configured_in(self, tmp_path):
        binary, model = self._files(tmp_path)
        with patch.object(LlamaServerManager, "_find_port", return_value=8179), \
             patch.object(LlamaServerManager, "_wait_ready", return_value=False), \
             patch("subprocess.Popen", return_value=_FakeProc()):
            with pytest.raises(LlamaServerError):
                LlamaServerManager.ensure(binary, model, 2048, 4, 99,
                                          load_epoch=LlamaServerManager.load_epoch())
        assert LlamaServerManager._disabled is True
        with patch("subprocess.Popen") as popen:
            with pytest.raises(LlamaServerError):
                LlamaServerManager.ensure(binary, model, 2048, 4, 99,
                                          load_epoch=LlamaServerManager.load_epoch())
        popen.assert_not_called()   # the same mode stops paying for the start
        LlamaServerManager.supersede()   # GPU off, or another model
        with patch.object(LlamaServerManager, "_find_port", return_value=8179), \
             patch.object(LlamaServerManager, "_wait_ready", return_value=True), \
             patch.object(LlamaServerManager, "_serving_model", return_value=True), \
             patch("subprocess.Popen", return_value=_FakeProc()):
            assert LlamaServerManager.ensure(
                binary, model, 2048, 4, 0, load_epoch=LlamaServerManager.load_epoch()) == 8179

    @pytest.mark.parametrize("failure", [TimeoutError("timed out"),
                                         ConnectionResetError("reset by peer")])
    def test_a_queued_restart_spares_the_replacement_a_switch_started(self, tmp_path, failure):
        # Sol PR 2 r4: the restart worker ran after the switch's start had
        # published the new mode's server, and its unconditional stop killed it.
        old = LlamaServerManager._process = _FakeProc()
        done, holder = self._hold_the_lock()   # the switch's start, mid-load
        try:
            with _ensure_serving(old), \
                 patch.object(LlamaServerManager, "still_ours", return_value=True), \
                 patch.object(LlamaServerManager, "complete", side_effect=failure):
                for _ in range(LlamaServerManager.MAX_CONSECUTIVE_FAILURES):
                    assert self._backend(tmp_path, -1)._server_generate("prompt", 8) is None
            LlamaServerManager.supersede()
            new = LlamaServerManager._process = _FakeProc()
            # Still blocked on the lock: once it is free they finish at once.
            restarts = [t for t in threading.enumerate() if t.name.startswith(
                "llama-server-") and t.name.endswith("restart")]
        finally:
            done.set()
            holder.join(5)
        assert restarts, "the failures queued no restart"
        for thread in restarts:
            thread.join(10)
        assert not new.terminated and LlamaServerManager._process is new


class TestRestartsAreBoundToTheChildARequestReached:
    """A failure is pinned inside ensure(), under the lock: another thread
    replacing the child before the caller reads it must not redirect the stop."""

    def _files(self, tmp_path):
        return TestSwitchCancellation._files(tmp_path)

    def test_serving_is_the_child_this_threads_ensure_handed_out(self, tmp_path):
        binary, model = self._files(tmp_path)
        first = _FakeProc()
        with patch.object(LlamaServerManager, "_find_port", return_value=8179), \
             patch.object(LlamaServerManager, "_wait_ready", return_value=True), \
             patch.object(LlamaServerManager, "_serving_model", return_value=True), \
             patch("subprocess.Popen", return_value=first):
            LlamaServerManager.ensure(binary, model, 2048, 4, 0)
        LlamaServerManager._process = _FakeProc()   # another thread's respawn
        assert LlamaServerManager.serving() is first
        seen = []
        other = threading.Thread(target=lambda: seen.append(LlamaServerManager.serving()))
        other.start()
        other.join(5)
        assert seen == [None]   # a thread that reached no child pins nothing

    def test_a_reused_server_is_pinned_too(self, tmp_path):
        binary, model = self._files(tmp_path)
        live = _FakeProc()
        with patch.object(LlamaServerManager, "_find_port", return_value=8179), \
             patch.object(LlamaServerManager, "_wait_ready", return_value=True), \
             patch.object(LlamaServerManager, "_serving_model", return_value=True), \
             patch("subprocess.Popen", return_value=live):
            LlamaServerManager.ensure(binary, model, 2048, 4, 0)
            LlamaServerManager._served.proc = None
            LlamaServerManager.ensure(binary, model, 2048, 4, 0)   # reuse
        assert LlamaServerManager.serving() is live

    def test_a_failed_ensure_pins_nothing(self, tmp_path):
        binary, model = self._files(tmp_path)
        LlamaServerManager._served.proc = _FakeProc()   # an earlier request's
        with patch.object(LlamaServerManager, "_find_port", return_value=None):
            with pytest.raises(LlamaServerError):
                LlamaServerManager.ensure(binary, model, 2048, 4, 0)
        assert LlamaServerManager.serving() is None

    def test_a_failure_after_another_thread_respawned_spares_the_respawn(self, tmp_path):
        from wayfinder.core.postprocessor import LlamaCppCliBackend
        binary, model = self._files(tmp_path)
        backend = LlamaCppCliBackend(
            llama_binary=str(tmp_path / "llama-simple"), model_path=model,
            n_gpu_layers=0, residency="auto")
        reached = _FakeProc()
        respawn = _FakeProc()

        def ensure_then_respawn(*_a, **_kw):
            LlamaServerManager._served.proc = reached
            LlamaServerManager._process = respawn   # lands before serving() is read
            return 8179

        with patch.object(LlamaServerManager, "ensure", side_effect=ensure_then_respawn), \
             patch.object(LlamaServerManager, "still_ours", return_value=True), \
             patch.object(LlamaServerManager, "complete", side_effect=TimeoutError("t")):
            assert backend._server_generate("prompt", 8) is None
        for thread in [t for t in threading.enumerate() if t.name == "llama-server-restart"]:
            thread.join(10)
        assert not respawn.terminated and LlamaServerManager._process is respawn


class TestNoStartAfterQuit:
    """Sol PR 2 r5: a request that passed its checks just before Quit started
    a server once the shutdown was done, and os._exit() orphaned it."""

    def _files(self, tmp_path):
        return TestSwitchCancellation._files(tmp_path)

    def test_a_request_after_quit_starts_nothing(self, tmp_path):
        binary, model = self._files(tmp_path)
        LlamaServerManager.close()
        with patch("subprocess.Popen") as popen:
            with pytest.raises(LlamaServerSuperseded):
                LlamaServerManager.ensure(binary, model, 2048, 4, 0,
                                          load_epoch=LlamaServerManager.load_epoch())
        popen.assert_not_called()
        assert LlamaServerManager._disabled is False

    def test_a_request_waiting_for_the_lock_through_quit_starts_nothing(self, tmp_path):
        binary, model = self._files(tmp_path)
        outcome = {}

        def request():   # unstamped, so no switch check stops it
            try:
                outcome["port"] = LlamaServerManager.ensure(binary, model, 2048, 4, 0)
            except Exception as exc:  # recorded for the assertions
                outcome["error"] = exc

        with patch.object(LlamaServerManager, "_find_port", return_value=8179), \
             patch.object(LlamaServerManager, "_wait_ready", return_value=True), \
             patch.object(LlamaServerManager, "_serving_model", return_value=True), \
             patch("subprocess.Popen", return_value=_FakeProc()) as popen:
            with LlamaServerManager._lock:   # Quit's stop, holding the lock
                thread = threading.Thread(target=request, daemon=True)
                thread.start()
                time.sleep(0.3)   # past the entry checks, waiting for the lock
                LlamaServerManager._closed = True
                LlamaServerManager.supersede()
            thread.join(5)
        assert isinstance(outcome.get("error"), LlamaServerSuperseded)
        popen.assert_not_called()
        assert LlamaServerManager._process is None

    def test_a_quit_landing_during_the_old_servers_stop_starts_nothing(self, tmp_path):
        # Sol PR 2 r6: an unstamped request was stopping the old server when
        # close() gave up waiting for the lock. The epoch it captured after
        # the stop already included the Quit, so it spawned with _closed set.
        binary, model = self._files(tmp_path)

        class _QuitDuringStop(_FakeProc):
            def terminate(self):
                # close()'s own steps, after its lock wait ran out
                LlamaServerManager._closed = True
                LlamaServerManager.supersede()
                super().terminate()

        old = _QuitDuringStop()
        LlamaServerManager._process, LlamaServerManager._requested = old, ("another model",)
        with patch.object(LlamaServerManager, "_find_port", return_value=8179), \
             patch.object(LlamaServerManager, "_wait_ready", return_value=True), \
             patch.object(LlamaServerManager, "_serving_model", return_value=True), \
             patch("subprocess.Popen", return_value=_FakeProc()) as popen:
            with pytest.raises(LlamaServerSuperseded):
                LlamaServerManager.ensure(binary, model, 2048, 4, 0)   # unstamped
        popen.assert_not_called()
        assert old.terminated and LlamaServerManager._process is None
        assert LlamaServerManager._disabled is False

    def test_a_restart_that_did_not_exit_starts_again(self, tmp_path):
        binary, model = self._files(tmp_path)
        LlamaServerManager.close()
        LlamaServerManager.reopen()
        with patch.object(LlamaServerManager, "_find_port", return_value=8179), \
             patch.object(LlamaServerManager, "_wait_ready", return_value=True), \
             patch.object(LlamaServerManager, "_serving_model", return_value=True), \
             patch("subprocess.Popen", return_value=_FakeProc()):
            assert LlamaServerManager.ensure(
                binary, model, 2048, 4, 0, load_epoch=LlamaServerManager.load_epoch()) == 8179


class TestReuseTakesOwnership:
    """Sol PR 2 r5: a request that reuses the resident server owns it for its
    own switch; an earlier switch's delayed teardown must not stop it."""

    def _start(self, binary, model, ngl=0):
        with patch.object(LlamaServerManager, "_find_port", return_value=8179), \
             patch.object(LlamaServerManager, "_wait_ready", return_value=True), \
             patch.object(LlamaServerManager, "_serving_model", return_value=True), \
             patch("subprocess.Popen", side_effect=lambda *a, **kw: _FakeProc()) as popen:
            LlamaServerManager.ensure(binary, model, 2048, 4, ngl,
                                      load_epoch=LlamaServerManager.load_epoch())
        return popen.call_count

    def test_a_server_reused_after_quick_switches_survives_their_teardowns(self, tmp_path):
        # GPU on, off and on again (or Professional, Normal, Professional)
        # before either teardown ran.
        binary, model = TestSwitchCancellation._files(tmp_path)
        assert self._start(binary, model) == 1
        resident = LlamaServerManager._process
        off = LlamaServerManager.supersede()
        on = LlamaServerManager.supersede()
        assert self._start(binary, model) == 0   # reused, not respawned
        LlamaServerManager.shutdown(before_epoch=off)
        LlamaServerManager.shutdown(before_epoch=on)
        assert not resident.terminated and LlamaServerManager._process is resident
        # Not reused by the next mode, it goes with that mode's teardown.
        LlamaServerManager.shutdown(before_epoch=LlamaServerManager.supersede())
        assert resident.terminated and LlamaServerManager._process is None

    def test_an_unstamped_reuse_does_not_claim_a_switch(self, tmp_path):
        binary, model = TestSwitchCancellation._files(tmp_path)
        self._start(binary, model)
        resident = LlamaServerManager._process
        switch = LlamaServerManager.supersede()
        with patch("subprocess.Popen") as popen:
            LlamaServerManager.ensure(binary, model, 2048, 4, 0)   # no load epoch
        popen.assert_not_called()
        LlamaServerManager.shutdown(before_epoch=switch)
        assert resident.terminated


class TestARequestIsBoundToItsChild:
    """Sol PR 2 r5: a request reached child A, then an internal restart put B
    on the same port before the POST. The request went to B with B's key, and
    B's timeout was pinned to A, so the child that timed out stayed."""

    def _backend(self, tmp_path):
        return TestSwitchCancellation()._backend(tmp_path, 0)

    def test_the_request_carries_its_own_childs_key(self, tmp_path):
        import urllib.error
        reached, replacement = _FakeProc(), _FakeProc()
        LlamaServerManager._child_keys[reached] = "key-a"
        LlamaServerManager._child_keys[replacement] = "key-b"
        LlamaServerManager._process, LlamaServerManager._api_key = reached, "key-a"
        sent = []

        def replace_then_prove(port, child=None):
            # An internal restart lands between the ownership check and the POST.
            LlamaServerManager._process, LlamaServerManager._api_key = replacement, "key-b"
            return True

        def answer(req, timeout=None):
            sent.append(req.get_header("Authorization"))
            # B refuses a key that is not its own.
            raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)

        with _ensure_serving(reached), \
             patch.object(LlamaServerManager, "still_ours", side_effect=replace_then_prove), \
             patch("wayfinder.core.llama_server.urlopen_loopback", side_effect=answer):
            assert self._backend(tmp_path)._server_generate("prompt", 8) is None
        assert sent == ["Bearer key-a"]
        # The replacement's stop is what failed it: no failure is counted
        # toward a restart, and the replacement keeps running.
        assert LlamaServerManager._consecutive_failures == 0
        assert not replacement.terminated and LlamaServerManager._process is replacement

    def test_a_timeout_after_a_replacement_restarts_nothing(self, tmp_path):
        reached, replacement = _FakeProc(), _FakeProc()

        def replace_then_time_out(*_a, **_kw):
            LlamaServerManager._process = replacement
            raise TimeoutError("timed out")

        LlamaServerManager._process = reached
        with _ensure_serving(reached), \
             patch.object(LlamaServerManager, "still_ours", return_value=True), \
             patch.object(LlamaServerManager, "complete", side_effect=replace_then_time_out):
            assert self._backend(tmp_path)._server_generate("prompt", 8) is None
        assert not [t for t in threading.enumerate() if t.name == "llama-server-restart"]
        assert not replacement.terminated

    def test_ownership_is_proved_for_the_child_the_request_reached(self):
        reached, replacement = _FakeProc(), _FakeProc()
        LlamaServerManager._process = replacement   # on the same port
        with patch.object(LlamaServerManager, "_owns_listener", return_value=True):
            assert LlamaServerManager.still_ours(8179, reached) is False
            assert LlamaServerManager.still_ours(8179, replacement) is True

    def test_each_spawn_records_its_childs_key(self, tmp_path):
        binary, model = TestSwitchCancellation._files(tmp_path)
        child = _FakeProc()
        with patch.object(LlamaServerManager, "_find_port", return_value=8179), \
             patch.object(LlamaServerManager, "_wait_ready", return_value=True), \
             patch.object(LlamaServerManager, "_serving_model", return_value=True), \
             patch.object(LlamaServerManager, "_owns_listener", return_value=True), \
             patch.object(LlamaServerManager, "_spawn_env_overrides",
                          side_effect=lambda: setattr(LlamaServerManager, "_api_key", "k1") or {}), \
             patch("subprocess.Popen", return_value=child):
            LlamaServerManager.ensure(binary, model, 2048, 4, 0)
        assert LlamaServerManager._auth_headers(child) == {"Authorization": "Bearer k1"}
        assert LlamaServerManager._auth_headers(_FakeProc()) == {}


class TestAReusedChildStillRecovers:
    """Sol PR 2 r6: GPU off and on again while a request ran, and the new mode
    reused the child the request reached. That child stayed published, but its
    timeout was dropped as a switch cut-off, so the wedged server stayed."""

    def _backend(self, tmp_path):
        backend = TestSwitchCancellation()._backend(tmp_path, 0)
        backend._llama_load_epoch = LlamaServerManager.load_epoch()
        return backend

    @staticmethod
    def _join_restarts():
        for thread in threading.enumerate():
            if thread.name.startswith("llama-server-") and thread.name.endswith("restart"):
                thread.join(5)

    def test_a_timeout_on_a_reused_child_still_restarts_it(self, tmp_path):
        child = _FakeProc()
        LlamaServerManager._process = child
        backend = self._backend(tmp_path)

        def switches_then_time_out(*_a, **_kw):
            LlamaServerManager.supersede()   # GPU off...
            LlamaServerManager.supersede()   # ...and on: the new mode reused it
            raise TimeoutError("timed out")

        with _ensure_serving(child), \
             patch.object(LlamaServerManager, "complete", side_effect=switches_then_time_out):
            assert backend._server_generate("prompt", 8) is None
            self._join_restarts()
        assert child.terminated and LlamaServerManager._process is None

    def test_failures_of_a_reused_child_still_count(self, tmp_path):
        child = _FakeProc()
        LlamaServerManager._process = child
        backend = self._backend(tmp_path)

        def switch_then_fail(*_a, **_kw):
            LlamaServerManager.supersede()
            raise ConnectionResetError("reset by peer")

        with _ensure_serving(child), \
             patch.object(LlamaServerManager, "complete", side_effect=switch_then_fail):
            for _ in range(LlamaServerManager.MAX_CONSECUTIVE_FAILURES):
                assert backend._server_generate("prompt", 8) is None
            self._join_restarts()
        assert child.terminated and LlamaServerManager._process is None

    @pytest.mark.parametrize("failure", [TimeoutError("timed out"),
                                         ConnectionResetError("reset by peer")])
    def test_a_request_whose_child_the_switch_stopped_is_not_a_failure(self, tmp_path, failure):
        child, new = _FakeProc(), _FakeProc()
        LlamaServerManager._process = child
        backend = self._backend(tmp_path)

        def switch_stops_it(*_a, **_kw):
            LlamaServerManager.shutdown(before_epoch=LlamaServerManager.supersede())
            LlamaServerManager._process = new   # the new mode's server
            raise failure

        with _ensure_serving(child), \
             patch.object(LlamaServerManager, "complete", side_effect=switch_stops_it):
            assert backend._server_generate("prompt", 8) is None
            self._join_restarts()
        assert child.terminated
        assert not new.terminated and LlamaServerManager._process is new
        assert LlamaServerManager._consecutive_failures == 0

    def test_an_old_childs_failures_do_not_count_against_its_replacement(self):
        old, new = _FakeProc(), _FakeProc()
        for _ in range(LlamaServerManager.MAX_CONSECUTIVE_FAILURES - 1):
            assert LlamaServerManager.note_failure(old) is False
        LlamaServerManager._process = new
        assert LlamaServerManager.note_failure(new) is False
        assert LlamaServerManager._consecutive_failures == 1
