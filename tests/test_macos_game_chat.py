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
    ("com.jagex.osrs", "Old School RuneScape", "jagex"),
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
    assert _send(game, "inc two from the left", WOW) == gc.ChatResult(1, False, 0)
    assert game.calls == [("return",), ("paste", "inc two from the left"), ("return",)]


def test_long_dictation_sends_one_message_and_leaves_the_next_part_for_enter():
    """One message per keypress: the second part waits in chat, unsent."""
    game = Game()
    text = " ".join(["Stack on the tank for the next mechanic please."] * 8)
    parts = gc.split_for_chat(text, 255)
    assert len(parts) >= 2
    result = _send(game, text, WOW)
    assert result == gc.ChatResult(1, True, len(parts) - 2)
    assert game.calls == [("return",), ("paste", parts[0]), ("return",),   # sent
                          ("return",), ("paste", parts[1])]                # waiting
    assert sum(1 for c in game.calls if c == ("return",)) == 3  # never a 2nd send


def test_three_or_more_parts_leave_the_rest_in_history():
    game = Game()
    text = " ".join(["Tanks stack left and swap at three stacks of the debuff."] * 16)
    result = _send(game, text, WOW)
    assert result.sent == 1 and result.waiting and result.left_over >= 1


def test_send_off_pastes_once_into_the_opened_chat_for_review():
    game = Game()
    long_text = "a " * 300
    assert _send(game, long_text, WOW, send=False) == gc.ChatResult(0, False, 0)
    assert game.calls == [("return",), ("paste", " ".join(long_text.split()))]


def test_eve_is_paste_only_return_is_its_confirm_key():
    game = Game()
    eve = gc.match_profile(None, "EVE Online")
    _send(game, "o7 fleet", eve)
    assert game.calls == [("paste", "o7 fleet")]  # no Return, ever


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
    assert DEFAULT_CONFIG["gamer_mode"] is True
    assert DEFAULT_CONFIG["game_chat_send"] is True


# --- Gamer mode: vocabulary and cleanup for this dictation ---------------------------

def test_gamer_vocabulary_puts_shared_slang_first_then_the_game():
    words = gc.gamer_vocabulary(WOW)
    assert words[:2] == ["inc", "pull"]          # the words Base misheard in testing
    assert "M+" in words and "Death Knight" in words
    assert len(words) == len({w.lower() for w in words})  # no duplicates


def test_gamer_overlay_primes_whisper_and_keeps_cleanup_normal():
    base = {"output_tone": "professional", "model_path": "/m.bin"}
    out = gc.gamer_asr_overlay(base, WOW)
    assert out["output_tone"] == "minimal"       # no style rewrite of chat
    assert out["gamer_vocabulary"][:2] == ["inc", "pull"]
    assert base["output_tone"] == "professional"  # saved config untouched
    assert gc.gamer_asr_overlay(base, None) is base


def test_transcriber_primes_whisper_with_gamer_vocabulary_without_ultra():
    from wayfinder.core.transcriber import get_backend
    cfg = {"transcription_backend": "whisper_cpp", "whisper_server_mode": False,
           "output_tone": "minimal", "gamer_vocabulary": gc.gamer_vocabulary(WOW)}
    vocab = get_backend(cfg).custom_vocabulary
    assert "inc" in vocab and "pull" in vocab and "LFG" in vocab
    plain = get_backend({"transcription_backend": "whisper_cpp", "whisper_server_mode": False,
                         "output_tone": "minimal"}).custom_vocabulary
    assert "inc" not in (plain or [])


def test_gamer_profile_is_taken_when_dictation_starts(monkeypatch):
    import wayfinder_main
    monkeypatch.setattr(wayfinder_main, "IS_MACOS", True)
    monkeypatch.setattr(gc, "frontmost_app", lambda: (42, "com.blizzard.worldofwarcraft", "World of Warcraft"))
    app = SimpleNamespace(config={"gamer_mode": True})
    assert wayfinder_main.WayfinderApp._gamer_profile_now(app).key == "wow"
    app.config["gamer_mode"] = False
    assert wayfinder_main.WayfinderApp._gamer_profile_now(app) is None
    monkeypatch.setattr(gc, "frontmost_app", lambda: (7, "com.apple.Terminal", "Terminal"))
    app.config["gamer_mode"] = True
    assert wayfinder_main.WayfinderApp._gamer_profile_now(app) is None


