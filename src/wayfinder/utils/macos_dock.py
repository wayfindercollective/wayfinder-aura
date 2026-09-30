"""One Dock icon for Wayfinder Aura on macOS.

Aura's helpers (the PyQt pill overlay, the child supervisor) run the same
executable as the app, so they share its Info.plist. With a plain Info.plist
every helper checked in with macOS as a regular app: the overlay put a
second "Wayfinder Aura" tile in the Dock for a moment on every start, and the
Dock kept it as a stale recent-apps tile. Qt hides the helper only after that
check-in, which is too late.

So the bundle declares LSUIElement (every process starts with no Dock icon)
and the main app asks for its Dock icon here, as soon as it knows it is the
one running instance. Tk would promote it anyway when it starts, but only
after Aura's imports (about a second in); this makes the icon appear at once.
"""

from __future__ import annotations

import ctypes
import sys

_APPLICATION_SERVICES = "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
_K_CURRENT_PROCESS = 2                         # <MacTypes.h> kCurrentProcess
_K_TRANSFORM_TO_FOREGROUND_APPLICATION = 1     # <Processes.h>


class _ProcessSerialNumber(ctypes.Structure):
    _fields_ = [("highLongOfPSN", ctypes.c_uint32), ("lowLongOfPSN", ctypes.c_uint32)]


def show_in_dock(platform_name: str | None = None, load_library=None) -> bool:
    """Give this process its Dock icon and menu bar. True when macOS agreed.

    Uses TransformProcessType rather than NSApplication so that no NSApp is
    created before Tk makes its own TKApplication (Tk needs that class).
    """
    if (platform_name or sys.platform) != "darwin":
        return False
    try:
        lib = (load_library or ctypes.cdll.LoadLibrary)(_APPLICATION_SERVICES)
        transform = lib.TransformProcessType
        transform.argtypes = [ctypes.POINTER(_ProcessSerialNumber), ctypes.c_uint32]
        transform.restype = ctypes.c_int32
        psn = _ProcessSerialNumber(0, _K_CURRENT_PROCESS)
        return transform(ctypes.byref(psn), _K_TRANSFORM_TO_FOREGROUND_APPLICATION) == 0
    except Exception as exc:
        print(f"[Dock] Could not show the Dock icon early ({exc}); Tk will.", flush=True)
        return False
