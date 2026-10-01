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


def _make_warm(device=None, idle_secs=30.0, **kwargs):
    from wayfinder.core.recorder import WarmMic
    return WarmMic(
        device=device,
        sample_rate=16000,
        idle_secs=idle_secs,
        **kwargs,
    )


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
    def test_wedged_device_trips_circuit_breaker(self, mock_sd, _sys, _rate):
        """A timed-out open must not launch concurrent fallbacks into PortAudio.

        The abandoned C call is still running, and PortAudio is process-global.
        Additional opens leak capture clients and wedge PipeWire system-wide.
        """
        blocker = threading.Event()  # never set -> device 4 hangs forever

        def open_stream(*args, **kwargs):
            if kwargs.get("device") == 4:
                blocker.wait()
            return MagicMock()
        mock_sd.InputStream.side_effect = open_stream

        warm = _make_warm(device=4)
        with pytest.raises(TimeoutError):
            warm.acquire(MagicMock())

        assert mock_sd.InputStream.call_count == 1
        assert warm._abandoned_open is True
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

    @patch("wayfinder.core.recorder._MIC_CLOSE_TIMEOUT", 0.1)
    @patch("wayfinder.core.recorder.get_supported_sample_rate", return_value=48000)
    @patch("wayfinder.core.recorder.sd")
    def test_stuck_idle_close_never_holds_shared_lock_or_freezes_acquire(
        self, mock_sd, _rate
    ):
        """Regression 2026-09-30: CoreAudio wedged in AudioOutputUnitStop.

        The idle timer owned WarmMic's RLock during abort(), so the next hotkey
        blocked Tk forever trying to acquire that lock.  Teardown may remain
        stuck, but the lock and UI must remain responsive and further PortAudio
        work must fail closed.
        """
        entered = threading.Event()
        release_abort = threading.Event()
        stream = MagicMock()
        stream.active = True

        def stuck_abort():
            entered.set()
            release_abort.wait(timeout=5)

        stream.abort.side_effect = stuck_abort
        mock_sd.InputStream.return_value = stream
        warm = _make_warm(device=7)
        warm.acquire(MagicMock())
        warm.release()

        idle = threading.Thread(target=warm._on_idle)
        idle.start()
        assert entered.wait(timeout=1)

        # The timer's native worker is stuck, but it must not own _lock.
        assert warm._lock.acquire(timeout=0.2)
        warm._lock.release()
        with pytest.raises(RuntimeError, match="stuck releasing"):
            warm.acquire(MagicMock())
        assert mock_sd.InputStream.call_count == 1

        release_abort.set()
        idle.join(timeout=1)

    @patch("wayfinder.core.recorder._MIC_CLOSE_TIMEOUT", 0.1)
    @patch("wayfinder.core.recorder.get_supported_sample_rate", return_value=48000)
    @patch("wayfinder.core.recorder.sd")
    def test_timed_out_close_quarantines_portaudio(self, mock_sd, _rate):
        blocker = threading.Event()
        notices = []
        stream = MagicMock()
        stream.active = True
        stream.abort.side_effect = lambda: blocker.wait(timeout=5)
        mock_sd.InputStream.return_value = stream
        warm = _make_warm(device=7, on_restart_required=notices.append)
        warm.acquire(MagicMock())
        warm.release()

        assert warm._close_stream() is False
        assert warm._abandoned_close is True
        assert notices == ["Microphone backend got stuck releasing the previous stream"]
        # Any later close/acquire path must not request multiple relaunches.
        warm._notify_restart_required("duplicate")
        assert len(notices) == 1
        with pytest.raises(RuntimeError, match="stuck releasing"):
            warm.acquire(MagicMock())
        assert mock_sd.InputStream.call_count == 1
        blocker.set()

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

    @patch("wayfinder.core.recorder.get_supported_sample_rate", return_value=48000)
    @patch("wayfinder.core.recorder.sd")
    def test_close_stream_detaches_before_teardown(self, mock_sd, _rate):
        """Regression 2026-07-13: double-close of the same PortAudio handle SEGV'd in
        ALSA (snd_async_del_handler). Drop the reference before stop/close so a
        concurrent idle/rescan path cannot re-enter on the same object.
        """
        warm = _make_warm(device=7)
        warm.acquire(MagicMock())
        stream = warm._stream
        assert stream is not None

        seen = []

        def tracking_abort():
            # While abort/close run, WarmMic must already have cleared _stream.
            seen.append(warm._stream)
            raise RuntimeError("pa boom")  # still must not leave a dangling ref

        stream.abort = tracking_abort
        stream.close = MagicMock(side_effect=RuntimeError("close boom"))

        warm._close_stream()
        assert warm._stream is None
        assert not warm.is_open
        assert seen == [None]  # detached before teardown
        stream.close.assert_called_once()

    @patch("wayfinder.core.recorder.get_supported_sample_rate", return_value=48000)
    @patch("wayfinder.core.recorder.sd")
    def test_close_stream_prefers_abort_over_stop(self, mock_sd, _rate):
        warm = _make_warm(device=7)
        warm.acquire(MagicMock())
        stream = warm._stream
        stream.abort = MagicMock()
        stream.stop = MagicMock()
        stream.close = MagicMock()

        warm._close_stream()
        stream.abort.assert_called_once()
        stream.stop.assert_not_called()
        stream.close.assert_called_once()

    @patch("wayfinder.core.recorder.get_supported_sample_rate", return_value=48000)
    @patch("wayfinder.core.recorder.sd")
    def test_concurrent_close_only_tears_down_once(self, mock_sd, _rate):
        warm = _make_warm(device=7)
        warm.acquire(MagicMock())
        stream = warm._stream
        stream.abort = MagicMock()
        stream.close = MagicMock()

        def closer():
            warm._close_stream()

        threads = [threading.Thread(target=closer) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=2)
        assert stream.abort.call_count == 1
        assert stream.close.call_count == 1
        assert warm._stream is None

    @patch("wayfinder.core.recorder.get_supported_sample_rate", return_value=48000)
    @patch("wayfinder.core.recorder._system_default_input_index", return_value=None)
    @patch("wayfinder.core.recorder.sd")
    def test_abandoned_open_skips_pa_rescan(self, mock_sd, _sys, _rate):
        """Watchdog-abandoned open must not call sd._terminate (SEGV race)."""
        warm = _make_warm(device=4)
        warm._abandoned_open = True
        with patch("wayfinder.core.recorder._pa_rescan") as rescan:
            assert warm.rescan() is False
            rescan.assert_not_called()

    @patch("wayfinder.core.recorder._MIC_OPEN_TIMEOUT", 0.2)
    @patch("wayfinder.core.recorder.get_supported_sample_rate", return_value=48000)
    @patch("wayfinder.core.recorder._system_default_input_index", return_value=None)
    @patch("wayfinder.core.recorder.sd")
    def test_later_open_still_skips_pa_rescan_after_abandon(self, mock_sd, _sys, _rate):
        """Sol R2: clearing _abandoned_open on the next _open() re-enabled rescan
        under a still-blocked PortAudio worker. Flag must stick for the WarmMic life.
        """
        blocker = threading.Event()

        def open_stream(*args, **kwargs):
            blocker.wait(timeout=5)
            return MagicMock()

        mock_sd.InputStream.side_effect = open_stream
        warm = _make_warm(device=4)
        with pytest.raises((TimeoutError, RuntimeError)):
            warm.acquire(MagicMock())
        assert warm._abandoned_open is True

        with patch("wayfinder.core.recorder._pa_rescan") as rescan:
            # A later acquire must fail immediately: no additional PortAudio
            # worker and no unsafe rescan while the abandoned one may be alive.
            calls_before = mock_sd.InputStream.call_count
            with pytest.raises(RuntimeError, match="restart Wayfinder Aura"):
                warm.acquire(MagicMock())
            assert mock_sd.InputStream.call_count == calls_before
            rescan.assert_not_called()
        blocker.set()


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