def test_app_overlay_only_applies_during_a_game_dictation():
    import wayfinder_main
    cfg = {"output_tone": "dev"}
    app = SimpleNamespace(_gamer_profile=None)
    assert wayfinder_main.WayfinderApp._gamer_asr_config(app, cfg) is cfg
    app._gamer_profile = WOW
    assert wayfinder_main.WayfinderApp._gamer_asr_config(app, cfg)["output_tone"] == "minimal"



# --- research-driven rules (2026-09-24 compatibility review) ---------------------------

def test_untested_games_open_and_paste_but_the_player_sends():
    ffxiv = gc.match_profile(None, "FINAL FANTASY XIV ONLINE")
    assert not ffxiv.auto_send and ffxiv.max_chars == 400
    game = Game()
    _send(game, "looking for group", ffxiv, send=True)
    assert game.calls == [("return",), ("paste", "looking for group")]


def test_only_wow_sends_on_its_own():
    assert [p.key for p in gc.PROFILES if p.auto_send] == ["wow"]


def test_keys_are_held_only_inside_hold_keys(monkeypatch):
    import importlib
    from types import ModuleType
    from wayfinder.core import macos_paste

    real = importlib.reload(macos_paste)  # the conftest stub replaces post_return
    quartz = ModuleType("Quartz")
    quartz.kCGHIDEventTap = 0
    quartz.CGEventCreateKeyboardEvent = lambda _s, code, down: {"code": code, "down": down}
    quartz.CGEventSetFlags = lambda e, f: None
    quartz.CGEventPost = lambda _t, e: None
    monkeypatch.setitem(__import__("sys").modules, "Quartz", quartz)
    slept = []
    monkeypatch.setattr("time.sleep", slept.append)
    real.post_return()
    assert slept == []                      # desktop pastes: no delay
    with real.hold_keys(0.035):
        real.post_return()
    assert slept == [0.035]                 # one hold, on key-down
    real.post_return()
    assert slept == [0.035]                 # restored after the block


# --- unlisted games are detected and protected ---------------------------------------

@pytest.mark.parametrize("bundle,name,category,path,expect", [
    ("com.mojang.minecraftlauncher", "Minecraft", "public.app-category.games", None, "listed as a game"),
    ("com.x.rpg", "Some RPG", "public.app-category.role-playing-games", None, "listed as a game"),
    (None, "wine64-preloader", None, None, "Wine"),
    ("com.nvidia.gfnpc.mall", "GeForce NOW", None, None, "cloud-gaming"),
    ("com.x.y", "Game", None, "/Users/a/Library/Application Support/Steam/steamapps/common/Game/Game.app", "Steam"),
])
def test_unlisted_games_are_recognised(bundle, name, category, path, expect):
    assert expect in gc.unlisted_game_reason(bundle, name, category, path)


@pytest.mark.parametrize("bundle,name,category", [
    ("com.apple.Terminal", "Terminal", "public.app-category.developer-tools"),
    ("com.tinyspeck.slackmacgap", "Slack", "public.app-category.business"),
    ("net.battle.app", "Battle.net", None),             # launchers are not games
    ("com.valvesoftware.steam", "Steam", None),
])
def test_ordinary_apps_are_not_games(bundle, name, category):
    assert gc.unlisted_game_reason(bundle, name, category, "/Applications/X.app") is None


def _hook(monkeypatch, frontmost, signals, config):
    import wayfinder_main
    from wayfinder.core import injector
    keys, clip = [], []
    monkeypatch.setattr(gc, "frontmost_app", lambda: frontmost)
    monkeypatch.setattr(gc, "app_signals", lambda pid: signals)
    monkeypatch.setattr(injector, "inject_text", lambda text, **k: keys.append(text))
    monkeypatch.setattr(injector, "press_enter", lambda: keys.append("return"))
    app, logs = _app(config)
    handled = wayfinder_main.WayfinderApp._inject_into_game_chat(app, "hello there", 3)
    return handled, keys, clip, logs


