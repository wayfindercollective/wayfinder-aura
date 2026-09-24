"""Keep long-lived native children bound to the macOS app lifetime.

Linux uses ``PR_SET_PDEATHSIG`` for its overlay. macOS has no equivalent, so a
small Python supervisor watches the owning PID and terminates the native child
if the app crashes. Normal shutdown signals are forwarded through the same path.

Windows needs no supervisor process: ``bind_to_app_lifetime`` puts the child in
a job object that the kernel closes when the app exits for any reason, crash
included, killing the child with it.
"""

from __future__ import annotations

import ctypes
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


_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_windows_job = None  # the handle stays open for the app's lifetime on purpose
_windows_job_lock = threading.Lock()


class _IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
    )]


class _BasicLimits(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimits),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def _kill_on_close_job():
    """This process's kill-on-close job object (created once), or None."""
    global _windows_job
    with _windows_job_lock:
        if _windows_job is not None:
            return _windows_job
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        k32.CreateJobObjectW.restype = ctypes.c_void_p
        k32.SetInformationJobObject.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
        k32.SetInformationJobObject.restype = ctypes.c_int
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        job = k32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = _ExtendedLimits()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k32.SetInformationJobObject(
            job, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(info), ctypes.sizeof(info),
        ):
            k32.CloseHandle(job)
            return None
        _windows_job = job
        return job


def bind_to_app_lifetime(proc: subprocess.Popen, *, platform_name: str | None = None) -> bool:
    """Windows: make *proc* die with this app, even if the app crashes.

    The child joins a job object flagged KILL_ON_JOB_CLOSE whose only handle
    this process holds; when the process ends, Windows closes the handle and
    terminates every process in the job. Best effort: False (child unbound,
    as before) if Windows refuses. No-op elsewhere.
    """
    if (platform_name or sys.platform) != "win32":
        return False
    try:
        job = _kill_on_close_job()
        handle = getattr(proc, "_handle", None)
        if job is None or handle is None:
            return False
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        k32.AssignProcessToJobObject.restype = ctypes.c_int
        return bool(k32.AssignProcessToJobObject(job, int(handle)))
    except Exception:
        return False


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
