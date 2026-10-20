"""Per-chunk level reports and short-lived local recording copies (20 Oct 2026:
47 s of speech came back as 93 characters; nothing showed whether the mic or
the transcriber lost it)."""
import os
import time
import wave
from datetime import datetime
from pathlib import Path

import numpy as np

from wayfinder.config import DEFAULT_CONFIG
from wayfinder.core import recording_diagnostics as rd

ROOT = Path(__file__).resolve().parents[1]
RATE = 16000


def write_wav(path, samples):
    data = (np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes()
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(data)
    return path


def speechlike(seconds, voiced_fraction=0.7, level=0.2, seed=1):
    """Noise floor with voiced bursts (on/off every 250 ms)."""
    rng = np.random.default_rng(seed)
    n = int(seconds * RATE)
    out = rng.normal(0, 0.001, n).astype(np.float32)
    t = np.arange(n) / RATE
    burst = (t % 0.25) < 0.25 * voiced_fraction
    out[burst] += (level * np.sin(2 * np.pi * 180 * t[burst])).astype(np.float32)
    return out


def test_off_by_default():
    assert DEFAULT_CONFIG["keep_recent_recordings_minutes"] == 0


def test_levels_of_silence_and_speech(tmp_path):
    quiet = rd.wav_levels(write_wav(tmp_path / "q.wav", np.zeros(RATE * 3)))
    assert quiet.seconds == 3 and quiet.voice_seconds == 0
    talk = rd.wav_levels(write_wav(tmp_path / "t.wav", speechlike(15)))
    assert 14.9 < talk.seconds < 15.1
    assert 8 < talk.voice_seconds < 12          # ~70% voiced
    assert talk.peak_db > -16 and talk.rms_db < talk.peak_db


def test_unreadable_audio_is_reported_not_raised(tmp_path):
    bad = tmp_path / "bad.wav"
    bad.write_bytes(b"not a wav")
    assert rd.wav_levels(bad) is None
    assert rd.chunk_report(2, "hello", None) == "🔊 Chunk 2: audio unreadable · 1 word"


def test_one_word_for_fifteen_seconds_of_voice_is_flagged(tmp_path):
    levels = rd.wav_levels(write_wav(tmp_path / "c.wav", speechlike(15)))
    line = rd.chunk_report(2, "to", levels)
    assert line.startswith("🔊 Chunk 2: 15.0s · level ")
    assert "1 word ⚠ far fewer words than the voice in this audio" in line


def test_normal_chunk_and_quiet_chunk_are_not_flagged(tmp_path):
    levels = rd.wav_levels(write_wav(tmp_path / "c.wav", speechlike(15)))
    assert "⚠" not in rd.chunk_report(1, " ".join(["word"] * 30), levels)
    quiet = rd.wav_levels(write_wav(tmp_path / "q.wav", np.zeros(RATE * 15)))
    assert "⚠" not in rd.chunk_report(3, "", quiet)   # nothing to hear: not the transcriber


def test_keep_copies_and_prunes_old_ones(tmp_path):
    src = write_wav(tmp_path / "chunk.wav", speechlike(2))
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


def test_turning_it_off_deletes_every_copy(tmp_path):
    src = write_wav(tmp_path / "chunk.wav", speechlike(1))
    rd.keep_recording(src, tmp_path, 30, session=1, chunk=1)
    assert rd.prune_recent(tmp_path, 0) == 1
    assert list(rd.recent_dir(tmp_path).glob("*.wav")) == []


def test_wired_into_chunk_transcription_on_windows_only():
    src = (ROOT / "wayfinder_main.py").read_text(encoding="utf-8")
    body = src.split("    def _transcribe_chunk(", 1)[1].split("\n    def ", 1)[0]
    assert body.count("if IS_WINDOWS and not IS_MACOS:\n") == 2
    assert body.count("WayfinderApp._chunk_diagnostics(self, chunk_path, chunk_index") == 2
    assert "prune_recent(get_cache_dir()" in src
