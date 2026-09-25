"""Is Windows blocking the microphone for desktop apps?

The counterpart of macOS's microphone authorization check: a blocked mic
records pure digital silence, which otherwise reads as "check your mute
switch". Windows keeps its three switches (Settings > Privacy & security >
Microphone) in the CapabilityAccessManager consent store:

* HKLM ...\\microphone "Value" = Deny: "Microphone access" is off for the
  whole device (an administrator setting).
* HKCU ...\\microphone "Value" = Deny: "Let apps access your microphone" off.
* HKCU ...\\microphone\\NonPackaged "Value" = Deny: "Let desktop apps access
  your microphone" off - the one that applies to Aura.

Read-only. Windows-only; None elsewhere.
"""

from __future__ import annotations

import subprocess
import sys

_KEY = r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone"
PRIVACY_MICROPHONE = "ms-settings:privacy-microphone"

DEVICE_OFF = "device"
APPS_OFF = "apps"
DESKTOP_APPS_OFF = "desktop_apps"


def _value(root, path: str) -> str | None:
    import winreg

    try:
        with winreg.OpenKey(root, path) as key:
            return str(winreg.QueryValueEx(key, "Value")[0])
    except OSError:
        return None


def microphone_block() -> str | None:
    """Which Windows switch blocks the mic for Aura, or None if none does."""
    if sys.platform != "win32":
        return None
    try:
        import winreg

        if _value(winreg.HKEY_LOCAL_MACHINE, _KEY) == "Deny":
            return DEVICE_OFF
        if _value(winreg.HKEY_CURRENT_USER, _KEY) == "Deny":
            return APPS_OFF
        if _value(winreg.HKEY_CURRENT_USER, _KEY + r"\NonPackaged") == "Deny":
            return DESKTOP_APPS_OFF
    except Exception:
        return None
    return None


def blocked_message(block: str | None) -> str | None:
    """User-facing sentence for *block* (None when nothing blocks the mic)."""
    if block == DEVICE_OFF:
        return ("Windows has microphone access turned off for this PC — turn on "
                "Microphone access in Settings → Privacy & security → Microphone "
                "(it may need an administrator), then try again")
    if block == APPS_OFF:
        return ("Windows is blocking the microphone — turn on “Let apps access your "
                "microphone” in Settings → Privacy & security → Microphone, then try again")
    if block == DESKTOP_APPS_OFF:
        return ("Windows is blocking the microphone for desktop apps — turn on “Let "
                "desktop apps access your microphone” in Settings → Privacy & security → "
                "Microphone, then try again")
    return None


def open_microphone_settings() -> bool:
    try:
        subprocess.Popen(["explorer.exe", PRIVACY_MICROPHONE])
        return True
    except Exception:
        return False
