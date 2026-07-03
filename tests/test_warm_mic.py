"""Tests for WarmMic — the persistent, shared capture stream kept open between recordings.

Why this exists: on SteamOS/PipeWire (the 'pulse' PCM in the Flatpak sandbox) opening a
capture stream costs ~0.4-0.5s, and a stale cached device index adds a ~0.15s dead-probe on
top. A fresh open per recording clipped the first words of short/rapid-fire dictations into
silence ("No speech detected"). WarmMic opens the stream once, keeps it warm, routes audio to
whichever recorder is currently attached, heals a dead index once, and auto-closes when idle.
"""

import threading
from unittest.mock import MagicMock, patch

import numpy as np
import pytest


def _make_warm(device=None, idle_secs=30.0):
    from wayfinder.core.recorder import WarmMic
    return WarmMic(device=device, sample_rate=16000, idle_secs=idle_secs)


class TestWarmMicLifecycle:
    @patch("wayfinder.core.recorder.get_supported_sample_rate", return_value=48000)
    @patch("wayfinder.core.recorder.sd")
    def test_opens_once_and_reuses_across_recordings(self, mock_sd, _rate):
        """The stream is opened on first acquire and reused on the next — no second open.

        This is the whole point: rapid-fire dictation must not pay the ~0.4s open each time.
        """
        warm = _make_warm(device=7)
        sink = MagicMock()

        warm.acquire(sink)
        warm.release()
        warm.acquire(sink)

        assert mock_sd.InputStream.call_count == 1
        assert warm.is_open

    @patch("wayfinder.core.recorder.get_supported_sample_rate", return_value=48000)
    @patch("wayfinder.core.recorder.sd")
    def test_sample_rate_reflects_open_stream(self, mock_sd, _rate):
        warm = _make_warm(device=7)
        assert warm.sample_rate == 16000  # target until opened
        warm.acquire(MagicMock())
        assert warm.sample_rate == 48000  # the device's supported rate after open

    @patch("wayfinder.core.recorder.get_supported_sample_rate", return_value=48000)
    @patch("wayfinder.core.recorder.sd")
    def test_routes_frames_to_attached_sink_only(self, mock_sd, _rate):
        """While acquired, frames reach the sink; after release, they are dropped."""
        warm = _make_warm(device=7)
        sink = MagicMock()
        warm.acquire(sink)

        data = np.zeros(160, dtype=np.float32)
        warm._callback(data, 160, None, None)
        assert sink.call_count == 1

        warm.release()
        warm._callback(data, 160, None, None)
        assert sink.call_count == 1  # dropped — no new call after release


class TestWarmMicDeviceHealing:
    @patch("wayfinder.core.recorder.get_supported_sample_rate", return_value=48000)
    @patch("wayfinder.core.recorder._system_default_input_index", return_value=6)
    @patch("wayfinder.core.recorder.sd")
    def test_falls_back_past_dead_index_and_remembers_working_one(self, mock_sd, _sys, _rate):
        """A dead cached index (4) is probed once, then healed: the working index (6) is
        remembered so it is never re-probed, and acquire() returns it so the app can sync."""
        def open_stream(*args, **kwargs):
            if kwargs.get("device") == 4:
                raise RuntimeError("Invalid device (acp5x output-only)")
            return MagicMock()
        mock_sd.InputStream.side_effect = open_stream

        warm = _make_warm(device=4)
        actual = warm.acquire(MagicMock())

        assert actual == 6
        assert warm.device == 6  # healed — next open uses 6 directly, no dead probe

    @patch("wayfinder.core.recorder.get_supported_sample_rate", return_value=48000)
    @patch("wayfinder.core.recorder._system_default_input_index", return_value=None)
    @patch("wayfinder.core.recorder.sd")
    def test_falls_back_to_system_default_none(self, mock_sd, _sys, _rate):
        def open_stream(*args, **kwargs):
            if kwargs.get("device") is not None:
                raise RuntimeError("dead")
            return MagicMock()
        mock_sd.InputStream.side_effect = open_stream

        warm = _make_warm(device=4)
        actual = warm.acquire(MagicMock())
        assert actual is None


class TestRunWithTimeout:
    """The watchdog that keeps a hung PortAudio open from freezing the Tk main thread."""

    def test_returns_result_of_fast_fn(self):
        from wayfinder.core.recorder import _run_with_timeout
        assert _run_with_timeout(lambda: 42, 1.0) == 42

    def test_raises_timeout_for_hanging_fn(self):
        from wayfinder.core.recorder import _run_with_timeout
        ev = threading.Event()  # never set -> the fn hangs
        with pytest.raises(TimeoutError):
            _run_with_timeout(ev.wait, 0.2)
        ev.set()  # release the abandoned worker

    def test_propagates_exception_from_fn(self):
        from wayfinder.core.recorder import _run_with_timeout
        def boom():
            raise ValueError("device exploded")
        with pytest.raises(ValueError, match="device exploded"):
            _run_with_timeout(boom, 1.0)


