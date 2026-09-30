"""Open Wayfinder Aura at login on Windows (the per-user Run key).

The Windows counterpart of ``macos_login_item``, with the same contract. The
login item is the installed app's own exe under
``HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run``: no admin rights,
per user, and Windows lists it in Settings > Apps > Startup and Task Manager's
Startup apps tab, where the user can also switch it off. Switching it off
there only writes a "disabled" marker under ``StartupApproved\\Run``, so that
marker counts as off here, and turning Aura's toggle back on clears it. The
installer removes the value on uninstall.
"""

from __future__ import annotations

import subprocess
import sys

VALUE_NAME = "Wayfinder Aura"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APPROVED_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"
STARTUP_SETTINGS = "ms-settings:startupapps"


def _command() -> str:
    return f'"{sys.executable}"'


def available() -> bool:
    """Only the installed (frozen) app can be a login item, not a source run."""
    return sys.platform == "win32" and bool(getattr(sys, "frozen", False))


def _read(key_path: str):
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            return winreg.QueryValueEx(key, VALUE_NAME)[0]
    except OSError:
        return None


def _disabled_in_task_manager() -> bool:
    data = _read(APPROVED_KEY)
    # First byte 2 (or 6) = enabled, 3 (or 7) = switched off by the user.
    return isinstance(data, (bytes, bytearray)) and len(data) > 0 and data[0] & 0x1 == 1


def is_enabled() -> bool:
    if not available():
        return False
    try:
        value = _read(RUN_KEY)
        if not isinstance(value, str) or not value.strip():
            return False
        if value.strip().strip('"').lower() != sys.executable.lower():
            return False  # another copy's entry (e.g. an older install path)
        return not _disabled_in_task_manager()
    except Exception:
        return False


def set_enabled(enabled: bool) -> tuple[bool, str | None]:
    """Add/remove the Run value. Returns (ok, message for the user or None)."""
    if not available():
        return False, "Available in the installed app."
    try:
        import winreg

        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            if enabled:
                winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, _command())
            else:
                try:
                    winreg.DeleteValue(key, VALUE_NAME)
                except FileNotFoundError:
                    pass
        # A Task Manager "disabled" marker would keep the new entry off.
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, APPROVED_KEY, 0,
                                winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, VALUE_NAME)
        except OSError:
            pass
    except Exception as exc:
        return False, f"Couldn't change the startup entry: {exc}"
    return True, None


def open_login_items_settings() -> bool:
    try:
        subprocess.Popen(["explorer.exe", STARTUP_SETTINGS])
        return True
    except Exception:
        return False