def test_unlisted_game_pastes_normally_with_a_note(monkeypatch):
    handled, keys, clip, logs = _hook(
        monkeypatch, (5, "com.mojang.minecraftlauncher", "Minecraft"),
        ("public.app-category.games", "/Applications/Minecraft.app"), {})
    assert handled is False  # the normal paste path takes over: no restriction
    assert any("hasn't been tested" in line for line in logs)


def test_ordinary_app_keeps_the_normal_paste_silently(monkeypatch):
    handled, keys, clip, logs = _hook(
        monkeypatch, (7, "com.apple.Terminal", "Terminal"),
        ("public.app-category.developer-tools", "/System/Applications/Utilities/Terminal.app"), {})
    assert handled is False and keys == [] and logs == []


@pytest.mark.parametrize("name", ["Old School RuneScape", "RuneLite", "League of Legends", "Black Desert"])
def test_not_recommended_games_are_not_restricted_just_explained(monkeypatch, name):
    handled, keys, clip, logs = _hook(monkeypatch, (9, None, name), (None, None), {})
    assert handled is False and keys == []          # normal paste, no Gamer mode keys
    assert any("Heads-up" in line for line in logs)


# --- Games tab: Steam Deck-style list ------------------------------------------------

def test_every_game_has_a_known_status_and_an_explanation():
    for entry in gc.game_list():
        assert entry.status in gc.STATUS_LABELS and entry.note.strip()


def test_wow_stays_untested_until_someone_tries_it_in_game():
    wow = next(e for e in gc.game_list() if e.name == "World of Warcraft")
    assert wow.status == gc.UNTESTED and "Don't press Enter first" in wow.note


def test_tested_profiles_become_verified():
    from dataclasses import replace
    assert gc.profile_entry(replace(WOW, tested=True)).status == gc.VERIFIED


def test_not_recommended_entries_say_why():
    lol = next(e for e in gc.game_list() if e.name == "League of Legends")
    assert lol.status == gc.NOT_RECOMMENDED and "paste" in lol.note


@pytest.mark.parametrize("query,expect", [
    ("warcraft", "World of Warcraft"),
    ("MINE", "Minecraft: Java Edition"),
    ("gw2", "Guild Wars 2"),
    ("geforce now", "Cloud gaming (GeForce NOW, Xbox Cloud, Boosteroid)"),
])
def test_search_finds_games_by_name_or_alias(query, expect):
    assert expect in [e.name for e in gc.search_games(query)]


def test_empty_search_lists_everything_and_nonsense_lists_nothing():
    assert len(gc.search_games("  ")) == len(gc.game_list())
    assert gc.search_games("zzz-not-a-game") == []


# --- researched game list (generated by scripts/build_game_list.py) ------------------

def test_game_list_has_no_duplicate_names():
    names = [e.name.lower() for e in gc.game_list()]
    assert len(names) == len(set(names))


def test_researched_entries_are_well_formed():
    data = pytest.importorskip("wayfinder.core.game_list_data")
    import re
    emoji = re.compile("[\U0001F300-\U0001FAFF☀-➿]")
    hand_written = {p.name.lower() for p in gc.PROFILES} | {e.name.lower() for e in gc.INFO_ONLY}
    assert data.RESEARCHED_GAMES, "the generated list is empty"
    for name, status, note, aliases, sources in data.RESEARCHED_GAMES:
        assert name.strip() and name.lower() not in hand_written, name
        assert status in (gc.UNTESTED, gc.NOT_RECOMMENDED), name
        assert note.strip() and "—" not in note and not emoji.search(note), name
        assert all(a == a.lower() for a in aliases), name


def test_researched_games_are_searchable():
    data = pytest.importorskip("wayfinder.core.game_list_data")
    name = data.RESEARCHED_GAMES[0][0]
    assert name in [e.name for e in gc.search_games(name.split()[0])]


def test_generated_list_matches_a_rebuild_from_the_saved_research():
    """game_list_data.py must be what scripts/build_game_list.py makes from
    scripts/data/game_chat_research.json (no hand edits drifting in)."""
    import importlib.util
    import json
    from pathlib import Path
    data = pytest.importorskip("wayfinder.core.game_list_data")
    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location("build_game_list", root / "scripts" / "build_game_list.py")
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    rows = json.loads((root / "scripts" / "data" / "game_chat_research.json").read_text())["games"]
    assert tuple(builder.build(rows)) == tuple(tuple(e) for e in data.RESEARCHED_GAMES)
