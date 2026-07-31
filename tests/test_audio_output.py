"""Audio playback policy tests (hardware-free; sounddevice is faked)."""

from __future__ import annotations

import sys
from types import ModuleType

import numpy as np
import pytest

from wayfinder.utils import audio_output


class FakePortAudioError(Exception):
    pass


def _fake_sounddevice(*, fail_rates=()):
    module = ModuleType("sounddevice")
    module.PortAudioError = FakePortAudioError
    module.calls = []
    module.waits = 0

    def query_devices(*, kind):
        assert kind == "output"
        return {"name": "PipeWire Default", "default_samplerate": 48000.0}

    def play(samples, rate):
        module.calls.append((np.asarray(samples).copy(), int(rate)))
        if int(rate) in fail_rates:
            raise FakePortAudioError(f"rate {rate} refused")

    def wait():
        module.waits += 1

    module.query_devices = query_devices
    module.play = play
    module.wait = wait
    return module


def test_playback_uses_recording_rate_when_audio_server_accepts_it(monkeypatch):
    fake = _fake_sounddevice()
    monkeypatch.setitem(sys.modules, "sounddevice", fake)
    samples = np.linspace(-0.5, 0.5, 160, dtype=np.float32)

    result = audio_output.play_blocking(samples, 16000)

    assert [rate for _audio, rate in fake.calls] == [16000]
    assert fake.waits == 1
    assert result.output_name == "PipeWire Default"
    assert result.resampled is False


def test_playback_retries_at_default_sink_rate_and_preserves_duration(monkeypatch):
    fake = _fake_sounddevice(fail_rates={16000})
    monkeypatch.setitem(sys.modules, "sounddevice", fake)
    samples = np.linspace(-0.5, 0.5, 160, dtype=np.float32)

    result = audio_output.play_blocking(samples, 16000)

    assert [rate for _audio, rate in fake.calls] == [16000, 48000]
    assert len(fake.calls[1][0]) == 480
    assert fake.waits == 1
    assert result.used_rate == 48000
    assert result.resampled is True


def test_playback_surfaces_failure_when_native_rate_also_cannot_open(monkeypatch):
    fake = _fake_sounddevice(fail_rates={16000, 48000})
    monkeypatch.setitem(sys.modules, "sounddevice", fake)

    with pytest.raises(FakePortAudioError, match="48000"):
        audio_output.play_blocking(np.zeros(160, dtype=np.float32), 16000)


def test_packaged_self_test_uses_real_mic_test_starting_rate(monkeypatch):
    captured = {}

    def fake_play(samples, rate):
        captured["frames"] = len(samples)
        captured["rate"] = rate
        return audio_output.PlaybackResult(rate, rate, "test", False)

    monkeypatch.setattr(audio_output, "play_blocking", fake_play)

    result = audio_output.output_self_test(duration_ms=80)

    assert captured == {"frames": 1280, "rate": 16000}
    assert result.output_name == "test"
