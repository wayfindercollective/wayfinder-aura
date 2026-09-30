"""Tell Windows that Aura is doing user-initiated work (no idle sleep).

The Windows counterpart of ``macos_activity``: idle sleep mid-dictation loses
the recording, and a slept multi-GB model download restarts from zero. A power
request (``PowerCreateRequest`` + ``PowerSetRequest(PowerRequestSystemRequired)``)
holds off idle sleep for exactly as long as the work runs. Unlike
``SetThreadExecutionState`` it is a handle, not per-thread state, so ``begin``
and ``end`` may run on different threads. The display may still turn off;
Windows lists the request in ``powercfg /requests`` and drops it if the
process exits.

No-ops off Windows.
"""

from __future__ import annotations

import ctypes
import sys
import threading

_lock = threading.Lock()
_active: dict[str, int] = {}

_POWER_REQUEST_CONTEXT_VERSION = 0
_POWER_REQUEST_CONTEXT_SIMPLE_STRING = 0x1
_POWER_REQUEST_SYSTEM_REQUIRED = 1
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class _Detailed(ctypes.Structure):
    _fields_ = [
        ("LocalizedReasonModule", ctypes.c_void_p),
        ("LocalizedReasonId", ctypes.c_ulong),
        ("ReasonStringCount", ctypes.c_ulong),
        ("ReasonStrings", ctypes.c_void_p),
    ]


class _Reason(ctypes.Union):
    _fields_ = [("Detailed", _Detailed), ("SimpleReasonString", ctypes.c_wchar_p)]


class _ReasonContext(ctypes.Structure):
    _fields_ = [("Version", ctypes.c_ulong), ("Flags", ctypes.c_ulong), ("Reason", _Reason)]


def _kernel32():
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.PowerCreateRequest.argtypes = [ctypes.POINTER(_ReasonContext)]
    k32.PowerCreateRequest.restype = ctypes.c_void_p
    k32.PowerSetRequest.argtypes = [ctypes.c_void_p, ctypes.c_int]
    k32.PowerSetRequest.restype = ctypes.c_int
    k32.PowerClearRequest.argtypes = [ctypes.c_void_p, ctypes.c_int]
    k32.PowerClearRequest.restype = ctypes.c_int
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    k32.CloseHandle.restype = ctypes.c_int
    return k32


def begin(key: str, reason: str) -> bool:
    """Start (once) the named power request. True if one is now held."""
    if sys.platform != "win32":
        return False
    with _lock:
        if key in _active:
            return True
        try:
            k32 = _kernel32()
            ctx = _ReasonContext()
            ctx.Version = _POWER_REQUEST_CONTEXT_VERSION
            ctx.Flags = _POWER_REQUEST_CONTEXT_SIMPLE_STRING
            ctx.Reason.SimpleReasonString = f"Wayfinder Aura: {reason}"
            handle = k32.PowerCreateRequest(ctypes.byref(ctx))
            if not handle or handle == _INVALID_HANDLE_VALUE:
                return False
            if not k32.PowerSetRequest(handle, _POWER_REQUEST_SYSTEM_REQUIRED):
                k32.CloseHandle(handle)
                return False
        except Exception:
            return False
        _active[key] = handle
        return True


def end(key: str) -> None:
    if sys.platform != "win32":
        return
    with _lock:
        handle = _active.pop(key, None)
    if handle is not None:
        try:
            k32 = _kernel32()
            k32.PowerClearRequest(handle, _POWER_REQUEST_SYSTEM_REQUIRED)
            k32.CloseHandle(handle)
        except Exception:
            pass


def is_held(key: str) -> bool:
    with _lock:
        return key in _active
