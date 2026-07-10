"""Regression tests for startup work kept off the first-frame path."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace


REPO = Path(__file__).resolve().parent.parent


def test_recorder_import_does_not_import_scipy_signal():
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO / "src")
    code = """
import sys
from wayfinder.core import recorder
assert 'scipy.signal' not in sys.modules
same_rate = recorder.resample_audio(recorder.np.zeros(8), 16000, 16000)
assert len(same_rate) == 8
assert 'scipy.signal' not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_audio_processing_preload_is_one_shot(monkeypatch):
    from wayfinder.core import recorder

    calls = []
    monkeypatch.setattr(recorder, "_get_scipy_signal_functions", lambda: calls.append(True))
    recorder.preload_audio_processing()
    assert calls == [True]


def test_steam_deck_defaults_to_idle_efficient_overlay():
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO / "src")
    env["WAYFINDER_STEAM_PLATFORM"] = "deck"
    code = """
from wayfinder.config import DEFAULT_CONFIG
assert DEFAULT_CONFIG['overlay_quality'] == 'performance'
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_inactive_tab_is_created_once_on_first_switch():
    from wayfinder_main import WayfinderApp

    class Frame:
        def __init__(self):
            self.packed = False

        def pack(self, **_kwargs):
            self.packed = True

        def pack_forget(self):
            self.packed = False

    class Button:
        def configure(self, **_kwargs):
            pass

    calls: list[str] = []
    app = SimpleNamespace(
        tab_frames={"dictate": Frame()},
        tab_buttons={"dictate": Button(), "settings": Button()},
        tab_colors={"dictate": "blue", "settings": "blue"},
        active_tab="dictate",
        _write_status_breadcrumb=lambda: None,
    )

    def create_settings():
        calls.append("settings")
        app.tab_frames["settings"] = Frame()

    app._create_dictate_tab = lambda: None
    app._create_settings_tab = create_settings
    app._create_style_tab = lambda: None
    app._create_history_tab = lambda: None
    app._ensure_tab_created = WayfinderApp._ensure_tab_created.__get__(app)

    WayfinderApp._switch_tab(app, "settings")
    WayfinderApp._switch_tab(app, "settings")

    assert calls == ["settings"]
    assert app.tab_frames["settings"].packed is True
    assert app.tab_frames["dictate"].packed is False


def test_log_lines_buffer_until_history_tab_exists():
    from wayfinder_main import WayfinderApp

    app = SimpleNamespace(_pending_ui_logs=[])
    WayfinderApp._do_log(app, "first\n")
    WayfinderApp._do_log(app, "second\n")
    assert app._pending_ui_logs == ["first\n", "second\n"]
