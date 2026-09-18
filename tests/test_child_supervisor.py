"""Parent-lifetime supervision for native macOS helper processes."""

from __future__ import annotations

import json
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
