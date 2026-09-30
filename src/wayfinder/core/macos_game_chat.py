"""Gamer mode on macOS: dictate straight into an MMO's chat box.

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
    # Press Return to send. Only for games whose chat behaviour is confirmed;
    # elsewhere Aura pastes and the player sends (EVE: Return is "Confirm").
    auto_send: bool = False
    # Not recommended: its rules forbid software key presses (or its chat
    # takes no paste). Aura doesn't restrict it - it gets the normal paste,
    # no Gamer mode keys - and the Games tab explains the risk.
    caution: bool = False
    reason: str = ""
    note: str = ""             # shown in the Games tab instead of the default line
    tested: bool = False       # tried in the real game -> "Verified" in the Games tab
    # Game-specific words for Whisper's prompt, after MMO_VOCABULARY.
    vocabulary: tuple[str, ...] = ()


# Gamer mode primes Whisper with these while a supported game is in front:
# the Base model heard "inc" as "Ink" and "pull" as "pole" in WoW chat. Most
# mis-heard short slang first (the prompt budget keeps the front).
MMO_VOCABULARY: tuple[str, ...] = (
    "inc", "pull", "LFG", "LFM", "DPS", "tank", "healer", "OOM", "AFK", "BRB",
    "GG", "wipe", "adds", "aggro", "trash", "res", "battle rez", "CC",
    "interrupt", "kite", "buff", "debuff", "AoE", "proc", "cooldowns", "ilvl",
    "loot", "raid", "dungeon", "boss", "PvP", "PvE", "gank", "guild", "party",
)


PROFILES: tuple[GameProfile, ...] = (
    # Retail, Classic and Classic Era all ship as "World of Warcraft*.app".
    # Chat: Return opens, Cmd+V pastes (documented by the Paste NG addon on
    # Mac), Return sends, 255 characters per message.
    GameProfile(
        "wow", "World of Warcraft",
        bundle_prefixes=("com.blizzard.worldofwarcraft",),
        name_markers=("world of warcraft",),
        open_chat=True, max_chars=255, auto_send=True,
        vocabulary=("M+", "Mythic+", "heroic", "LFR", "Bloodlust", "Heroism",
                    "Hearthstone", "Horde", "Alliance", "Azeroth", "Stormwind",
                    "Orgrimmar", "Dornogal", "Death Knight", "Demon Hunter",
                    "Evoker", "Paladin", "Warlock", "Druid", "Shaman"),
    ),
    # Not yet tried in the game: open chat and paste; the player sends.
    # (FFXIV's and LOTRO's Mac builds run under Wine and show up as
    # "wine64-preloader", so they only match when the name does.)
    GameProfile("ffxiv", "Final Fantasy XIV", name_markers=("final fantasy xiv",), max_chars=400),
    GameProfile("eso", "The Elder Scrolls Online", name_markers=("elder scrolls online",), max_chars=350),
    GameProfile("lotro", "The Lord of the Rings Online", name_markers=("lord of the rings online",)),
    GameProfile("albion", "Albion Online", name_markers=("albion online",)),
    GameProfile("poe", "Path of Exile", name_markers=("path of exile",),
                note="Opens chat and pastes; you press Enter. Path of Exile's tool policy is "
                     "one message per keypress, so Aura never sends for you here."),
    # Return is EVE's locked "Confirm" key: paste only, never Return.
    GameProfile("eve", "EVE Online", name_markers=("eve online",), open_chat=False,
                note="Click into chat first. Aura pastes and never presses Enter, "
                     "which is EVE's Confirm key."),
    # Not recommended: the normal paste only, with the reason explained.
    GameProfile(
        "jagex", "RuneScape", name_markers=("runescape", "runelite"), caution=True,
        reason="Jagex's rules forbid software that generates key presses, and its chat takes no paste",
    ),
    GameProfile(
        "lol", "League of Legends", name_markers=("league of legends",), caution=True,
        reason="its chat does not accept pasted text",
    ),
    GameProfile(
        "bdo", "Black Desert", name_markers=("black desert",), caution=True,
        reason="it bans macros on a first offence",
    ),
)

# Seconds. The chat box needs a frame or two to open and to read the paste;
# MMOs also throttle fast bursts of messages.
OPEN_SETTLE_S = 0.2
# Each key is held this long: some engines read the keyboard once per frame
# and miss a key whose down and up arrive together.
KEY_HOLD_S = 0.035
PASTE_SETTLE_S = 0.2
BETWEEN_MESSAGES_S = 0.45


# Games tab verdicts, modelled on Steam Deck Verified.
VERIFIED, PLAYABLE, UNTESTED, NOT_RECOMMENDED = "verified", "playable", "untested", "not_recommended"
STATUS_LABELS = {
    VERIFIED: "Verified",
    PLAYABLE: "Playable",
    UNTESTED: "Untested",
    NOT_RECOMMENDED: "Not recommended",
}
STATUS_MEANINGS = {
    VERIFIED: "Tested in the game. Aura opens chat, pastes and sends.",
    PLAYABLE: "Works, with a step from you (like opening chat first).",
    UNTESTED: "Set up from the game's documentation; not tried in the game yet.",
    NOT_RECOMMENDED: "Aura still works, but the game's rules or chat make it a bad idea.",
}


@dataclass(frozen=True)
class GameEntry:
    name: str
    status: str
    note: str
    aliases: tuple[str, ...] = ()


def profile_entry(profile: GameProfile) -> GameEntry:
    """The Games tab row for a game Aura has a profile for."""
    if profile.caution:
        return GameEntry(profile.name, NOT_RECOMMENDED,
                         f"Pastes normally, but {profile.reason}. Use it at your own risk.")
    if profile.auto_send:
        what = (f"Opens chat, pastes and sends one message. If you say more than "
                f"{profile.max_chars} characters, the next part waits in chat for your "
                f"Enter. Don't press Enter first.")
    elif profile.open_chat:
        what = profile.note or "Opens chat and pastes; you check it and press Enter."
    else:
        what = profile.note or "Click into chat first; Aura pastes."
    return GameEntry(profile.name, VERIFIED if (profile.tested and profile.auto_send)
                     else PLAYABLE if profile.tested else UNTESTED, what)


# Games with no profile (Aura pastes normally), so players can look them up.
# Sources: the 2026-09-24 compatibility review (docs/MACOS-PORT.md, Gamer mode).
INFO_ONLY: tuple[GameEntry, ...] = (
    GameEntry("Minecraft: Java Edition", UNTESTED,
              "Chat opens with T, not Enter: press T, then dictate. Aura pastes.", ("minecraft",)),
    GameEntry("Roblox", UNTESTED,
              "Chat opens with /: press /, then dictate. Aura pastes. Roblox chat needs an "
              "age-checked account, and some experiences replace the chat."),
    GameEntry("Dota 2", UNTESTED,
              "Players report Ctrl+V pastes in Dota 2 but Cmd+V may not; not tried yet."),
    GameEntry("Terraria", UNTESTED, "Enter opens chat; Aura pastes. Not tried yet."),
    GameEntry("Stardew Valley", UNTESTED, "T opens chat; Aura pastes. Not tried yet."),
    GameEntry("Guild Wars 2", UNTESTED,
              "No Mac version since 2021. Through CrossOver or cloud gaming, Cmd+V does not "
              "paste yet (Wine turns it into Alt+V).", ("gw2",)),
    GameEntry("Cloud gaming (GeForce NOW, Xbox Cloud, Boosteroid)", UNTESTED,
              "Aura can't tell which game is streaming, and pasting may need Ctrl+V.",
              ("geforce now", "xbox cloud", "boosteroid")),
    GameEntry("Valheim", UNTESTED, "Chat opens with Enter (check in game); Aura pastes."),
    GameEntry("Garry's Mod", UNTESTED,
              "Roleplay servers chat with Y (global) or U (team); press it, then dictate. "
              "Pasting there hasn't been tried.", ("gmod", "darkrp")),
    GameEntry("Project Zomboid", UNTESTED, "Chat opens with T (check in game); Aura pastes."),
    GameEntry("Don't Starve Together", UNTESTED, "Chat opens with Y (check in game); Aura pastes."),
    GameEntry("Age of Empires II: Definitive Edition", UNTESTED,
              "Chat opens with Enter (check in game); Aura pastes.", ("aoe2", "age of empires")),
    GameEntry("StarCraft II", UNTESTED, "Chat opens with Enter (check in game); Aura pastes.", ("sc2",)),
    GameEntry("Among Us", UNTESTED,
              "Mac: the iPad app only. Click the chat bubble first; free chat needs age 13+."),
    GameEntry("Discord", PLAYABLE,
              "Not a game, but where most players type: a normal text box, so Aura pastes "
              "like anywhere else."),
    GameEntry("Lost Ark", NOT_RECOMMENDED,
              "Only playable through cloud gaming on a Mac, and its anti-cheat is strict."),
    GameEntry("Hearthstone", NOT_RECOMMENDED, "No text chat in matches (emotes only)."),
)


def _researched() -> list[GameEntry]:
    """Generated entries (scripts/build_game_list.py); empty if not built yet."""
    try:
        from wayfinder.core.game_list_data import RESEARCHED_GAMES
    except ImportError:
        return []
    return [GameEntry(name, status, note, tuple(aliases))
            for name, status, note, aliases, _sources in RESEARCHED_GAMES]


def game_list() -> list[GameEntry]:
    """Every game the Games tab shows, sorted by name. Profiles and the
    hand-written entries win over researched ones with the same name."""
    entries = [profile_entry(p) for p in PROFILES] + list(INFO_ONLY)
    names = {e.name.lower() for e in entries}
    entries += [e for e in _researched() if e.name.lower() not in names]
    return sorted(entries, key=lambda e: e.name.lower())


def search_games(query: str) -> list[GameEntry]:
    """Case-insensitive match on the name or an alias; empty query = all."""
    q = " ".join((query or "").lower().split())
    if not q:
        return game_list()
    return [e for e in game_list()
            if q in e.name.lower() or any(q in a for a in e.aliases)]


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


def gamer_vocabulary(profile: GameProfile) -> list[str]:
    """Words to prime Whisper with for this game (shared MMO slang first)."""
    seen, words = set(), []
    for word in MMO_VOCABULARY + profile.vocabulary:
        if word.lower() not in seen:
            seen.add(word.lower())
            words.append(word)
    return words


def gamer_asr_overlay(config: dict, profile: Optional[GameProfile]) -> dict:
    """Per-dictation config for Gamer mode (never written back to disk).

    Whisper gets the game's vocabulary, and cleanup stays Normal: chat keeps
    your words (fillers removed), no style rewrite.
    """
    if profile is None:
        return config
    overlay = dict(config)
    overlay["gamer_vocabulary"] = gamer_vocabulary(profile)
    overlay["output_tone"] = "minimal"
    return overlay


# Games that aren't on the list: Aura gives them the normal paste and only
# mentions (in the activity log) that it hasn't been tested with them.
_GAME_CATEGORY = re.compile(r"^public\.app-category\.([a-z-]+-)?games$")
_GAME_HOST_NAMES = ("wine64-preloader", "wine-preloader", "wineloader", "geforce now")
_GAME_HOST_BUNDLES = ("com.nvidia.gfnpc",)          # GeForce NOW
_GAME_PATH_MARKERS = ("/steamapps/common/",)        # a Steam game's install folder


def unlisted_game_reason(
    bundle_id: Optional[str], app_name: Optional[str],
    category: Optional[str] = None, bundle_path: Optional[str] = None,
) -> Optional[str]:
    """Why an app that has no profile looks like a game, or None if it doesn't."""
    if category and _GAME_CATEGORY.match(category.strip().lower()):
        return "it is listed as a game"
    name = (app_name or "").lower()
    if any(marker in name for marker in _GAME_HOST_NAMES):
        return ("it is a Windows game running through Wine" if "wine" in name
                else "it is a cloud-gaming app")
    if bundle_id and any(bundle_id.lower().startswith(b) for b in _GAME_HOST_BUNDLES):
        return "it is a cloud-gaming app"
    if bundle_path and any(marker in bundle_path.lower() for marker in _GAME_PATH_MARKERS):
        return "it is a Steam game"
    return None


