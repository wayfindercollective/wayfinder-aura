"""Per-chunk audio diagnostics and short-lived local copies of recordings.

20 Oct 2026: 47 s of dictation came back as 93 characters (a 15 s chunk gave
the single word "to"). The log couldn't tell a quiet microphone from the
transcriber dropping speech, and the audio was already deleted. So:

* ``chunk_report`` gives each chunk's mic level as captured (before Aura's
  normalization gain, from ``ChunkedRecorder.raw_levels``), the gain applied,
  seconds of speech-like activity and the word count, and flags a very quiet
  mic or a chunk with far fewer words than its activity;
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
# Speech-like frame: 6 dB over the chunk's own quietest frames (its noise
# floor) and above an absolute floor. Continuous speech has few true pauses,
# so a stricter margin undercounts it.
_ACTIVE_OVER_FLOOR = 2.0
_ACTIVE_ABS_FLOOR = 0.003
# Flag a chunk with this much activity but under this many words per second.
_FLAG_MIN_ACTIVE_S = 5.0
_FLAG_WORDS_PER_ACTIVE_S = 0.5
# A mic whose loudest moment stays below this needs its gain raised.
_QUIET_MIC_PEAK_DB = -30.0
MAX_KEEP_MINUTES = 24 * 60
RECENT_DIR_NAME = "recent-recordings"


@dataclass(frozen=True)
class ChunkLevels:
    seconds: float
    peak_db: float          # of the saved (normalized) chunk
    active_seconds: float
    mic_peak_db: float | None = None   # as captured, before normalization
    mic_rms_db: float | None = None


def _db(x: float) -> float:
    return 20.0 * math.log10(max(float(x), 1e-6))


def wav_levels(path: str | Path,
               raw: tuple[float, float] | None = None) -> ChunkLevels | None:
    """Levels of a 16-bit PCM WAV, or None if unreadable. *raw* is the
    (peak, rms) the recorder measured before normalization, if known."""
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
    mic_peak = _db(raw[0]) if raw else None
    mic_rms = _db(raw[1]) if raw else None
    if not rate or samples.size == 0:
        return ChunkLevels(0.0, -120.0, 0.0, mic_peak, mic_rms)
    step = max(1, int(rate * _FRAME_S))
    usable = samples[: samples.size - samples.size % step]
    frame_rms = (np.sqrt(np.mean(usable.reshape(-1, step) ** 2, axis=1))
                 if usable.size else np.zeros(0, dtype=np.float32))
    floor = float(np.percentile(frame_rms, 10)) if frame_rms.size else 0.0
    threshold = max(floor * _ACTIVE_OVER_FLOOR, _ACTIVE_ABS_FLOOR)
    active = int(np.count_nonzero(frame_rms > threshold))
    return ChunkLevels(
        seconds=samples.size / float(rate),
        peak_db=_db(float(np.max(np.abs(samples)))),
        active_seconds=active * step / float(rate),
        mic_peak_db=mic_peak,
        mic_rms_db=mic_rms,
    )


def chunk_report(number: int, text: str, levels: ChunkLevels | None) -> str:
    """One activity-log line for a transcribed chunk (1-based *number*)."""
    words = len((text or "").split())
    count = f"{words} word{'s' if words != 1 else ''}"
    if levels is None:
        return f"🔊 Chunk {number}: audio unreadable · {count}"
    parts = [f"🔊 Chunk {number}: {levels.seconds:.1f}s"]
    if levels.mic_peak_db is not None:
        gain = levels.peak_db - levels.mic_peak_db
        parts.append(f"mic peak {levels.mic_peak_db:.0f} dB, level {levels.mic_rms_db:.0f} dB"
                     + (f" (boosted {gain:+.0f} dB)" if abs(gain) >= 1 else ""))
    else:
        parts.append(f"peak {levels.peak_db:.0f} dB after boost")
    parts.append(f"speech-like ~{levels.active_seconds:.0f}s")
    parts.append(count)
    line = " · ".join(parts)
    flags = []
    if (levels.mic_peak_db is not None and levels.mic_peak_db < _QUIET_MIC_PEAK_DB
            and levels.seconds >= 1):
        flags.append("mic very quiet")
    if (levels.active_seconds >= _FLAG_MIN_ACTIVE_S
            and words < _FLAG_WORDS_PER_ACTIVE_S * levels.active_seconds):
        flags.append("far fewer words than the speech-like audio")
    return line + "".join(f" ⚠ {f}" for f in flags)


def keep_minutes(value) -> float:
    """The setting as minutes: 0 (keep nothing) unless a finite positive
    number, capped at a day."""
    try:
        minutes = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(minutes) or minutes <= 0:
        return 0.0
    return min(minutes, float(MAX_KEEP_MINUTES))


def recent_dir(cache_dir: Path) -> Path:
    return Path(cache_dir) / RECENT_DIR_NAME


def prune_recent(cache_dir: Path, minutes, *, now: float | None = None) -> int:
    """Delete kept recordings older than *minutes* (all of them when the
    setting is off or invalid). Returns how many were removed."""
    minutes = keep_minutes(minutes)
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


def keep_recording(src: str | Path, cache_dir: Path, minutes, *, session: int,
                   chunk: int, when: datetime | None = None) -> Path | None:
    """Copy *src* into the recent-recordings folder (when the setting is on)
    and prune old copies. Returns the copy's path, or None."""
    minutes = keep_minutes(minutes)
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
