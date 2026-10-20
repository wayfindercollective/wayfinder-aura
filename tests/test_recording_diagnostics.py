"""Per-chunk level reports and short-lived local recording copies (20 Oct 2026:
47 s of speech came back as 93 characters; nothing showed whether the mic or
the transcriber lost it)."""
import os
import time
import wave
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest

from wayfinder.config import DEFAULT_CONFIG
from wayfinder.core import recording_diagnostics as rd

ROOT = Path(__file__).resolve().parents[1]
RATE = 16000


def write_wav(path, samples, channels=1, width=2):
    data = (np.clip(samples, -1, 1) * 32767).astype("<i2")
    if channels == 2:
        data = np.repeat(data, 2)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(RATE)
        w.writeframes(data.tobytes() if width == 2 else bytes(len(data)))
    return path


def t(seconds):
    return np.arange(int(seconds * RATE)) / RATE


def bursty_speech(seconds, level=0.2, seed=1):
    """Noise floor with voiced bursts (on 70% of every 250 ms)."""
    x = t(seconds)
    out = np.random.default_rng(seed).normal(0, 0.001, x.size)
    burst = (x % 0.25) < 0.175
    out[burst] += level * np.sin(2 * np.pi * 180 * x[burst])
    return out


def continuous_speech(seconds, level=0.2, seed=2):
    """Fluent speech: no real pauses, syllable-rate loudness swings of ~10 dB."""
    x = t(seconds)
    envelope = 0.55 + 0.45 * np.sin(2 * np.pi * 4 * x)
    out = np.random.default_rng(seed).normal(0, 0.001, x.size)
    return out + level * envelope * np.sin(2 * np.pi * 160 * x)


def noisy_room(seconds, seed=3):
    """Steady fan noise only."""
    return np.random.default_rng(seed).normal(0, 0.02, t(seconds).size)


def test_off_by_default():
    assert DEFAULT_CONFIG["keep_recent_recordings_minutes"] == 0


def test_speech_like_seconds(tmp_path):
    quiet = rd.wav_levels(write_wav(tmp_path / "q.wav", np.zeros(RATE * 3)))
    assert quiet.seconds == 3 and quiet.active_seconds == 0
    bursty = rd.wav_levels(write_wav(tmp_path / "b.wav", bursty_speech(15)))
    assert 9 < bursty.active_seconds < 12
    fluent = rd.wav_levels(write_wav(tmp_path / "f.wav", continuous_speech(15)))
    assert fluent.active_seconds >= 9           # the old 12 dB margin scored ~3 s
    fan = rd.wav_levels(write_wav(tmp_path / "n.wav", noisy_room(15)))
    assert fan.active_seconds < 2               # steady noise isn't speech


def test_stereo_and_unsupported_formats(tmp_path):
    stereo = rd.wav_levels(write_wav(tmp_path / "s.wav", bursty_speech(2), channels=2))
    assert 1.9 < stereo.seconds < 2.1
    assert rd.wav_levels(write_wav(tmp_path / "8.wav", np.zeros(100), width=1)) is None
    bad = tmp_path / "bad.wav"
    bad.write_bytes(b"not a wav")
    assert rd.wav_levels(bad) is None
    assert rd.chunk_report(2, "hello", None) == "🔊 Chunk 2: audio unreadable · 1 word"


def test_report_shows_the_mic_level_before_the_boost(tmp_path):
    # Mic peaked at -25 dB; normalization lifted the saved chunk to about -14 dB.
    levels = rd.wav_levels(write_wav(tmp_path / "c.wav", bursty_speech(15)),
                           raw=(10 ** (-25 / 20), 10 ** (-38 / 20)))
    line = rd.chunk_report(1, " ".join(["word"] * 30), levels)
    assert "mic peak -25 dB, level -38 dB (boosted +11 dB)" in line
    assert "⚠" not in line


def test_quiet_mic_is_flagged(tmp_path):
    levels = rd.wav_levels(write_wav(tmp_path / "c.wav", bursty_speech(15)),
                           raw=(10 ** (-40 / 20), 10 ** (-55 / 20)))
    assert "⚠ mic very quiet" in rd.chunk_report(1, " ".join(["word"] * 30), levels)


def test_one_word_for_fifteen_seconds_of_speech_is_flagged(tmp_path):
    for audio in (bursty_speech(15), continuous_speech(15)):
        levels = rd.wav_levels(write_wav(tmp_path / "c.wav", audio))
        line = rd.chunk_report(2, "to", levels)
        assert line.startswith("🔊 Chunk 2: 15.0s · peak ")       # no raw: says "after boost"
        assert "after boost" in line
        assert "1 word ⚠ far fewer words than the speech-like audio" in line


