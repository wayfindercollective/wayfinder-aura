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
