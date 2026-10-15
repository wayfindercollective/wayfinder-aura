"""Game reports in the app: counting dictations through the real
``_inject_into_game_chat()``, and the visibility hook that asks once the
player can see Aura's window (design docs/plans/game-reports-design-2026-10-15.md,
section 4.1). Platform helpers and Tk are faked; the state files are real.
"""

from __future__ import annotations

import inspect
import sys
import time
from types import ModuleType, SimpleNamespace

import pytest

from tests.game_report_fixtures import Transport, http_date, labels_doc
from wayfinder.core import game_labels, game_reports
from wayfinder.core import macos_game_chat as gc


@pytest.fixture(autouse=True)
def _mac_backend_and_temp_state(tmp_path, monkeypatch):
    import wayfinder_main
    monkeypatch.setattr(wayfinder_main, "IS_WINDOWS", False)
    monkeypatch.setattr(wayfinder_main, "_IS_LINUX", False)
    (tmp_path / "config").mkdir()
    (tmp_path / "cache").mkdir()
    monkeypatch.setattr(game_reports, "get_config_dir", lambda: tmp_path / "config")
    monkeypatch.setattr(game_labels, "get_cache_dir", lambda: tmp_path / "cache")
    monkeypatch.setattr(game_reports, "_is_packaged", lambda: True)
    monkeypatch.setattr(game_labels, "_is_packaged", lambda: True)
    monkeypatch.setattr(game_reports, "_send_soon", lambda *a, **k: None)
    monkeypatch.setattr(game_reports, "_withdraw_soon", lambda *a, **k: None)
    monkeypatch.setattr(game_reports, "coarse_os", lambda: ("macos", "15"))
    monkeypatch.setattr(game_reports, "_change_listener", None)


@pytest.fixture
def transport(monkeypatch):
    return Transport(monkeypatch)


def _uses(key="world-of-warcraft") -> int:
    with game_reports._lock:
        return game_reports._load()["games"].get(key, {}).get("uses", 0)


# --- 4. counting through the real _inject_into_game_chat() -----------------------------

def _inject(monkeypatch, frontmost, *, signals=(None, None), switch_after=None):
    import wayfinder_main

    from wayfinder.core import injector
    calls = []
    fronts = iter([frontmost] * 50)

    def front():
        calls.append("front")
        if switch_after is not None and calls.count("front") > switch_after:
            return (999, "com.apple.Terminal", "Terminal")
        return next(fronts)

    monkeypatch.setattr(gc, "frontmost_app", front)
    monkeypatch.setattr(gc, "app_signals", lambda pid: signals)
    monkeypatch.setattr(gc, "OPEN_SETTLE_S", 0)
    monkeypatch.setattr(gc, "PASTE_SETTLE_S", 0)
    monkeypatch.setattr(injector, "inject_text", lambda text, **k: calls.append(("paste", text)))
    monkeypatch.setattr(injector, "press_enter", lambda: calls.append("return"))
    app = SimpleNamespace(config={"game_chat_send": True}, session_generation=3,
                          log=lambda *_: None)
    return wayfinder_main.WayfinderApp._inject_into_game_chat(app, "gg all", 3)


def test_a_dictation_into_a_supported_game_counts(monkeypatch):
    wow = (42, "com.blizzard.worldofwarcraft", "World of Warcraft")
    assert _inject(monkeypatch, wow) is True
    assert _inject(monkeypatch, wow) is True
    assert _uses() == 2


def test_a_not_recommended_game_is_never_counted(monkeypatch):
    assert _inject(monkeypatch, (7, "com.jagex.osrs", "Old School RuneScape")) is False
    assert _uses("runescape") == 0
    assert not game_reports.state_path().exists()


def test_an_unlisted_game_is_never_counted(monkeypatch):
    handled = _inject(monkeypatch, (5, "com.mojang.minecraftlauncher", "Minecraft"),
                      signals=("public.app-category.games", "/Applications/Minecraft.app"))
    assert handled is False
    assert not game_reports.state_path().exists()


