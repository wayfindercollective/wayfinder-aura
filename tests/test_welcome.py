"""
Tests for the first-run welcome tour.

Covers the pure ``WelcomeFlow`` state machine and confirms the module imports
headlessly (WelcomeFlow available without touching Tk / creating windows).
"""


class TestWelcomeFlow:
    """State-machine transitions for the 3-step tour."""

    def test_starts_on_mic(self):
        from wayfinder.ui.welcome import WelcomeFlow

        flow = WelcomeFlow()
        assert flow.current == "mic"
        assert flow.index == 0
        assert flow.is_complete is False

    def test_advance_through_all_steps_completes(self):
        from wayfinder.ui.welcome import WelcomeFlow

        seen = []
        flow = WelcomeFlow(on_complete=lambda: seen.append("done"))

        flow.advance()
        assert flow.current == "hotkey"
        flow.advance()
        assert flow.current == "dictate"
        assert flow.is_complete is False
        flow.advance()  # past last step -> complete
        assert flow.is_complete is True
        assert flow.current is None
        assert seen == ["done"]

    def test_skip_from_any_step_completes(self):
        from wayfinder.ui.welcome import WelcomeFlow

        for pre_advances in (0, 1, 2):
            seen = []
            flow = WelcomeFlow(on_complete=lambda: seen.append(1))
            for _ in range(pre_advances):
                flow.advance()
            flow.skip()
            assert flow.is_complete is True
            assert seen == [1], f"on_complete not fired once after {pre_advances} advances"

    def test_on_complete_fires_exactly_once(self):
        from wayfinder.ui.welcome import WelcomeFlow

        calls = []
        flow = WelcomeFlow(on_complete=lambda: calls.append(1))
        flow.skip()
        flow.skip()      # idempotent
        flow.advance()   # no-op after complete
        assert calls == [1]

    def test_complete_is_idempotent(self):
        from wayfinder.ui.welcome import WelcomeFlow

        flow = WelcomeFlow()
        flow.advance()
        flow.advance()
        flow.advance()  # complete
        assert flow.is_complete is True
        flow.advance()  # should not raise or reopen
        assert flow.is_complete is True
        assert flow.current is None

    def test_goto_jumps_to_named_step(self):
        from wayfinder.ui.welcome import WelcomeFlow

        flow = WelcomeFlow()
        flow.goto("dictate")
        assert flow.current == "dictate"
        assert flow.index == 2

    def test_goto_after_complete_is_noop(self):
        from wayfinder.ui.welcome import WelcomeFlow

        flow = WelcomeFlow()
        flow.skip()
        flow.goto("mic")
        assert flow.current is None
        assert flow.is_complete is True


class TestWelcomeModuleImport:
    """The module must be importable and WelcomeFlow usable even when
    customtkinter is unavailable — ctk is imported lazily inside WelcomePane."""

    def test_flow_works_without_customtkinter(self, monkeypatch):
        import importlib
        import sys

        # Force `import customtkinter` to fail anywhere it's attempted, then
        # (re)import the welcome module and exercise WelcomeFlow headlessly.
        monkeypatch.setitem(sys.modules, "customtkinter", None)
        sys.modules.pop("wayfinder.ui.welcome", None)
        mod = importlib.import_module("wayfinder.ui.welcome")

        flow = mod.WelcomeFlow()
        assert flow.current == "mic"
        flow.advance()
        flow.advance()
        flow.advance()
        assert flow.is_complete is True


