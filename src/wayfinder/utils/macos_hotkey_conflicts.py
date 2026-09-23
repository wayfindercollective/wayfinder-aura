"""Warn when a macOS record hotkey collides with a system or popular app shortcut.

Only reads preferences (com.apple.symbolichotkeys) and /Applications; never
changes anything. Returns one short, plain sentence for the Settings caption,
or None when the chord is clear. macOS-only; returns None elsewhere.
"""

from __future__ import annotations

import plistlib
import subprocess
import sys
from pathlib import Path

_SPACE = 57

# (modifiers, symbolichotkeys id or None, message). An id means "only when that
# system shortcut is enabled"; ids absent from the prefs use Apple's default.
_SYSTEM = (
    ({"super"}, 64, "⌘Space opens Spotlight."),
    ({"super", "alt"}, 65, "⌘⌥Space opens a Finder search window."),
    ({"ctrl"}, 60, "⌃Space switches input sources."),
    ({"ctrl", "alt"}, 61, "⌃⌥Space switches input sources."),
)
_DEFAULT_ENABLED = {64: True, 65: True, 60: True, 61: True}

_APPS_OPTION_SPACE = ("Raycast.app", "Alfred 5.app", "Alfred 4.app", "ChatGPT.app")


def _symbolic_hotkeys() -> dict:
    try:
        raw = subprocess.run(
            ["defaults", "export", "com.apple.symbolichotkeys", "-"],
            capture_output=True, timeout=2,
        ).stdout
        return plistlib.loads(raw).get("AppleSymbolicHotKeys", {}) if raw else {}
    except Exception:
        return {}


def _enabled(hotkeys: dict, ident: int) -> bool:
    entry = hotkeys.get(str(ident))
    if isinstance(entry, dict) and "enabled" in entry:
        return bool(entry["enabled"])
    return _DEFAULT_ENABLED.get(ident, False)


def _installed(names, roots=(Path("/Applications"), Path.home() / "Applications")) -> list[str]:
    found = []
    for name in names:
        if any((root / name).exists() for root in roots):
            found.append(name.removesuffix(".app").removesuffix(" 5").removesuffix(" 4"))
    return found


def conflict_for(code, modifiers, *, hotkeys: dict | None = None, installed=None) -> str | None:
    """A one-line warning for this chord on this Mac, or None."""
    if sys.platform != "darwin":
        return None
    mods = {str(m).lower() for m in (modifiers or ())}
    if "ctrl" in mods and "alt" in mods and code != _SPACE:
        return "⌃⌥ is VoiceOver's modifier; this may clash when VoiceOver is on."
    if code == _SPACE:
        table = _symbolic_hotkeys() if hotkeys is None else hotkeys
        for wanted, ident, message in _SYSTEM:
            if mods == wanted and _enabled(table, ident):
                return message + " Pick another key or turn that shortcut off in Keyboard Shortcuts."
        if mods == {"alt"}:
            apps = _installed(_APPS_OPTION_SPACE) if installed is None else installed
            if apps:
                return f"⌥Space is also {', '.join(apps)}'s default shortcut."
    if mods == {"fn"} or "fn" in mods:
        return ("Fn only works if your keyboard's Fn/Globe key reaches the Mac — "
                "many third-party keyboards don't send it. Right Option works on every keyboard.")
    return None