def test_silence_and_fan_noise_are_not_blamed_on_the_transcriber(tmp_path):
    for audio in (np.zeros(RATE * 15), noisy_room(15)):
        levels = rd.wav_levels(write_wav(tmp_path / "q.wav", audio))
        assert "far fewer words" not in rd.chunk_report(3, "", levels)


@pytest.mark.parametrize("value, minutes", [
    (0, 0), (None, 0), ("x", 0), (-5, 0), (float("nan"), 0), (float("inf"), 0),
    (30, 30), ("45", 45), (10 ** 9, rd.MAX_KEEP_MINUTES),
])
def test_keep_minutes_is_sanitized(value, minutes):
    assert rd.keep_minutes(value) == minutes


def test_keep_copies_and_prunes_old_ones(tmp_path):
    src = write_wav(tmp_path / "chunk.wav", bursty_speech(2))
    cache = tmp_path / "cache"
    assert rd.keep_recording(src, cache, 0, session=1, chunk=1) is None   # off
    assert not rd.recent_dir(cache).exists()
    kept = rd.keep_recording(src, cache, 30, session=7, chunk=2,
                             when=datetime(2026, 10, 20, 16, 7, 25))
    assert kept.name == "2026-10-20_160725_s7_chunk2.wav"
    assert kept.read_bytes() == src.read_bytes()
    old = rd.recent_dir(cache) / "old.wav"
    old.write_bytes(b"x")
    hour_ago = time.time() - 3600
    os.utime(old, (hour_ago, hour_ago))
    assert rd.prune_recent(cache, 30) == 1
    assert kept.exists() and not old.exists()


def test_off_or_invalid_setting_deletes_every_copy(tmp_path):
    src = write_wav(tmp_path / "chunk.wav", bursty_speech(1))
    for setting in (0, float("nan")):
        rd.keep_recording(src, tmp_path, 30, session=1, chunk=1)
        assert rd.prune_recent(tmp_path, setting) == 1
        assert list(rd.recent_dir(tmp_path).glob("*.wav")) == []


def test_prune_touches_only_wavs_in_its_folder(tmp_path):
    folder = rd.recent_dir(tmp_path)
    folder.mkdir(parents=True)
    (folder / "notes.txt").write_text("keep")
    (folder / "sub").mkdir()
    (folder / "sub" / "x.wav").write_bytes(b"x")
    (tmp_path / "other.wav").write_bytes(b"x")
    rd.prune_recent(tmp_path, 0)
    assert (folder / "notes.txt").exists() and (folder / "sub" / "x.wav").exists()
    assert (tmp_path / "other.wav").exists()


def test_recorder_measures_the_mic_before_preprocessing(tmp_path, monkeypatch):
    from wayfinder.core import recorder as rec
    from wayfinder.utils import fs_security

    monkeypatch.setattr(fs_security, "get_app_temp_dir", lambda: tmp_path)
    chunker = rec.ChunkedRecorder(sample_rate=RATE, preprocessing="light")
    chunker.recording_sample_rate = RATE
    audio = (0.05 * np.sin(2 * np.pi * 200 * t(1))).astype(np.float32)   # peak -26 dB
    path = chunker._save_chunk(audio)
    peak, rms = chunker.raw_levels[path]
    assert abs(rd._db(peak) - rd._db(0.05)) < 0.5
    assert rms < peak


def test_wired_in_on_windows_only():
    src = (ROOT / "wayfinder_main.py").read_text(encoding="utf-8")
    chunk = src.split("    def _transcribe_chunk(", 1)[1].split("\n    def ", 1)[0]
    # spoken, skipped and failed chunks all get the line and the kept copy
    assert chunk.count("WayfinderApp._chunk_diagnostics(self, chunk_path, chunk_index") == 3
    one_shot = src.split("    def transcribe_and_inject(", 1)[1].split("\n    def ", 1)[0]
    assert "WayfinderApp._chunk_diagnostics(self, audio_path, 0" in one_shot
    for body in (chunk, one_shot):
        for line in [ln for ln in body.splitlines() if "_chunk_diagnostics(" in ln]:
            i = body.splitlines().index(line)
            assert "IS_WINDOWS and not IS_MACOS" in "\n".join(body.splitlines()[i - 4:i])
    assert "self.after(60_000, self._prune_kept_recordings)" in src
