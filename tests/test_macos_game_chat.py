"""macOS game chat: World of Warcraft first, other MMOs by profile.

No real key events: paste/Return/frontmost are fakes that record calls.
"""

from types import SimpleNamespace

import pytest

from wayfinder.core import macos_game_chat as gc


# --- which apps are games -----------------------------------------------------

@pytest.mark.parametrize("bundle,name,key", [
    ("com.blizzard.worldofwarcraft", "World of Warcraft", "wow"),
    ("com.blizzard.worldofwarcraft.classic", "World of Warcraft Classic", "wow"),
    (None, "World of Warcraft Classic Era", "wow"),
    ("com.square-enix.ffxiv", "FINAL FANTASY XIV ONLINE", "ffxiv"),
    ("com.jagex.osrs", "Old School RuneScape", "runescape"),
    (None, "EVE Online", "eve"),
])
def test_supported_games_are_recognised(bundle, name, key):
    assert gc.match_profile(bundle, name).key == key


@pytest.mark.parametrize("bundle,name", [
    ("net.battle.app", "Battle.net"),          # the launcher is not the game
    ("com.apple.Terminal", "Terminal"),
    ("com.tinyspeck.slackmacgap", "Slack"),
    (None, None),
])
def test_other_apps_keep_the_normal_paste(bundle, name):
    assert gc.match_profile(bundle, name) is None


def test_wow_uses_its_255_character_chat_limit_and_opens_chat():
    wow = gc.match_profile("com.blizzard.worldofwarcraft", "World of Warcraft")
    assert wow.max_chars == 255 and wow.open_chat is True


# --- splitting long dictation -------------------------------------------------

def test_short_text_is_one_message_with_whitespace_normalised():
    assert gc.split_for_chat("  pull  the\nnext pack  ", 255) == ["pull the next pack"]


def test_long_text_splits_at_sentences_within_the_limit():
    sentence = "Tank pulls the next pack after the patrol walks past the bridge."
    text = " ".join([sentence] * 8)
    messages = gc.split_for_chat(text, 255)
    assert len(messages) > 1
    assert all(len(m) <= 255 for m in messages)
    assert all(m.endswith(".") for m in messages)  # whole sentences kept together
    assert " ".join(messages).split() == text.split()  # nothing lost or reordered


def test_a_sentence_longer_than_the_limit_splits_at_words():
    text = " ".join(["word"] * 100)  # 499 chars, no sentence breaks
    messages = gc.split_for_chat(text, 60)
    assert all(len(m) <= 60 for m in messages)
    assert " ".join(messages).split() == text.split()


def test_only_a_single_oversized_word_is_cut():
    assert gc.split_for_chat("x" * 25, 10) == ["x" * 10, "x" * 10, "x" * 5]


def test_empty_text_sends_nothing():
    assert gc.split_for_chat("   ", 255) == []


# --- the open / paste / send sequence -------------------------------------------

class Game:
    """Records what reached the 'game'; the frontmost app can change mid-send."""

    def __init__(self, pid=42, switch_after=None):
        self.pid, self.calls, self.switch_after = pid, [], switch_after

    def paste(self, text):
        self.calls.append(("paste", text))

    def press_return(self):
        self.calls.append(("return",))

    def frontmost(self):
        if self.switch_after is not None and len(self.calls) >= self.switch_after:
            return 999  # the player alt-tabbed
        return self.pid


def _send(game, text, profile, **kw):
    return gc.send_to_chat(
        text, profile, game_pid=game.pid, paste=game.paste,
        press_return=game.press_return, frontmost_pid=game.frontmost,
        sleep=lambda _s: None, **kw)


WOW = gc.match_profile(None, "World of Warcraft")


def test_wow_opens_chat_pastes_and_sends():
    game = Game()
    assert _send(game, "inc two from the left", WOW) == 1
    assert game.calls == [("return",), ("paste", "inc two from the left"), ("return",)]


def test_long_wow_dictation_goes_out_as_several_messages():
    game = Game()
    text = " ".join(["Stack on the tank for the next mechanic please."] * 8)
    sent = _send(game, text, WOW)
    assert sent > 1
    pasted = [c[1] for c in game.calls if c[0] == "paste"]
    # Each message: open chat, paste, send - in that order.
    expected = []
    for message in pasted:
        expected += [("return",), ("paste", message), ("return",)]
    assert game.calls == expected and len(pasted) == sent
    assert all(len(m) <= 255 for m in pasted) and " ".join(pasted).split() == text.split()


def test_send_off_pastes_once_into_the_opened_chat_for_review():
    game = Game()
    long_text = "a " * 300
    assert _send(game, long_text, WOW, send=False) == 1
    assert game.calls == [("return",), ("paste", " ".join(long_text.split()))]


def test_games_with_live_chat_input_get_no_return_first():
    game = Game()
    runescape = gc.match_profile(None, "Old School RuneScape")
    _send(game, "gf", runescape)
    assert game.calls == [("paste", "gf"), ("return",)]


def test_alt_tab_mid_send_never_presses_return_elsewhere():
    game = Game(switch_after=2)  # after "return" + "paste" the game loses focus
    with pytest.raises(gc.GameChatAborted):
        _send(game, "ready check", WOW)
    assert game.calls == [("return",), ("paste", "ready check")]  # no send Return


def test_a_newer_dictation_stops_the_sequence_before_any_key():
    game = Game()
    with pytest.raises(gc.GameChatAborted):
        _send(game, "hello", WOW, still_current=lambda: False)
    assert game.calls == []


# --- the app hook ------------------------------------------------------------------

def _app(config=None):
    logs = []
    return SimpleNamespace(
        config=config or {}, session_generation=3, log=logs.append, logs=logs,
    ), logs


def test_app_hook_leaves_non_games_to_the_normal_paste(monkeypatch):
    import wayfinder_main
    monkeypatch.setattr(gc, "frontmost_app", lambda: (7, "com.apple.Terminal", "Terminal"))
    app, _ = _app()
    assert wayfinder_main.WayfinderApp._inject_into_game_chat(app, "ls", 3) is False


def test_app_hook_sends_to_wow_through_paste_and_return(monkeypatch):
    import wayfinder_main
    from wayfinder.core import injector

    calls = []
    monkeypatch.setattr(gc, "frontmost_app", lambda: (42, "com.blizzard.worldofwarcraft", "World of Warcraft"))
    monkeypatch.setattr(gc, "OPEN_SETTLE_S", 0)
    monkeypatch.setattr(gc, "PASTE_SETTLE_S", 0)
    monkeypatch.setattr(injector, "inject_text", lambda text, **k: calls.append(("paste", text)))
    monkeypatch.setattr(injector, "press_enter", lambda: calls.append(("return",)))
    app, logs = _app({"game_chat_send": True})
    assert wayfinder_main.WayfinderApp._inject_into_game_chat(app, "summon please", 3) is True
    assert calls == [("return",), ("paste", "summon please"), ("return",)]
    assert any("World of Warcraft chat" in line for line in logs)


def test_game_chat_is_on_by_default_in_the_single_config_source():
    from wayfinder.config import DEFAULT_CONFIG
    assert DEFAULT_CONFIG["macos_game_chat"] is True
    assert DEFAULT_CONFIG["game_chat_send"] is True
