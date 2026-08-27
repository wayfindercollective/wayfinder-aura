"""Shared audio-output helpers for mic playback, cues, and release probes.

The recorded mic-test WAV is 16 kHz, while many ALSA hardware devices only
accept 44.1 or 48 kHz.  Desktop audio servers normally resample it, but a raw
device may reject it.  Keep the native-rate retry in one tested function so the
UI and packaged-artifact smoke test exercise the exact same path.
"""

from __future__ import annotations

import shutil
import subprocess
import threading
from dataclasses import dataclass


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


def _is_flatpak_runtime() -> bool:
    from wayfinder.utils.platform import is_wayfinder_flatpak_env

    return is_wayfinder_flatpak_env()


def _play_via_desktop_client(audio, sample_rate: int) -> PlaybackResult:
    """Play through paplay on the desktop PulseAudio/PipeWire graph.

    For AppImages this resolves to the host client with a bundle-clean
    environment.  In Flatpak it resolves to the pinned runtime's client, which
    reaches the host graph through ``--socket=pulseaudio``.
    """
    import numpy as np

    from wayfinder.utils.hostexec import host_env

    env = host_env()
    paplay = shutil.which("paplay", path=env.get("PATH"))
    if not paplay:
        raise FileNotFoundError("paplay is unavailable")

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
        "System default (PulseAudio/PipeWire)",
        False,
    )


def play_blocking(audio, sample_rate: int) -> PlaybackResult:
    """Play mono float audio and wait, retrying at the default sink's native rate.

    Flatpak always uses its runtime's ``paplay`` client. Keeping playback out of
    process is important: PortAudio/ALSA stream closes are process-global and can
    segfault when two Python playback threads finish together. An AppImage also
    prefers the host's ``paplay`` client with every bundle path removed from its
    environment, retaining its PortAudio compatibility fallback. Source runs use
    PortAudio directly.
    """
    import numpy as np

    requested_rate = int(sample_rate)
    if _is_flatpak_runtime():
        # The Flatpak manifest always supplies paplay. Never cross back into
        # PortAudio if the desktop audio graph is unavailable: a clear playback
        # error is safer than reintroducing concurrent native stream teardown.
        return _play_via_desktop_client(audio, requested_rate)
    if _is_appimage_runtime():
        try:
            return _play_via_desktop_client(audio, requested_rate)
        except PlaybackCancelled:
            raise
        except Exception:
            pass
    import sounddevice as sd

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
    if _is_flatpak_runtime():
        # Flatpak playback never owns a sounddevice stream. Calling sd.stop()
        # here can race an unrelated PortAudio user and is exactly the native
        # teardown class this subprocess path is meant to avoid.
        return
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