class TestFitCardSize:
    """The card is capped at the design size and NEVER wider than its host.

    Regression: on a Steam Deck the tab-content cavity is 407x430 px, and the
    card was a hard-coded 520x360 — 113 px of overflow, ~56 px lost off each
    side. Pure function, so this needs no display.
    """

    def test_roomy_host_gets_the_design_size(self):
        from wayfinder.ui.welcome import _CARD_MAX_H, _CARD_MAX_W, fit_card_size

        assert fit_card_size(1100, 700) == (_CARD_MAX_W, _CARD_MAX_H)

    def test_steam_deck_cavity_shrinks_to_fit(self):
        from wayfinder.ui.welcome import _CARD_MARGIN, fit_card_size

        w, h = fit_card_size(407, 430)
        assert w <= 407 - 2 * _CARD_MARGIN
        assert h <= 430 - 2 * _CARD_MARGIN
        assert (w, h) == (383, 360)  # width clamped, height still under the cap

    def test_never_exceeds_the_host_at_any_size(self):
        from wayfinder.ui.welcome import fit_card_size

        for host in (1, 40, 128, 260, 300, 407, 519, 520, 521, 2000):
            w, h = fit_card_size(host, host)
            assert w <= host, f"width {w} overflows host {host}"
            assert h <= host, f"height {h} overflows host {host}"
            assert w >= 1 and h >= 1

    def test_no_minimum_reintroduces_overflow(self):
        """A floor like max(MIN_W, min(MAX_W, avail)) would overflow here."""
        from wayfinder.ui.welcome import fit_card_size

        w, _ = fit_card_size(128, 260)
        assert w < 128

    def test_unmapped_host_falls_back_to_the_design_size(self):
        from wayfinder.ui.welcome import _CARD_MAX_H, _CARD_MAX_W, fit_card_size

        # winfo_width() is 1 (or 0) before the first layout pass; the refit on
        # <Configure> corrects it once real geometry lands.
        assert fit_card_size(0, 0) == (_CARD_MAX_W, _CARD_MAX_H)
        assert fit_card_size(-5, -5) == (_CARD_MAX_W, _CARD_MAX_H)

    def test_importable_without_customtkinter(self, monkeypatch):
        import importlib
        import sys

        monkeypatch.setitem(sys.modules, "customtkinter", None)
        sys.modules.pop("wayfinder.ui.welcome", None)
        mod = importlib.import_module("wayfinder.ui.welcome")

        assert mod.fit_card_size(407, 430)[0] <= 407


class TestRefreshHotkeyDisplay:
    """The portal reports what is bound asynchronously; every surface that
    shows the hotkey token has to be able to catch up.

    Pane-free: the method's decision is exercised through a stand-in so this
    stays a headless test, matching the rest of this module.
    """

    def _pane(self, step, *, help_shown=False, transcript=None, complete=False):
        from types import SimpleNamespace

        from wayfinder.ui.welcome import WelcomeFlow, WelcomePane

        flow = WelcomeFlow()
        flow.goto(step)
        if complete:
            flow.skip()
        rendered = []
        pane = SimpleNamespace(
            flow=flow,
            _help_shown=help_shown,
            _transcript=transcript,
            _render_step=lambda: rendered.append(True),
        )
        pane.refresh_hotkey_display = WelcomePane.refresh_hotkey_display.__get__(
            pane, SimpleNamespace
        )
        return pane, rendered

    def test_refreshes_on_the_hotkey_step(self):
        pane, rendered = self._pane("hotkey")
        pane.refresh_hotkey_display()
        assert rendered == [True]

    def test_refreshes_the_dictate_help_state(self):
        """The troubleshooting variant re-shows the token, so it goes stale too."""
        pane, rendered = self._pane("dictate", help_shown=True)
        pane.refresh_hotkey_display()
        assert rendered == [True]

    def test_skips_the_dictate_step_before_help_appears(self):
        """No token on screen — re-rendering would only risk the help timer."""
        pane, rendered = self._pane("dictate")
        pane.refresh_hotkey_display()
        assert rendered == []

    def test_skips_the_transcript_variant(self):
        pane, rendered = self._pane("dictate", help_shown=True, transcript="hello")
        pane.refresh_hotkey_display()
        assert rendered == []

    def test_skips_the_mic_step(self):
        pane, rendered = self._pane("mic")
        pane.refresh_hotkey_display()
        assert rendered == []

    def test_skips_a_completed_flow(self):
        pane, rendered = self._pane("hotkey", complete=True)
        pane.refresh_hotkey_display()
        assert rendered == []
