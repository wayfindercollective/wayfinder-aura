"""Gamer mode on Linux: detection from the X11 window, and the DAoC typing path."""
from __future__ import annotations

import pytest

from wayfinder.core import linux_game_chat as gc


@pytest.mark.parametrize("cls, title, key", [
    ("eden_2gb.dll", None, "daoc"),                          # Eden's client, as seen live
    ("camelot.exe", "Dark Age of Camelot", "daoc"),        # retail client (Wine)
    ("Camelot.exe", None, "daoc"),
    ("wow.exe", "World of Warcraft", "wow"),                 # Battle.net under Wine
    ("steam_app_2694490", "Path of Exile 2", "poe"),         # Proton
    ("steam_app_39210", None, "ffxiv"),
    ("steam_app_8500", None, "eve"),
    ("firefox", "Dark Age of Camelot guide - Mozilla Firefox", None),  # class decides
    ("steam_app_1091500", "Cyberpunk 2077", None),           # a Steam game with no profile
    ("", "Dark Age of Camelot", "daoc"),                     # no class: title fallback
])
def test_match_profile_from_the_window(cls, title, key):
    profile = gc.match_profile(cls, title)
    assert (profile.key if profile else None) == key


def test_unlisted_reasons():
    assert gc.unlisted_game_reason("steam_app_1091500") == "it is a Steam game"
    assert "Wine" in gc.unlisted_game_reason("sekiro.exe")
    assert gc.unlisted_game_reason("konsole") is None


def test_daoc_types_into_the_open_chat_then_sends_once():
    profile = gc.match_profile("eden_2gb.dll", None)
    assert profile.key in gc.TYPE_PROFILES
    typed, returns = [], []
    result = gc.send_to_chat(
        "inc two on the left keep door", profile, game_pid=42, send=True,
        paste=typed.append, press_return=lambda: returns.append(1),
        frontmost_pid=lambda: 42, sleep=lambda _s: None,
    )
    assert typed == ["inc two on the left keep door"]
    assert returns == [1]            # send only: the player opened the chat
    assert result.sent == 1


def test_daoc_never_types_a_second_part_into_a_closed_chat():
    profile = gc.match_profile("eden_2gb.dll", None)
    long_text = " ".join(["word"] * 400)  # far past any chat limit
    typed, returns = [], []
    gc.send_to_chat(long_text, profile, game_pid=42, send=True, paste=typed.append,
                    press_return=lambda: returns.append(1), frontmost_pid=lambda: 42,
                    sleep=lambda _s: None)
    assert typed == [long_text] and returns == [1]


def test_daoc_with_send_off_only_types():
    profile = gc.match_profile("eden_2gb.dll", None)
    typed, returns = [], []
    gc.send_to_chat("hello", profile, game_pid=42, send=False, paste=typed.append,
                    press_return=lambda: returns.append(1), frontmost_pid=lambda: 42,
                    sleep=lambda _s: None)
    assert typed == ["hello"] and returns == []


def test_daoc_stops_if_the_game_left_the_front():
    profile = gc.match_profile("camelot.exe", None)
    with pytest.raises(gc.GameChatAborted):
        gc.send_to_chat("hello", profile, game_pid=42, paste=lambda m: None,
                        press_return=lambda: None, frontmost_pid=lambda: 99,
                        sleep=lambda _s: None)


def test_games_tab_lists_daoc_and_the_shared_games():
    names = {e.name for e in gc.game_list()}
    assert {"Dark Age of Camelot", "World of Warcraft", "Path of Exile"} <= names
    assert gc.search_games("camelot")[0].name == "Dark Age of Camelot"


def test_linux_uses_the_linux_backend(monkeypatch):
    import wayfinder_main as wm

    monkeypatch.setattr(wm, "IS_MACOS", False)
    monkeypatch.setattr(wm, "IS_WINDOWS", False)
    monkeypatch.setattr(wm, "_IS_LINUX", True)
    assert wm._game_chat_module() is gc