def test_an_aborted_send_is_never_counted(monkeypatch):
    import wayfinder_main
    with pytest.raises(wayfinder_main.InjectionError):
        _inject(monkeypatch, (42, "com.blizzard.worldofwarcraft", "World of Warcraft"),
                switch_after=1)
    assert _uses() == 0


# --- 5. the visibility hook --------------------------------------------------------------

class FakeAppKit(ModuleType):
    """NSApplication that is not hidden, and fails the test if activated."""

    def __init__(self, hidden=False):
        super().__init__("AppKit")
        self.hidden = hidden
        outer = self

        class _App:
            def isHidden(self):
                return outer.hidden

            def activateIgnoringOtherApps_(self, _flag):
                raise AssertionError("activated the app")

            def unhide_(self, _sender):
                pass

        class NSApplication:
            @staticmethod
            def sharedApplication():
                return _App()

        self.NSApplication = NSApplication


def _app_class():
    import wayfinder_main
    W = wayfinder_main.WayfinderApp

    class FakeApp:
        _main_window_shown = W._main_window_shown
        _on_main_window_visible = W._on_main_window_visible
        _schedule_main_window_visible = W._schedule_main_window_visible
        _game_report_cold_launch = W._game_report_cold_launch
        _install_game_report_hooks = W._install_game_report_hooks
        _game_report_frontmost_name = W._game_report_frontmost_name
        _on_game_labels_refreshed = W._on_game_labels_refreshed

        def __init__(self, *, minimized=False, viewable=True, state="normal"):
            self.config = {"game_report_prompts": True, "start_minimized": minimized}
            self.app_state = wayfinder_main.AppState.IDLE
            self.event_queue = SimpleNamespace(put=lambda item: self.queued.append(item))
            self.queued, self.jobs, self.binds, self.cards, self.fetches = [], [], [], [], []
            self._viewable, self._state = viewable, state
            self._game_report_card = None

        # Tk
        def after(self, ms, fn):
            self.jobs.append((ms, fn))
            return f"job{len(self.jobs)}"

        def bind(self, sequence, fn, add=None):
            self.binds.append((sequence, fn, add))

        def state(self):
            return self._state

        def winfo_viewable(self):
            return self._viewable

        def focus_force(self):
            raise AssertionError("focus_force")

        def lift(self, *a):
            raise AssertionError("lift")

        def deiconify(self):
            raise AssertionError("deiconify")

        def focus_set(self):
            raise AssertionError("focus_set")

        # UI, faked: the card itself is built by game_report_card
        def _show_game_report_card(self, game):
            self.cards.append(game)
            self._game_report_card = object()

        def _refresh_game_labels(self):
            self.fetches.append("fetch")

        def run_jobs(self):
            jobs, self.jobs = self.jobs, []
            for _ms, fn in jobs:
                fn()

    return FakeApp


@pytest.fixture
def appkit(monkeypatch):
    import wayfinder_main
    fake = FakeAppKit()
    monkeypatch.setitem(sys.modules, "AppKit", fake)
    monkeypatch.setattr(wayfinder_main, "IS_MACOS", True)
    return fake


def _open_intake_now(transport):
    """A fresh labels fetch at the real current time (the hook reads the clock)."""
    now = time.time()
    transport.answer((200, labels_doc(generated_ms=int(now * 1000)), {"Date": http_date(now)}))
    assert game_labels.refresh(now=now)


@pytest.fixture
def eligible(transport, monkeypatch):
    """World of Warcraft played 3 times, intake open, Terminal in front."""
    _open_intake_now(transport)
    for _ in range(3):
        game_reports.note_game_use("World of Warcraft")
    monkeypatch.setattr(gc, "frontmost_app", lambda: (7, "com.apple.Terminal", "Terminal"))


def test_the_cold_launch_reaches_the_hook_and_shows_the_card(appkit, eligible, monkeypatch):
    app = _app_class()()
    app._install_game_report_hooks()
    (cold,) = [fn for _ms, fn in app.jobs]
    assert cold == app._game_report_cold_launch
    assert {seq for seq, _fn, add in app.binds} == {"<Map>", "<FocusIn>"}
    assert all(add == "+" for _seq, _fn, add in app.binds)
    app.run_jobs()
    assert app.cards == [game_reports.PromptGame("world-of-warcraft", "World of Warcraft")]


