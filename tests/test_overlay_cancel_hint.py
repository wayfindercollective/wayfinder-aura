"""The recording pill's cancel hint ("✕ Shift+Esc"), drawn just outside the pill.

Kept apart from tests that import wayfinder_main: on macOS a QApplication
created after Tk/AppKit set-up aborts the process.
"""
from __future__ import annotations

_QT_APP = None

def _overlay(**kwargs):
    import os

    import pytest

    pytest.importorskip("PyQt6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication

    global _QT_APP  # keep a reference: an unreferenced QApplication is collected at once
    _QT_APP = QApplication.instance() or QApplication([])
    from wayfinder.ui import overlay as O

    return O, O.GlassmorphicOverlay(scale=1.0, **kwargs)


def test_overlay_reserves_hint_space_above_the_pill_without_moving_it():
    O, plain = _overlay()
    _, hinted = _overlay(cancel_hint="Shift+Esc")
    try:
        assert plain.hint_space == 0
        assert hinted.widget_height == plain.widget_height + hinted.hint_space
        # Bottom anchor: the strip is on top, so the pill keeps its distance from
        # the widget bottom (which is what positioning anchors to).
        assert hinted.widget_height - hinted.pill_top == plain.widget_height - plain.pill_top
    finally:
        plain.deleteLater()
        hinted.deleteLater()


def test_overlay_hint_goes_below_a_pill_on_the_top_edge():
    O, top = _overlay(cancel_hint="Shift+Esc", anchor="top-right")
    try:
        assert top.hint_above is False
        assert top.pill_top == top.glow_margin  # pill keeps its distance from the top
    finally:
        top.deleteLater()


def test_overlay_hint_rect_sits_outside_the_pill():
    from PyQt6.QtCore import QRectF

    O, ov = _overlay(cancel_hint="Shift+Esc")
    try:
        bar = QRectF(ov.glow_margin, ov.pill_top, 160, ov.scaled_height)
        hint = ov._cancel_hint_rect(bar)
        assert hint.bottom() < bar.top() and hint.top() >= 0
        assert abs(hint.center().x() - bar.center().x()) < 0.5
    finally:
        ov.deleteLater()


def test_overlay_cancel_hint_command_updates_and_clears(monkeypatch):
    O, ov = _overlay()
    try:
        monkeypatch.setattr(ov, "_position_at_bottom", lambda: None)
        ov.set_cancel_hint("Ctrl+Esc")
        assert ov._cancel_hint == "Ctrl+Esc" and ov.height() == ov.widget_height
        ov.set_cancel_hint("")
        assert ov.hint_space == 0
    finally:
        ov.deleteLater()


