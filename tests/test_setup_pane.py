"""Tests for the first-run dependency setup pane.

Covers the pure ``SetupFlow`` state machine, the pure footer/model-selector
decision helpers, and the pure first-run orchestration (``first_run_plan`` /
``should_chain_welcome``) — the logic that used to live inline in the modal
``SetupWizard`` and in main.py. Everything here is headless (no Tk / no window);
customtkinter is imported lazily inside ``SetupPane`` only, mirroring
``wayfinder.ui.welcome``.
"""

from dataclasses import dataclass


@dataclass
class _FakeDep:
    """Minimal stand-in for core.setup.Dependency for the pure footer tests."""
    id: str
    required: bool
    is_ok: bool
    status: object = None  # DependencyStatus in prod; None here = no blocking warning


# ─── SetupFlow state machine ─────────────────────────────────────────────────


class TestSetupFlow:
    def test_starts_incomplete(self):
        from wayfinder.ui.setup_pane import SetupFlow

        flow = SetupFlow()
        assert flow.is_complete is False
        assert flow.result is False

    def test_complete_sets_result_true_and_fires_once(self):
        from wayfinder.ui.setup_pane import SetupFlow

        seen = []
        flow = SetupFlow(on_complete=lambda: seen.append("done"))
        flow.complete()
        assert flow.is_complete is True
        assert flow.result is True
        assert seen == ["done"]

    def test_skip_sets_result_false_and_fires_once(self):
        from wayfinder.ui.setup_pane import SetupFlow

        seen = []
        flow = SetupFlow(on_complete=lambda: seen.append(1))
        flow.skip()
        assert flow.is_complete is True
        assert flow.result is False
        assert seen == [1]

    def test_on_complete_fires_exactly_once(self):
        from wayfinder.ui.setup_pane import SetupFlow

        calls = []
        flow = SetupFlow(on_complete=lambda: calls.append(1))
        flow.complete()
        flow.complete()   # idempotent
        flow.skip()       # no-op after complete — result stays True
        assert calls == [1]
        assert flow.result is True

    def test_skip_then_complete_keeps_skip_result(self):
        from wayfinder.ui.setup_pane import SetupFlow

        flow = SetupFlow()
        flow.skip()
        flow.complete()   # no-op: first outcome wins
        assert flow.result is False
        assert flow.is_complete is True

    def test_no_callback_is_fine(self):
        from wayfinder.ui.setup_pane import SetupFlow

        flow = SetupFlow()
        flow.complete()  # must not raise without on_complete
        assert flow.is_complete is True


# ─── SetupSequence auto-install state machine ────────────────────────────────


class TestSetupSequence:
    """The pure, headless sequencing that drives the guided auto-install — where
    the step ordering / advance / pause / retry / cancel logic lives (NOT in
    widget code), so the new auto-advance flow is unit-testable."""

    def _seq(self, *ids):
        from wayfinder.ui.setup_pane import SetupSequence

        return SetupSequence(list(ids) or ["system_packages", "whisper_cpp", "whisper_model"])

    def test_empty_sequence_is_immediately_finished(self):
        from wayfinder.ui.setup_pane import SetupSequence

        seq = SetupSequence([])
        assert seq.start() is None
        assert seq.finished is True

    def test_starts_queued_then_activates_first(self):
        seq = self._seq("a", "b", "c")
        assert seq.finished is False
        assert seq.start() == "a"
        assert seq.active == "a"
        assert seq.status["a"] == "active"
        assert seq.remaining() == ["b", "c"]  # active not counted as remaining

    def test_advances_through_all_steps_then_finishes(self):
        seq = self._seq("a", "b", "c")
        assert seq.start() == "a"
        assert seq.on_success("a") == "b"
        assert seq.on_success("b") == "c"
        assert seq.on_success("c") is None
        assert seq.finished is True
        assert all(v == "done" for v in seq.status.values())

    def test_position_is_one_based(self):
        seq = self._seq("a", "b", "c")
        assert seq.position("a") == (1, 3)
        assert seq.position("c") == (3, 3)

    def test_failure_pauses_and_does_not_advance(self):
        seq = self._seq("a", "b")
        seq.start()
        seq.on_failure("a")
        assert seq.active == "a"          # stays on the failed step
        assert seq.status["a"] == "paused"
        assert seq.finished is False      # paused ≠ finished

    def test_retry_reactivates_paused_step(self):
        seq = self._seq("a", "b")
        seq.start()
        seq.on_failure("a")
        assert seq.retry() == "a"
        assert seq.status["a"] == "active"
        assert seq.on_success("a") == "b"  # resumes the queue

    def test_skip_step_advances_past_a_paused_step(self):
        seq = self._seq("a", "b")
        seq.start()
        seq.on_failure("a")
        assert seq.skip_step() == "b"
        assert seq.status["a"] == "skipped"
        assert seq.on_success("b") is None
        assert seq.finished is True

    def test_skip_last_paused_step_finishes(self):
        seq = self._seq("a")
        seq.start()
        seq.on_failure("a")
        assert seq.skip_step() is None
        assert seq.finished is True

    def test_cancel_skips_remaining_and_finishes(self):
        seq = self._seq("a", "b", "c")
        seq.start()
        seq.on_success("a")   # b now active
        seq.cancel()
        assert seq.finished is True
        assert seq.cancelled is True
        assert seq.status["c"] == "skipped"   # queued → skipped
        # No further step starts after cancel.
        assert seq._activate_next() is None


# ─── Pure footer / model-selector decisions ──────────────────────────────────