def test_a_minimized_launch_shows_nothing(appkit, eligible, monkeypatch):
    app = _app_class()(minimized=True)
    app._install_game_report_hooks()
    app.run_jobs()
    assert app.cards == []


def test_map_and_focus_events_reach_it_once_per_burst_from_the_root_only(appkit, eligible,
                                                                        monkeypatch):
    app = _app_class()()
    app._install_game_report_hooks()
    app.jobs.clear()
    handlers = {seq: fn for seq, fn, _add in app.binds}
    child = SimpleNamespace(widget=object())
    handlers["<FocusIn>"](child)
    assert app.jobs == []                                # a child widget: ignored
    for _ in range(3):
        handlers["<Map>"](SimpleNamespace(widget=app))
        handlers["<FocusIn>"](SimpleNamespace(widget=app))
    assert [ms for ms, _fn in app.jobs] == [250]          # coalesced
    app.run_jobs()
    assert len(app.cards) == 1
    handlers["<FocusIn>"](SimpleNamespace(widget=app))
    app.run_jobs()
    assert len(app.cards) == 1                            # a card is already showing


def test_show_window_reaches_it(appkit, eligible, monkeypatch):
    import wayfinder_main
    App = _app_class()

    class Shown(App):
        _show_window = wayfinder_main.WayfinderApp._show_window

        def deiconify(self):
            pass

        def lift(self, *a):
            pass

        def focus_force(self):
            pass

        def attributes(self, *a):
            pass

        def _sync_macos_hero_visibility(self):
            pass

        def _schedule_settings_preload(self, **k):
            pass

    appkit.NSApplication.sharedApplication().__class__.activateIgnoringOtherApps_ = (
        lambda self, flag: None)  # _show_window itself may activate: it's the tray's Open
    app = Shown()
    app._show_window()
    app.jobs = [(ms, fn) for ms, fn in app.jobs if ms == 250]
    assert len(app.jobs) == 1
    app.run_jobs()
    assert len(app.cards) == 1


def test_the_labels_callback_reaches_it_through_the_event_queue(appkit, transport, monkeypatch):
    import wayfinder_main
    app = _app_class()()
    app._on_game_labels_refreshed()
    assert [ms for ms, _fn in app.jobs] == [250]
    source = inspect.getsource(wayfinder_main.WayfinderApp._refresh_game_labels)
    assert "event_queue.put" in source and "UI_CALLBACK" in source
    assert "_on_game_labels_refreshed" in source


def test_the_hook_never_focuses_raises_or_activates(appkit, eligible, monkeypatch):
    app = _app_class()()
    app._on_main_window_visible()        # FakeApp/FakeAppKit raise on any of them
    assert len(app.cards) == 1


@pytest.mark.parametrize("why", ["hidden", "withdrawn", "iconic", "unviewable", "busy",
                                 "switch_off", "game_mode"])
def test_the_hook_does_nothing_unless_the_window_is_really_shown_and_idle(appkit, eligible,
                                                                         monkeypatch, why):
    import wayfinder_main
    app = _app_class()(viewable=why != "unviewable",
                       state={"withdrawn": "withdrawn", "iconic": "iconic"}.get(why, "normal"))
    appkit.hidden = why == "hidden"
    if why == "busy":
        app.app_state = wayfinder_main.AppState.RECORDING
    if why == "switch_off":
        app.config["game_report_prompts"] = False
    if why == "game_mode":
        app._game_mode_session = True
    app._on_main_window_visible()
    assert app.cards == []


