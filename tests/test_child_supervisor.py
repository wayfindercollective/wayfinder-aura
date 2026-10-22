"""Parent-lifetime supervision for native macOS helper processes."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

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


def test_a_dead_supervisors_group_names_only_its_helpers():
    app = "/Applications/Wayfinder Aura.app/Contents/Frameworks/bin"
    rows = [
        "  901   901 /Applications/Wayfinder Aura.app/Contents/MacOS/Wayfinder Aura",
        f"  950   900 {app}/whisper-server",
        f"  953   900 {app}/whisper-coreml/whisper-server",
        "  951   900 /usr/bin/caffeinate",
        f"  952   901 {app}/whisper-server",
        "garbage",
    ]
    assert child_supervisor.group_survivors(rows, 900) == [950, 953]
    # A live leader: the group id was reused, so nothing in it is ours.
    assert child_supervisor.group_survivors(rows + ["  900   900 /usr/bin/login"], 900) == []


def test_only_macos_looks_for_what_a_reaped_supervisor_left(monkeypatch):
    scanned = []
    monkeypatch.setattr(child_supervisor, "_stop_group_survivors",
                        lambda pgid, kill: scanned.append((pgid, kill)))
    proc = subprocess.Popen(["true"])
    proc.wait()
    child_supervisor.signal_supervised(proc, platform_name="linux")
    child_supervisor.stop_supervised(proc, platform_name="darwin")
    assert scanned == [(proc.pid, False)]


@pytest.mark.skipif(not sys.platform.startswith("linux"),
                    reason="names the helper through a symlink, which Linux's comm reports")
@pytest.mark.parametrize("ignores_term", [False, True])
def test_a_server_whose_supervisor_died_first_is_still_stopped(tmp_path, ignores_term):
    # Sol mac r5: the supervisor SIGKILLed while whisper-server lived on. Once
    # the supervisor was reaped, stop_supervised returned at once, so recovery
    # lost the server and Quit left it holding the model and port.
    import signal
    import time

    link = tmp_path / "whisper-server"
    link.symlink_to(sys.executable)
    pid_file = tmp_path / "server.pid"
    server = [str(link), "-c",
              "import os, signal, time\n"
              + ("signal.signal(signal.SIGTERM, signal.SIG_IGN)\n" if ignores_term else "")
              + f"open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
              "time.sleep(60)\n"]
    command, env = child_supervisor.wrap_macos_child_command(server, dict(os.environ), platform_name="darwin")
    proc = subprocess.Popen(command, env=env,
                            **child_supervisor.supervised_popen_kwargs(platform_name="darwin"))
    deadline = time.monotonic() + 10
    while not (pid_file.exists() and pid_file.read_text()):
        assert time.monotonic() < deadline, "the supervised server never started"
        time.sleep(0.05)
    server_pid = int(pid_file.read_text())
    os.kill(proc.pid, signal.SIGKILL)
    proc.wait()   # reaped: the supervisor's pid is free, its group lives on
    assert _state(server_pid), "the server died with its supervisor"

    child_supervisor.stop_supervised(proc, platform_name="darwin")

    deadline = time.monotonic() + 3
    while _state(server_pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert _state(server_pid) == "", "the server outlived its dead supervisor"
