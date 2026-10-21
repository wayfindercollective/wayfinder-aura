"""core.audio_dsp on the recorder's awkward inputs, without SciPy installed.

The parity checks against scipy.signal live in test_audio_dsp.py and skip when
SciPy is absent; these run everywhere, including the Windows CI job.
"""

import time

import numpy as np
import pytest

from wayfinder.core import audio_dsp, recorder


@pytest.mark.parametrize("length", [0, 1, 3, 9, 10, 100])
@pytest.mark.parametrize("level", ["off", "light", "medium", "heavy"])
def test_preprocess_keeps_length_and_dtype_on_short_clips(length, level):
    audio = (np.random.default_rng(length).normal(0, 0.1, length)).astype(np.float32)
    out = recorder.preprocess_audio(audio, 16000, level=level)
    assert out.dtype == np.float32 and len(out) == length
    assert np.all(np.isfinite(out))


@pytest.mark.parametrize("length", [0, 1, 2, 3, 47999, 48000])
def test_resample_to_whisper_rate_has_the_expected_length(length):
    out = recorder.resample_audio(np.zeros(length, np.float32), 48000, 16000)
    assert out.dtype == np.float32 and len(out) == -(-length // 3)


def test_rumble_filter_removes_hum_and_keeps_voice():
    rate = 16000
    t = np.arange(rate * 2) / rate
    hum, voice = np.sin(2 * np.pi * 20 * t), np.sin(2 * np.pi * 300 * t)
    b, a = audio_dsp.butter(2, recorder.RUMBLE_CUTOFF_HZ / (rate / 2), btype="high")
    out = audio_dsp.filtfilt(b, a, 0.3 * hum + 0.3 * voice)
    spectrum = np.abs(np.fft.rfft(out)) / len(out)
    assert spectrum[40] < 0.02 * spectrum[600]  # bins at 20 Hz and 300 Hz (0.5 Hz each)


def test_unstable_filter_is_refused_not_hung():
    with pytest.raises(ValueError):
        audio_dsp.lfilter([1.0], [1.0, -1.01], np.ones(32))


@pytest.mark.perf
def test_five_minute_rumble_filter_stays_fast():
    rate = 16000
    x = np.random.default_rng(3).normal(0, 0.1, rate * 300)
    b, a = audio_dsp.butter(2, recorder.RUMBLE_CUTOFF_HZ / (rate / 2), btype="high")
    audio_dsp.filtfilt(b, a, x[:rate])
    start = time.perf_counter()
    audio_dsp.filtfilt(b, a, x)
    assert time.perf_counter() - start < 1.0
