"""Windows pill topmost keeper (20 Oct 2026: the pill, still flagged topmost,
was ranked below ordinary windows and hidden behind them)."""
import ctypes
import sys
from pathlib import Path

import pytest

# The keeper's logic is pure Python driven through a faked user32 (FakeUser32
# below), so it's tested that way on macOS/Linux too — only the two tests
# that need a real HWND/windll are Windows-only.
windows_only = pytest.mark.skipif(not hasattr(ctypes, "windll"), reason="Windows only")

ROOT = Path(__file__).resolve().parents[1]
PILL = 100


class FakeUser32:
    """A z-order (top first) of (hwnd, visible, topmost[, cloaked])."""

    def __init__(self, stack, sticky=False):
        self.stack = [tuple(w) + (False,) * (4 - len(w)) for w in stack]
        self.restored = []
        self.sticky = sticky  # True: restoring doesn't move the pill (it gets pushed back)

    def _index(self, h):
        return [w[0] for w in self.stack].index(h)

    def GetTopWindow(self, _):
        return self.stack[0][0] if self.stack else None

    def GetWindow(self, h, _cmd):
        i = self._index(h) + 1
        return self.stack[i][0] if i < len(self.stack) else None

    def IsWindowVisible(self, h):
        return self.stack[self._index(h)][1]

    def GetWindowLongW(self, h, _idx):
        return 0x8 if self.stack[self._index(h)][2] else 0

    def SetWindowPos(self, h, after, *_rest):
        self.restored.append((h, after))
        if not self.sticky:
            entry = self.stack.pop(self._index(h))
            # after=HWND_TOPMOST (-1, never a real hwnd here): straight to the
            # top, as before. A real hwnd: insert directly behind it, matching
            # Win32's hWndInsertAfter semantics.
            ids = [w[0] for w in self.stack]
            at = ids.index(after) + 1 if after in ids else 0
            self.stack.insert(at, entry)
        return 1


@pytest.fixture
def wt(monkeypatch):
    from wayfinder.ui import windows_topmost
    return windows_topmost


def use(monkeypatch, wt, stack, sticky=False):
    fake = FakeUser32(stack, sticky)
    monkeypatch.setattr(wt, "_user32", fake)
    monkeypatch.setattr(wt, "_cloaked", lambda h: fake.stack[fake._index(h)][3])
    return fake


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def keeper_for(wt, reports, clock=None, front="Code.exe"):
    return wt.TopmostKeeper(lambda: PILL, reports.append, clock=clock or Clock(),
                            front=lambda: front)


def test_healthy_pill_has_nothing_above_it(monkeypatch, wt):
    use(monkeypatch, wt, [(1, True, True), (PILL, True, True), (2, True, False)])
    assert wt.normal_windows_above(PILL) == 0


def test_demoted_pill_counts_visible_ordinary_windows_only(monkeypatch, wt):
    use(monkeypatch, wt, [(1, True, True),      # Magnifier-style topmost: fine
                          (2, True, False),     # VS Code
                          (3, False, False),    # hidden: doesn't count
                          (4, True, False),     # Explorer
                          (PILL, True, True)])
    assert wt.normal_windows_above(PILL) == 2


def test_missing_pill_reads_as_not_demoted(monkeypatch, wt):
    use(monkeypatch, wt, [(1, True, False), (2, True, False)])
    assert wt.normal_windows_above(PILL) == 0


def test_cloaked_windows_do_not_count(monkeypatch, wt):
    use(monkeypatch, wt, [(2, True, False, True),   # other virtual desktop
                          (PILL, True, True)])
    assert wt.normal_windows_above(PILL) == 0


def test_keeper_restores_without_activating_and_reports_at_once(monkeypatch, wt):
    fake = use(monkeypatch, wt, [(2, True, False), (PILL, True, True)])
    reports = []
    keeper = keeper_for(wt, reports)
    assert keeper.check() == 1
    assert fake.restored == [(PILL, wt._HWND_TOPMOST)]
    assert reports == [{"restores": 1, "above": 1, "fronts": ["Code.exe"], "held": True}]
    assert keeper.check() == 0          # back on top: nothing more to do
    assert len(reports) == 1


def test_restore_never_places_the_pill_above_a_legitimate_topmost_window(monkeypatch, wt):
    """20 Oct: with [legit topmost A, ordinary B, pill], HWND_TOPMOST always
    goes to the very top, giving [pill, A, B] and un-topping A. The repair
    must land the pill just below A instead, leaving A on top."""
    fake = use(monkeypatch, wt, [(1, True, True), (2, True, False), (PILL, True, True)])
    reports = []
    keeper = keeper_for(wt, reports)
    assert keeper.check() == 1               # only ordinary B (2) counts as "above"
    assert fake.restored == [(PILL, 1)]       # inserted just below A (hwnd 1), not HWND_TOPMOST
    order = [w[0] for w in fake.stack]
    assert order.index(1) < order.index(PILL) < order.index(2)   # A, pill, B
    assert reports[-1]["held"] is True


def test_later_restores_are_batched_and_flushed_in_a_quiet_spell(monkeypatch, wt):
    fake = use(monkeypatch, wt, [(2, True, False), (PILL, True, True)])
    clock, reports = Clock(), []
    fronts = iter(["Code.exe", "Slack.exe", "explorer.exe"])
    keeper = wt.TopmostKeeper(lambda: PILL, reports.append, clock=clock,
                              front=lambda: next(fronts))
    keeper.check()                      # reported at once
    for step in (12, 12):               # two more demotions inside 30 s
        clock.t += step
        fake.stack.insert(0, fake.stack.pop(fake._index(2)))
        keeper.check()
    assert len(reports) == 1
    clock.t += 10                       # no demotion, but 30 s since the report
    keeper.check()
    assert reports[1] == {"restores": 2, "above": 1, "fronts": ["Slack.exe", "explorer.exe"],
                          "held": True}