def app_signals(pid: Optional[int]) -> tuple[Optional[str], Optional[str]]:
    """(LSApplicationCategoryType, bundle path) for a running app; Nones if unknown."""
    if pid is None:
        return None, None
    try:
        import plistlib
        from AppKit import NSRunningApplication

        app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
        url = app.bundleURL() if app is not None else None
        path = str(url.path()) if url is not None else None
        if not path:
            exe = app.executableURL() if app is not None else None
            return None, (str(exe.path()) if exe is not None else None)
        category = None
        try:
            with open(f"{path}/Contents/Info.plist", "rb") as fh:
                category = plistlib.load(fh).get("LSApplicationCategoryType")
        except (OSError, ValueError):
            pass
        return category, path
    except Exception:
        return None, None


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


@dataclass(frozen=True)
class ChatResult:
    sent: int          # messages Aura sent (0 or 1: one message per dictation)
    waiting: bool      # a further part was pasted into chat for the player to send
    left_over: int     # parts beyond that, left in History


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
) -> ChatResult:
    """Open chat, paste and (optionally) send - one message per dictation.

    Game rules draw the line at one message per keypress (Path of Exile's
    tool policy says so; it is Blizzard players' rule of thumb too), so Aura
    sends at most one message. If the dictation is longer than the chat
    limit, the next part is pasted into a freshly opened chat box for the
    player to send with Enter; anything past that stays in History.

    With ``send`` off, or for a game without ``auto_send``, the whole
    dictation is pasted once for the player to review and send (the game
    truncates past its limit). "Not recommended" games never get here.
    """
    send = send and profile.auto_send
    messages = split_for_chat(text, profile.max_chars) if send else [" ".join(text.split())]
    messages = [m for m in messages if m]

    def _guard() -> None:
        if not still_current():
            raise GameChatAborted("a newer dictation started")
        if game_pid is not None and frontmost_pid() != game_pid:
            raise GameChatAborted(f"{profile.name} is no longer in front")

    def _open_and_paste(message: str) -> None:
        _guard()
        if profile.open_chat:
            press_return()
            sleep(OPEN_SETTLE_S)
            _guard()
        paste(message)

    if not messages:
        return ChatResult(0, False, 0)
    _open_and_paste(messages[0])
    if not send:
        return ChatResult(0, False, 0)
    sleep(PASTE_SETTLE_S)
    _guard()
    press_return()
    if len(messages) == 1:
        return ChatResult(1, False, 0)
    sleep(BETWEEN_MESSAGES_S)
    _open_and_paste(messages[1])  # waits in chat: the player's Enter sends it
    return ChatResult(1, True, len(messages) - 2)
