"""macOS resident llama-server: kernel ownership proof, per-spawn key, env.

Linux keeps main's behaviour byte-for-byte (no key, no extra argv, inherited
environment); those assertions run on every platform by faking sys.platform.
"""
import json
import socket
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from wayfinder.core import llama_server as ls  # noqa: E402
from wayfinder.core.llama_server import LlamaServerManager  # noqa: E402
from wayfinder.core import postprocessor as pp  # noqa: E402
from wayfinder.utils import macos_procinfo  # noqa: E402

darwin_only = pytest.mark.skipif(sys.platform != "darwin", reason="libproc is macOS-only")


@pytest.fixture(autouse=True)
def _clean_state():
    LlamaServerManager.reset_for_tests()
    LlamaServerManager._api_key = None
    yield
    LlamaServerManager.reset_for_tests()
    LlamaServerManager._api_key = None


def _cmd(platform):
    with patch.object(ls.sys, "platform", platform):
        return LlamaServerManager._spawn_attempts(
            "/b/llama-server", "/m/x.gguf", 2048, 8, 99, 8179)


class TestSpawnShape:
    def test_macos_hides_the_slots_endpoint(self):
        assert all("--no-slots" in cmd for cmd in _cmd("darwin"))

    def test_linux_argv_is_unchanged(self):
        assert all("--no-slots" not in cmd for cmd in _cmd("linux"))

    def test_macos_env_carries_a_fresh_key_and_no_residency(self):
        with patch.object(ls.sys, "platform", "darwin"):
            first = LlamaServerManager._spawn_env_overrides()
            second = LlamaServerManager._spawn_env_overrides()
        assert first["GGML_METAL_NO_RESIDENCY"] == "1"
        assert "GGML_METAL_DEVICES" not in first, "Free's -ngl 0 keeps Metal prompt offload"
        assert len(first["LLAMA_API_KEY"]) >= 24
        assert first["LLAMA_API_KEY"] != second["LLAMA_API_KEY"]
        assert LlamaServerManager._api_key == second["LLAMA_API_KEY"]

    def test_the_key_never_appears_in_argv(self):
        with patch.object(ls.sys, "platform", "darwin"):
            env = LlamaServerManager._spawn_env_overrides()
            cmds = LlamaServerManager._spawn_attempts(
                "/b/llama-server", "/m/x.gguf", 2048, 8, 99, 8179)
        assert all(env["LLAMA_API_KEY"] not in " ".join(cmd) for cmd in cmds)

    def test_linux_inherits_the_environment_unchanged(self):
        with patch.object(ls.sys, "platform", "linux"):
            assert LlamaServerManager._spawn_env_overrides() is None
        assert LlamaServerManager._api_key is None


class TestAuthHeaders:
    def test_no_key_no_header(self):
        assert LlamaServerManager._auth_headers() == {}

    def test_completion_and_props_send_the_key(self):
        LlamaServerManager._api_key = "k123"
        seen = []

        class _Resp:
            def read(self):
                return json.dumps({"content": "ok", "model_path": "/m"}).encode()

        def fake_urlopen(req, timeout=None):
            seen.append(req)
            return _Resp()

        with patch.object(ls.urllib.request, "urlopen", fake_urlopen):
            LlamaServerManager.complete("p", 8, port=8179)
            LlamaServerManager._props(8179)
        assert [r.get_header("Authorization") for r in seen] == ["Bearer k123"] * 2


class TestMacOwnership:
    @darwin_only
    def test_libproc_sees_our_own_listener(self):
        import os
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        port = srv.getsockname()[1]
        try:
            assert macos_procinfo.pid_listens_on_tcp(os.getpid(), port) is False
            srv.listen(1)
            assert macos_procinfo.pid_listens_on_tcp(os.getpid(), port) is True
            assert macos_procinfo.pid_listens_on_tcp(os.getpid(), port + 1 if port < 65535 else port - 1) is False
        finally:
            srv.close()

    @darwin_only
    def test_child_owns_its_listener_and_a_stranger_does_not(self):
        child = subprocess.Popen(
            [sys.executable, "-c",
             "import socket,time;s=socket.socket();s.bind(('127.0.0.1',0));s.listen();"
             "print(s.getsockname()[1],flush=True);time.sleep(30)"],
            stdout=subprocess.PIPE, text=True)
        stranger = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)"])
        try:
            port = int(child.stdout.readline())
            assert LlamaServerManager._owns_listener(child, port) is True
            assert LlamaServerManager._owns_listener(stranger, port) is False
        finally:
            for proc in (child, stranger):
                proc.kill()
                proc.wait()

    @darwin_only
    def test_a_gone_process_is_not_proven(self):
        assert macos_procinfo.pid_listens_on_tcp(2 ** 22 + 12345, 8179) is None

    def test_off_macos_the_helper_never_answers(self):
        with patch.object(macos_procinfo.sys, "platform", "linux"):
            assert macos_procinfo.pid_listens_on_tcp(1, 8179) is None


class TestCleanupThreads:
    def setup_method(self):
        pp._MACOS_CLEANUP_THREADS = None

    def test_macos_default_follows_performance_cores(self):
        with patch.object(pp.sys, "platform", "darwin"), \
                patch("wayfinder.utils.gpu.get_optimal_thread_count", return_value=12):
            assert pp._cleanup_threads({"llama_cpp_n_threads": 4}) == 12

    def test_macos_never_goes_below_the_old_default(self):
        with patch.object(pp.sys, "platform", "darwin"), \
                patch("wayfinder.utils.gpu.get_optimal_thread_count", return_value=2):
            assert pp._cleanup_threads({}) == 4

    def test_an_explicit_value_is_kept(self):
        with patch.object(pp.sys, "platform", "darwin"):
            assert pp._cleanup_threads({"llama_cpp_n_threads": 6}) == 6

    def test_linux_keeps_the_configured_value(self):
        with patch.object(pp.sys, "platform", "linux"):
            assert pp._cleanup_threads({"llama_cpp_n_threads": 4}) == 4
            assert pp._cleanup_threads({}) == 4
