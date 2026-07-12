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


def test_update_leaving_listen_soft_updates_without_restart():
    """Leaving listen must soft-update — hard restart steals Wayland focus (ydotool)."""
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
    ctrl._stop_audio_polling.assert_called_once()
    ctrl._send_command.assert_called_once()
    ctrl.refresh.assert_not_called()
    assert ctrl._current_state == "processing"
    assert ctrl._restart_pending is False


def test_update_failed_write_still_restarts_when_allowed():
    from wayfinder_main import OverlayController

    ctrl = OverlayController()
    ctrl._process = MagicMock()
    ctrl._process.poll.return_value = None
    ctrl._current_state = "ready"
    ctrl._stop_audio_polling = MagicMock()
    ctrl._send_command = MagicMock(return_value=False)
    ctrl.refresh = MagicMock(return_value=True)
    ctrl._log = MagicMock()

    assert ctrl.update("processing", allow_restart=True) is True
    ctrl.refresh.assert_called_once()


def test_update_failed_write_defers_restart_when_disallowed():
    """Dictation-critical path must not remap overlay/tray mid-inject."""
    from wayfinder_main import OverlayController

    ctrl = OverlayController()
    ctrl._process = MagicMock()
    ctrl._process.poll.return_value = None
    ctrl._current_state = "listening"
    ctrl._stop_audio_polling = MagicMock()
    ctrl._send_command = MagicMock(return_value=False)
    ctrl.refresh = MagicMock(return_value=True)
    ctrl._log = MagicMock()

    assert ctrl.update("processing", allow_restart=False) is False
    ctrl.refresh.assert_not_called()
    assert ctrl._restart_pending is True

    ctrl._send_command = MagicMock(return_value=True)
    assert ctrl.flush_deferred_restart() is True
    ctrl.refresh.assert_called_once()
    assert ctrl._restart_pending is False

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


def test_wait_for_ack_success():
    """Qt-thread nonce acks are collected from the stdout drain set."""
    from wayfinder_main import OverlayController

    ctrl = OverlayController()
    ctrl._process = MagicMock()
    ctrl._process.poll.return_value = None
    with ctrl._ack_lock:
        ctrl._received_acks.add("n-test")
    assert ctrl._wait_for_ack("n-test", timeout=0.1) is True
    # Consumed once
    assert ctrl._wait_for_ack("n-test", timeout=0.05) is False


def test_wait_for_ack_timeout():
    from wayfinder_main import OverlayController

    ctrl = OverlayController()
    ctrl._process = MagicMock()
    ctrl._process.poll.return_value = None
    assert ctrl._wait_for_ack("never", timeout=0.05) is False


def test_critical_send_soft_success_when_ack_arrives():
    """Write + matching nonce ack → True (soft path proven on Qt thread)."""
    from wayfinder_main import OverlayController

    ctrl = OverlayController()
    ctrl._ack_timeout_s = 0.3
    proc = MagicMock()
    proc.poll.return_value = None
    proc.stdin = MagicMock()
    ctrl._process = proc

    # Deliver ack shortly after write so the waiter succeeds.
    def fake_write(_line):
        # Nonce is attached inside _send_command before write; pull from last dumps.
        # Simpler: after any write, inject all recent nonces — use spy on dumps.
        pass

    written = []

    def capture_write(line):
        written.append(line)
        import json as _json

        cmd = _json.loads(line.strip())
        nonce = cmd.get("nonce")
        if nonce is not None:
            with ctrl._ack_lock:
                ctrl._received_acks.add(str(nonce))
        return len(line)

    proc.stdin.write.side_effect = capture_write
    proc.stdin.flush = MagicMock()

    import select as select_mod

    real_select = select_mod.select

    def select_stdin(r, w, x, timeout=None):
        # stdin always ready when process is mocked
        if w:
            return [], w, []
        return real_select(r, w, x, timeout)

    import wayfinder_main as wm

    original = select_mod.select
    select_mod.select = select_stdin
    try:
        ok = ctrl._send_command({"cmd": "show", "state": "processing"}, critical=True)
    finally:
        select_mod.select = original

    assert ok is True
    assert written, "expected a write to overlay stdin"
    assert '"nonce"' in written[0]


def test_critical_send_ack_timeout_returns_false():
    """No Qt ack within timeout → False (caller defers or restarts)."""
    from wayfinder_main import OverlayController
    import select as select_mod

    ctrl = OverlayController()
    ctrl._ack_timeout_s = 0.05
    proc = MagicMock()
    proc.poll.return_value = None
    proc.stdin = MagicMock()
    ctrl._process = proc

    def select_stdin(r, w, x, timeout=None):
        if w:
            return [], w, []
        return [], [], []

    original = select_mod.select
    select_mod.select = select_stdin
    try:
        ok = ctrl._send_command({"cmd": "show", "state": "processing"}, critical=True)
    finally:
        select_mod.select = original

    assert ok is False


def test_update_ack_timeout_defers_when_disallowed():
    """Missed ack on dictation path → _restart_pending, never hard restart."""
    from wayfinder_main import OverlayController

    ctrl = OverlayController()
    ctrl._process = MagicMock()
    ctrl._process.poll.return_value = None
    ctrl._current_state = "listening"
    ctrl._stop_audio_polling = MagicMock()
    ctrl._send_command = MagicMock(return_value=False)  # simulates ack timeout
    ctrl.refresh = MagicMock(return_value=True)
    ctrl._log = MagicMock()

    assert ctrl.update("processing", allow_restart=False) is False
    ctrl.refresh.assert_not_called()
    assert ctrl._restart_pending is True


def test_default_desktop_paste_on_focus_drift_is_off():
    from wayfinder.config import DEFAULT_CONFIG

    assert DEFAULT_CONFIG.get("desktop_paste_on_focus_drift") is False
