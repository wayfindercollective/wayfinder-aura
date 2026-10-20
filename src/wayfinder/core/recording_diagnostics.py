"""Per-chunk audio diagnostics and short-lived local copies of recordings.

20 Oct 2026: 47 s of dictation came back as 93 characters (a 15 s chunk gave
the single word "to"). The log couldn't tell a quiet microphone from the
transcriber dropping speech, and the audio was already deleted. So:

* ``chunk_report`` measures each chunk (length, level, seconds that sound like
  voice) and flags one whose words are far too few for its voice-like audio;
* ``keep_recording`` copies a chunk into ``recent-recordings`` in the cache
  dir and deletes copies older than the configured minutes. Off by default
  (``keep_recent_recordings_minutes`` = 0): audio stays on this machine only.
"""
from __future__ import annotations

import math
import shutil
import time
import wave
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np

_FRAME_S = 0.02
# Voice-like frame: well above the recording's own noise floor (robust to the
# light normalization chunks get) and above an absolute floor (-46 dBFS).
_VOICE_OVER_FLOOR = 4.0
_VOICE_ABS_FLOOR = 0.005
# Flag a chunk with this much voice-like audio but under this many words/s.
_FLAG_MIN_VOICE_S = 5.0
_FLAG_WORDS_PER_VOICE_S = 0.5
RECENT_DIR_NAME = "recent-recordings"


@dataclass(frozen=True)
class ChunkLevels:
    seconds: float
    rms_db: float
    peak_db: float
    voice_seconds: float


def _db(x: float) -> float:
    return 20.0 * math.log10(max(x, 1e-6))


def wav_levels(path: str | Path) -> ChunkLevels | None:
    """Levels of a 16-bit PCM WAV, or None if it can't be read."""
    try:
        with wave.open(str(path), "rb") as wav:
            if wav.getsampwidth() != 2:
                return None
            rate = wav.getframerate()
            channels = wav.getnchannels()
            frames = wav.readframes(wav.getnframes())
    except (OSError, EOFError, wave.Error):
        return None
    samples = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    if channels > 1:
        samples = samples[: samples.size - samples.size % channels].reshape(-1, channels).mean(axis=1)
    if not rate or samples.size == 0:
        return ChunkLevels(0.0, -120.0, -120.0, 0.0)
    step = max(1, int(rate * _FRAME_S))
    usable = samples[: samples.size - samples.size % step]
    frame_rms = (np.sqrt(np.mean(usable.reshape(-1, step) ** 2, axis=1))
                 if usable.size else np.zeros(0, dtype=np.float32))
    floor = float(np.percentile(frame_rms, 10)) if frame_rms.size else 0.0
    threshold = max(floor * _VOICE_OVER_FLOOR, _VOICE_ABS_FLOOR)
    voice_frames = int(np.count_nonzero(frame_rms > threshold))
    return ChunkLevels(
        seconds=samples.size / float(rate),
        rms_db=_db(float(np.sqrt(np.mean(samples ** 2)))),
        peak_db=_db(float(np.max(np.abs(samples)))),
        voice_seconds=voice_frames * step / float(rate),
    )


def chunk_report(number: int, text: str, levels: ChunkLevels | None) -> str:
    """One activity-log line for a transcribed chunk (1-based *number*)."""
    words = len((text or "").split())
    count = f"{words} word{'s' if words != 1 else ''}"
    if levels is None:
        return f"🔊 Chunk {number}: audio unreadable · {count}"
    line = (f"🔊 Chunk {number}: {levels.seconds:.1f}s · level {levels.rms_db:.0f} dB "
            f"(peak {levels.peak_db:.0f} dB) · voice ~{levels.voice_seconds:.0f}s · {count}")
    if (levels.voice_seconds >= _FLAG_MIN_VOICE_S
            and words < _FLAG_WORDS_PER_VOICE_S * levels.voice_seconds):
        line += " ⚠ far fewer words than the voice in this audio"
    return line


def recent_dir(cache_dir: Path) -> Path:
    return Path(cache_dir) / RECENT_DIR_NAME


def prune_recent(cache_dir: Path, minutes: float, *, now: float | None = None) -> int:
    """Delete kept recordings older than *minutes* (all of them if 0). Count removed."""
    folder = recent_dir(cache_dir)
    if not folder.is_dir():
        return 0
    now = time.time() if now is None else now
    removed = 0
    for path in folder.glob("*.wav"):
        try:
            if minutes <= 0 or now - path.stat().st_mtime > minutes * 60:
                path.unlink()
                removed += 1
        except OSError:
            pass
    return removed


def keep_recording(src: str | Path, cache_dir: Path, minutes: float, *, session: int,
                   chunk: int, when: datetime | None = None) -> Path | None:
    """Copy *src* into the recent-recordings folder (if *minutes* > 0) and
    prune old copies. Returns the copy's path, or None."""
    if minutes <= 0:
        return None
    try:
        folder = recent_dir(cache_dir)
        folder.mkdir(parents=True, exist_ok=True)
        stamp = (when or datetime.now()).strftime("%Y-%m-%d_%H%M%S")
        dest = folder / f"{stamp}_s{session}_chunk{chunk}.wav"
        shutil.copyfile(src, dest)
        prune_recent(cache_dir, minutes)
        return dest
    except OSError:
        return None
