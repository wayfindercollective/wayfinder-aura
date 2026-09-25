"""Windows job object that ties Aura's native children to the app's lifetime.

Used through ``child_supervisor.bind_to_app_lifetime`` (Windows only): the
whisper-server and the overlay join a KILL_ON_JOB_CLOSE job whose only handle
Aura holds, so they end with the app even when it crashes (Linux uses
PR_SET_PDEATHSIG, macOS a supervisor process).
"""

from __future__ import annotations

import ctypes
import subprocess
import threading


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


def assign_to_kill_on_close_job(proc: subprocess.Popen) -> bool:
    """Put *proc* in this app's kill-on-close job object. True on success.

    The child joins a job object flagged KILL_ON_JOB_CLOSE whose only handle
    this process holds; when the process ends, Windows closes the handle and
    terminates every process in the job. Best effort: False (child unbound,
    as before) if Windows refuses.
    """
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
