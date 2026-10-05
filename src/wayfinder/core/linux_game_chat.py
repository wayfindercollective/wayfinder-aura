"""Gamer mode on Linux: the same feature as ``macos_game_chat`` / ``windows_game_chat``.

Everything platform-neutral is reused (profiles and their chat conventions,
message splitting, one message per dictation, the vocabulary overlay, the
Games tab list and verdicts). What differs on Linux:

* The game in front is found from its X11 window. The Flatpak runs in its own
  PID namespace, so host processes can't be read; the window says enough:
  Wine/Lutris games carry their Windows exe name in WM_CLASS (``camelot.exe``,
  ``wow.exe``), Proton games carry ``steam_app_<appid>``, and gamescope
  (SteamOS Game Mode) tags windows with ``STEAM_GAME=<appid>``. The title is
  only a fallback when the class can't be read.
* Paste is the clipboard (set by the app's Tk window, which owns the X11
  CLIPBOARD that Wine reads) plus Ctrl+V; Enter and Ctrl+V are held briefly
  with xdotool keydown/keyup, for engines that poll input once per frame.
* Some chats take no paste (Dark Age of Camelot): there the player opens the
  chat box with Enter and Aura *types* the text into it at a game-safe rate,
  pressing no game keys itself (letters are keybinds when chat is closed).
* Keys go through the desktop's RemoteDesktop portal when the player allowed it
  (KDE/GNOME Wayland: the compositor delivers them like a real keyboard) and
  through xdotool otherwise (X11 sessions, SteamOS Game Mode).
* Wine can leave X keyboard focus on another of its windows (a launcher) while
  the game is the active window; keys then go nowhere. Before typing, focus is
  handed back to the game window (``ensure_game_focus``).

Linux-only helpers return Nones elsewhere.
"""
from __future__ import annotations

import subprocess
import sys
import time
from contextlib import contextmanager
from typing import Optional

from .macos_game_chat import (  # noqa: F401 - re-exported: same interface as the Mac module
    BETWEEN_MESSAGES_S,
    KEY_HOLD_S,
    MMO_VOCABULARY,
    NOT_RECOMMENDED,
    OPEN_SETTLE_S,
    PASTE_SETTLE_S,
    PLAYABLE,
    PROFILES as _SHARED_PROFILES,
    STATUS_LABELS,
    STATUS_MEANINGS,
    UNTESTED,
    VERIFIED,
    ChatResult,
    GameChatAborted,
    GameEntry,
    GameProfile,
    gamer_asr_overlay,
    gamer_vocabulary,
    profile_entry,
    send_to_chat,
    split_for_chat,
)
from .windows_game_chat import EXE_PROFILES as _WINDOWS_EXE_PROFILES
from .windows_game_chat import INFO_ONLY, windows_note  # Windows facts hold under Wine too

# Dark Age of Camelot (e.g. the Eden freeshard under Lutris): Enter opens chat
# and Enter sends, but the client has no paste, so Aura types into the chat box
# the player opened and then sends it with Enter (the "send" toggle in Games).
# Aura never opens chat itself, and never splits: a second part would be typed
# into a closed chat, where letters are keybinds (max_chars is effectively off;
# the game trims an overlong line).
DAOC = GameProfile(
    "daoc", "Dark Age of Camelot",
    name_markers=("dark age of camelot",),
    open_chat=False, auto_send=True, max_chars=100_000,
    note="Press Enter to open chat, then dictate: Aura types it into the chat box "
         "(DAoC's chat has no paste) and sends it with Enter. Aura never opens chat "
         "for you. If keys stop reaching the game after Alt+Tab, Wine is giving focus "
         "to the launcher window: set UseTakeFocus=N in the game's Wine prefix "
         "(registry HKCU\\Software\\Wine\\X11 Driver) and restart the game.",
    vocabulary=("realm", "RvR", "keep", "relic", "frontier", "Albion", "Midgard",
                "Hibernia", "Emain", "Agramon", "Thidranki", "zerg", "stealther",
                "caster", "bodyguard", "speed", "rez", "RA", "RR"),
)

PROFILES: tuple[GameProfile, ...] = _SHARED_PROFILES + (DAOC,)
_BY_KEY = {p.key: p for p in PROFILES}

# Profiles whose chat takes no paste: Aura types the text instead.
TYPE_PROFILES = frozenset({"daoc"})

