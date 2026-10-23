"""The NumPy DSP fallback must match scipy.signal on the recorder's real inputs."""

import numpy as np
import pytest

from wayfinder.core import audio_dsp

signal = pytest.importorskip("scipy.signal")


def _speechlike(rate: int, seconds: float, seed: int = 7) -> np.ndarray:
    rng = np.random.default_rng(seed)
    t = np.arange(int(rate * seconds)) / rate
    voice = sum(np.sin(2 * np.pi * f * t) * a for f, a in ((140, 0.3), (410, 0.2), (2600, 0.05)))
    rumble = 0.2 * np.sin(2 * np.pi * 25 * t)
    return (voice * (0.5 + 0.5 * np.sin(2 * np.pi * 3 * t)) + rumble
            + rng.normal(0, 0.01, t.size)).astype(np.float32)


@pytest.mark.parametrize("wn", [80 / 8000, 80 / 22050, 0.25])
@pytest.mark.parametrize("btype", ["high", "low"])
def test_butter_matches_scipy(wn, btype):
    b, a = audio_dsp.butter(2, wn, btype=btype)
    sb, sa = signal.butter(2, wn, btype=btype)
    np.testing.assert_allclose(b, sb, rtol=1e-12, atol=1e-15)
    np.testing.assert_allclose(a, sa, rtol=1e-12, atol=1e-15)


def test_lfilter_zi_matches_scipy():
    b, a = signal.butter(2, 80 / 8000, btype="high")
    np.testing.assert_allclose(audio_dsp.lfilter_zi(b, a), signal.lfilter_zi(b, a), rtol=1e-10)


@pytest.mark.parametrize("seconds", [0.002, 0.5, 7.3])
def test_filtfilt_matches_scipy_on_the_rumble_filter(seconds):
    # The recorder's Medium/Heavy rumble filter: 2nd-order 80 Hz high-pass at 16 kHz.
    audio = _speechlike(16000, seconds)
    b, a = signal.butter(2, 80 / 8000, btype="high")
    ours = audio_dsp.filtfilt(b, a, audio)
    np.testing.assert_allclose(ours, signal.filtfilt(b, a, audio), rtol=0, atol=1e-9)


def test_filtfilt_rejects_input_shorter_than_its_padding():
    b, a = audio_dsp.butter(2, 0.01, btype="high")
    with pytest.raises(ValueError):
        audio_dsp.filtfilt(b, a, np.zeros(9))


@pytest.mark.parametrize("orig", [48000, 44100, 22050, 32000, 96000, 8000])
def test_resample_poly_matches_scipy_for_every_device_rate(orig):
    from math import gcd

    audio = _speechlike(orig, 1.37)
    g = gcd(orig, 16000)
    up, down = 16000 // g, orig // g
    ours = audio_dsp.resample_poly(audio, up, down)
    theirs = signal.resample_poly(audio.astype(np.float64), up, down)
    assert ours.shape == theirs.shape
    np.testing.assert_allclose(ours, theirs, rtol=0, atol=1e-9)


def test_recorder_runs_on_the_numpy_twins():
    # No bundle ships SciPy any more: the recorder must not reach for it.
    from wayfinder.core import recorder

    assert recorder.butter is audio_dsp.butter
    assert recorder.filtfilt is audio_dsp.filtfilt
    assert recorder.resample_poly is audio_dsp.resample_poly
    audio = _speechlike(48000, 1.0)
    out = recorder.resample_audio(audio, 48000, 16000)
    assert out.dtype == np.float32 and len(out) == 16000
    medium = recorder.preprocess_audio(_speechlike(16000, 1.0), 16000, "medium")
    spectrum = np.abs(np.fft.rfft(medium))
    assert spectrum[25] < spectrum[140] * 0.2  # 25 Hz rumble removed, 140 Hz voice kept


@pytest.mark.parametrize("length", [0, 1, 2, 5, 37])
@pytest.mark.parametrize("up, down", [(1, 3), (160, 441), (2, 3), (1, 1)])
def test_resample_poly_matches_scipy_on_tiny_inputs(length, up, down):
    # The recorder no longer falls back to np.interp: these run on every
    # clipped, aborted or near-silent recording.
    x = np.linspace(-1.0, 1.0, length)
    expected = signal.resample_poly(x, up, down)
    got = audio_dsp.resample_poly(x, up, down)
    assert got.shape == expected.shape
    np.testing.assert_allclose(got, expected, atol=1e-9)


@pytest.mark.parametrize("seconds", [60, 300])
def test_filtfilt_matches_scipy_on_long_recordings(seconds):
    # Overlap-add blocks: block edges must not show in a long dictation.
    rate = 16000
    x = _speechlike(rate, seconds).astype(np.float64)
    b, a = signal.butter(2, 80 / (rate / 2), btype="high")
    np.testing.assert_allclose(
        audio_dsp.filtfilt(b, a, x), signal.filtfilt(b, a, x), rtol=0, atol=1e-9
    )
