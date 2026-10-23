"""Regression tests for startup work kept off the first-frame path."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


REPO = Path(__file__).resolve().parent.parent


def test_audio_processing_never_imports_scipy():
    # SciPy is not bundled any more; nothing on the dictation path may need it.
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO / "src")
    code = """
import sys
from wayfinder.core import recorder
assert 'scipy' not in sys.modules
same_rate = recorder.resample_audio(recorder.np.zeros(8), 16000, 16000)
assert len(same_rate) == 8
audio = recorder.np.random.default_rng(0).normal(0, 0.1, 48000).astype(recorder.np.float32)
resampled = recorder.resample_audio(audio, 48000, 16000)
assert len(resampled) == 16000
for level in ('light', 'medium', 'heavy'):
    recorder.preprocess_audio(resampled, 16000, level)
recorder.preload_audio_processing()
assert 'scipy' not in sys.modules, sorted(m for m in sys.modules if m.startswith('scipy'))
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


def test_launch_does_not_import_requests():
    # main.py imports the setup pane on every launch to decide the first-run
    # flow; requests (~35 ms warm) is needed only once a download starts.
    env = os.environ.copy()
    env.update(PYTHONPATH=str(REPO / "src"), QT_QPA_PLATFORM="offscreen")
    code = """
import sys
import wayfinder_main
from wayfinder.ui.setup_pane import first_run_plan, should_chain_welcome
assert 'requests' not in sys.modules, 'requests imported at launch'
"""
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=REPO, env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_audio_processing_preload_warms_the_rumble_filter():
    from wayfinder.core import audio_dsp, recorder

    audio_dsp._impulse_response.cache_clear()
    recorder.preload_audio_processing()
    assert audio_dsp._impulse_response.cache_info().currsize == 1
    b, a = audio_dsp.butter(2, recorder.RUMBLE_CUTOFF_HZ / 8000, btype="high")
    audio_dsp.filtfilt(b, a, [0.0] * 64)
    assert audio_dsp._impulse_response.cache_info().hits >= 1


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


@pytest.mark.parametrize("is_macos", [False, True])
def test_inactive_tab_is_created_once_on_first_switch(monkeypatch, is_macos):
    import wayfinder_main
    from wayfinder_main import WayfinderApp

    monkeypatch.setattr(wayfinder_main, "IS_MACOS", is_macos)
    monkeypatch.setattr(wayfinder_main, "place_in_macos_content_pane", lambda widget: widget.place())

    class Frame:
        def __init__(self):
            self.manager = ""
            self.raised = False

        def pack(self, **_kwargs):
            self.manager = "pack"

        def pack_forget(self):
            self.manager = ""

        def grid_forget(self):
            self.manager = ""

        def place(self, **_kwargs):
            self.manager = "place"

        def lift(self):
            self.raised = True

        def winfo_manager(self):
            return self.manager

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
    if is_macos:
        # Aqua: every built page stays mapped; the selected one is raised.
        assert app.tab_frames["settings"].manager == "place"
        assert app.tab_frames["settings"].raised is True
        assert app.tab_frames["dictate"].manager == "place"
    else:
        # Linux (as on main): only the selected page is packed.
        assert app.tab_frames["settings"].manager == "pack"
        assert app.tab_frames["dictate"].manager == ""


def test_settings_preload_advances_one_slice_and_reschedules():
    from wayfinder_main import AppState, WayfinderApp, _settings_preload_interval_ms

    built: list[str] = []
    scheduled: list[tuple[int, object]] = []

    def steps():
        built.append("audio")
        yield "audio"
        built.append("processing")
        yield "processing"

    app = SimpleNamespace(
        _settings_preload_job="current",
        _settings_build_complete=False,
        _settings_build_iterator=None,
        _setup_active=False,
        _welcome_active=False,
        app_state=AppState.IDLE,
        winfo_viewable=lambda: True,
        _create_settings_tab_steps=steps,
        after=lambda delay, callback: scheduled.append((delay, callback)) or "next",
    )
    app._schedule_settings_preload = (
        WayfinderApp._schedule_settings_preload.__get__(app)
    )
    app._preload_settings_slice = WayfinderApp._preload_settings_slice.__get__(app)

    WayfinderApp._preload_settings_slice(app)

    assert built == ["audio"]
    assert len(scheduled) == 1
    assert scheduled[0][0] == _settings_preload_interval_ms()
    assert scheduled[0][1].__func__ is WayfinderApp._preload_settings_slice
    assert app._settings_preload_job == "next"


def test_settings_preload_skips_hidden_or_first_run_without_retry():
    from wayfinder_main import AppState, WayfinderApp

    for visible, setup_active in ((False, False), (True, True)):
        scheduled = []
        app = SimpleNamespace(
            _settings_preload_job="current",
            _settings_build_complete=False,
            _settings_build_iterator=None,
            _setup_active=setup_active,
            _welcome_active=False,
            app_state=AppState.IDLE,
            winfo_viewable=lambda visible=visible: visible,
            after=lambda delay, callback: scheduled.append((delay, callback)),
        )

        WayfinderApp._preload_settings_slice(app)

        assert app._settings_build_iterator is None
        assert scheduled == []


def test_early_settings_click_drains_an_existing_staged_build():
    from wayfinder_main import WayfinderApp

    built: list[str] = []
    app = SimpleNamespace(
        _settings_build_complete=False,
        _settings_build_iterator=None,
    )

    def steps():
        built.append("audio")
        yield "audio"
        built.append("processing")
        yield "processing"
        app._settings_build_complete = True

    app._create_settings_tab_steps = steps
    app._settings_build_iterator = steps()
    next(app._settings_build_iterator)

    WayfinderApp._finish_settings_build(app)

    assert built == ["audio", "processing"]
    assert app._settings_build_complete is True
    assert app._settings_build_iterator is None


def test_hotkey_scan_publishes_all_names_before_filter(monkeypatch):
    import wayfinder_main

    keyboard = SimpleNamespace(name="Keyboard")
    mouse = SimpleNamespace(name="Mouse")
    monkeypatch.setattr(
        wayfinder_main,
        "get_all_input_devices",
        lambda: [
            {"name": "Keyboard", "device": keyboard},
            {"name": "Mouse", "device": mouse},
        ],
    )
    snapshots = []

    selected = wayfinder_main.find_keyboard_devices(["Mouse"], snapshots.append)

    assert snapshots == [["Keyboard", "Mouse"]]
    assert selected == [mouse]


def test_log_lines_buffer_until_history_tab_exists():
    from wayfinder_main import WayfinderApp

    app = SimpleNamespace(_pending_ui_logs=[])
    WayfinderApp._do_log(app, "first\n")
    WayfinderApp._do_log(app, "second\n")
    assert app._pending_ui_logs == ["first\n", "second\n"]
