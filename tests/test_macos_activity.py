"""Idle-sleep holds during dictation and downloads (macOS)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from wayfinder.utils import macos_activity  # noqa: E402


@pytest.mark.skipif(sys.platform != "darwin", reason="NSProcessInfo")
def test_real_activity_shows_in_pmset():
    import subprocess
    assert macos_activity.begin("test-activity", "Wayfinder test activity")
    try:
        out = subprocess.run(["pmset", "-g", "assertions"], capture_output=True, text=True).stdout
        assert "Wayfinder test activity" in out
    finally:
        macos_activity.end("test-activity")
    assert not macos_activity.is_held("test-activity")


def test_begin_is_idempotent_and_end_is_safe(monkeypatch):
    calls = []

    class _Info:
        def beginActivityWithOptions_reason_(self, opts, reason):
            calls.append(("begin", reason))
            return object()

        def endActivity_(self, token):
            calls.append(("end", None))

    monkeypatch.setattr(macos_activity.sys, "platform", "darwin")
    monkeypatch.setattr(macos_activity, "_process_info", lambda: _Info())
    assert macos_activity.begin("k", "r") and macos_activity.begin("k", "r")
    macos_activity.end("k")
    macos_activity.end("k")
    assert calls == [("begin", "r"), ("end", None)]


def test_off_macos_nothing_happens(monkeypatch):
    monkeypatch.setattr(macos_activity.sys, "platform", "linux")
    assert macos_activity.begin("k", "r") is False
    with macos_activity.held("k", "r") as held:
        assert held is False
