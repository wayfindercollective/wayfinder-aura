"""Windows pill topmost keeper (20 Oct 2026: the pill, still flagged topmost,
was ranked below ordinary windows and hidden behind them)."""
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows only")

ROOT = Path(__file__).resolve().parents[1]
PILL = 100


class FakeUser32:
    """A z-order (top first) of (hwnd, visible, topmost)."""

    def __init__(self, stack):
        self.stack = list(stack)
        self.restored = []

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
        entry = self.stack.pop(self._index(h))
        self.stack.insert(0, entry)
        return 1


@pytest.fixture
def wt(monkeypatch):
    from wayfinder.ui import windows_topmost
    return windows_topmost


def use(monkeypatch, wt, stack):
    fake = FakeUser32(stack)
    monkeypatch.setattr(wt, "_user32", fake)
    return fake


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


def test_keeper_restores_without_activating_and_reports(monkeypatch, wt):
    fake = use(monkeypatch, wt, [(2, True, False), (PILL, True, True)])
    reports = []
    keeper = wt.TopmostKeeper(lambda: PILL, reports.append)
    assert keeper.check() == 1
    assert fake.restored == [(PILL, wt._HWND_TOPMOST)]
    assert reports == [1]
    assert keeper.check() == 0          # back on top: nothing more to do
    assert reports == [1]


def test_keeper_leaves_a_hidden_pill_alone(monkeypatch, wt):
    fake = use(monkeypatch, wt, [(2, True, False), (PILL, True, True)])
    keeper = wt.TopmostKeeper(lambda: None, lambda n: None)
    assert keeper.check() == 0
    assert fake.restored == []


def test_keeper_never_raises(monkeypatch, wt):
    def boom():
        raise RuntimeError("winId gone")
    keeper = wt.TopmostKeeper(boom, lambda n: None)
    assert keeper.check() == 0


def test_restore_flags_never_activate_move_or_resize(wt):
    flags = wt._SWP_NOSIZE | wt._SWP_NOMOVE | wt._SWP_NOACTIVATE
    assert flags == 0x13


def test_real_foreground_hook_installs_and_uninstalls(wt):
    keeper = wt.TopmostKeeper(lambda: None, lambda n: None)
    assert keeper.install_foreground_hook()
    keeper.uninstall()
    assert keeper._hook is None


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
