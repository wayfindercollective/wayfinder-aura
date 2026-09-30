"""Open Wayfinder Aura at login (macOS 13+), via SMAppService.

The login item is the signed app bundle itself (``mainAppService``): macOS
lists it in System Settings > General > Login Items, where the user can also
switch it off. Nothing is written to LaunchAgents. ServiceManagement is loaded
straight from the system framework through PyObjC, so no extra wrapper package
ships in the app.
"""

from __future__ import annotations

import subprocess
import sys

NOT_REGISTERED = 0
ENABLED = 1
REQUIRES_APPROVAL = 2
NOT_FOUND = 3

LOGIN_ITEMS_PANE = "x-apple.systempreferences:com.apple.LoginItems-Settings.extension"


def _service():
    import objc
    from Foundation import NSBundle

    NSBundle.bundleWithPath_("/System/Library/Frameworks/ServiceManagement.framework").load()
    for selector in (b"registerAndReturnError:", b"unregisterAndReturnError:"):
        objc.registerMetaDataForSelector(
            b"SMAppService", selector, {"arguments": {2: {"type_modifier": objc._C_OUT}}})
    return objc.lookUpClass("SMAppService").mainAppService()


def available() -> bool:
    """Only a real .app bundle can be a login item (not a source run)."""
    if sys.platform != "darwin":
        return False
    try:
        from Foundation import NSBundle

        return str(NSBundle.mainBundle().bundlePath()).endswith(".app")
    except Exception:
        return False


def status() -> int | None:
    if not available():
        return None
    try:
        return int(_service().status())
    except Exception:
        return None


def is_enabled() -> bool:
    return status() == ENABLED


def set_enabled(enabled: bool) -> tuple[bool, str | None]:
    """Register/unregister. Returns (ok, message for the user or None)."""
    if not available():
        return False, "Available in the installed app."
    try:
        svc = _service()
        if enabled:
            ok, error = svc.registerAndReturnError_(None)
        else:
            ok, error = svc.unregisterAndReturnError_(None)
    except Exception as exc:
        return False, f"Couldn't change the login item: {exc}"
    current = status()
    if enabled and current == REQUIRES_APPROVAL:
        return True, "Approve Wayfinder Aura in System Settings ▸ Login Items."
    if not ok:
        detail = str(error.localizedDescription()) if error is not None else "unknown error"
        return False, f"macOS refused: {detail}"
    return True, None


def open_login_items_settings() -> bool:
    try:
        return subprocess.run(["open", LOGIN_ITEMS_PANE], capture_output=True,
                              timeout=5).returncode == 0
    except Exception:
        return False
