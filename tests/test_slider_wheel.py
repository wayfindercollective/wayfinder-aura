"""A slider inside a scrolling settings page must not eat the mouse wheel.

Field report: scrolling Settings in fullscreen made the UI zoom jump "from 200%
to 190%" and the page appear to reload. CTkSlider binds <MouseWheel>/<Button-4>/
<Button-5> onto its own internal canvas; widget-level bindings run BEFORE
bind_all, and CTk's handler does not return "break", so the page scrolled AND
the slider moved. Three other sliders (overlay scale, overlay position, ducking
percent) were changing silently the whole time.
"""

import ast
import os
import shutil
from pathlib import Path

import pytest

MAIN = Path(__file__).resolve().parents[1] / "wayfinder_main.py"


class TestTheNeuterIsInstalledBeforeAnySliderExists:
    """Bindings capture the bound method at bind time, so patching the class
    after a slider has been constructed cannot retroactively change it — the
    same trap already documented for CTkScrollableFrame in this file."""

    def _source(self):
        return MAIN.read_text()

    def test_the_class_handler_is_neutered(self):
        assert "ctk.CTkSlider._mouse_scroll_event = lambda" in self._source()

    def test_it_happens_at_import_time_not_inside_a_function(self):
        """Module scope is the whole point: inside a method it would run after
        setup_ui() had already built (and bound) every slider."""
        tree = ast.parse(self._source())
        at_module_scope = any(
            isinstance(n, ast.Assign)
            and any(getattr(t, "attr", None) == "_mouse_scroll_event" for t in n.targets)
            for n in tree.body
        )
        assert at_module_scope, "the neuter is not at module scope"

    def test_it_precedes_every_slider_construction(self):
        src = self._source()
        neuter = src.index("ctk.CTkSlider._mouse_scroll_event = lambda")
        first_slider = src.index("ctk.CTkSlider(")
        assert neuter < first_slider, (
            "a slider is constructed before the neuter runs; its canvas would "
            "capture CTk's real handler and keep eating the wheel")


@pytest.mark.skipif(
    not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")),
    reason="needs a display: Tk only dispatches event_generate to mapped widgets",
)
class TestWheelOverASliderDoesNotMoveIt:
    """Runtime proof. Verified to FAIL without the neuter: one Button-4/5 notch
    moves a 0.7-2.5 slider by 1/20 of its range = 0.09, i.e. 2.00 -> 2.09, which
    the snap-to-5% display renders as a 10-point jump."""

    def _slider(self, neuter):
        ctk = pytest.importorskip("customtkinter")
        if neuter:
            ctk.CTkSlider._mouse_scroll_event = lambda _self, _event: None
        root = ctk.CTk()
        # Mapped, or event_generate is silently dropped — the first version of
        # this test used a withdrawn window and BOTH arms read "no change",
        # which looks like a pass and proves nothing.
        root.geometry("300x80+3000+3000")
        slider = ctk.CTkSlider(root, from_=0.7, to=2.5)
        slider.pack(fill="x")
        root.update()
        return root, slider

    def test_a_wheel_notch_leaves_the_value_alone(self):
        ctk = pytest.importorskip("customtkinter")
        original = ctk.CTkSlider._mouse_scroll_event
        root = None
        try:
            root, slider = self._slider(neuter=True)
            slider.set(2.0)
            root.update()
            for seq, kw in (("<Button-4>", {}), ("<Button-5>", {}),
                            ("<MouseWheel>", {"delta": -120})):
                slider._canvas.event_generate(seq, x=5, y=5, when="now", **kw)
                root.update()
                assert slider.get() == pytest.approx(2.0), f"{seq} moved the slider"
        finally:
            ctk.CTkSlider._mouse_scroll_event = original
            if root is not None:
                root.destroy()

    def test_the_control_arm_still_reproduces_the_bug(self):
        """Guards the test itself: if CTk ever stops binding the wheel, the test
        above would pass for the wrong reason and quietly stop protecting us."""
        ctk = pytest.importorskip("customtkinter")
        original = ctk.CTkSlider._mouse_scroll_event
        root = None
        try:
            root, slider = self._slider(neuter=False)
            slider.set(2.0)
            root.update()
            slider._canvas.event_generate("<Button-5>", x=5, y=5, when="now")
            root.update()
            moved = abs(slider.get() - 2.0)
            if moved == 0:
                pytest.skip("this CTk build no longer reacts to a synthetic wheel")
            assert moved == pytest.approx(0.09, abs=0.02), (
                f"expected ~1/20 of the range, got {moved}")
        finally:
            ctk.CTkSlider._mouse_scroll_event = original
            if root is not None:
                root.destroy()