def test_a_delayed_labels_callback_after_the_game_is_back_in_front_shows_no_card(
        appkit, transport, monkeypatch):
    for _ in range(3):
        game_reports.note_game_use("World of Warcraft")
    monkeypatch.setattr(gc, "frontmost_app", lambda: (7, "com.apple.Terminal", "Terminal"))
    app = _app_class()()
    app._on_main_window_visible()                         # never fetched: ask for labels
    assert app.fetches == ["fetch"] and app.cards == []
    # The fetch finishes while the player has gone back to the game.
    _open_intake_now(transport)
    monkeypatch.setattr(gc, "frontmost_app",
                        lambda: (42, "com.blizzard.worldofwarcraft", "World of Warcraft"))
    app._on_game_labels_refreshed()
    app.run_jobs()
    assert app.cards == []
    # The next visibility event, with Aura in front again, asks.
    monkeypatch.setattr(gc, "frontmost_app", lambda: (7, "com.apple.Terminal", "Terminal"))
    app._schedule_main_window_visible()
    app.run_jobs()
    assert [g.key for g in app.cards] == ["world-of-warcraft"]


def test_entry_points_are_wired_in_the_app():
    import wayfinder_main
    W = wayfinder_main.WayfinderApp
    assert "self._install_game_report_hooks()" in inspect.getsource(W.__init__)
    assert "_schedule_main_window_visible" in inspect.getsource(W._show_window)
    assert "_schedule_main_window_visible" in inspect.getsource(W._on_macos_app_visibility_changed)
    crash_pass = inspect.getsource(W._start_crash_report_pass)
    assert "game_reports" in crash_pass and "retry_pending" in crash_pass
    assert "_refresh_game_labels" in inspect.getsource(W._switch_tab)


FORBIDDEN = ("focus_force", ".lift(", "deiconify", "activateIgnoringOtherApps_", "focus_set",
             "CTkToplevel", "Toplevel(", "bell(", "notify")


def test_no_card_code_takes_focus_or_opens_a_window():
    import wayfinder_main

    from wayfinder.ui import game_report_card
    W = wayfinder_main.WayfinderApp
    sources = [inspect.getsource(game_report_card)] + [
        inspect.getsource(getattr(W, name)) for name in (
            "_on_main_window_visible", "_show_game_report_card", "_close_game_report_card",
            "_schedule_main_window_visible", "_main_window_shown", "_game_report_cold_launch")]
    for source in sources:
        for word in FORBIDDEN:
            assert word not in source, word


def test_the_switch_is_on_by_default_with_a_tooltip():
    import wayfinder_main

    from wayfinder.config import DEFAULT_CONFIG
    assert DEFAULT_CONFIG["game_report_prompts"] is True
    tip = wayfinder_main.SETTING_TOOLTIPS["game_report_prompts"]
    assert "never over the game" in tip and "Nothing is sent unless you press Send" in tip
    assert "Ask how games went" in inspect.getsource(wayfinder_main.WayfinderApp._create_game_chat_rows)


def test_the_card_packs_under_the_update_banner():
    import wayfinder_main
    source = inspect.getsource(wayfinder_main.WayfinderApp._show_game_report_card)
    assert "before=" in source and "_dictate_banner_anchor" in source
    banner = inspect.getsource(wayfinder_main.WayfinderApp._show_app_update_banner)
    assert "_game_report_card" in banner


