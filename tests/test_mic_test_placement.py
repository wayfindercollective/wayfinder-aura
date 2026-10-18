"""The Mic Test lives on the Dictate tab, and never runs beside a real dictation."""

import types
from pathlib import Path

import pytest

import wayfinder_main as wm

SRC = (Path(__file__).resolve().parents[1] / "wayfinder_main.py").read_text()


def _method(name: str) -> str:
    """Source of one WayfinderApp method (up to the next method)."""
    body = SRC.split(f"    def {name}(", 1)[1]
    return body.split("\n    def ", 1)[0]


class _Label:
    def __init__(self):
        self.calls = []

    def configure(self, **kwargs):
        self.calls.append(kwargs)


class TestPlacement:
    def test_dictate_tab_builds_the_mic_test(self):
        assert "self._build_audio_calibration_section(mic_card)" in _method("_create_dictate_tab")

    def test_settings_no_longer_builds_it(self):
        assert "_build_audio_calibration_section" not in _method("_create_settings_tab_steps")

    def test_the_50ms_meter_loop_keeps_its_whitelisted_name(self):
        # test_design_ratchet whitelists the sub-100 ms timer by method name.
        assert "self.after(50, self._update_mic_test)" in _method("_update_mic_test")


class TestStartGuard:
    def _app(self, state):
        status = _Label()
        return types.SimpleNamespace(app_state=state, _mic_test_status=status), status

    @pytest.mark.parametrize("state", [
        wm.AppState.RECORDING, wm.AppState.PROCESSING, wm.AppState.PASTING,
    ])
    def test_refuses_to_open_a_second_stream_during_a_dictation(self, state, monkeypatch):
        def _no_recorder(*args, **kwargs):
            raise AssertionError("opened a recorder during a dictation")

        monkeypatch.setattr(wm, "AudioRecorder", _no_recorder)
        app, status = self._app(state)
        wm.WayfinderApp._start_mic_test(app)
        assert status.calls and "dictation" in status.calls[-1]["text"]
        assert not getattr(app, "_mic_test_recording", False)


class TestTeardown:
    def test_leaving_the_dictate_tab_stops_a_running_test(self):
        body = _method("_switch_tab")
        guard = body.index('tab_id != "dictate" and getattr(self, "_mic_test_recording", False)')
        assert "self._stop_mic_test()" in body[guard:guard + 300]
        # Before the licence gate and the early returns, so it always runs.
        assert guard < body.index("feature_for_tab(tab_id)")

    def test_a_dictation_stops_a_running_test_before_any_capture_begins(self):
        body = _method("start_recording")
        guard = body.index('if getattr(self, "_mic_test_recording", False):')
        assert "self._stop_mic_test()" in body[guard:guard + 200]
        # On macOS/Windows capture starts BEFORE update_state(), so the stop must
        # come ahead of the first capture call, not ride on the state change.
        first_capture_call = body.index("\n                _begin_capture()")
        assert guard < first_capture_call
        assert guard < body.index("self.update_state(AppState.RECORDING)")

    def test_update_state_does_not_stop_the_test(self):
        # Regression (review finding): stopping from update_state() ran after the
        # dictation's capture had attached, and detached its sink.
        assert "_stop_mic_test" not in _method("update_state")

    def test_start_recording_is_the_only_way_into_recording(self):
        assert SRC.count("update_state(AppState.RECORDING)") == 1


class TestWhyTheOrderMatters:
    """WarmMic.release() detaches whatever sink is attached, not just the caller's."""

    def test_stopping_the_test_after_a_dictation_attached_drops_the_dictation(self):
        from unittest.mock import MagicMock, patch

        from wayfinder.core.recorder import WarmMic

        with patch("wayfinder.core.recorder.get_supported_sample_rate", return_value=48000), \
                patch("wayfinder.core.recorder.sd"):
            warm = WarmMic(device=7, sample_rate=16000, idle_secs=30.0)
            mic_test_sink, dictation_sink = MagicMock(), MagicMock()
            warm.acquire(mic_test_sink)
            # Wrong order (the bug): the dictation attaches, then the test stops.
            warm.acquire(dictation_sink)
            warm.release()  # what AudioRecorder.stop() does for the mic test
            assert warm.in_use is False  # the dictation lost its sink

            # Right order: the test stops first, then the dictation attaches.
            warm.acquire(mic_test_sink)
            warm.release()
            warm.acquire(dictation_sink)
            assert warm.in_use is True
