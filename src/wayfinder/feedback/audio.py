"""Short audio cues for Game Mode dictation.

In SteamOS Game Mode the PyQt overlay can't render over a fullscreen game
(gamescope shows a single surface), so the only feedback the user can perceive
is sound. These cues mark the dictation lifecycle:

    start  -> recording began
    stop   -> recording ended, transcribing
    done   -> text was injected successfully
    error  -> dictation failed / nothing landed

Design constraints (see docs/superpowers/specs and CLAUDE.md):

* Must work INSIDE the Flatpak sandbox. The sandbox grants PulseAudio/PipeWire
  access and the app already ships ``sounddevice`` (PortAudio) for recording,
  so playback goes through the same stack — no dependency on host binaries
  (``canberra-gtk-play`` etc.) that may be absent from the runtime.
* Fire-and-forget and NON-blocking: cues run on a daemon thread so the Tk UI
  thread is never held up.
* SILENT no-op on any failure (no output device, device busy, missing file,
  PortAudio error). A missing chime must never break dictation.
* Uses a dedicated short-lived ``OutputStream`` rather than the module-global
  ``sd.play``/``sd.stop`` (which the mic-test UI uses), so a cue can never cut
  off other playback and is independent of the recorder's input stream.

WAVs live in ``sounds/`` next to this module and ship via the Flatpak
manifest's existing ``cp -r src/wayfinder`` (no manifest change).
"""

from __future__ import annotations

import threading
import wave
from pathlib import Path

_SOUNDS_DIR = Path(__file__).parent / "sounds"
_CUES = ("start", "stop", "done", "error")

# name -> (float32 mono ndarray, samplerate). Populated lazily / by prewarm().
_cache: dict[str, tuple] = {}
_cache_lock = threading.Lock()


def _load(name: str):
    """Decode a cue WAV to (float32 samples, samplerate), cached. None on failure."""
    with _cache_lock:
        if name in _cache:
            return _cache[name]
    try:
        import numpy as np

        path = _SOUNDS_DIR / f"{name}.wav"
        with wave.open(str(path), "rb") as wf:
            sr = wf.getframerate()
            n_channels = wf.getnchannels()
            frames = wf.readframes(wf.getnframes())
        audio = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
        if n_channels > 1:
            audio = audio.reshape(-1, n_channels).mean(axis=1)
        result = (audio, sr)
    except Exception:
        return None
    with _cache_lock:
        _cache[name] = result
    return result


def _play_blocking(name: str) -> None:
    data = _load(name)
    if data is None:
        return
    audio, sr = data
    try:
        import sounddevice as sd

        # Dedicated stream (not sd.play's shared global): isolated from the
        # mic-test playback and from the recorder's input stream.
        stream = sd.OutputStream(samplerate=sr, channels=1, dtype="float32")
        stream.start()
        try:
            stream.write(audio.reshape(-1, 1))
        finally:
            stream.stop()
            stream.close()
    except Exception:
        pass  # no device / busy / PortAudio error — stay silent


def play_cue(name: str) -> None:
    """Play a named cue (``start``/``stop``/``done``/``error``), fire-and-forget.

    Never blocks the caller and never raises — a failed cue is silent.
    """
    if name not in _CUES:
        return
    threading.Thread(
        target=_play_blocking, args=(name,), name=f"wf-cue-{name}", daemon=True
    ).start()


def prewarm() -> None:
    """Decode all cue WAVs into memory so the hot path does no disk I/O."""
    for name in _CUES:
        _load(name)