# Lower-case Windows exe name (WM_CLASS under Wine/Proton) -> profile key.
EXE_PROFILES = dict(_WINDOWS_EXE_PROFILES)
# Eden's patched client names its window after its dll (seen live on Lutris,
# 2026-09-28: WM_CLASS "eden_2gb.dll"); the retail client is camelot.exe/game.dll.
EXE_PROFILES.update({"camelot.exe": "daoc", "game.dll": "daoc",
                     "eden_2gb.dll": "daoc", "eden.dll": "daoc"})

# Steam app id (Proton's steam_app_<id> class, gamescope's STEAM_GAME) -> profile key.
STEAM_APP_PROFILES = {
    "238960": "poe", "2694490": "poe",          # Path of Exile, Path of Exile 2
    "39210": "ffxiv",                            # Final Fantasy XIV Online
    "306130": "eso",                             # The Elder Scrolls Online
    "212500": "lotro",                           # The Lord of the Rings Online
    "761890": "albion",                          # Albion Online
    "8500": "eve",                               # EVE Online
    "1343400": "jagex", "1343370": "jagex",      # RuneScape, Old School RuneScape
    "582660": "bdo",                             # Black Desert
}

# Characters per keystroke gap when typing into a game chat. Chat input reads
# key events, not per-frame state, but Wine games drop bursts under XWayland.
TYPE_DELAY_MS = 12


def game_list() -> list[GameEntry]:
    """Every game the Games tab shows, sorted by name (Windows notes: the Mac
    facts don't hold; the Windows ones do under Wine/Proton)."""
    from .windows_game_chat import _researched

    entries = [_linux_profile_entry(p) for p in PROFILES] + list(INFO_ONLY)
    names = {e.name.lower() for e in entries}
    entries += [e for e in _researched() if e.name.lower() not in names]
    return sorted(entries, key=lambda e: e.name.lower())


def _linux_profile_entry(profile: GameProfile) -> GameEntry:
    """The shared Games tab row, except that a typed chat shows its own note:
    the shared "opens chat, pastes and sends; don't press Enter first" is the
    opposite of what a player must do in Dark Age of Camelot."""
    entry = profile_entry(profile)
    if profile.key in TYPE_PROFILES and profile.note:
        return GameEntry(entry.name, entry.status, profile.note, entry.aliases)
    return entry


def search_games(query: str) -> list[GameEntry]:
    """Case-insensitive match on the name or an alias; empty query = all."""
    q = " ".join((query or "").lower().split())
    if not q:
        return game_list()
    return [e for e in game_list()
            if q in e.name.lower() or any(q in a for a in e.aliases)]


def match_profile(window_class: Optional[str], window_title: Optional[str]) -> Optional[GameProfile]:
    """The profile for the window in front, or None when it isn't a supported game.

    The window class decides (exe name or steam_app_<id>). The title is only a
    fallback when there is no class at all: a browser tab titled "Dark Age of
    Camelot guide" must never get Gamer mode.
    """
    cls = (window_class or "").strip().lower()
    if cls.startswith("steam_app_"):
        key = STEAM_APP_PROFILES.get(cls[len("steam_app_"):])
        return _BY_KEY.get(key) if key else None
    key = EXE_PROFILES.get(cls)
    if key and key in _BY_KEY:
        return _BY_KEY[key]
    if cls:
        return None
    title = (window_title or "").lower()
    if title:
        for profile in PROFILES:
            if any(marker in title for marker in profile.name_markers):
                return profile
    return None


def unlisted_game_reason(window_class: Optional[str], window_title: Optional[str] = None,
                         category: Optional[str] = None,
                         exe_path: Optional[str] = None) -> Optional[str]:
    """Why a window with no profile looks like a game, or None if it doesn't."""
    cls = (window_class or "").lower()
    if cls.startswith("steam_app_"):
        return "it is a Steam game"
    if cls.endswith(".exe"):
        return "it is a Windows game running under Wine/Proton"
    return None


def _window_facts(win) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """(class, title, steam app id) of one X window; Nones where unreadable."""
    from Xlib import X

    cls = title = appid = None
    try:
        wm_class = win.get_wm_class()
        if wm_class:
            # Wine: ("camelot.exe", "camelot.exe"); Proton: ("steam_app_N", "steam_app_N").
            cls = next((c for c in wm_class if c), None)
    except Exception:
        pass
    try:
        disp = win.display
        name = win.get_full_property(disp.intern_atom("_NET_WM_NAME"),
                                     disp.intern_atom("UTF8_STRING"))
        if name is not None and name.value:
            title = name.value.decode("utf-8", "replace") if isinstance(name.value, bytes) \
                else str(name.value)
        else:
            title = win.get_wm_name() or None
    except Exception:
        pass
    try:
        steam = win.get_full_property(win.display.intern_atom("STEAM_GAME"), X.AnyPropertyType)
        if steam is not None and len(steam.value) and int(steam.value[0]) > 0:
            appid = str(int(steam.value[0]))
    except Exception:
        pass
    return cls, (title or None), appid


