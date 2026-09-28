"""Discarding a recording must not create audio or start transcription."""

from __future__ import annotations

import threading
from types import SimpleNamespace

from wayfinder.core.recorder import AudioRecorder, ChunkedRecorder
from wayfinder_main import AppState, WayfinderApp


class _WarmMic:
    def __init__(self):
        self.released = 0

    def release(self):
        self.released += 1


def test_simple_recorder_cancel_discards_frames_without_wav():
    recorder = AudioRecorder.__new__(AudioRecorder)
    recorder._active = True
    recorder.warm_mic = _WarmMic()
    recorder.stream = None
    recorder.frames = [object()]
    recorder._temp_file = None

    recorder.cancel()

    assert recorder._active is False
    assert recorder.warm_mic.released == 1
    assert recorder.frames == []


def test_chunked_recorder_cancel_discards_buffer_and_chunks():
    recorder = ChunkedRecorder.__new__(ChunkedRecorder)
    recorder._active = True
    recorder._stop_event = threading.Event()
    recorder.warm_mic = _WarmMic()
    recorder._stream = None
    recorder._chunk_thread = None
    recorder._buffer_lock = threading.Lock()
    recorder._buffer = [object()]
    recorder._temp_files = []

    recorder.cancel()

    assert recorder._active is False
    assert recorder._stop_event.is_set()
    assert recorder.warm_mic.released == 1
    assert recorder._buffer == []


def test_app_cancel_returns_to_idle_without_processing():
    events = []
    recorder = SimpleNamespace(cancel=lambda: events.append("discard"))
    app = SimpleNamespace(
        app_state=AppState.RECORDING,
        session_generation=4,
        _duration_update_job=None,
        _recording_start_time=123.0,
        chunked_recorder=None,
        recorder=recorder,
        update_state=lambda state: events.append(state),
        _set_status_indicator=lambda state: events.append(state),
        log=lambda message: events.append(message),
    )

    assert WayfinderApp.cancel_recording(app) is True
    assert app.session_generation == 5
    assert app._recording_start_time is None
    assert events[0] == "discard"
    assert AppState.IDLE in events
    assert "ready" in events
    assert not any("Processing" in str(event) for event in events)
