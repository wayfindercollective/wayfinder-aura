"""Keep long-lived native children bound to the macOS app lifetime.

Linux uses ``PR_SET_PDEATHSIG`` for its overlay. macOS has no equivalent, so a
small Python supervisor watches the owning PID and terminates the native child
if the app crashes. Normal shutdown signals are forwarded through the same path.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Mapping, Sequence


COMMAND_ENV = "WAYFINDER_SUPERVISED_COMMAND"
PARENT_PID_ENV = "WAYFINDER_SUPERVISOR_PARENT_PID"


def parent_process_alive(pid: int) -> bool:
    """Return whether *pid* still exists (permission denied still means alive)."""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def wrap_macos_child_command(
    command: Sequence[str],
    env: Mapping[str, str],
    *,
    platform_name: str | None = None,
    parent_pid: int | None = None,
) -> tuple[list[str], dict[str, str]]:
    """Wrap a native child so it is reaped if the macOS owner disappears."""
    active_platform = platform_name or sys.platform
    child_env = dict(env)
    original = [str(part) for part in command]
    if active_platform != "darwin":
        return original, child_env

    child_env[COMMAND_ENV] = json.dumps(original)
    child_env[PARENT_PID_ENV] = str(parent_pid or os.getpid())
    if getattr(sys, "frozen", False):
        supervisor = [sys.executable, "--child-supervisor"]
    else:
        supervisor = [sys.executable, str(Path(__file__).resolve())]
    return supervisor, child_env


def _terminate_child(proc: subprocess.Popen, timeout: float = 3.0) -> None:
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            pass
    except (OSError, ProcessLookupError):
        pass


def run_supervised_command(
    command: Sequence[str],
    parent_pid: int,
    *,
    poll_interval: float = 0.2,
) -> int:
    """Run *command* until it exits or its owning app process disappears."""
    if parent_pid <= 1 or not parent_process_alive(parent_pid):
        return 0

    stop = threading.Event()

    def request_stop(_signum, _frame):
        stop.set()

    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, request_stop)

    child_env = os.environ.copy()
    child_env.pop(COMMAND_ENV, None)
    child_env.pop(PARENT_PID_ENV, None)
    proc = subprocess.Popen([str(part) for part in command], env=child_env)
    while proc.poll() is None:
        if stop.is_set() or not parent_process_alive(parent_pid):
            _terminate_child(proc)
            return 0
        time.sleep(poll_interval)
    return int(proc.returncode or 0)


def run_from_env() -> int:
    """Supervisor entry point used by source and frozen application processes."""
    try:
        command = json.loads(os.environ[COMMAND_ENV])
        parent_pid = int(os.environ[PARENT_PID_ENV])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(f"Invalid child-supervisor request: {exc}", file=sys.stderr, flush=True)
        return 2
    if not isinstance(command, list) or not command or not all(
        isinstance(part, str) and part for part in command
    ):
        print("Invalid child-supervisor command", file=sys.stderr, flush=True)
        return 2
    return run_supervised_command(command, parent_pid)


if __name__ == "__main__":
    raise SystemExit(run_from_env())
