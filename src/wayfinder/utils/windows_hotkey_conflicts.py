"""Warn when a Windows record hotkey collides with a system or popular app shortcut.

The Windows counterpart of ``macos_hotkey_conflicts``: read-only (it only
checks which apps are installed), one short plain sentence for the Settings
caption, or None when the chord is clear. Windows-only; None elsewhere.

Codes are the evdev codes the app stores for every platform.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_SPACE = 57
_ENTER = 28
# evdev F1..F12
_F = {59: 1, 60: 2, 61: 3, 62: 4, 63: 5, 64: 6, 65: 7, 66: 8, 67: 9, 68: 10, 87: 11, 88: 12}

# Bare function keys most apps already use. Aura's hotkey does not swallow
# the key on Windows, so the app in front still receives it.
_F_KEY_USES = {
    1: "opens Help",
    3: "opens Find",
    5: "refreshes",
    6: "moves to the address bar",
    7: "turns on caret browsing",
    10: "opens the menu bar",
    11: "toggles full screen",
    12: "opens developer tools or Save As",
}

_SYSTEM = (
    ({"alt"}, _SPACE, "Alt+Space opens the window menu (and PowerToys Run)."),
    ({"ctrl"}, _SPACE, "Ctrl+Space switches the input method for Chinese/Japanese/Korean "
                       "keyboards and triggers suggestions in many editors."),
    ({"shift"}, _SPACE, "Shift+Space switches half/full-width input on East Asian keyboards."),
    ({"alt"}, _ENTER, "Alt+Enter toggles full screen in games and opens Properties in Explorer."),
    ({"ctrl"}, _ENTER, "Ctrl+Enter sends the message in many chat and mail apps."),
)


def _local_appdata() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))


def _app_roots() -> list[Path]:
    roots = [_local_appdata(), _local_appdata() / "Programs"]
    for var in ("ProgramFiles", "ProgramFiles(x86)"):
        if os.environ.get(var):
            roots.append(Path(os.environ[var]))
    return roots


# App -> (folder names that mean it is installed, its default chord, what it does)
_APPS = (
    ("1Password", ("1Password",), ({"ctrl", "shift"}, _SPACE), "Quick Access"),
    ("PowerToys", ("PowerToys",), ({"alt"}, _SPACE), "PowerToys Run"),
)


def _installed_apps(roots=None) -> set[str]:
    roots = _app_roots() if roots is None else roots
    found = set()
    for name, folders, _chord, _what in _APPS:
        if any((root / folder).exists() for root in roots for folder in folders):
            found.add(name)
    return found


def conflict_for(code, modifiers, *, installed=None) -> str | None:
    """A one-line warning for this chord on this PC, or None."""
    if sys.platform != "win32":
        return None
    mods = {str(m).lower() for m in (modifiers or ())}
    for wanted, key, message in _SYSTEM:
        if mods == wanted and code == key:
            return message + " Pick another key."
    apps = _installed_apps() if installed is None else set(installed)
    for name, _folders, (wanted, key), what in _APPS:
        if name in apps and mods == wanted and code == key and name != "PowerToys":
            return f"{'+'.join(m.capitalize() for m in sorted(wanted))}+Space is {name}'s {what} shortcut."
    if not mods and code in _F and _F[code] in _F_KEY_USES:
        return (f"F{_F[code]} alone also reaches the app in front (it {_F_KEY_USES[_F[code]]}). "
                "Add Ctrl+Alt, or pick a key nothing uses.")
    return None