class TestWarmMicOpenTimeout:
    """A wedged audio device must never block the caller (Tk main thread)."""

    @patch("wayfinder.core.recorder._MIC_OPEN_TIMEOUT", 0.3)
    @patch("wayfinder.core.recorder.get_supported_sample_rate", return_value=48000)
    @patch("wayfinder.core.recorder._system_default_input_index", return_value=6)
    @patch("wayfinder.core.recorder.sd")
    def test_wedged_device_times_out_and_falls_through(self, mock_sd, _sys, _rate):
        """Device 4's open HANGS (wedged PipeWire node). The watchdog times out and
        the next fallback (6) opens instead of the app freezing. Regression: 2026-07-03."""
        blocker = threading.Event()  # never set -> device 4 hangs forever

        def open_stream(*args, **kwargs):
            if kwargs.get("device") == 4:
                blocker.wait()
            return MagicMock()
        mock_sd.InputStream.side_effect = open_stream

        warm = _make_warm(device=4)
        actual = warm.acquire(MagicMock())

        assert actual == 6          # skipped the hung device, opened the fallback
        assert warm.device == 6
        blocker.set()               # release the abandoned watchdog worker

    @patch("wayfinder.core.recorder._MIC_OPEN_TIMEOUT", 0.2)
    @patch("wayfinder.core.recorder.get_supported_sample_rate", return_value=48000)
    @patch("wayfinder.core.recorder._system_default_input_index", return_value=None)
    @patch("wayfinder.core.recorder.sd")
    def test_all_devices_wedged_raises_instead_of_freezing(self, mock_sd, _sys, _rate):
        """If EVERY device hangs, acquire() raises (app can surface an error) rather
        than blocking the Tk main thread forever."""
        blocker = threading.Event()

        def open_stream(*args, **kwargs):
            blocker.wait()
        mock_sd.InputStream.side_effect = open_stream

        warm = _make_warm(device=4)
        with pytest.raises((TimeoutError, RuntimeError)):
            warm.acquire(MagicMock())
        blocker.set()


class TestWarmMicIdleClose:
    @patch("wayfinder.core.recorder.get_supported_sample_rate", return_value=48000)
    @patch("wayfinder.core.recorder.sd")
    def test_release_arms_timer_acquire_cancels_it(self, mock_sd, _rate):
        warm = _make_warm(device=7)
        warm.acquire(MagicMock())
        assert warm._idle_timer is None
        warm.release()
        assert warm._idle_timer is not None  # armed to close after idle
        warm.acquire(MagicMock())
        assert warm._idle_timer is None  # cancelled — back in use

    @patch("wayfinder.core.recorder.get_supported_sample_rate", return_value=48000)
    @patch("wayfinder.core.recorder.sd")
    def test_idle_fire_closes_when_still_idle(self, mock_sd, _rate):
        warm = _make_warm(device=7)
        warm.acquire(MagicMock())
        warm.release()
        warm._on_idle()  # the timer's target
        assert not warm.is_open

    @patch("wayfinder.core.recorder.get_supported_sample_rate", return_value=48000)
    @patch("wayfinder.core.recorder.sd")
    def test_idle_fire_is_noop_when_reacquired(self, mock_sd, _rate):
        """If a recording started after the timer fired but before it ran, don't close."""
        warm = _make_warm(device=7)
        warm.acquire(MagicMock())
        warm.release()
        warm.acquire(MagicMock())  # re-acquired before the stale timer runs
        warm._on_idle()
        assert warm.is_open

    @patch("wayfinder.core.recorder.get_supported_sample_rate", return_value=48000)
    @patch("wayfinder.core.recorder.sd")
    def test_set_device_drops_warm_stream(self, mock_sd, _rate):
        """Changing the mic in settings must drop the warm stream so the next recording
        reopens on the new device, not the old one."""
        warm = _make_warm(device=7)
        warm.acquire(MagicMock())
        warm.release()
        warm.set_device(9)
        assert warm.device == 9
        assert not warm.is_open

    @patch("wayfinder.core.recorder.get_supported_sample_rate", return_value=48000)
    @patch("wayfinder.core.recorder.sd")
    def test_close_is_idempotent(self, mock_sd, _rate):
        warm = _make_warm(device=7)
        warm.acquire(MagicMock())
        warm.close()
        warm.close()
        assert not warm.is_open


class TestRecordersUseWarmMic:
    """The recorders delegate stream ownership to WarmMic when given one, but keep their own
    buffer/peak/chunk logic (so the existing recorder tests stay valid)."""

    def test_audio_recorder_acquires_and_releases(self):
        from wayfinder.core.recorder import AudioRecorder

        warm = MagicMock()
        warm.sample_rate = 48000
        warm.acquire.return_value = 6  # acquire() returns the device that actually opened
        rec = AudioRecorder(device=4, warm_mic=warm)

        rec.start()
        warm.acquire.assert_called_once_with(rec._audio_callback)
        assert rec.is_recording() is True
        assert rec.device == 6  # synced from the warm mic's healed device

        rec.frames = [np.ones(1000, dtype=np.float32)]  # so stop() has audio
        with patch("wayfinder.core.recorder.sd"):
            rec.stop()
        warm.release.assert_called_once()
        assert rec.is_recording() is False

    def test_chunked_recorder_acquires_and_sizes_chunks_from_warm_rate(self):
        from wayfinder.core.recorder import ChunkedRecorder

        warm = MagicMock()
        warm.sample_rate = 48000
        warm.acquire.return_value = 6  # acquire() returns the device that actually opened
        rec = ChunkedRecorder(device=4, chunk_duration=30.0, chunk_overlap=2.0, warm_mic=warm)

        rec.start()
        warm.acquire.assert_called_once_with(rec._audio_callback)
        assert rec.is_recording() is True
        # chunk math must use the warm stream's actual rate, not the 16k target
        assert rec._chunk_samples == int(30.0 * 48000)
        assert rec._overlap_samples == int(2.0 * 48000)

        rec.stop()
        warm.release.assert_called_once()
        assert rec.is_recording() is False
