"""Lightweight freeze recovery: recycle overlay; optional RECORDING cap; ordered duck.

Sol (gpt-5.6-sol xhigh) required:
1. Reset always refreshes overlay (write-success ≠ Qt alive).
2. Duck/restore serialized FIFO (no permanent attenuation race).
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock


def test_default_recording_cap_is_off():
    from wayfinder.config import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["max_recording_duration"] == 0


def test_start_recording_watchdog_arms_when_configured():
    from wayfinder_main import WayfinderApp

    app = MagicMock(spec=WayfinderApp)
    app.config = {"max_recording_duration": 30}
    app.session_generation = 3
    app._recording_watchdog_job = None
    app.after = MagicMock(return_value="job-1")
    app._cancel_recording_watchdog = lambda: WayfinderApp._cancel_recording_watchdog(app)

    WayfinderApp._start_recording_watchdog(app)

    app.after.assert_called_once()
    assert app.after.call_args[0][0] == 30_000


def test_on_recording_timeout_stops_matching_session():
    from wayfinder_main import WayfinderApp, AppState

    app = MagicMock(spec=WayfinderApp)
    app.app_state = AppState.RECORDING
    app.session_generation = 5
    app._recording_watchdog_job = "stale"
    app.log = MagicMock()
    app.stop_recording_and_process = MagicMock()
    app.force_reset = MagicMock()

    WayfinderApp._on_recording_timeout(app, gen=5, timeout_s=30)

    app.stop_recording_and_process.assert_called_once()


def test_update_leaving_listen_always_restarts_overlay():
    """Leaving listen recycles child even when writes would report success."""
    from wayfinder_main import OverlayController

    ctrl = OverlayController()
    ctrl._process = MagicMock()
    ctrl._process.poll.return_value = None
    ctrl._current_state = "listening"
    ctrl._stop_audio_polling = MagicMock()
    ctrl.refresh = MagicMock(return_value=True)
    ctrl._start_audio_polling = MagicMock()
    ctrl._log = MagicMock()
    ctrl._send_command = MagicMock(return_value=True)

    assert ctrl.update("processing") is True
    ctrl.refresh.assert_called_once()
    # Must not rely on a successful-but-unhandled write path alone.
    assert ctrl._current_state == "processing"


def test_update_failed_write_still_restarts():
    from wayfinder_main import OverlayController

    ctrl = OverlayController()
    ctrl._process = MagicMock()
    ctrl._process.poll.return_value = None
    ctrl._current_state = "ready"
    ctrl._stop_audio_polling = MagicMock()
    ctrl._send_command = MagicMock(return_value=False)
    ctrl.refresh = MagicMock(return_value=True)
    ctrl._log = MagicMock()

    assert ctrl.update("processing") is True
    ctrl.refresh.assert_called_once()


def test_force_reset_always_refreshes_overlay():
    """Reset unsticks pill/tray even when stdin writes report success."""
    from wayfinder_main import WayfinderApp

    app = MagicMock()
    app.session_generation = 1
    app.log = MagicMock()
    app.chunked_recorder = None
    app.recorder = None
    app.warm_mic = None
    app._finish_injection_job = None
    app._duration_update_job = None
    app._recording_start_time = None
    app._processing_start_time = None
    app._has_visual_pyqt_overlay = MagicMock(return_value=True)
    ctrl = MagicMock()
    app.overlay_controller = ctrl
    app.update_state = MagicMock()

    WayfinderApp._do_force_reset(app)

    ctrl._stop_audio_polling.assert_called_once()
    assert ctrl._current_state == "ready"
    ctrl.refresh.assert_called_once()
    app.update_state.assert_called_once()


def test_duck_actions_run_fifo_in_order():
    """A slow duck must finish before a later restore (no permanent attenuation)."""
    from wayfinder_main import WayfinderApp

    app = MagicMock(spec=WayfinderApp)
    app._duck_action_queue = None
    order: list[str] = []
    duck_started = threading.Event()
    release_duck = threading.Event()

    def fake_safe(action: str) -> None:
        if action == "duck":
            duck_started.set()
            release_duck.wait(timeout=2.0)
        order.append(action)

    app._duck_audio_safe = fake_safe

    WayfinderApp._enqueue_duck_action(app, "duck")
    assert duck_started.wait(timeout=1.0)
    WayfinderApp._enqueue_duck_action(app, "restore")
    # restore must not run until duck completes
    time.sleep(0.05)
    assert order == []
    release_duck.set()
    deadline = time.time() + 2.0
    while time.time() < deadline and order != ["duck", "restore"]:
        time.sleep(0.01)
    assert order == ["duck", "restore"]


def test_is_healthy_is_process_liveness_only():
    from wayfinder_main import OverlayController

    ctrl = OverlayController()
    ctrl._process = MagicMock()
    ctrl._process.poll.return_value = None
    assert ctrl.is_healthy() is True
    ctrl._process.poll.return_value = 1
    assert ctrl.is_healthy() is False