def _front_window() -> tuple[Optional[int], Optional[str], Optional[str]]:
    """(window id, class, title) of the X11 window in front; Nones if unknown.

    With an EWMH window manager (KWin, Mutter) only _NET_ACTIVE_WINDOW counts:
    when a Wayland-native app is active it is 0, and a stale X input focus must
    not make the last game look frontmost. Without one (gamescope) the input
    focus is the answer, walked up to the window that carries a class.
    """
    if not sys.platform.startswith("linux"):
        return None, None, None
    try:
        from Xlib import X, display
    except Exception:
        return None, None, None
    try:
        disp = display.Display()
    except Exception:
        return None, None, None
    try:
        root = disp.screen().root
        ewmh = root.get_full_property(disp.intern_atom("_NET_SUPPORTING_WM_CHECK"),
                                      X.AnyPropertyType) is not None
        win = None
        active = root.get_full_property(disp.intern_atom("_NET_ACTIVE_WINDOW"),
                                        X.AnyPropertyType)
        if active is not None and len(active.value) and int(active.value[0]):
            win = disp.create_resource_object("window", int(active.value[0]))
        elif not ewmh:
            focus = disp.get_input_focus().focus
            if focus not in (None, X.NONE, X.PointerRoot) and not isinstance(focus, int):
                win = focus
        if win is None:
            return None, None, None
        # Walk up to the first window that names itself (class or STEAM_GAME).
        for _ in range(8):
            cls, title, appid = _window_facts(win)
            if cls or appid:
                # Fresh facts win: an untagged window clears a stale entry for
                # its id (X window ids are reused).
                _note_steam_tag(int(win.id), appid)
                if appid and not (cls or "").lower().startswith("steam_app_"):
                    if not (cls or "").lower().endswith(".exe"):
                        cls = f"steam_app_{appid}"
                return int(win.id), cls, title
            tree = win.query_tree()
            if not tree.parent or tree.parent.id == root.id:
                return int(win.id), None, title
            win = tree.parent
        return int(win.id), None, None
    except Exception:
        return None, None, None
    finally:
        try:
            disp.close()
        except Exception:
            pass


# Window ids that carried STEAM_GAME when last seen in front. A Proton game can
# keep its Windows exe name as the class (so exe profiles still match); this
# keeps it known as a Steam game for the paste-only decision.
_STEAM_WINDOWS: dict[int, str] = {}


def _note_steam_tag(window_id: int, appid: Optional[str]) -> None:
    """Record what the window says right now: its STEAM_GAME id, or none."""
    if not appid:
        _STEAM_WINDOWS.pop(window_id, None)
        return
    if len(_STEAM_WINDOWS) > 64:
        _STEAM_WINDOWS.clear()
    _STEAM_WINDOWS[window_id] = appid


def is_steam_game(window_id: Optional[int], window_class: Optional[str]) -> bool:
    """True for a window Steam identifies: a steam_app_<id> class, or a window
    that carried gamescope's STEAM_GAME tag (whatever its class)."""
    if str(window_class or "").lower().startswith("steam_app_"):
        return True
    return window_id is not None and window_id in _STEAM_WINDOWS


def frontmost_app() -> tuple[Optional[int], Optional[str], Optional[str]]:
    """(window id, class, title): the Mac's (pid, bundle id, app name) triple,
    so the app code is shared. The window id is the send guard's identity."""
    return _front_window()


def app_signals(pid: Optional[int]) -> tuple[Optional[str], Optional[str]]:
    """(category, exe path) - Linux has neither for a sandboxed app."""
    return None, None


@contextmanager
def hold_keys(seconds: float):
    """Same shape as the Mac/Windows helpers; Linux key presses hold themselves."""
    yield


