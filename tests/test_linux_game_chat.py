"""Gamer mode on Linux: detection from the X11 window, and the DAoC typing path."""
from __future__ import annotations

import pytest

from wayfinder.core import linux_game_chat as gc

_REAL_PASTE_CLIPBOARD = gc.paste_clipboard


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


def test_games_tab_tells_daoc_players_to_open_chat_first():
    entries = {e.name: e for e in gc.game_list()}
    daoc = entries["Dark Age of Camelot"]
    assert daoc.note == gc.DAOC.note
    assert "Press Enter to open chat" in daoc.note and "UseTakeFocus=N" in daoc.note
    assert "Don't press Enter first" not in daoc.note
    # Pasted games keep the shared wording.
    assert "pastes" in entries["World of Warcraft"].note


def _linux_app(monkeypatch, frontmost):
    from types import SimpleNamespace

    import wayfinder_main as wm
    from wayfinder.core import injector

    monkeypatch.setattr(wm, "IS_MACOS", False)
    monkeypatch.setattr(wm, "IS_WINDOWS", False)
    monkeypatch.setattr(wm, "_IS_LINUX", True)
    monkeypatch.setattr(gc, "frontmost_app", lambda: frontmost)
    monkeypatch.setattr(gc, "ensure_game_focus", lambda window_id: False)
    calls, logs = [], []
    def fake_paste(guard=None):
        if guard is not None:
            guard()                       # raises to stop the paste
        calls.append(("ctrl+v",))

    monkeypatch.setattr(gc, "paste_clipboard", fake_paste)
    monkeypatch.setattr(injector, "inject_text", lambda *a, **k: calls.append(("typed",) + a))
    monkeypatch.setattr(injector, "_require_modifier_release", lambda *_a: None)
    monkeypatch.setattr(gc, "_STEAM_WINDOWS", {})
    app = SimpleNamespace(config={}, session_generation=1, log=logs.append)
    app._set_clipboard_from_worker = (
        lambda text, timeout=2.0: calls.append(("clipboard", text)) or True)
    app._linux_game_paste_only = (
        lambda text, window_id, gen=None:
        wm.WayfinderApp._linux_game_paste_only(app, text, window_id, gen))
    app._linux_paste_into_game = (
        lambda text, window_id, gen=None:
        wm.WayfinderApp._linux_paste_into_game(app, text, window_id, gen))
    return wm, app, calls, logs


@pytest.mark.parametrize("frontmost", [
    (5, "steam_app_1091500", "Cyberpunk 2077"),   # unlisted Proton game
    (7, "steam_app_1343400", "RuneScape"),        # a not-recommended profile
])
def test_games_without_a_chat_profile_paste_and_never_type(monkeypatch, frontmost):
    wm, app, calls, logs = _linux_app(monkeypatch, frontmost)
    assert wm.WayfinderApp._inject_into_game_chat(app, "hello there", 1) is True
    assert calls == [("clipboard", "hello there"), ("ctrl+v",)]
    assert any("pasting normally" in line.lower() for line in logs)


@pytest.mark.parametrize("frontmost", [
    (8, "konsole", "Konsole"),
    (6, "notepad.exe", "Untitled - Notepad"),   # any Wine window has an .exe class
])
def test_ordinary_apps_keep_the_normal_injection(monkeypatch, frontmost):
    wm, app, calls, logs = _linux_app(monkeypatch, frontmost)
    assert wm.WayfinderApp._inject_into_game_chat(app, "ls", 1) is False
    assert calls == []
    assert not any("pasting" in line.lower() for line in logs)


def test_game_paste_stops_if_the_game_left_the_front(monkeypatch):
    """Setting the clipboard waits on the Tk thread; an Alt+Tab meanwhile must
    not paste the dictation into the other app."""
    wm, app, calls, _ = _linux_app(monkeypatch, (5, "steam_app_1091500", "Cyberpunk 2077"))
    fronts = iter([(5, "steam_app_1091500", "Cyberpunk 2077"), (9, "firefox", "Mail")])
    monkeypatch.setattr(gc, "frontmost_app", lambda: next(fronts))
    with pytest.raises(wm.InjectionError, match="left the front"):
        wm.WayfinderApp._inject_into_game_chat(app, "secret plan", 1)
    assert ("ctrl+v",) not in calls


def test_game_paste_stops_if_the_dictation_was_reset(monkeypatch):
    wm, app, calls, _ = _linux_app(monkeypatch, (5, "steam_app_1091500", "Cyberpunk 2077"))
    app._set_clipboard_from_worker = (
        lambda text, timeout=2.0: setattr(app, "session_generation", 2) or True)
    with pytest.raises(wm.InjectionError, match="reset"):
        wm.WayfinderApp._inject_into_game_chat(app, "old dictation", 1)
    assert ("ctrl+v",) not in calls


