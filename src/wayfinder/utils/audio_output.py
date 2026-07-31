"""Shared audio-output helpers for mic playback and package release probes.

The recorded mic-test WAV is 16 kHz, while many ALSA hardware devices only
accept 44.1 or 48 kHz.  Desktop audio servers normally resample it, but a raw
device may reject it.  Keep the native-rate retry in one tested function so the
UI and packaged-artifact smoke test exercise the exact same path.
"""

from __future__ import annotations

from dataclasses import dataclass
import shutil
import subprocess
import threading


class PlaybackCancelled(Exception):
    """Raised internally when the user stops host-command playback."""


_host_process_lock = threading.Lock()
_active_host_process: subprocess.Popen | None = None
_host_stop_requested = threading.Event()


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


def _is_appimage_runtime() -> bool:
    from wayfinder.utils.platform import get_wayfinder_appimage_dir

    return get_wayfinder_appimage_dir() is not None


def _play_via_host(audio, sample_rate: int) -> PlaybackResult:
    """Play through the host Pulse/PipeWire client, outside AppImage libraries."""
    import numpy as np

    from wayfinder.utils.hostexec import host_env

    env = host_env()
    paplay = shutil.which("paplay", path=env.get("PATH"))
    if not paplay:
        raise FileNotFoundError("host paplay is unavailable")

    samples = np.asarray(audio, dtype="<f4").reshape(-1)
    timeout = max(5.0, samples.size / max(1, sample_rate) + 5.0)
    command = [
        paplay,
        "--playback",
        "--raw",
        "--format=float32le",
        f"--rate={int(sample_rate)}",
        "--channels=1",
        "--stream-name=Wayfinder Aura Mic Test",
    ]

    global _active_host_process
    _host_stop_requested.clear()
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        env=env,
    )
    with _host_process_lock:
        _active_host_process = process
    try:
        _stdout, stderr = process.communicate(samples.tobytes(), timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        process.kill()
        process.communicate()
        raise RuntimeError("host audio playback timed out") from exc
    finally:
        with _host_process_lock:
            if _active_host_process is process:
                _active_host_process = None

    if process.returncode != 0:
        if _host_stop_requested.is_set():
            raise PlaybackCancelled()
        detail = (stderr or b"").decode("utf-8", "replace").strip()
        raise RuntimeError(detail or f"paplay exited with status {process.returncode}")
    return PlaybackResult(
        int(sample_rate),
        int(sample_rate),
        "System default (host PulseAudio/PipeWire)",
        False,
    )


def play_blocking(audio, sample_rate: int) -> PlaybackResult:
    """Play mono float audio and wait, retrying at the default sink's native rate.

    Flatpak/source builds use ``sounddevice``. An AppImage instead prefers the
    host's ``paplay`` client with every bundle path removed from its environment.
    This avoids loading Fedora/Bazzite's PipeWire ALSA plugin into the AppImage's
    older, self-contained recording stack. If the host client is absent or
    refuses playback, the normal PortAudio path remains a compatibility fallback.
    """
    import numpy as np
    import sounddevice as sd

    requested_rate = int(sample_rate)
    if _is_appimage_runtime():
        try:
            return _play_via_host(audio, requested_rate)
        except PlaybackCancelled:
            raise
        except Exception:
            pass
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


def stop_playback() -> None:
    """Stop either the AppImage host player or sounddevice's shared playback."""
    _host_stop_requested.set()
    with _host_process_lock:
        process = _active_host_process
    if process is not None and process.poll() is None:
        try:
            process.terminate()
        except OSError:
            pass
    try:
        import sounddevice as sd

        sd.stop()
    except Exception:
        pass


def output_self_test(duration_ms: int = 80) -> PlaybackResult:
    """Open and drain the real packaged playback path with a silent 16 kHz clip."""
    import numpy as np

    sample_rate = 16000
    frames = max(1, int(sample_rate * duration_ms / 1000))
    return play_blocking(np.zeros(frames, dtype=np.float32), sample_rate)
