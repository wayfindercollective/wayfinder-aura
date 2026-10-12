"""Capture opens validate the format: never pre-open a throwaway stream."""
import os
import wave
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from wayfinder.core import recorder
from wayfinder.utils import macos_audio


@pytest.fixture
def audio(monkeypatch):
    info = {"name": "Test Mic", "hostapi": 0, "max_input_channels": 1, "default_samplerate": 48000.0}
    streams = []

    def make_stream(**kwargs):
        stream = SimpleNamespace(samplerate=kwargs["samplerate"], start=Mock(), close=Mock(), active=True)
        streams.append(stream)
        return stream

    sd = SimpleNamespace(query_devices=Mock(return_value=info), InputStream=Mock(side_effect=make_stream),
                         check_input_settings=Mock(), PortAudioError=RuntimeError,
                         _terminate=Mock(), _initialize=Mock())
    monkeypatch.setattr(recorder, "sd", sd)
    monkeypatch.setattr(recorder, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(macos_audio, "input_sample_rate", Mock(return_value=48000))
    return sd, info, streams


@pytest.mark.parametrize("platform,expected", [("linux", 16000), ("win32", 16000), ("darwin", 48000)])
def test_first_open_never_runs_format_probe(audio, monkeypatch, platform, expected):
    sd, _, _ = audio
    monkeypatch.setattr(recorder, "sys", SimpleNamespace(platform=platform))
    stream, rate, phases = recorder._open_capture_stream(7, 16000, 1, Mock())
    assert rate == expected and phases["attempts"] == 1
    sd.check_input_settings.assert_not_called()
    sd.InputStream.assert_called_once()
    stream.start.assert_called_once()
    assert sd.InputStream.call_args.kwargs["samplerate"] == expected
    assert sd.InputStream.call_args.kwargs["latency"] == "high"
    assert macos_audio.input_sample_rate.called is (platform == "darwin")


def test_mac_metadata_failure_still_tries_real_capture(audio, monkeypatch):
    sd, _, _ = audio
    monkeypatch.setattr(recorder, "sys", SimpleNamespace(platform="darwin"))
    macos_audio.input_sample_rate.return_value = None
    _, rate, _ = recorder._open_capture_stream(7, 16000, 1, Mock())
    assert rate == 16000
    sd.check_input_settings.assert_not_called()


def test_rate_rejection_uses_legacy_fallback_on_same_device(audio, monkeypatch):
    sd, _, streams = audio
    factory = sd.InputStream.side_effect

    def open_stream(**kwargs):
        if kwargs["samplerate"] == 16000:
            raise RuntimeError("Invalid sample rate", -9997)
        return factory(**kwargs)

    sd.InputStream.side_effect = open_stream
    search = Mock(return_value=48000)
    monkeypatch.setattr(recorder, "get_supported_sample_rate", search)
    _, rate, phases = recorder._open_capture_stream(7, 16000, 2, Mock())
    assert rate == 48000 and phases["attempts"] == 2
    assert [call.kwargs["device"] for call in sd.InputStream.call_args_list] == [7, 7]
    search.assert_called_once_with(7, 16000, channels=2)
    assert len(streams) == 1


@pytest.mark.parametrize("error", [-9996, -9985, -9999])
def test_device_or_permission_failure_does_not_probe_rates(audio, monkeypatch, error):
    sd, _, _ = audio
    sd.InputStream.side_effect = RuntimeError("Cannot open microphone", error)
    search = Mock()
    monkeypatch.setattr(recorder, "get_supported_sample_rate", search)
    with pytest.raises(RuntimeError):
        recorder._open_capture_stream(7, 16000, 1, Mock())
    assert sd.InputStream.call_count == 1
    search.assert_not_called()


def test_successful_fallback_cached_until_device_metadata_or_rescan_changes(audio, monkeypatch):
    sd, info, _ = audio
    factory = sd.InputStream.side_effect

    def open_stream(**kwargs):
        if kwargs["samplerate"] == 16000:
            raise RuntimeError("Invalid sample rate", -9997)
        return factory(**kwargs)

    sd.InputStream.side_effect = open_stream
    search = Mock(return_value=48000)
    monkeypatch.setattr(recorder, "get_supported_sample_rate", search)
    cache = {}
    for _ in range(2):
        recorder._open_capture_stream(7, 16000, 1, Mock(), rate_cache=cache)
    assert [call.kwargs["samplerate"] for call in sd.InputStream.call_args_list] == [16000, 48000, 48000]
    assert search.call_count == 1
    info["name"] = "Different Mic at the Same Index"
    recorder._open_capture_stream(7, 16000, 1, Mock(), rate_cache=cache)
    assert search.call_count == 2
    original_epoch = recorder._PA_DEVICE_EPOCH
    monkeypatch.setattr(recorder, "_PA_DEVICE_EPOCH", original_epoch)
    assert recorder._pa_rescan()
    recorder._open_capture_stream(7, 16000, 1, Mock(), rate_cache=cache)
    assert search.call_count == 3


def test_changed_native_rate_invalidates_mac_cache(audio, monkeypatch):
    sd, _, _ = audio
    monkeypatch.setattr(recorder, "sys", SimpleNamespace(platform="darwin"))
    cache = {}
    recorder._open_capture_stream(7, 16000, 1, Mock(), rate_cache=cache)
    macos_audio.input_sample_rate.return_value = 44100
    recorder._open_capture_stream(7, 16000, 1, Mock(), rate_cache=cache)
    assert [call.kwargs["samplerate"] for call in sd.InputStream.call_args_list] == [48000, 44100]


def test_rejected_cached_rate_is_invalidated_and_reprobed(audio, monkeypatch):
    sd, _, _ = audio
    cache = {}
    recorder._open_capture_stream(7, 16000, 1, Mock(), rate_cache=cache)
    sd.InputStream.side_effect = [RuntimeError("Invalid sample rate", -9997),
                                 SimpleNamespace(samplerate=44100, start=Mock(), close=Mock())]
    monkeypatch.setattr(recorder, "get_supported_sample_rate", Mock(return_value=44100))
    _, rate, _ = recorder._open_capture_stream(7, 16000, 1, Mock(), rate_cache=cache)
    assert rate == 44100 and list(cache.values()) == [44100]


def test_start_failure_closes_handle_before_rate_retry(audio, monkeypatch):
    sd, _, _ = audio
    failed = SimpleNamespace(samplerate=16000, start=Mock(side_effect=RuntimeError("Invalid sample rate", -9997)), close=Mock())
    good = SimpleNamespace(samplerate=48000, start=Mock(), close=Mock())
    sd.InputStream.side_effect = [failed, good]
    monkeypatch.setattr(recorder, "get_supported_sample_rate", Mock(return_value=48000))
    result, _, _ = recorder._open_capture_stream(7, 16000, 1, Mock())
    assert result is good
    failed.close.assert_called_once()


def test_failed_cleanup_quarantines_warmmic_without_rescan(audio, monkeypatch):
    sd, _, _ = audio
    failed = SimpleNamespace(start=Mock(side_effect=RuntimeError("Cannot start", -9997)),
                             close=Mock(side_effect=RuntimeError("Cannot release")))
    sd.InputStream.return_value = failed
    sd.InputStream.side_effect = None
    rescan = Mock()
    monkeypatch.setattr(recorder, "_pa_rescan", rescan)
    monkeypatch.setattr(recorder, "_system_default_input_index", lambda: None)
    warm = recorder.WarmMic(device=7)
    with pytest.raises(recorder.MicrophoneRestartRequired):
        warm.acquire(Mock())
    assert warm._abandoned_close
    assert sd.InputStream.call_count == 1
    rescan.assert_not_called()
    with pytest.raises(recorder.MicrophoneRestartRequired):
        warm.acquire(Mock())
    assert sd.InputStream.call_count == 1


def test_native_capture_resamples_to_whisper_rate(audio, monkeypatch):
    monkeypatch.setattr(recorder, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(recorder, "_system_default_input_index", lambda: None)
    warm = recorder.WarmMic(device=7, idle_secs=0)
    record = recorder.AudioRecorder(warm_mic=warm, preprocessing="off")
    record.start()
    assert record.recording_sample_rate == 48000
    warm._callback(np.full((480, 1), .05, dtype=np.float32), 480, None, None)
    path = record.stop()
    try:
        with wave.open(path) as wav:
            assert wav.getframerate() == 16000
            assert wav.getnframes() == 160
    finally:
        os.unlink(path)  # synthetic test samples only
        warm.close()