@pytest.mark.ui
def test_the_games_tab_builds_with_community_labels_and_your_reports(transport, monkeypatch):
    """The real _create_games_tab() in a Tk window (needs a display)."""
    import customtkinter as ctk
    import wayfinder_main

    from wayfinder.ui import icons
    from wayfinder.ui.ui_inspect import describe, flatten
    monkeypatch.setattr(icons, "_CACHE", {})  # icons belong to one Tk root
    os_family = ("macos" if wayfinder_main.IS_MACOS else
                 "windows" if wayfinder_main.IS_WINDOWS else "linux")
    now = time.time()
    doc = labels_doc(generated_ms=int(now * 1000), games=[{
        "key": "world-of-warcraft", "os": os_family, "complete": True,
        "lines": [{"line": "1.2", "level": "works", "verified": False, "sourceLine": "1.2",
                   "carried": False, "works": 3, "someIssues": 0, "doesntWork": 0}]}])
    transport.answer((200, doc, {"Date": http_date(now)}))
    assert game_labels.refresh(now=now)
    monkeypatch.setattr(wayfinder_main, "__version__", "1.2.0", raising=False)
    game_reports.submit("World of Warcraft", "works", "Fine.")

    root = ctk.CTk()
    try:
        W = wayfinder_main.WayfinderApp
        container = ctk.CTkFrame(root)
        container.pack(fill="both", expand=True)
        app = SimpleNamespace(
            tab_content_container=container, tab_frames={}, font_body=("DejaVu Sans",),
            font_header=("DejaVu Sans",), font_sizes=dict.fromkeys(("display", "title", "heading", "body", "small", "caption"), 12),
            after=root.after, after_cancel=root.after_cancel,
            event_queue=SimpleNamespace(put=lambda item: None),
            _create_game_chat_rows=lambda parent: None)
        app._post_ui = lambda fn: fn()
        W._create_games_tab(app)
        app.tab_frames["games"].pack(fill="both", expand=True)
        root.geometry("620x900")
        root.update()
        search = next(w for w in _walk(app.tab_frames["games"]) if type(w).__name__ == "CTkEntry")
        search.insert(0, "warcraft")      # 85 games, 40 shown: search like a player
        root.update()
        time.sleep(0.3)                   # let the search's one-shot render (180 ms) run
        root.update()
        nodes = flatten(describe(root, root))
        texts = [n["text"] for n in nodes if n.get("text")]
        for expected in ("Works with (community)", "Community",
                         "Played one of these? Click it to tell us how Aura worked.",
                         "3 anonymous reporters say it works on Aura 1.2.",
                         "You said: Works", "Your reports: 1. See or withdraw."):
            assert expected in texts, expected
        # Clicking the World of Warcraft row opens the card under it.
        label = next(w for w in _walk(app.tab_frames["games"])
                     if type(w).__name__ == "CTkLabel" and w.cget("text") == "World of Warcraft")
        label._label.event_generate("<Button-1>")
        root.update()
        texts = [n["text"] for n in flatten(describe(root, root)) if n.get("text")]
        assert "How did Aura work in World of Warcraft?" in texts
        assert "Don't ask again" not in texts             # only when Aura asks
        label._label.event_generate("<Button-1>")   # a second click closes it
        root.update()
        # Intake closed: no new-report UI at all, but Your reports stays.
        closed = dict(doc, accepting=False, generatedAt=int(time.time() * 1000))
        transport.answer((200, closed, {"Date": http_date(time.time())}))
        assert game_labels.refresh(force=True)
        app._games_tab_render()
        root.update()
        label = next(w for w in _walk(app.tab_frames["games"])
                     if type(w).__name__ == "CTkLabel" and w.cget("text") == "World of Warcraft")
        label._label.event_generate("<Button-1>")
        root.update()
        texts = [n["text"] for n in flatten(describe(root, root))
                 if n.get("text") and n.get("mapped", True)]
        assert "Played one of these? Click it to tell us how Aura worked." not in texts
        assert "How did Aura work in World of Warcraft?" not in texts
        assert "Your reports: 1. See or withdraw." in texts
        assert "3 anonymous reporters say it works on Aura 1.2." in texts  # labels still show
    finally:
        root.destroy()


def _walk(widget):
    yield widget
    for child in widget.winfo_children():
        yield from _walk(child)


def test_showing_the_card_counts_as_asked_so_it_never_returns(monkeypatch):
    """PRIVACY.md: Aura "never asks twice about the same game", even if the
    player quits with the card still showing."""
    import wayfinder_main
    shown = []

    class FakeCard:
        def __init__(self, *a, **k):
            shown.append(a[2])  # (parent, app, key, name)

        def pack(self, **k):
            pass

    import wayfinder.ui.game_report_card as card_module
    monkeypatch.setattr(card_module, "GameReportCard", FakeCard)
    for _ in range(3):
        game_reports.note_game_use("World of Warcraft")
    app = SimpleNamespace(_dictate_banner_anchor=SimpleNamespace(master=object()),
                          _post_ui=lambda fn: None, _close_game_report_card=lambda: None,
                          _game_reports_dont_ask=lambda: None)
    wayfinder_main.WayfinderApp._show_game_report_card(
        app, game_reports.PromptGame("world-of-warcraft", "World of Warcraft"))
    assert shown == ["world-of-warcraft"]
    with game_reports._lock:
        assert game_reports._load()["games"]["world-of-warcraft"]["asked"] is True
