"""Packaged microphone capture and recovery tests."""

import os
import sys
import threading
import time
from unittest.mock import MagicMock, patch

import numpy as np
import pytest


def test_command_capture_stream_delivers_float_frames_and_closes():
    from wayfinder.utils.audio_input import CommandCaptureStream

    received = []
    all_frames_received = threading.Event()

    def callback(data, frames, _time, _status):
        received.append((data.copy(), frames))
        if sum(count for _block, count in received) >= 2048:
            all_frames_received.set()

    command = [
        sys.executable,
        "-c",
        (
            "import sys,time; "
            "sys.stdout.buffer.write(b'\\x00\\x00\\x80?' * 2048); "
            "sys.stdout.buffer.flush(); time.sleep(2)"
        ),
    ]
    stream = CommandCaptureStream(
        "test capture",
        command,
        os.environ.copy(),
        callback,
        channels=1,
        startup_timeout=1.0,
    )

    stream.start()
    assert stream.active
    assert all_frames_received.wait(1.0)
    stream.close()

    assert received
    assert sum(frames for _data, frames in received) == 2048
    assert np.allclose(np.concatenate([data for data, _frames in received]), 1.0)
    assert not stream.active


def test_command_capture_stream_stall_is_killable():
    from wayfinder.utils.audio_input import CommandCaptureStream, PackagedCaptureStalled

    stream = CommandCaptureStream(
        "stalled capture",
        [sys.executable, "-c", "import time; time.sleep(30)"],
        os.environ.copy(),
        MagicMock(),
        channels=1,
        startup_timeout=0.1,
    )
    started = time.monotonic()
    with pytest.raises(PackagedCaptureStalled, match="delivered no microphone frames"):
        stream.start()
    assert time.monotonic() - started < 2.0
    assert not stream.active


def test_saved_raw_alsa_name_maps_to_pipewire_source():
    from wayfinder.core.recorder import _pulse_source_for_preference

    sources = [{
        "name": "alsa_input.usb-Shure_Inc_Shure_MV7-00.mono-fallback",
        "description": "Shure MV7 Mono",
    }]
    with patch("wayfinder.core.recorder._pactl_input_sources", return_value=sources):
        result = _pulse_source_for_preference("Shure MV7: USB Audio (hw:3,0)")

    assert result == "alsa_input.usb-Shure_Inc_Shure_MV7-00.mono-fallback"


def test_warm_mic_prefers_killable_packaged_capture_over_portaudio():
    from wayfinder.core.recorder import WarmMic

    stream = MagicMock()
    stream.active = True
    stream.backend = "host PipeWire"
    sink = MagicMock()

    with (
        patch("wayfinder.core.recorder.should_use_packaged_capture", return_value=True),
        patch(
            "wayfinder.core.recorder._pulse_source_for_preference",
            return_value="shure-source",
        ),
        patch("wayfinder.core.recorder.open_packaged_capture", return_value=stream) as open_capture,
        patch("wayfinder.core.recorder.sd") as sounddevice,
    ):
        warm = WarmMic(device=4, preferred_name="Shure MV7")
        warm.acquire(sink)

    open_capture.assert_called_once_with(
        warm._callback, 16000, 1, source_name="shure-source"
    )
    sounddevice.InputStream.assert_not_called()
    assert warm.is_open


def test_stalled_packaged_capture_does_not_rescan_or_open_raw_alsa():
    from wayfinder.core.recorder import WarmMic
    from wayfinder.utils.audio_input import PackagedCaptureStalled

    with (
        patch("wayfinder.core.recorder.should_use_packaged_capture", return_value=True),
        patch("wayfinder.core.recorder._pulse_source_for_preference", return_value=None),
        patch(
            "wayfinder.core.recorder.open_packaged_capture",
            side_effect=PackagedCaptureStalled("desktop source stalled"),
        ),
        patch("wayfinder.core.recorder._pa_rescan") as rescan,
        patch("wayfinder.core.recorder.sd") as sounddevice,
    ):
        warm = WarmMic(device=4)
        with pytest.raises(PackagedCaptureStalled, match="desktop source stalled"):
            warm.acquire(MagicMock())

    rescan.assert_not_called()
    sounddevice.InputStream.assert_not_called()