def test_restore_that_does_not_hold_backs_off_for_a_minute(monkeypatch, wt):
    fake = use(monkeypatch, wt, [(2, True, False), (PILL, True, True)], sticky=True)
    clock, reports = Clock(), []
    keeper = keeper_for(wt, reports, clock)
    keeper.check()
    assert reports[-1]["held"] is False
    for _ in range(59):
        clock.t += 1
        keeper.check()
    assert len(fake.restored) == 1      # no fight every second
    clock.t += 2
    keeper.check()
    assert len(fake.restored) == 2      # tries again after the pause


def test_restores_that_hold_are_never_paused(monkeypatch, wt):
    """Demoted on every window switch (20 Oct): every restore must go ahead,
    or the pill would stay hidden behind the window just switched to."""
    fake = use(monkeypatch, wt, [(2, True, False), (PILL, True, True)])
    clock, reports = Clock(), []
    keeper = keeper_for(wt, reports, clock)
    for _ in range(20):                     # 20 switches in 4 s
        fake.stack.insert(0, fake.stack.pop(fake._index(2)))
        assert keeper.check() == 1
        assert wt.normal_windows_above(PILL) == 0
        clock.t += 0.2
    assert len(fake.restored) == 20
    assert all(r["held"] for r in reports)


def test_delayed_redemotion_against_an_unchanged_foreground_backs_off(monkeypatch, wt):
    """20 Oct: the sticky=True test above only models a restore that fails to
    hold AT ONCE. Here every restore holds the instant check() verifies it,
    but the same foreground window re-demotes the pill again before the next
    tick — a mock with an unchanged foreground produced 120 restores in 120 s
    with no back-off (window-order fighting, flicker)."""
    fake = use(monkeypatch, wt, [(2, True, False), (PILL, True, True)])
    clock, reports = Clock(), []
    keeper = keeper_for(wt, reports, clock)   # front="Code.exe" the whole time
    for _ in range(120):                      # re-demoted once a second for 2 minutes
        fake.stack.insert(0, fake.stack.pop(fake._index(2)))
        keeper.check()
        clock.t += 1.0
    assert len(fake.restored) < 20            # backed off well short of fighting every tick
    assert all(r["held"] for r in reports)    # each individual restore still held when checked


def test_contention_resets_when_the_foreground_changes(monkeypatch, wt):
    """Switching to a different app must get a clean slate, not inherit the
    previous app's back-off — the contention is specific to one offender."""
    fake = use(monkeypatch, wt, [(2, True, False), (PILL, True, True)])
    clock, reports = Clock(), []
    fronts = iter(["Code.exe"] * 11 + ["Slack.exe"])
    keeper = wt.TopmostKeeper(lambda: PILL, reports.append, clock=clock,
                              front=lambda: next(fronts))
    for _ in range(11):                       # just long enough to trip the back-off
        fake.stack.insert(0, fake.stack.pop(fake._index(2)))
        keeper.check()
        clock.t += 1.0
    assert clock.t < keeper._paused_until     # Code.exe tripped the back-off
    fake.stack.insert(0, fake.stack.pop(fake._index(2)))
    assert keeper.check() == 1                # Slack.exe gets a fresh try, not the pause
    assert fake.restored[-1] == (PILL, wt._HWND_TOPMOST)


def test_keeper_leaves_a_hidden_pill_alone(monkeypatch, wt):
    fake = use(monkeypatch, wt, [(2, True, False), (PILL, True, True)])
    keeper = wt.TopmostKeeper(lambda: None, lambda info: None)
    assert keeper.check() == 0
    assert fake.restored == []


def test_keeper_never_raises(monkeypatch, wt):
    def boom():
        raise RuntimeError("winId gone")
    keeper = wt.TopmostKeeper(boom, lambda info: None)
    assert keeper.check() == 0


def test_restore_flags_never_activate_move_or_resize(wt):
    assert wt._RESTORE_FLAGS == 0x213   # NOSIZE | NOMOVE | NOACTIVATE | NOOWNERZORDER


@windows_only
def test_real_foreground_hook_installs_and_uninstalls(wt):
    keeper = wt.TopmostKeeper(lambda: None, lambda info: None)
    assert keeper.install_foreground_hook()
    keeper.uninstall()
    assert keeper._hook is None


@windows_only
def test_real_z_order_walk_finds_a_real_window(wt):
    import tkinter as tk

    root = tk.Tk()
    try:
        root.attributes("-topmost", True)
        root.update()
        hwnd = int(root.wm_frame(), 16)
        assert wt.normal_windows_above(hwnd) == 0
    finally:
        root.destroy()


def test_overlay_wires_the_keeper_on_windows_only():
    src = (ROOT / "src" / "wayfinder" / "ui" / "overlay.py").read_text(encoding="utf-8")
    block = src.split('if sys.platform == "win32" and not tray_only:', 1)[1].split(
        "# Send ready signal.", 1)[0]
    assert "TopmostKeeper(" in block
    assert "install_foreground_hook()" in block
    assert "_topmost_timer.start(1000)" in block


def test_main_logs_the_restore():
    src = (ROOT / "wayfinder_main.py").read_text(encoding="utf-8")
    assert 'data.get("event") == "topmost"' in src
    assert "🫧 Pill was pushed behind" in src
