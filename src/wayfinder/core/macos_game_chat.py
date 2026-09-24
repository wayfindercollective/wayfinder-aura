"""Game chat on macOS: dictate straight into an MMO's chat box.

World of Warcraft first. In WoW the chat box opens with Return, holds 255
characters, and sends with Return. So when WoW is in front at paste time,
Aura opens chat, pastes, and sends, splitting longer dictation into several
messages at sentence (then word) breaks.

Safety rules:

* Never type keystrokes into a game. There, letters are keybinds (W walks,
  1 casts); a paste that misses a chat box does nothing. Every message goes
  in with Cmd+V.
* Return is only pressed while the game that was matched is still the
  frontmost app, so an alt-tab mid-sequence cannot send Return elsewhere.
* Modifiers are released first (the paste and Return helpers wait): Option +
  Return toggles WoW's window mode, and hold-to-talk holds Option.

Profiles other than WoW use the games' known chat conventions but have not
been tested in the game (``verified=False``); only WoW's were designed
against the game's documented chat behaviour.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Callable, Optional


@dataclass(frozen=True)
class GameProfile:
    key: str
    name: str
    # Matched against the frontmost app's bundle identifier (prefix) and its
    # display name (case-insensitive substring).
    bundle_prefixes: tuple[str, ...] = ()
    name_markers: tuple[str, ...] = ()
    open_chat: bool = True     # Return opens the chat box before the paste
    max_chars: int = 200       # per message; longer dictation is split
    verified: bool = False     # checked in the game itself


PROFILES: tuple[GameProfile, ...] = (
    # Retail, Classic and Classic Era all ship as "World of Warcraft*.app".
    GameProfile(
        "wow", "World of Warcraft",
        bundle_prefixes=("com.blizzard.worldofwarcraft",),
        name_markers=("world of warcraft",),
        open_chat=True, max_chars=255,
    ),
    GameProfile("ffxiv", "Final Fantasy XIV", name_markers=("final fantasy xiv",)),
    GameProfile("eso", "The Elder Scrolls Online", name_markers=("elder scrolls online",)),
    GameProfile("lotro", "The Lord of the Rings Online", name_markers=("lord of the rings online",)),
    GameProfile("albion", "Albion Online", name_markers=("albion online",)),
    # Chat input is always live (typing goes straight into chat), so no Return first.
    GameProfile("runescape", "RuneScape", name_markers=("runescape", "runelite"), open_chat=False),
    GameProfile("eve", "EVE Online", name_markers=("eve online",), open_chat=False),
)

# Seconds. The chat box needs a frame or two to open and to read the paste;
# MMOs also throttle fast bursts of messages.
OPEN_SETTLE_S = 0.15
PASTE_SETTLE_S = 0.2
BETWEEN_MESSAGES_S = 0.45


def match_profile(bundle_id: Optional[str], app_name: Optional[str]) -> Optional[GameProfile]:
    """The profile for an app, or None when it isn't a supported game."""
    bundle = (bundle_id or "").lower()
    name = (app_name or "").lower()
    for profile in PROFILES:
        if bundle and any(bundle.startswith(p) for p in profile.bundle_prefixes):
            return profile
        if name and any(marker in name for marker in profile.name_markers):
            return profile
    return None


def frontmost_app() -> tuple[Optional[int], Optional[str], Optional[str]]:
    """(pid, bundle id, name) of the frontmost app; Nones when unknown.

    NSWorkspace/NSRunningApplication are thread-safe, so the paste thread may
    call this (unlike Text Input Sources; see macos_paste).
    """
    try:
        from AppKit import NSWorkspace

        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        if app is None:
            return None, None, None
        return (
            int(app.processIdentifier()),
            str(app.bundleIdentifier() or "") or None,
            str(app.localizedName() or "") or None,
        )
    except Exception:
        return None, None, None


def split_for_chat(text: str, limit: int) -> list[str]:
    """Split ``text`` into messages of at most ``limit`` characters.

    Whole sentences are kept together where they fit, then whole words; only
    a single word longer than the limit is cut.
    """
    text = " ".join((text or "").split())
    if not text:
        return []
    if len(text) <= limit:
        return [text]
    pieces: list[str] = []
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        if len(sentence) <= limit:
            pieces.append(sentence)
            continue
        for word in sentence.split(" "):
            while len(word) > limit:
                pieces.append(word[:limit])
                word = word[limit:]
            if word:
                pieces.append(word)
    messages: list[str] = []
    current = ""
    for piece in pieces:
        candidate = f"{current} {piece}" if current else piece
        if len(candidate) <= limit:
            current = candidate
        else:
            messages.append(current)
            current = piece
    if current:
        messages.append(current)
    return messages


class GameChatAborted(Exception):
    """The game left the foreground (or a newer dictation began) mid-send."""


def send_to_chat(
    text: str,
    profile: GameProfile,
    *,
    game_pid: Optional[int],
    send: bool = True,
    paste: Callable[[str], None],
    press_return: Callable[[], None],
    frontmost_pid: Callable[[], Optional[int]],
    still_current: Callable[[], bool] = lambda: True,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Open chat, paste and (optionally) send; returns messages sent or pasted.

    With ``send`` off, the whole dictation is pasted once into the open chat
    box for the player to review and send (the game truncates past its limit).
    """
    messages = split_for_chat(text, profile.max_chars) if send else [" ".join(text.split())]
    messages = [m for m in messages if m]

    def _guard() -> None:
        if not still_current():
            raise GameChatAborted("a newer dictation started")
        if game_pid is not None and frontmost_pid() != game_pid:
            raise GameChatAborted(f"{profile.name} is no longer in front")

    done = 0
    for index, message in enumerate(messages):
        if index:
            sleep(BETWEEN_MESSAGES_S)
        _guard()
        if profile.open_chat:
            press_return()
            sleep(OPEN_SETTLE_S)
            _guard()
        paste(message)
        done += 1
        if send:
            sleep(PASTE_SETTLE_S)
            _guard()
            press_return()
    return done
