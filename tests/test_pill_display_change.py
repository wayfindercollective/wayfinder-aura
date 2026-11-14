"""Windows: the pill re-places itself after a display change (14 Nov 2026:
placed for a brief 100% scale, it sat half behind the taskbar once the panel
went back to 175%, until Aura restarted)."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = (ROOT / "src" / "wayfinder" / "ui" / "overlay.py").read_text(encoding="utf-8")
WIN_BLOCK = SRC.split('if sys.platform == "win32" and not tray_only:', 1)[1].split(
    "# Keep the pill above ordinary windows.", 1)[0]


def test_every_display_signal_goes_through_the_reposition():
    for signal in ("geometryChanged", "logicalDotsPerInchChanged", "availableGeometryChanged",
                   "screenAdded", "screenRemoved", "primaryScreenChanged"):
        line = next(l for l in WIN_BLOCK.splitlines() if f".{signal}.connect(" in l
                    or (f".{signal}.connect(" in l.replace(" ", "")))
        nxt = WIN_BLOCK.splitlines()[WIN_BLOCK.splitlines().index(line) + 1]
        assert "_on_display_change(" in line + nxt, signal
    assert "_emit_screen_event(\"" not in WIN_BLOCK.split("def _on_display_change", 1)[1]


def test_the_reposition_is_debounced_and_repeated_once_settled():
    body = WIN_BLOCK.split("def _on_display_change", 1)[1].split("def _watch_screen", 1)[0]
    assert "_emit_screen_event(kind, screen)" in body      # still logged
    assert "_reposition_timer.start(400)" in body          # after the last event
    assert "_settle_timer.start(2500)" in body              # and once Windows has settled
    assert re.search(r"_reposition_timer\.setSingleShot\(True\)", WIN_BLOCK)
    assert re.search(r"_settle_timer\.setSingleShot\(True\)", WIN_BLOCK)


def test_the_reposition_uses_the_normal_placement_and_never_raises():
    body = WIN_BLOCK.split("def _reposition_after_display_change", 1)[1].split(
        "_reposition_timer = QTimer()", 1)[0]
    assert "overlay._position_at_bottom()" in body
    assert "except Exception" in body