class TestWindowsSlowCloseRecovery:
    """Windows: a healthy stream close takes ~0.35 s (MME, USB mic), so one slow
    moment overran the 1 s watchdog and blocked recording until a restart
    (owner, 2026-10-01). Windows allows 5 s, and a close that overruns but
    finishes lifts the quarantine. The Mac keeps its restart."""

    def _slow_close(self, monkeypatch, mock_sd, platform):
        from wayfinder.core import recorder

        monkeypatch.setattr(recorder, "_IS_WINDOWS", platform == "win32")
        monkeypatch.setattr(recorder, "_MIC_CLOSE_TIMEOUT", 0.1)
        blocker = threading.Event()
        stream = MagicMock()
        stream.active = True
        stream.abort.side_effect = lambda: blocker.wait(timeout=5)
        mock_sd.InputStream.return_value = stream
        warm = _make_warm(device=7)
        warm.acquire(MagicMock())
        warm.release()
        assert warm._close_stream() is False          # overran the watchdog
        assert warm._abandoned_close is True
        blocker.set()                                  # ...but then finishes
        assert warm._close_done.wait(timeout=2)
        return warm

    @patch("wayfinder.core.recorder.get_supported_sample_rate", return_value=48000)
    @patch("wayfinder.core.recorder.sd")
    def test_windows_recovers_when_the_slow_close_finishes(self, mock_sd, _rate, monkeypatch):
        warm = self._slow_close(monkeypatch, mock_sd, "win32")
        assert warm._abandoned_close is False
        warm.acquire(MagicMock())                      # records again, no restart
        assert mock_sd.InputStream.call_count == 2

    @patch("wayfinder.core.recorder.get_supported_sample_rate", return_value=48000)
    @patch("wayfinder.core.recorder.sd")
    def test_mac_still_requires_a_restart(self, mock_sd, _rate, monkeypatch):
        warm = self._slow_close(monkeypatch, mock_sd, "darwin")
        assert warm._abandoned_close is True
        with pytest.raises(RuntimeError, match="stuck releasing"):
            warm.acquire(MagicMock())

    def test_close_budget_is_five_seconds_only_on_windows(self):
        import sys as real_sys

        from wayfinder.core import recorder

        assert recorder._MIC_CLOSE_TIMEOUT == (5.0 if real_sys.platform == "win32" else 1.0)


    @patch("wayfinder.core.recorder.get_supported_sample_rate", return_value=48000)
    @patch("wayfinder.core.recorder.sd")
    def test_windows_acquire_retries_when_the_close_finished_during_the_wait(
        self, mock_sd, _rate, monkeypatch
    ):
        """The close returned (its flags cleared under the lock) but _close_done
        was not set yet when acquire's wait gave up: retry, never quarantine."""
        from wayfinder.core import recorder

        monkeypatch.setattr(recorder, "_IS_WINDOWS", True)
        monkeypatch.setattr(recorder, "_MIC_CLOSE_TIMEOUT", 0.05)
        mock_sd.InputStream.return_value = MagicMock(active=True)
        warm = _make_warm(device=7)
        warm._close_done.clear()          # the gap: close done, Event not yet set
        warm._closing_stream = False
        threading.Timer(0.2, warm._close_done.set).start()
        warm.acquire(MagicMock())
        assert warm._abandoned_close is False and mock_sd.InputStream.call_count == 1

    @patch("wayfinder.core.recorder.get_supported_sample_rate", return_value=48000)
    @patch("wayfinder.core.recorder.sd")
    def test_windows_no_stale_restart_notice_after_recovery(self, mock_sd, _rate, monkeypatch):
        from wayfinder.core import recorder

        monkeypatch.setattr(recorder, "_IS_WINDOWS", True)
        notices = []
        warm = _make_warm(device=7, on_restart_required=notices.append)
        warm._abandoned_close = False      # recovered before the notice went out
        warm._notify_restart_required("stale")
        assert notices == []
        warm._abandoned_close = True       # genuinely stuck: still reported
        warm._notify_restart_required("stuck")
        assert notices == ["stuck"]
