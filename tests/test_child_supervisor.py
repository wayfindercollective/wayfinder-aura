"""Parent-lifetime supervision for native macOS helper processes."""

from __future__ import annotations

import json
import os
import subprocess

from wayfinder.utils import child_supervisor


def test_non_macos_command_is_unchanged():
    command, env = child_supervisor.wrap_macos_child_command(
        ["whisper-server", "--port", "8178"],
        {"A": "1"},
        platform_name="linux",
    )
    assert command == ["whisper-server", "--port", "8178"]
    assert env == {"A": "1"}


def test_macos_source_command_uses_supervisor_and_records_owner(monkeypatch):
    monkeypatch.delattr(child_supervisor.sys, "frozen", raising=False)
    monkeypatch.setattr(child_supervisor.sys, "executable", "/python312")
    command, env = child_supervisor.wrap_macos_child_command(
        ["whisper-server", "--port", "8178"],
        {"A": "1"},
        platform_name="darwin",
        parent_pid=4242,
    )
    assert command[0] == "/python312"
    assert command[1].endswith("child_supervisor.py")
    assert json.loads(env[child_supervisor.COMMAND_ENV]) == [
        "whisper-server", "--port", "8178"
    ]
    assert env[child_supervisor.PARENT_PID_ENV] == "4242"


def test_supervisor_terminates_child_when_owner_dies(monkeypatch):
    class Proc:
        returncode = None

        def __init__(self):
            self.terminated = False

        def poll(self):
            return 0 if self.terminated else None

        def terminate(self):
            self.terminated = True
            self.returncode = -15

        def wait(self, timeout):
            assert timeout > 0
            return self.returncode

        def kill(self):
            raise AssertionError("graceful termination should have succeeded")

    proc = Proc()
    alive_checks = iter([True, False])
    monkeypatch.setattr(
        child_supervisor, "parent_process_alive", lambda _pid: next(alive_checks)
    )
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: proc)
    monkeypatch.setattr(child_supervisor.time, "sleep", lambda _seconds: None)

    assert child_supervisor.run_supervised_command(["helper"], 4242) == 0
    assert proc.terminated is True


def _state(pid: int) -> str:
    """ps state letter, or "" once the process is gone (zombies count as gone)."""
    out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout
    return "" if out.strip().startswith("Z") else out.strip()


def test_killing_the_supervisor_group_cannot_orphan_a_wedged_server(tmp_path):
    # Recovery stops the server with a short SIGTERM wait, then SIGKILL. A
    # SIGKILL to the supervisor alone left whisper-server running under
    # launchd, still holding the model and port 8178 (MacBook Air, 1.2.0).
    import sys
    import time

    pid_file = tmp_path / "server.pid"
    server = [sys.executable, "-c",
              "import os, signal, time\n"
              "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
              f"open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
              "time.sleep(60)\n"]
    command, env = child_supervisor.wrap_macos_child_command(server, dict(os.environ), platform_name="darwin")
    proc = subprocess.Popen(command, env=env,
                            **child_supervisor.supervised_popen_kwargs(platform_name="darwin"))
    deadline = time.monotonic() + 10
    while not (pid_file.exists() and pid_file.read_text()):
        assert time.monotonic() < deadline, "the supervised server never started"
        time.sleep(0.05)
    server_pid = int(pid_file.read_text())

    child_supervisor.stop_supervised(proc, term_wait=0.2, kill_wait=2, platform_name="darwin")

    assert proc.returncode is not None
    deadline = time.monotonic() + 3
    while _state(server_pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert _state(server_pid) == "", "the server outlived its supervisor"


def test_signal_supervised_never_targets_a_reaped_pid(monkeypatch):
    sent = []
    monkeypatch.setattr(child_supervisor.os, "killpg", lambda pid, sig: sent.append((pid, sig)))
    proc = subprocess.Popen(["true"])
    proc.wait()
    child_supervisor.signal_supervised(proc, kill=True, platform_name="darwin")
    assert sent == []


def test_orphaned_helpers_are_only_launchd_parented_aura_binaries():
    app = "/Applications/Wayfinder Aura.app/Contents/Frameworks/bin"
    moved = "/Users/p/Downloads/Wayfinder Aura 2.app/Contents/Frameworks/bin"
    rows = [
        "501 55437     1 /Applications/Wayfinder Aura.app/Contents/MacOS/Wayfinder Aura",
        f"501 55485     1 {app}/whisper-server -m /Users/p/models/ggml-large-v3-turbo-q5_0.bin --port 8178",
        f"501 55493 55492 {app}/whisper-server -m /Users/p/models/ggml-large-v3-turbo-q5_0.bin --port 8178",
        f"501 55500     1 {app}/whisper-coreml/whisper-server -m /x --port 50123",
        f"501 55501     1 {moved}/llama-server -m /Users/p/llm/gemma.gguf",
        f"501 55502     1 {app}/coreml-compile /tmp/a /tmp/b",
        f"502 55503     1 {app}/whisper-server -m /other/user",
        f"501 55504     1 /Users/p/dev/wayfinder-aura/bin/whisper-server -m /x",
        f"501 55505     1 {app}/whisper-server-helper-unrelated",
        "garbage",
    ]
    assert child_supervisor.orphaned_helper_pids(rows, 501) == [55485, 55500, 55501, 55502]


def test_reaping_is_macos_only():
    assert child_supervisor.reap_orphaned_helpers(platform_name="linux") == []


def test_a_child_outside_its_own_group_is_still_stopped():
    proc = subprocess.Popen(["sleep", "30"])  # no supervised_popen_kwargs
    child_supervisor.stop_supervised(proc, term_wait=2, platform_name="darwin")
    assert proc.returncode is not None