class TestFooterDecision:
    def _deps(self, *specs):
        # spec = (id, required, is_ok)
        return [_FakeDep(*s) for s in specs]

    def test_all_required_ok_true_when_optionals_missing(self):
        from wayfinder.ui.setup_pane import all_required_ok

        deps = self._deps(("audio", True, True), ("gpu", False, False))
        assert all_required_ok(deps) is True

    def test_all_required_ok_false_when_a_required_missing(self):
        from wayfinder.ui.setup_pane import all_required_ok

        deps = self._deps(("audio", True, False), ("gpu", False, True))
        assert all_required_ok(deps) is False

    def test_any_missing_counts_optionals(self):
        from wayfinder.ui.setup_pane import any_missing

        deps = self._deps(("audio", True, True), ("gpu", False, False))
        assert any_missing(deps) is True

    def test_footer_missing_required_hides_continue_shows_install(self):
        from wayfinder.ui.setup_pane import footer_decision

        deps = self._deps(("audio", True, False), ("whisper_model", True, False))
        d = footer_decision(deps, installing=False)
        assert d == {"show_continue": False, "show_install": True, "install_enabled": True}

    def test_footer_installing_disables_install_button(self):
        from wayfinder.ui.setup_pane import footer_decision

        deps = self._deps(("audio", True, False))
        d = footer_decision(deps, installing=True)
        assert d["show_continue"] is False
        assert d["show_install"] is True
        assert d["install_enabled"] is False

    def test_footer_required_ok_but_optional_missing_shows_both(self):
        from wayfinder.ui.setup_pane import footer_decision

        # All required satisfied; an optional is still missing -> Continue AND a
        # still-clickable Install button (user may install the optional).
        deps = self._deps(("audio", True, True), ("gpu", False, False))
        d = footer_decision(deps, installing=False)
        assert d == {"show_continue": True, "show_install": True, "install_enabled": True}

    def test_footer_everything_ok_hides_install(self):
        from wayfinder.ui.setup_pane import footer_decision

        deps = self._deps(("audio", True, True), ("whisper_model", True, True))
        d = footer_decision(deps, installing=False)
        assert d == {"show_continue": True, "show_install": False, "install_enabled": False}

    def test_required_dep_with_blocking_warning_is_not_ok(self):
        # F11: a required dep that's "installed" but carries a blocking warning
        # (e.g. the ydotool binary is present but its daemon is down, so typing
        # will actually fail) must NOT satisfy the gate — otherwise Setup declares
        # "All set!" while injection is silently broken.
        from types import SimpleNamespace
        from wayfinder.ui.setup_pane import all_required_ok

        ok_dep = _FakeDep("audio", True, True)
        blocked = _FakeDep("injection", True, True,
                           status=SimpleNamespace(blocking_warning=True))
        assert all_required_ok([ok_dep, blocked]) is False
        # The same dep without the blocking warning -> gate passes.
        unblocked = _FakeDep("injection", True, True,
                             status=SimpleNamespace(blocking_warning=False))
        assert all_required_ok([ok_dep, unblocked]) is True

    def test_model_selector_visible_until_model_ok(self):
        from wayfinder.ui.setup_pane import should_show_model_selector

        missing = self._deps(("whisper_model", True, False))
        present = self._deps(("whisper_model", True, True))
        no_model = self._deps(("audio", True, True))
        assert should_show_model_selector(missing) is True
        assert should_show_model_selector(present) is False
        assert should_show_model_selector(no_model) is False


# ─── Pure first-run orchestration (main.py branching) ────────────────────────


class TestFirstRunPlan:
    def test_fresh_install_shows_setup_first(self):
        from wayfinder.ui.setup_pane import first_run_plan

        plan = first_run_plan(setup_completed=False, welcome_completed=False, frozen=False)
        assert plan == {"show_setup": True, "show_welcome": False}

    def test_setup_done_welcome_pending_shows_welcome_directly(self):
        from wayfinder.ui.setup_pane import first_run_plan

        plan = first_run_plan(setup_completed=True, welcome_completed=False, frozen=False)
        assert plan == {"show_setup": False, "show_welcome": True}

    def test_all_done_shows_nothing(self):
        from wayfinder.ui.setup_pane import first_run_plan

        plan = first_run_plan(setup_completed=True, welcome_completed=True, frozen=False)
        assert plan == {"show_setup": False, "show_welcome": False}

    def test_frozen_skips_setup_but_runs_welcome(self):
        from wayfinder.ui.setup_pane import first_run_plan

        # Frozen bundles deps + crashes on package-manager probes -> never show
        # setup; welcome (pure UI) still runs on first launch.
        plan = first_run_plan(setup_completed=False, welcome_completed=False, frozen=True)
        assert plan == {"show_setup": False, "show_welcome": True}

    def test_chain_welcome_only_when_pending(self):
        from wayfinder.ui.setup_pane import should_chain_welcome

        assert should_chain_welcome(welcome_completed=False) is True
        assert should_chain_welcome(welcome_completed=True) is False


# ─── Headless import (no customtkinter) ──────────────────────────────────────


class TestSetupPaneModuleImport:
    """The module must import and its pure logic must work even when
    customtkinter is unavailable — ctk is imported lazily inside SetupPane."""

    def test_flow_works_without_customtkinter(self, monkeypatch):
        import importlib
        import sys

        monkeypatch.setitem(sys.modules, "customtkinter", None)
        sys.modules.pop("wayfinder.ui.setup_pane", None)
        mod = importlib.import_module("wayfinder.ui.setup_pane")

        flow = mod.SetupFlow()
        flow.complete()
        assert flow.is_complete is True
        assert flow.result is True
        assert mod.first_run_plan(False, False, False)["show_setup"] is True
