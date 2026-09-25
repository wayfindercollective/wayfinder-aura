"""Gamer mode on Windows: the same feature as ``macos_game_chat``.

Everything platform-neutral is reused from the Mac module - the profiles and
their chat conventions, message splitting, "one message per dictation", the
vocabulary overlay, the Games tab list and verdicts - so both ports behave
alike. What differs on Windows:

* The game in front is found from the foreground window's process: its exe
  name (Wow.exe, ffxiv_dx11.exe, exefile.exe...); the window title only
  when the exe can't be read (the Mac matches bundle ids and app names).
* Paste and Enter are Ctrl+V / Enter as held scan codes
  (``injector_windows.hold_keys``), and in games Aura only ever pastes: no
  SendInput typing fallback, because letters are keybinds.
* Games-tab notes that describe the Mac (Cmd+V, Wine, the iPad Among Us) are
  replaced with their Windows facts.

Windows-only helpers return Nones elsewhere.
"""

from __future__ import annotations

import sys
from dataclasses import replace
from typing import Optional

from .macos_game_chat import (  # noqa: F401 - re-exported: same interface as the Mac module
    BETWEEN_MESSAGES_S,
    KEY_HOLD_S,
    MMO_VOCABULARY,
    NOT_RECOMMENDED,
    OPEN_SETTLE_S,
    PASTE_SETTLE_S,
    PLAYABLE,
    PROFILES,
    STATUS_LABELS,
    STATUS_MEANINGS,
    UNTESTED,
    VERIFIED,
    ChatResult,
    GameChatAborted,
    GameEntry,
    GameProfile,
    INFO_ONLY as _MAC_INFO_ONLY,
    gamer_asr_overlay,
    gamer_vocabulary,
    profile_entry,
    send_to_chat,
    split_for_chat,
)

# Lower-case exe name -> profile key.
EXE_PROFILES = {
    "wow.exe": "wow", "wowclassic.exe": "wow", "wowt.exe": "wow", "wowb.exe": "wow",
    "ffxiv_dx11.exe": "ffxiv", "ffxiv.exe": "ffxiv",
    "eso64.exe": "eso", "eso.exe": "eso",
    "lotroclient64.exe": "lotro", "lotroclient.exe": "lotro",
    "albion-online.exe": "albion",
    "pathofexile.exe": "poe", "pathofexile_x64.exe": "poe",
    "pathofexilesteam.exe": "poe", "pathofexile_x64steam.exe": "poe",
    "exefile.exe": "eve",
    "rs2client.exe": "jagex", "runelite.exe": "jagex", "osclient.exe": "jagex",
    "league of legends.exe": "lol",
    "blackdesert64.exe": "bdo", "blackdesert32.exe": "bdo",
}

_BY_KEY = {p.key: p for p in PROFILES}

# Games-tab notes whose Mac wording doesn't hold on Windows.
_WINDOWS_NOTES = {
    "Dota 2": (UNTESTED, "Enter opens chat; Aura pastes with Ctrl+V. Not tried yet."),
    "Guild Wars 2": (UNTESTED, "Enter opens chat; Aura pastes with Ctrl+V. Not tried yet."),
    "Cloud gaming (GeForce NOW, Xbox Cloud, Boosteroid)": (
        UNTESTED, "Aura can't tell which game is streaming; it pastes with Ctrl+V."),
    "Among Us": (UNTESTED, "Click the chat bubble first; free chat needs age 13+."),
    "Lost Ark": (NOT_RECOMMENDED,
                 "Its anti-cheat is strict about input tools; Aura still pastes, at your own risk."),
}

INFO_ONLY: tuple[GameEntry, ...] = tuple(
    replace(entry, status=_WINDOWS_NOTES[entry.name][0], note=_WINDOWS_NOTES[entry.name][1])
    if entry.name in _WINDOWS_NOTES else entry
    for entry in _MAC_INFO_ONLY
)


def game_list() -> list[GameEntry]:
    """Every game the Games tab shows (Windows notes), sorted by name."""
    entries = [profile_entry(p) for p in PROFILES] + list(INFO_ONLY)
    return sorted(entries, key=lambda e: e.name.lower())


def search_games(query: str) -> list[GameEntry]:
    """Case-insensitive match on the name or an alias; empty query = all."""
    q = " ".join((query or "").lower().split())
    if not q:
        return game_list()
    return [e for e in game_list()
            if q in e.name.lower() or any(q in a for a in e.aliases)]


def match_profile(exe_name: Optional[str], window_title: Optional[str]) -> Optional[GameProfile]:
    """The profile for the foreground program, or None when it isn't a supported game.

    The exe name decides. The window title is only a fallback when the exe
    can't be read at all: a browser tab titled "World of Warcraft guide"
    must never get Gamer mode's Enter presses.
    """
    exe = (exe_name or "").strip().lower()
    key = EXE_PROFILES.get(exe)
    if key and key in _BY_KEY:
        return _BY_KEY[key]
    if exe:
        return None
    title = (window_title or "").lower()
    if title:
        for profile in PROFILES:
            if any(marker in title for marker in profile.name_markers):
                return profile
    return None


_STORE_MARKERS = (
    ("\\steamapps\\common\\", "it is a Steam game"),
    ("\\epic games\\", "it is an Epic Games Store game"),
    ("\\gog galaxy\\games\\", "it is a GOG game"),
    ("\\xboxgames\\", "it is an Xbox app game"),
)
_CLOUD_EXES = ("geforcenow.exe", "boosteroid.exe", "shadow.exe")


def unlisted_game_reason(exe_name: Optional[str], window_title: Optional[str] = None,
                         category: Optional[str] = None,
                         exe_path: Optional[str] = None) -> Optional[str]:
    """Why a program with no profile looks like a game, or None if it doesn't."""
    if (exe_name or "").lower() in _CLOUD_EXES:
        return "it is a cloud-gaming app"
    path = (exe_path or "").lower()
    for marker, reason in _STORE_MARKERS:
        if marker in path:
            return reason
    return None


def _foreground():
    """(pid, exe path, window title) of the foreground window; Nones if unknown."""
    if sys.platform != "win32":
        return None, None, None
    try:
        import ctypes
        from ctypes import wintypes

        user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
        user32.GetForegroundWindow.restype = wintypes.HWND
        kernel32.OpenProcess.restype = wintypes.HANDLE
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None, None, None
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        length = user32.GetWindowTextLengthW(hwnd)
        title_buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, title_buf, length + 1)
        path = None
        process = kernel32.OpenProcess(0x1000, False, pid.value)  # QUERY_LIMITED_INFORMATION
        if process:
            try:
                buf = ctypes.create_unicode_buffer(1024)
                size = wintypes.DWORD(len(buf))
                if kernel32.QueryFullProcessImageNameW(process, 0, buf, ctypes.byref(size)):
                    path = buf.value
            finally:
                kernel32.CloseHandle(process)
        return int(pid.value) or None, path, title_buf.value or None
    except Exception:
        return None, None, None


def frontmost_app() -> tuple[Optional[int], Optional[str], Optional[str]]:
    """(pid, exe name, window title) of the foreground program: the Mac's
    (pid, bundle id, app name) triple, so the app code is shared."""
    pid, path, title = _foreground()
    exe = path.replace("/", "\\").rsplit("\\", 1)[-1] if path else None
    return pid, exe, title


def app_signals(pid: Optional[int]) -> tuple[Optional[str], Optional[str]]:
    """(category, exe path) for the foreground program - Windows has no app category."""
    fg_pid, path, _title = _foreground()
    if pid is None or fg_pid != pid:
        return None, None
    return None, path
