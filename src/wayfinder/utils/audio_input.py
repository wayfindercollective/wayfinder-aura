"""Killable packaged-audio capture for Linux desktop bundles.

An AppImage must not open a raw ALSA ``hw:`` device while PipeWire owns the
same microphone. On Bazzite/SteamOS that open can block inside PortAudio and,
because the C call is not cancellable, poison microphone capture until the app
restarts. Run the host's PipeWire/Pulse client as a subprocess instead: it
shares the desktop audio graph normally and a stalled client can be terminated.

The Flatpak uses its runtime's ``parec`` through ``--socket=pulseaudio``. The
AppImage prefers the host's native ``pw-record`` and falls back to ``parec``.
Source runs keep their existing PortAudio path.
"""

from __future__ import annotations

from dataclasses import dataclass
import shutil
import subprocess
import threading
import time
from typing import Callable

import numpy as np


class PackagedCaptureUnavailable(RuntimeError):
    """No packaged capture client/server is available; PortAudio may be tried."""


class PackagedCaptureStalled(RuntimeError):
    """A desktop audio client connected but delivered no frames."""


@dataclass(frozen=True)
class InputSelfTestResult:
    backend: str
    frames: int
    source_name: str


def should_use_packaged_capture() -> bool:
    """Whether this process is the Wayfinder AppImage or Flatpak."""
    from wayfinder.utils.platform import get_wayfinder_appimage_dir, is_wayfinder_flatpak_env

    return get_wayfinder_appimage_dir() is not None or is_wayfinder_flatpak_env()


def _capture_candidates(
    sample_rate: int,
    channels: int,
    source_name: str | None,
) -> list[tuple[str, list[str], dict]]:
    """Return backend/argv/environment candidates in platform preference order."""
    from wayfinder.utils.hostexec import host_env
    from wayfinder.utils.platform import get_wayfinder_appimage_dir, is_wayfinder_flatpak_env

    env = host_env()
    candidates: list[tuple[str, list[str], dict]] = []
    is_appimage = get_wayfinder_appimage_dir() is not None
    is_flatpak = is_wayfinder_flatpak_env()

    # AppImage: use the host's native PipeWire client first. host_env keeps
    # Fedora/Bazzite libraries from mixing with the Ubuntu-built bundle.
    if is_appimage:
        pw_record = shutil.which("pw-record", path=env.get("PATH"))
        if pw_record:
            command = [
                pw_record,
                "--raw",
                "--rate", str(int(sample_rate)),
                "--channels", str(int(channels)),
                "--format", "f32",
            ]
            if source_name:
                command.extend(["--target", source_name])
            command.append("-")
            candidates.append(("host PipeWire", command, env))

    # Flatpak: parec belongs to the pinned runtime and reaches the host through
    # --socket=pulseaudio. AppImage uses the host copy as a fallback.
    if is_appimage or is_flatpak:
        parec = shutil.which("parec", path=env.get("PATH"))
        if parec:
            command = [
                parec,
                "--record",
                "--raw",
                "--format=float32le",
                f"--rate={int(sample_rate)}",
                f"--channels={int(channels)}",
                "--latency-msec=100",
                "--process-time-msec=20",
                "--client-name=Wayfinder Aura",
                "--stream-name=Wayfinder Aura Dictation",
            ]
            if source_name:
                command.append(f"--device={source_name}")
            candidates.append(("PulseAudio/PipeWire", command, env))

    return candidates


