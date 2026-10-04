"""Tooltips never strand over another tab (fix for the Windows report).

Switching tabs unmaps the pane with pack_forget(), so <Leave> never fires;
the tab switcher dismisses every live tooltip and the tooltip itself listens
for <Unmap>/<Destroy>.
"""
from __future__ import annotations

from types import SimpleNamespace

import wayfinder_main as wm


class _Widget:
    def __init__(self, mapped=True):
        self.bindings = {}
        self.mapped = mapped
        self.cancelled = []

    def bind(self, event, fn, add=None):
        self.bindings.setdefault(event, []).append(fn)

    def after(self, ms, fn):
        return "timer-1"

    def after_cancel(self, ident):
        self.cancelled.append(ident)

    def winfo_exists(self):
        return True

    def winfo_ismapped(self):
        return self.mapped


def test_tooltip_listens_for_unmap_and_destroy():
    widget = _Widget()
    wm.ToolTip(widget, "hint")
    assert "<Unmap>" in widget.bindings and "<Destroy>" in widget.bindings


def test_hide_all_tooltips_closes_open_tips_and_pending_timers():
    widget = _Widget()
    tip = wm.ToolTip(widget, "hint")
    closed = []
    tip.tooltip_window = SimpleNamespace(destroy=lambda: closed.append(True))
    tip.scheduled_id = "timer-1"

    wm.hide_all_tooltips()

    assert closed == [True] and tip.tooltip_window is None
    assert widget.cancelled == ["timer-1"] and tip.scheduled_id is None


def test_a_timer_firing_after_the_tab_switched_shows_nothing():
    widget = _Widget(mapped=False)
    tip = wm.ToolTip(widget, "hint")
    tip.show_tooltip()
    assert tip.tooltip_window is None


def test_destroyed_tooltip_leaves_the_live_set():
    widget = _Widget()
    tip = wm.ToolTip(widget, "hint")
    tip.on_destroy()
    assert tip not in wm._LIVE_TOOLTIPS
