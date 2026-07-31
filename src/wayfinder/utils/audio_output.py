"""Shared audio-output helpers for mic playback and package release probes.

The recorded mic-test WAV is 16 kHz, while many ALSA hardware devices only
accept 44.1 or 48 kHz.  Desktop audio servers normally resample it, but a raw
device may reject it.  Keep the native-rate retry in one tested function so the
UI and packaged-artifact smoke test exercise the exact same path.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PlaybackResult:
    requested_rate: int
    used_rate: int
    output_name: str
    resampled: bool


def _default_output(sounddevice) -> tuple[dict, str]:
    info = sounddevice.query_devices(kind="output")
    return info, str(info.get("name", "default output"))


def _resample_linear(audio, source_rate: int, target_rate: int, numpy):
    if source_rate == target_rate or not len(audio):
        return audio
    count = max(1, int(round(len(audio) * target_rate / source_rate)))
    old_positions = numpy.linspace(0.0, 1.0, num=len(audio), endpoint=False)
    new_positions = numpy.linspace(0.0, 1.0, num=count, endpoint=False)
    return numpy.interp(new_positions, old_positions, audio).astype(numpy.float32)


def play_blocking(audio, sample_rate: int) -> PlaybackResult:
    """Play mono float audio and wait, retrying at the default sink's native rate.

    ``sounddevice.play`` deliberately remains the backend: it uses the Flatpak
    runtime's ALSA -> Pulse/PipeWire path in the sandbox and the host-matched
    ALSA plugin path from an AppImage.  No host command or sandbox escape is
    required.
    """
    import numpy as np
    import sounddevice as sd

    requested_rate = int(sample_rate)
    try:
        output_info, output_name = _default_output(sd)
    except Exception:
        output_info, output_name = {}, "default output"

    try:
        sd.play(audio, requested_rate)
        sd.wait()
        return PlaybackResult(requested_rate, requested_rate, output_name, False)
    except sd.PortAudioError:
        # A raw ALSA device can reject 16 kHz even though the desktop audio
        # server accepts it.  Retry once at the advertised native rate.
        native_rate = int(output_info.get("default_samplerate") or 48000)
        converted = _resample_linear(audio, requested_rate, native_rate, np)
        sd.play(converted, native_rate)
        sd.wait()
        return PlaybackResult(requested_rate, native_rate, output_name, True)


def output_self_test(duration_ms: int = 80) -> PlaybackResult:
    """Open and drain the real packaged playback path with a silent 16 kHz clip."""
    import numpy as np

    sample_rate = 16000
    frames = max(1, int(sample_rate * duration_ms / 1000))
    return play_blocking(np.zeros(frames, dtype=np.float32), sample_rate)