class CommandCaptureStream:
    """Small ``sounddevice.InputStream``-compatible subprocess adapter."""

    def __init__(
        self,
        backend: str,
        command: list[str],
        env: dict,
        callback: Callable,
        channels: int,
        startup_timeout: float = 2.5,
    ):
        self.backend = backend
        self.command = command
        self.env = env
        self.callback = callback
        self.channels = channels
        self.startup_timeout = startup_timeout
        self._process: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None
        self._stderr_reader: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._startup_event = threading.Event()
        self._received_frames = False
        self._stderr = bytearray()

    @property
    def active(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def _read_audio(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            self._startup_event.set()
            return
        bytes_per_frame = 4 * self.channels
        pending = b""
        try:
            while not self._stop_event.is_set():
                data = process.stdout.read(1024 * bytes_per_frame)
                if not data:
                    break
                pending += data
                usable = len(pending) - (len(pending) % bytes_per_frame)
                if not usable:
                    continue
                block, pending = pending[:usable], pending[usable:]
                audio = np.frombuffer(block, dtype="<f4").reshape(-1, self.channels)
                self._received_frames = True
                self._startup_event.set()
                self.callback(audio, len(audio), None, None)
        finally:
            self._startup_event.set()

    def _read_stderr(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        try:
            while True:
                data = process.stderr.read(1024)
                if not data:
                    return
                self._stderr.extend(data)
                if len(self._stderr) > 8192:
                    del self._stderr[:-8192]
        except OSError:
            return

    def _error_detail(self) -> str:
        return bytes(self._stderr).decode("utf-8", "replace").strip()

    def start(self) -> None:
        self._stop_event.clear()
        try:
            self._process = subprocess.Popen(
                self.command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=self.env,
            )
        except OSError as exc:
            raise PackagedCaptureUnavailable(f"{self.backend} could not start: {exc}") from exc

        self._stderr_reader = threading.Thread(
            target=self._read_stderr, daemon=True, name="packaged-mic-stderr"
        )
        self._stderr_reader.start()
        self._reader = threading.Thread(
            target=self._read_audio, daemon=True, name="packaged-mic-capture"
        )
        self._reader.start()

        if not self._startup_event.wait(self.startup_timeout):
            self.close()
            raise PackagedCaptureStalled(
                f"{self.backend} connected but delivered no microphone frames within "
                f"{self.startup_timeout:.1f}s"
            )
        if not self._received_frames:
            time.sleep(0.01)  # let the stderr drainer consume an immediate error
            detail = self._error_detail()
            code = self._process.poll() if self._process is not None else None
            self.close()
            raise PackagedCaptureUnavailable(
                detail or f"{self.backend} exited before microphone capture (status {code})"
            )

    def stop(self) -> None:
        self.close()

    def abort(self) -> None:
        self.close()

    def close(self) -> None:
        self._stop_event.set()
        process = self._process
        self._process = None
        if process is not None and process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=1.0)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    process.kill()
                    process.wait(timeout=1.0)
                except (OSError, subprocess.TimeoutExpired):
                    pass
        for thread in (self._reader, self._stderr_reader):
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=1.0)


def open_packaged_capture(
    callback: Callable,
    sample_rate: int,
    channels: int,
    source_name: str | None = None,
    startup_timeout: float = 2.5,
) -> CommandCaptureStream:
    """Open the best packaged capture client, trying safe subprocess fallbacks."""
    candidates = _capture_candidates(sample_rate, channels, source_name)
    if not candidates:
        raise PackagedCaptureUnavailable("no PipeWire/Pulse recording client is installed")

    unavailable: list[str] = []
    stalled: list[str] = []
    for backend, command, env in candidates:
        stream = CommandCaptureStream(
            backend, command, env, callback, channels, startup_timeout=startup_timeout
        )
        try:
            stream.start()
            return stream
        except PackagedCaptureStalled as exc:
            stalled.append(str(exc))
        except PackagedCaptureUnavailable as exc:
            unavailable.append(str(exc))

    if stalled:
        # Never follow a connected-but-stalled desktop client with raw ALSA.
        raise PackagedCaptureStalled("; ".join(stalled + unavailable))
    raise PackagedCaptureUnavailable("; ".join(unavailable))


def input_self_test(duration_ms: int = 120) -> InputSelfTestResult:
    """Open the real packaged capture path and confirm that frames arrive."""
    frame_count = 0
    lock = threading.Lock()

    def callback(_indata, frames, _time_info, _status) -> None:
        nonlocal frame_count
        with lock:
            frame_count += int(frames)

    stream = open_packaged_capture(callback, 16000, 1)
    try:
        time.sleep(max(0.02, duration_ms / 1000))
    finally:
        stream.close()
    with lock:
        frames = frame_count
    if frames <= 0:
        raise RuntimeError("packaged capture opened but returned no frames")
    return InputSelfTestResult(stream.backend, frames, "system default")