def _ok(argv=None, **_kwargs):
    from types import SimpleNamespace
    return SimpleNamespace(returncode=0, stderr="", stdout="")


def test_xdotool_failures_never_carry_the_dictated_text(monkeypatch):
    import subprocess

    from wayfinder.core.injector import InjectionError

    def missing(argv, **kwargs):
        raise FileNotFoundError(argv[0])

    monkeypatch.setattr(gc.subprocess, "run", missing)
    with pytest.raises(InjectionError, match="xdotool not found"):
        gc._xdotool("type", "--", "secret words")

    def slow(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(gc.subprocess, "run", slow)
    with pytest.raises(InjectionError, match="timed out") as exc:
        gc._xdotool("type", "--", "secret words")
    assert "secret" not in str(exc.value)


# -- Codex review finding 3: _xdotool's own refusals, piece zero included --
# (type_text's per-piece use of _piece_of_a_typing is covered end-to-end in
# test_injector.py's TestFirstPieceProvenPreDelivery; these test _xdotool's
# OWN classification of a failure as proven-pre-delivery or not, straight
# from the MagicMock result it got.)

def test_xdotool_missing_binary_is_proven_pre_delivery(monkeypatch):
    monkeypatch.setattr(gc.subprocess, "run",
                        lambda argv, **k: (_ for _ in ()).throw(FileNotFoundError(argv[0])))
    try:
        gc._xdotool("type", "--", "words")
    except Exception as exc:
        assert getattr(exc, "proven_pre_delivery", False) is True
    else:
        pytest.fail("expected an InjectionError")


def test_xdotool_timeout_is_not_proven_pre_delivery(monkeypatch):
    import subprocess

    def slow(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(gc.subprocess, "run", slow)
    try:
        gc._xdotool("type", "--", "words")
    except Exception as exc:
        assert getattr(exc, "proven_pre_delivery", False) is False
    else:
        pytest.fail("expected an InjectionError")


@pytest.mark.parametrize("detail", [
    "X connection refused",
    "Error: Can't open display: (null)",
])
def test_xdotool_display_refusal_is_proven_pre_delivery(monkeypatch, detail):
    from types import SimpleNamespace
    failed = SimpleNamespace(returncode=1, stdout="", stderr=detail)
    monkeypatch.setattr(gc.subprocess, "run", lambda argv, **k: failed)
    try:
        gc._xdotool("type", "--", "words")
    except Exception as exc:
        assert getattr(exc, "proven_pre_delivery", False) is True
    else:
        pytest.fail("expected an InjectionError")


def test_xdotool_unrecognised_exit_is_not_proven_pre_delivery(monkeypatch):
    from types import SimpleNamespace
    failed = SimpleNamespace(returncode=1, stdout="", stderr="BadWindow")
    monkeypatch.setattr(gc.subprocess, "run", lambda argv, **k: failed)
    try:
        gc._xdotool("type", "--", "words")
    except Exception as exc:
        assert getattr(exc, "proven_pre_delivery", False) is False
    else:
        pytest.fail("expected an InjectionError")


def test_xdotool_runs_with_the_host_environment(monkeypatch):
    from wayfinder.utils import hostexec

    seen = {}
    monkeypatch.setattr(hostexec, "host_env", lambda: {"MARK": "host"})
    monkeypatch.setattr(gc.subprocess, "run", lambda argv, **k: seen.update(k) or _ok())
    gc._xdotool("key", "Return")
    assert seen["env"] == {"MARK": "host"}


def test_press_held_releases_the_key_when_the_first_keyup_fails(monkeypatch):
    from types import SimpleNamespace

    from wayfinder.core import injector

    monkeypatch.setattr(injector, "_require_modifier_release", lambda *_a: None)
    monkeypatch.setattr(gc, "_portal", lambda: None)
    monkeypatch.setattr(gc.time, "sleep", lambda _s: None)
    calls = []

    def run(argv, **kwargs):
        calls.append(argv[1])
        first_keyup = argv[1] == "keyup" and calls.count("keyup") == 1
        return SimpleNamespace(returncode=1 if first_keyup else 0,
                               stderr="BadWindow" if first_keyup else "")

    monkeypatch.setattr(gc.subprocess, "run", run)
    gc.press_held("Return", 0)
    assert calls == ["keydown", "keyup", "keyup"]


def test_type_text_without_the_portal_uses_the_hardened_xdotool_command(monkeypatch):
    from wayfinder.core import injector

    monkeypatch.setattr(injector, "_require_modifier_release", lambda *_a: None)
    monkeypatch.setattr(injector, "_running_under_xwayland", lambda: True)
    monkeypatch.setattr(gc, "_portal", lambda: None)
    seen = []
    monkeypatch.setattr(gc.subprocess, "run",
                        lambda argv, **k: seen.append((argv, k["timeout"])) or _ok())
    text = "RvR “inc” at Emain… GO"
    gc.type_text(text)
    folded = injector.fold_typography_for_typing(text)

    def command(piece):
        return injector.build_xdotool_type_command(piece, gc.TYPE_DELAY_MS, warmup=True)

    # Its gaps and Shift sweeps put it past one piece's half second.
    pieces = injector.typing_chunks(
        folded, lambda p: injector.xdotool_scheduled_seconds(command(p)))
    assert "".join(pieces) == folded and len(pieces) == 2
    assert [argv for argv, _ in seen] == [command(p) for p in pieces]
    assert all(timeout >= 10 for _, timeout in seen)

    seen.clear()
    gc.type_text("x" * 2000)  # ~24 s of typing at 12 ms per key, in pieces
    pieces = [(argv[argv.index("--") + 1], timeout) for argv, timeout in seen]
    assert "".join(piece for piece, _ in pieces) == "x" * 2000 and len(pieces) > 1
    assert all(timeout > len(piece) * gc.TYPE_DELAY_MS / 1000 for piece, timeout in pieces)



def test_game_paste_rechecks_after_waiting_for_held_keys(monkeypatch):
    """A held modifier delays the keys by up to seconds (inside press_held);
    an Alt+Tab during that wait must not send the paste to the other app."""
    from wayfinder.core import injector
    wm, app, calls, _ = _linux_app(monkeypatch, (5, "steam_app_1091500", "Cyberpunk 2077"))
    monkeypatch.setattr(gc, "paste_clipboard", _REAL_PASTE_CLIPBOARD)   # the real wait
    monkeypatch.setattr(gc, "_portal", lambda: None)
    sent = []
    monkeypatch.setattr(gc, "_xdotool", lambda *a, **k: sent.append(a))
    front = {"now": (5, "steam_app_1091500", "Cyberpunk 2077")}
    monkeypatch.setattr(gc, "frontmost_app", lambda: front["now"])
    monkeypatch.setattr(injector, "_require_modifier_release",
                        lambda *_a: front.update(now=(9, "firefox", "Mail")))
    with pytest.raises(wm.InjectionError, match="left the front"):
        wm.WayfinderApp._inject_into_game_chat(app, "secret plan", 1)
    assert sent == []                     # no keydown went out


def test_a_proton_game_with_an_exe_class_is_still_a_steam_game(monkeypatch):
    """gamescope's STEAM_GAME tag marks the window even when the class keeps
    the Windows exe name; Notepad under Wine carries no such tag."""
    wm, app, calls, _ = _linux_app(monkeypatch, (6, "sekiro.exe", "Sekiro"))
    gc._note_steam_tag(6, "814380")
    assert wm.WayfinderApp._inject_into_game_chat(app, "gg", 1) is True
    assert calls == [("clipboard", "gg"), ("ctrl+v",)]
    assert gc.is_steam_game(6, "sekiro.exe") and not gc.is_steam_game(7, "notepad.exe")



def test_a_reused_window_id_without_the_steam_tag_is_not_a_game(monkeypatch):
    """X window ids are reused: once the id shows up untagged (Wine Notepad),
    the cached Steam entry must not route it to game paste."""
    monkeypatch.setattr(gc, "_STEAM_WINDOWS", {})
    gc._note_steam_tag(6, "814380")
    assert gc.is_steam_game(6, "sekiro.exe")
    gc._note_steam_tag(6, None)            # fresh sighting: no STEAM_GAME
    assert not gc.is_steam_game(6, "notepad.exe")



class _XWindow:
    def __init__(self, display, wid, wm_class=None, steam=None, title=None, parent=None):
        self.display, self.id = display, wid
        self._class, self._steam, self._title, self._parent = wm_class, steam, title, parent

    def get_wm_class(self):
        return (self._class, self._class) if self._class else None

    def get_full_property(self, atom, kind):
        from types import SimpleNamespace
        if atom == "STEAM_GAME" and self._steam:
            return SimpleNamespace(value=[int(self._steam)])
        if atom == "_NET_WM_NAME" and self._title:
            return SimpleNamespace(value=self._title.encode())
        return None

    def get_wm_name(self):
        return self._title

    def query_tree(self):
        from types import SimpleNamespace
        return SimpleNamespace(parent=self._parent)


class _XDisplay:
    def __init__(self):
        from types import SimpleNamespace
        self.windows, self.active = {}, 0
        display = self

        class _Root:
            id = 1

            def get_full_property(self, atom, kind):
                if atom == "_NET_SUPPORTING_WM_CHECK":
                    return SimpleNamespace(value=[1])
                if atom == "_NET_ACTIVE_WINDOW":
                    return SimpleNamespace(value=[display.active])
                return None

        self.root = _Root()

    def intern_atom(self, name):
        return name

    def screen(self):
        from types import SimpleNamespace
        return SimpleNamespace(root=self.root)

    def create_resource_object(self, kind, wid):
        return self.windows[wid]

    def close(self):
        pass


def _fake_xlib(monkeypatch, display):
    import sys
    import types
    xlib = types.ModuleType("Xlib")
    xlib.X = types.SimpleNamespace(AnyPropertyType=0, NONE=0, PointerRoot=1)
    xlib.display = types.SimpleNamespace(Display=lambda: display)
    monkeypatch.setitem(sys.modules, "Xlib", xlib)
    monkeypatch.setattr(gc.sys, "platform", "linux")


@pytest.mark.parametrize("next_window", [
    {"wm_class": "notepad.exe", "title": "Untitled - Notepad"},   # reused id, untagged
    {"wm_class": None, "title": "Notepad"},                         # classless sighting
])
def test_the_front_window_refreshes_the_steam_tag(monkeypatch, next_window):
    """X window ids are reused: what the window in front says now decides,
    through _front_window itself (classless windows included)."""
    monkeypatch.setattr(gc, "_STEAM_WINDOWS", {})
    disp = _XDisplay()
    _fake_xlib(monkeypatch, disp)
    disp.windows[6] = _XWindow(disp, 6, "sekiro.exe", steam="814380", title="Sekiro",
                               parent=disp.root)
    disp.active = 6
    wid, cls, _title = gc.frontmost_app()
    assert (wid, cls) == (6, "sekiro.exe") and gc.is_steam_game(wid, cls)

    disp.windows[6] = _XWindow(disp, 6, parent=disp.root, **next_window)
    wid, cls, _title = gc.frontmost_app()
    assert wid == 6 and not gc.is_steam_game(wid, cls)



def test_game_chat_xdotool_presses_nothing_for_a_retired_dictation(monkeypatch):
    # Sol mac r12: under do_inject's scope, no key down and no typing for a
    # retired dictation; a release always goes out.
    from types import SimpleNamespace
    from wayfinder.core import injector
    runs = []
    monkeypatch.setattr(gc.subprocess, "run",
                        lambda argv, **k: runs.append(argv) or SimpleNamespace(
                            returncode=0, stdout="", stderr=""))
    with injector.cancellable(lambda: True):
        for args in (("keydown", "Return"), ("type", "--", "gg")):
            with pytest.raises(injector.InjectionCancelled):
                gc._xdotool(*args)
        assert runs == []
        gc._xdotool("keyup", "Return")
    assert runs == [["xdotool", "keyup", "Return"]]


def _tk_clipboard_app(run):
    """An app whose after(0, fn) hands fn to *run*, as the Tk loop would; the
    clipboard it writes is in *app.clip*."""
    from types import SimpleNamespace
    app = SimpleNamespace(clip=["theirs"])
    app.after = lambda _ms, fn: run(fn)
    app.clipboard_clear = lambda: app.clip.clear()
    app.clipboard_append = lambda text: app.clip.append(text)
    app.update_idletasks = lambda: None
    return app


def test_the_tk_clipboard_write_asks_the_workers_dictation_on_the_tk_thread():
    # Sol mac r13: the portal paste and Linux game chat queue the write on
    # Tk and wait; a reset before Tk ran it still wrote the retired text. The
    # worker's scope does not reach the Tk thread, so the write carries it.
    import threading
    import wayfinder_main as wm
    from wayfinder.core import injector
    retired = [False]

    def tk_later(fn):
        def run():
            retired[0] = True                     # Escape before Tk got to it
            fn()
        threading.Thread(target=run).start()

    app = _tk_clipboard_app(tk_later)
    with injector.cancellable(lambda: retired[0]):
        with pytest.raises(injector.InjectionCancelled):
            wm.WayfinderApp._set_clipboard_from_worker(app, "retired words", 2.0)
    assert app.clip == ["theirs"]


def test_a_tk_clipboard_write_the_worker_gave_up_on_never_runs_late():
    import wayfinder_main as wm
    from wayfinder.core import injector
    queued = []
    app = _tk_clipboard_app(queued.append)        # Tk is busy
    with injector.cancellable(lambda: False):
        assert wm.WayfinderApp._set_clipboard_from_worker(app, "late words", 0.05) is False
    queued[0]()                                   # Tk gets to it after all
    assert app.clip == ["theirs"]


def test_a_live_tk_clipboard_write_goes_out():
    import wayfinder_main as wm
    from wayfinder.core import injector
    app = _tk_clipboard_app(lambda fn: fn())
    with injector.cancellable(lambda: False):
        assert wm.WayfinderApp._set_clipboard_from_worker(app, "live words", 2.0) is True
    assert app.clip == ["live words"]
    assert wm.WayfinderApp._set_clipboard_from_worker(app, "no scope", 2.0) is True
    assert app.clip == ["no scope"]