def ensure_game_focus(window_id: Optional[int]) -> bool:
    """Give X keyboard focus back to the active game window when Wine left it
    on another window. True when focus was moved.

    Seen live with Dark Age of Camelot (Eden, Lutris): after Alt+Tab back,
    KWin activated the game window but Wine set the input focus on the hidden
    launcher window, so typed keys never reached the game. Wine's
    ``UseTakeFocus=N`` avoids it for good; this repairs it for one dictation.
    """
    if not window_id or not sys.platform.startswith("linux"):
        return False
    try:
        from Xlib import X, display
    except Exception:
        return False
    try:
        disp = display.Display()
    except Exception:
        return False
    try:
        root = disp.screen().root
        focus = disp.get_input_focus().focus
        if not isinstance(focus, int) and focus not in (None, X.NONE, X.PointerRoot):
            win = focus
            for _ in range(16):  # the focus may sit on a child of the game window
                if int(win.id) == int(window_id):
                    return False
                parent = win.query_tree().parent
                if not parent or int(parent.id) == int(root.id):
                    break
                win = parent
        game = disp.create_resource_object("window", int(window_id))
        game.set_input_focus(X.RevertToParent, X.CurrentTime)
        disp.sync()
        return True
    except Exception:
        return False
    finally:
        try:
            disp.close()
        except Exception:
            pass


def _portal():
    """The portal keyboard when it is ready, else None (xdotool)."""
    try:
        from . import portal_keyboard
        return portal_keyboard.keyboard() if portal_keyboard.ready() else None
    except Exception:
        return None


def _xdotool(*args: str, timeout: float = 10) -> None:
    """Run the host's xdotool. Every failure is an InjectionError whose message
    never carries the argv (it can hold the dictated text)."""
    from ..utils.hostexec import host_env
    from .injector import InjectionError

    try:
        # A host binary: never hand it the bundle's library path.
        result = subprocess.run(["xdotool", *args], capture_output=True, text=True,
                                timeout=timeout, env=host_env())
    except FileNotFoundError:
        raise InjectionError(
            "xdotool not found: install xdotool, or let Aura type through the desktop "
            "(Settings → System → Type into every app)") from None
    except subprocess.TimeoutExpired:
        raise InjectionError(f"xdotool {args[0]} timed out after {timeout:.0f}s") from None
    if result.returncode != 0:
        raise InjectionError(f"xdotool {' '.join(args[:2])} failed: {result.stderr.strip()}")


def press_held(keys: str, hold_s: float = KEY_HOLD_S, guard=None) -> None:
    """Press ``keys`` (e.g. "Return", "ctrl+v") down for ``hold_s``, then release.

    ``guard`` (raises to abort) runs after the wait for held modifiers, which
    can take seconds, immediately before the keys go out.
    """
    from .injector import InjectionError, _require_modifier_release

    _require_modifier_release()
    if guard is not None:
        guard()
    kb = _portal()
    if kb is not None:
        from .portal_keyboard import PortalKeyboardError, X11Layout
        layout = X11Layout()
        try:
            kb.press_keys(keys, max(0.0, hold_s), layout)
            return
        except PortalKeyboardError as e:
            raise InjectionError(str(e)) from e
        finally:
            layout.close()
    _xdotool("keydown", "--clearmodifiers", keys)
    try:
        time.sleep(max(0.0, hold_s))
    finally:
        try:
            _xdotool("keyup", "--clearmodifiers", keys)
        except InjectionError:
            # A key left down autorepeats (a held Return keeps sending): once more.
            _xdotool("keyup", keys)


def press_return() -> None:
    press_held("Return")


def paste_clipboard(guard=None) -> None:
    """Ctrl+V into the game (the caller has already set the clipboard)."""
    press_held("ctrl+v", guard=guard)


def type_text(text: str) -> None:
    """Type into the chat box the player opened (games without paste)."""
    from .injector import InjectionError, _require_modifier_release, fold_typography_for_typing

    _require_modifier_release()
    kb = _portal()
    if kb is not None:
        from .portal_keyboard import (PortalKeyboardError, X11Layout, ascii_fold,
                                      keysym_for_char, untypeable_chars)
        text = fold_typography_for_typing(text)
        layout = X11Layout()
        try:
            if layout.available and untypeable_chars(text, layout):
                # No paste in these chats: keep the letters, drop what has no key.
                text = "".join(ch for ch in ascii_fold(text)
                               if layout.key_for(keysym_for_char(ch)) is not None)
            kb.type_text(text, TYPE_DELAY_MS, layout)
            return
        except PortalKeyboardError as e:
            raise InjectionError(f"{e} (typed {getattr(e, 'typed', 0)} characters)") from e
        finally:
            layout.close()
    from .injector import _running_under_xwayland, build_xdotool_type_command

    # The injector's hardening: a warm-up key under XWayland (its first synthetic
    # key is dropped) and Shift released again after shifted runs.
    text = fold_typography_for_typing(text)
    argv = build_xdotool_type_command(text, TYPE_DELAY_MS, warmup=_running_under_xwayland())
    # Never cut a long message off mid-chat: allow twice the typing time.
    _xdotool(*argv[1:], timeout=10 + 2 * len(text) * TYPE_DELAY_MS / 1000)
