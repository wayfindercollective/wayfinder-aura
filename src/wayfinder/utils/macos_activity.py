"""Tell macOS that Aura is doing user-initiated work (no idle sleep, no App Nap).

Idle system sleep during a long dictation used to cancel the recording (the
will-sleep handler must stop the mic), and a multi-GB model download died
when the Mac slept and restarted from zero. NSProcessInfo activities are the
supported way to hold that off for exactly as long as the work runs; they
show up in ``pmset -g assertions`` and end automatically if the app exits.

No-ops off macOS.
"""

from __future__ import annotations

import sys
import threading
from contextlib import contextmanager

_lock = threading.Lock()
_active: dict[str, object] = {}


def _process_info():
    from Foundation import NSProcessInfo

    return NSProcessInfo.processInfo()


def begin(key: str, reason: str) -> bool:
    """Start (once) the named activity. True if one is now held."""
    if sys.platform != "darwin":
        return False
    with _lock:
        if key in _active:
            return True
        try:
            from Foundation import NSActivityUserInitiated

            token = _process_info().beginActivityWithOptions_reason_(NSActivityUserInitiated, reason)
        except Exception:
            return False
        _active[key] = token
        return True


def end(key: str) -> None:
    if sys.platform != "darwin":
        return
    with _lock:
        token = _active.pop(key, None)
    if token is not None:
        try:
            _process_info().endActivity_(token)
        except Exception:
            pass


def is_held(key: str) -> bool:
    with _lock:
        return key in _active


@contextmanager
def held(key: str, reason: str):
    """``with held("download", "Downloading a model"): ...``"""
    began = begin(key, reason) and True
    try:
        yield began
    finally:
        if began:
            end(key)
