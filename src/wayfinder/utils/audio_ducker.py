"""Safe, reversible system-audio ducking for Wayfinder Aura.

Linux uses ``pactl`` for both PulseAudio and PipeWire's PulseAudio server. A
Flatpak client may inspect other playback streams but cannot change them unless
PipeWire classifies that *control client* as a manager. Only the short-lived
``pactl`` mutation processes receive ``PULSE_PROP_media.category=Manager``;
Aura's recorder and every other subprocess keep their normal sandbox identity.

Every successful mutation is journaled and restoration is identity- and
value-checked. This prevents a recycled stream index or a user's concurrent
volume change from being overwritten, and lets the next launch recover after a
hard crash.

macOS and Windows lower the default output's main volume instead (Core Audio
there, the MMDevice endpoint volume on Windows: utils/windows_audio.py), with
the same journal and value-checked restore.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

PULSE_NORMAL_VOLUME = 65_536
_MANAGER_ENV = {"PULSE_PROP_media.category": "Manager", "LC_ALL": "C"}
_QUERY_ENV = {"LC_ALL": "C"}
_DEFAULT_RECOVERY_PATH = object()


class DuckingStatus(str, Enum):
    """Machine-readable outcome for a duck or restore operation."""

    APPLIED = "applied"
    RESTORED = "restored"
    PARTIAL = "partial"
    NO_STREAMS = "no_streams"
    NO_CHANGE = "no_change"
    ALREADY_DUCKED = "already_ducked"
    NOT_DUCKED = "not_ducked"
    UNAVAILABLE = "unavailable"
    PERMISSION_DENIED = "permission_denied"
    ERROR = "error"
    CLOSED = "closed"


@dataclass(frozen=True)
class DuckingResult:
    """Outcome returned by :class:`AudioDucker` operations."""

    status: DuckingStatus
    changed_count: int = 0
    failed_count: int = 0
    skipped_count: int = 0
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.status in {
            DuckingStatus.APPLIED,
            DuckingStatus.RESTORED,
            DuckingStatus.PARTIAL,
            DuckingStatus.NO_STREAMS,
            DuckingStatus.NO_CHANGE,
            DuckingStatus.ALREADY_DUCKED,
            DuckingStatus.NOT_DUCKED,
        }

    @property
    def should_notify(self) -> bool:
        """Whether the UI should show a concise warning for this outcome."""
        return self.status in {
            DuckingStatus.PARTIAL,
            DuckingStatus.UNAVAILABLE,
            DuckingStatus.PERMISSION_DENIED,
            DuckingStatus.ERROR,
        }

    def __bool__(self) -> bool:
        return self.ok


@dataclass(frozen=True)
class _PactlResult:
    success: bool
    error_kind: str = ""
    detail: str = ""


def is_pactl_available() -> bool:
    """Return whether a ``pactl`` executable is visible to this process."""
    return shutil.which("pactl") is not None


def is_macos() -> bool:
    """Check if running on macOS."""
    return platform.system() == "Darwin"


def is_windows() -> bool:
    """Check if running on Windows."""
    return platform.system() == "Windows"


# Windows: the output endpoint a duck lowered, so restore (and crash recovery)
# touch that device even if the default output changes mid-dictation.
_WINDOWS_ENDPOINT: str | None = None


def _system_volume_name() -> str:
    return "Windows" if is_windows() else "macOS"


def _core_audio():
    """The Core Audio volume helpers, or None when they cannot load."""
    try:
        from wayfinder.utils import macos_audio

        return macos_audio if macos_audio._load() is not None else None
    except Exception:
        return None


def _get_macos_volume() -> int | None:
    """Get current macOS output volume (0-100). Returns None on failure.

    Core Audio answers in well under a millisecond; osascript took ~150 ms per
    call (music stayed loud ~0.3 s into speech) and is only the fallback.
    Windows reads the default output's endpoint volume.
    """
    if is_windows():
        from wayfinder.utils.windows_audio import output_volume

        value = output_volume(_WINDOWS_ENDPOINT)
        return None if value is None else int(round(value * 100))
    core = _core_audio()
    if core is not None:
        value = core.output_volume()
        return None if value is None else int(round(value * 100))
    try:
        result = subprocess.run(
            ["osascript", "-e", "output volume of (get volume settings)"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return int(result.stdout.strip())
    except Exception:
        pass
    return None


def _set_macos_volume(volume: int) -> bool:
    """Set macOS output volume (0-100). Returns True on success."""
    if is_windows():
        from wayfinder.utils.windows_audio import set_output_volume

        return set_output_volume(max(0, min(100, volume)) / 100.0, _WINDOWS_ENDPOINT)
    core = _core_audio()
    if core is not None:
        return core.set_output_volume(max(0, min(100, volume)) / 100.0)
    try:
        volume = max(0, min(100, volume))
        result = subprocess.run(
            ["osascript", "-e", f"set volume output volume {volume}"],
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False


def _pactl_env(*, manager: bool = False) -> dict:
    from wayfinder.utils.hostexec import host_env

    env = host_env(_MANAGER_ENV if manager else _QUERY_ENV)
    if not manager:
        # Never inherit a manager identity into read-only enumeration by accident.
        env.pop("PULSE_PROP_media.category", None)
    return env


def _run_pactl(args: Sequence[str], *, manager: bool = False, timeout: float = 5) -> _PactlResult:
    """Run pactl and classify failures without exposing localized stderr parsing."""
    try:
        result = subprocess.run(
            ["pactl", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_pactl_env(manager=manager),
        )
    except FileNotFoundError:
        return _PactlResult(False, "unavailable", "pactl is not installed")
    except subprocess.TimeoutExpired:
        return _PactlResult(False, "timeout", "the audio server did not respond")
    except Exception as exc:
        return _PactlResult(False, "error", str(exc))

    if result.returncode == 0:
        return _PactlResult(True, detail=result.stdout)

    detail = (result.stderr or result.stdout or "pactl failed").strip()
    lowered = detail.lower()
    if "access denied" in lowered or "permission denied" in lowered:
        kind = "permission_denied"
    elif "no such entity" in lowered or "not found" in lowered:
        kind = "stream_gone"
    elif "connection refused" in lowered or "connection failure" in lowered:
        kind = "unavailable"
    else:
        kind = "error"
    return _PactlResult(False, kind, detail)


def _stream_identity(stream: dict) -> str:
    """Return an identity stronger than a recyclable sink-input index."""
    serial = str(stream.get("serial") or "").strip()
    if serial:
        return f"serial:{serial}"
    properties = stream.get("properties") or {}
    fallback = {
        "client": stream.get("client"),
        "application.id": properties.get("application.id"),
        "application.name": properties.get("application.name"),
        "application.process.id": properties.get("application.process.id"),
        "application.process.binary": properties.get("application.process.binary"),
        "media.name": properties.get("media.name"),
        "module-stream-restore.id": properties.get("module-stream-restore.id"),
    }
    return "fallback:" + json.dumps(fallback, sort_keys=True, separators=(",", ":"))


def _finish_stream(stream: dict) -> dict:
    volumes = [max(0, int(value)) for value in stream.get("channel_volumes", [])]
    if not volumes:
        percent = max(0, int(stream.get("volume_percent", 100)))
        volumes = [round(PULSE_NORMAL_VOLUME * percent / 100)]
    stream["channel_volumes"] = volumes
    stream["volume_percent"] = round(volumes[0] * 100 / PULSE_NORMAL_VOLUME)
    stream["muted"] = bool(stream.get("muted", False))
    stream["properties"] = stream.get("properties") or {}
    stream["serial"] = str(
        stream.get("serial") or stream["properties"].get("object.serial") or ""
    )
    stream["identity"] = _stream_identity(stream)
    return stream


def _parse_sink_inputs_json(output: str) -> list[dict]:
    payload = json.loads(output or "[]")
    if not isinstance(payload, list):
        raise ValueError("pactl JSON output was not a list")

    sink_inputs: list[dict] = []
    for item in payload:
        if not isinstance(item, dict) or "index" not in item:
            continue
        properties = item.get("properties") if isinstance(item.get("properties"), dict) else {}
        volume = item.get("volume") if isinstance(item.get("volume"), dict) else {}
        channel_volumes = []
        for channel in volume.values():
            if isinstance(channel, dict) and isinstance(channel.get("value"), (int, float)):
                channel_volumes.append(int(channel["value"]))
        sink_inputs.append(
            _finish_stream(
                {
                    "id": int(item["index"]),
                    "client": item.get("client"),
                    "sink": item.get("sink"),
                    "channel_volumes": channel_volumes,
                    "muted": bool(item.get("mute", False)),
                    "app_name": str(properties.get("application.name") or "Unknown"),
                    "serial": str(properties.get("object.serial") or ""),
                    "properties": properties,
                }
            )
        )
    return sink_inputs


def _parse_sink_inputs(output: str) -> list[dict]:
    """Parse stable fields from ``pactl list sink-inputs`` text output.

    This is the compatibility path for older pactl versions without ``-f json``.
    ``LC_ALL=C`` is forced at the subprocess boundary so labels remain stable.
    """
    sink_inputs: list[dict] = []
    current: dict | None = None

    def finish_current() -> None:
        if current is not None:
            sink_inputs.append(_finish_stream(current))

    for line in output.splitlines():
        match = re.match(r"^Sink Input #(\d+)", line)
        if match:
            finish_current()
            current = {
                "id": int(match.group(1)),
                "client": None,
                "sink": None,
                "channel_volumes": [],
                "volume_percent": 100,
                "muted": False,
                "app_name": "Unknown",
                "serial": "",
                "properties": {},
            }
            continue
        if current is None:
            continue

        stripped = line.strip()
        if stripped.startswith("Client:"):
            current["client"] = stripped.partition(":")[2].strip()
        elif stripped.startswith("Sink:"):
            current["sink"] = stripped.partition(":")[2].strip()
        elif stripped.startswith("Volume:"):
            values = [int(value) for value in re.findall(r":\s*(\d+)\s*/\s*\d+%", stripped)]
            if values:
                current["channel_volumes"] = values
            else:
                percent = re.search(r"/\s*(\d+)%", stripped)
                if percent:
                    current["volume_percent"] = int(percent.group(1))
        elif stripped.startswith("Mute:"):
            current["muted"] = stripped.partition(":")[2].strip().lower() == "yes"
        else:
            prop = re.match(r'([\w.-]+)\s*=\s*"(.*)"$', stripped)
            if prop:
                key, value = prop.groups()
                current["properties"][key] = value
                if key == "application.name":
                    current["app_name"] = value
                elif key == "object.serial":
                    current["serial"] = value

    finish_current()
    return sink_inputs


def _query_sink_inputs() -> tuple[list[dict], _PactlResult]:
    json_result = _run_pactl(["-f", "json", "list", "sink-inputs"], timeout=10)
    if json_result.success:
        try:
            return _parse_sink_inputs_json(json_result.detail), json_result
        except (TypeError, ValueError, json.JSONDecodeError):
            pass

    # pactl versions before JSON output support still work via the C-locale text form.
    text_result = _run_pactl(["list", "sink-inputs"], timeout=10)
    if not text_result.success:
        return [], text_result
    return _parse_sink_inputs(text_result.detail), text_result


def get_sink_inputs() -> list[dict]:
    """Return current playback streams, or an empty list if pactl is unavailable."""
    streams, _result = _query_sink_inputs()
    return streams


def set_sink_input_volume(sink_input_id: int, volume_percent: int) -> bool:
    """Compatibility helper: set one stream to a scalar percentage."""
    volume_percent = max(0, min(150, int(volume_percent)))
    return _run_pactl(
        ["set-sink-input-volume", str(sink_input_id), f"{volume_percent}%"],
        manager=True,
    ).success


def _set_sink_input_channel_volumes(
    sink_input_id: int, channel_volumes: Sequence[int]
) -> _PactlResult:
    values = [str(max(0, int(value))) for value in channel_volumes]
    if not values:
        return _PactlResult(False, "error", "no channel volumes supplied")
    return _run_pactl(
        ["set-sink-input-volume", str(sink_input_id), *values],
        manager=True,
    )


def _default_recovery_file() -> Path:
    if platform.system() == "Darwin":
        root = Path.home() / "Library" / "Application Support" / "wayfinder-aura"
    elif os.name == "nt":
        root = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "wayfinder-aura"
    else:
        root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "wayfinder-aura"
    return root / "audio-duck-recovery.json"


def _pid_is_alive(pid: object) -> bool:
    try:
        numeric_pid = int(pid)
        if numeric_pid <= 0:
            return False
        if is_windows():
            # os.kill(pid, 0) on Windows is TerminateProcess, not a probe: ask
            # whether the process is still running instead.
            return _windows_pid_is_alive(numeric_pid)
        os.kill(numeric_pid, 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def _windows_pid_is_alive(pid: int) -> bool:
    """Windows: True while *pid* names a running process (never signals it)."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.restype = wintypes.HANDLE
    handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
    if not handle:
        # Access denied still means the process exists.
        return ctypes.GetLastError() == 5
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True
        return code.value == 259  # STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


class AudioDucker:
    """Reduce existing application playback while Aura records, then restore it."""

    def __init__(
        self,
        duck_percent: float = 20.0,
        exclude_apps: list[str] | None = None,
        *,
        recovery_path: object = _DEFAULT_RECOVERY_PATH,
    ):
        self._duck_percent = max(0.0, min(100.0, float(duck_percent)))
        defaults = ["Wayfinder Aura", "io.wayfindercollective.WayfinderAura"]
        self._exclude_apps = [*defaults, *(exclude_apps or [])]
        self._records: dict[int, dict] = {}
        # Retained as a compatibility/debugging view for older callers.
        self._original_volumes: dict[int, int] = {}
        self._macos_original_volume: int | None = None
        self._macos_ducked_volume: int | None = None
        # Windows: ducked endpoints whose restore couldn't run yet (the device
        # was unplugged). Kept in the journal and retried before every duck and
        # at the next launch, so a later duck never overwrites them.
        self._windows_pending: list[dict] = []
        self._is_ducked = False
        self._closed = False
        self._lock = threading.RLock()
        # Windows uses the same main-volume path as macOS.
        self._use_macos = (is_macos() or is_windows()) and not is_pactl_available()
        self._available = is_pactl_available() or self._use_macos
        self._recovery_path = (
            _default_recovery_file()
            if recovery_path is _DEFAULT_RECOVERY_PATH
            else Path(recovery_path) if recovery_path is not None else None
        )
        self.last_result = DuckingResult(DuckingStatus.NO_CHANGE)
        self.recovery_result = DuckingResult(DuckingStatus.NO_CHANGE)

        if not self._available:
            print("⚠ pactl not available - audio ducking disabled")
        elif self._use_macos:
            if is_windows():
                print("ℹ Using Windows Core Audio (endpoint volume) for audio ducking")
            else:
                print("ℹ Using macOS " + ("Core Audio" if _core_audio() else "osascript")
                      + " for audio ducking")
            self.recovery_result = self._recover_stale_macos_journal()
        else:
            self.recovery_result = self._recover_stale_journal()

    @property
    def is_available(self) -> bool:
        return self._available

    @property
    def is_ducked(self) -> bool:
        return self._is_ducked

    def set_duck_percent(self, percent: float) -> None:
        self._duck_percent = max(0.0, min(100.0, float(percent)))

    def _excluded(self, stream: dict) -> bool:
        properties = stream.get("properties") or {}
        candidates = [
            stream.get("app_name"),
            properties.get("application.id"),
            properties.get("application.name"),
            properties.get("application.process.binary"),
            properties.get("pipewire.access.portal.app_id"),
        ]
        lowered = " ".join(str(value or "").lower() for value in candidates)
        return any(exclusion.lower() in lowered for exclusion in self._exclude_apps)

    @staticmethod
    def _record_for(stream: dict, target: Sequence[int]) -> dict:
        return {
            "id": int(stream["id"]),
            "serial": str(stream.get("serial") or ""),
            "identity": str(stream.get("identity") or _stream_identity(stream)),
            "app_name": str(stream.get("app_name") or "Unknown"),
            "original_volumes": [int(value) for value in stream["channel_volumes"]],
            "ducked_volumes": [int(value) for value in target],
            "original_muted": bool(stream.get("muted", False)),
        }

    @staticmethod
    def _same_stream(record: dict, stream: dict) -> bool:
        if int(record.get("id", -1)) != int(stream.get("id", -2)):
            return False
        record_serial = str(record.get("serial") or "")
        stream_serial = str(stream.get("serial") or "")
        if record_serial or stream_serial:
            return bool(record_serial and stream_serial and record_serial == stream_serial)
        return bool(record.get("identity") and record.get("identity") == stream.get("identity"))

    def _journal_entries(self) -> list[dict]:
        return [self._records[key] for key in sorted(self._records)]

    def _write_journal(self) -> None:
        if self._recovery_path is None:
            return
        if not self._records:
            self._clear_journal()
            return
        path = self._recovery_path
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        payload = {"version": 1, "pid": os.getpid(), "streams": self._journal_entries()}
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, separators=(",", ":"))
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                pass
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, path)

    def _clear_journal(self) -> None:
        if self._recovery_path is None:
            return
        if is_windows() and self._windows_pending:
            self._write_windows_pending_journal()
            return
        try:
            self._recovery_path.unlink(missing_ok=True)
        except OSError:
            pass
        try:
            self._recovery_path.with_suffix(self._recovery_path.suffix + ".tmp").unlink(
                missing_ok=True
            )
        except OSError:
            pass

    def _write_macos_journal(self, original: int, ducked: int) -> None:
        """Persist master-volume ownership before changing it."""
        if self._recovery_path is None:
            return
        path = self._recovery_path
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        payload = {
            "version": 2,
            "pid": os.getpid(),
            "macos": {"original": int(original), "ducked": int(ducked)},
        }
        if is_windows() and _WINDOWS_ENDPOINT:
            payload["macos"]["endpoint"] = _WINDOWS_ENDPOINT
        if is_windows() and self._windows_pending:
            payload["windows_pending"] = list(self._windows_pending)
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, separators=(",", ":"))
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                pass
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, path)

    def _recover_stale_macos_journal(self) -> DuckingResult:
        path = self._recovery_path
        if path is None or not path.exists():
            return DuckingResult(DuckingStatus.NO_CHANGE)
        try:
            with open(path, encoding="utf-8") as handle:
                payload = json.load(handle)
            if payload.get("pid") != os.getpid() and _pid_is_alive(payload.get("pid")):
                return DuckingResult(DuckingStatus.NO_CHANGE)
            if is_windows():
                return self._recover_windows_records(payload)
            record = payload.get("macos")
            if not isinstance(record, dict):
                return DuckingResult(DuckingStatus.NO_CHANGE)
            original = max(0, min(100, int(record["original"])))
            ducked = max(0, min(100, int(record["ducked"])))
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            self._clear_journal()
            return DuckingResult(DuckingStatus.NO_CHANGE)

        if is_windows():
            global _WINDOWS_ENDPOINT
            _WINDOWS_ENDPOINT = record.get("endpoint") or None
        try:
            return self._recover_macos_volume(original, ducked)
        finally:
            if is_windows():
                _WINDOWS_ENDPOINT = None

    def _write_windows_pending_journal(self) -> None:
        """Persist only the not-yet-restorable Windows endpoints."""
        path = self._recovery_path
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        payload = {"version": 2, "pid": os.getpid(), "windows_pending": list(self._windows_pending)}
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, separators=(",", ":"))
        os.replace(tmp, path)

    def _recover_windows_records(self, payload: dict) -> DuckingResult:
        """Windows: restore every journaled endpoint that is present; keep the rest.

        A record whose device can't be read (unplugged headset) stays pending
        instead of being dropped, so its original volume is restored once it
        is back - never overwritten by the next dictation's duck.
        """
        global _WINDOWS_ENDPOINT
        records = []
        main = payload.get("macos")
        if isinstance(main, dict):
            records.append(main)
        records.extend(r for r in (payload.get("windows_pending") or []) if isinstance(r, dict))
        self._windows_pending = []
        results = []
        for record in records[:16]:
            try:
                original = max(0, min(100, int(record["original"])))
                ducked = max(0, min(100, int(record["ducked"])))
            except (TypeError, ValueError, KeyError):
                continue
            endpoint = record.get("endpoint") or None
            _WINDOWS_ENDPOINT = endpoint
            try:
                result = self._recover_macos_volume(original, ducked)
            finally:
                _WINDOWS_ENDPOINT = None
            if result.status == DuckingStatus.ERROR and endpoint and not any(
                    p.get("endpoint") == endpoint for p in self._windows_pending):
                self._windows_pending.append(
                    {"original": original, "ducked": ducked, "endpoint": endpoint})
            results.append(result)
        self._clear_journal()  # rewrites pending-only, or deletes when nothing is left
        for status in (DuckingStatus.RESTORED, DuckingStatus.ERROR):
            for result in results:
                if result.status == status:
                    return result
        return results[0] if results else DuckingResult(DuckingStatus.NO_CHANGE)

    def _recover_macos_volume(self, original: int, ducked: int) -> DuckingResult:
        current = _get_macos_volume()
        if current is None:
            return DuckingResult(
                DuckingStatus.ERROR,
                failed_count=1,
                message="Could not inspect system volume for crash recovery.",
            )
        if current != ducked:
            # The user changed volume after the crash; their newer value wins.
            self._clear_journal()
            return DuckingResult(DuckingStatus.NO_CHANGE, skipped_count=1)
        if _set_macos_volume(original):
            self._clear_journal()
            print(f"🔊 Recovered {_system_volume_name()} volume to {original}% after an interrupted session")
            return DuckingResult(DuckingStatus.RESTORED, changed_count=1)
        return DuckingResult(
            DuckingStatus.ERROR,
            failed_count=1,
            message="Could not restore system volume after an interrupted session.",
        )
    def _read_stale_records(self) -> dict[int, dict] | None:
        if self._recovery_path is None or not self._recovery_path.exists():
            return {}
        try:
            with open(self._recovery_path, encoding="utf-8") as handle:
                payload = json.load(handle)
            if not isinstance(payload, dict) or not isinstance(payload.get("streams"), list):
                raise ValueError("invalid audio ducking recovery journal")
            if payload.get("pid") != os.getpid() and _pid_is_alive(payload.get("pid")):
                # Another live Aura process owns these mutations; never fight it.
                return None
            records: dict[int, dict] = {}
            for record in payload["streams"][:1024]:
                if not isinstance(record, dict):
                    continue
                sink_id = int(record["id"])
                if not record.get("identity"):
                    continue
                if not isinstance(record.get("original_volumes"), list):
                    continue
                if not isinstance(record.get("ducked_volumes"), list):
                    continue
                records[sink_id] = record
            return records
        except (OSError, TypeError, ValueError, json.JSONDecodeError, KeyError):
            self._clear_journal()
            return {}

    def _recover_stale_journal(self) -> DuckingResult:
        stale = self._read_stale_records()
        if stale is None or not stale:
            return DuckingResult(DuckingStatus.NO_CHANGE)
        self._records = stale
        self._original_volumes = {
            sink_id: round(record["original_volumes"][0] * 100 / PULSE_NORMAL_VOLUME)
            for sink_id, record in stale.items()
            if record.get("original_volumes")
        }
        self._is_ducked = True
        result = self._restore_linux()
        if result.changed_count:
            print(f"🔊 Recovered {result.changed_count} audio source(s) after an interrupted session")
        return result

    def duck(self) -> DuckingResult:
        """Apply the configured reduction to eligible, currently playing streams."""
        with self._lock:
            if self._closed:
                return self._remember(DuckingResult(DuckingStatus.CLOSED, message="audio ducking is closed"))
            if not self._available:
                return self._remember(
                    DuckingResult(
                        DuckingStatus.UNAVAILABLE,
                        message="Audio ducking needs PulseAudio or PipeWire volume control (pactl).",
                    )
                )
            if self._is_ducked:
                return self._remember(DuckingResult(DuckingStatus.ALREADY_DUCKED))
            if self._duck_percent <= 0:
                return self._remember(DuckingResult(DuckingStatus.NO_CHANGE))

            if self._use_macos:
                core = _core_audio()
                if core is not None and not core.output_volume_settable():
                    return self._remember(DuckingResult(
                        DuckingStatus.UNAVAILABLE,
                        message=("The current sound output has no volume control "
                                 "(e.g. HDMI/DisplayPort), so music can't be lowered "
                                 "while you dictate."),
                    ))
                if is_windows():
                    global _WINDOWS_ENDPOINT
                    from wayfinder.utils.windows_audio import default_output_id

                    if self._windows_pending:
                        # A device ducked before a crash may be back now.
                        self._recover_stale_macos_journal()
                    _WINDOWS_ENDPOINT = default_output_id()
                current = _get_macos_volume()
                if current is None:
                    return self._remember(DuckingResult(DuckingStatus.ERROR, message="Could not read system volume."))
                target = round(current * (100 - self._duck_percent) / 100)
                if target == current:
                    return self._remember(DuckingResult(DuckingStatus.NO_CHANGE))
                self._macos_original_volume = current
                self._macos_ducked_volume = target
                self._write_macos_journal(current, target)
                if not _set_macos_volume(target):
                    self._macos_original_volume = None
                    self._macos_ducked_volume = None
                    self._clear_journal()
                    return self._remember(DuckingResult(DuckingStatus.ERROR, message="Could not lower system volume."))
                self._is_ducked = True
                print(f"🔉 Ducked {_system_volume_name()} volume {current}% → {target}%")
                return self._remember(DuckingResult(DuckingStatus.APPLIED, changed_count=1))

            streams, query = _query_sink_inputs()
            if not query.success:
                return self._remember(self._failure_result(query))
            eligible = [stream for stream in streams if not self._excluded(stream)]
            if not eligible:
                return self._remember(DuckingResult(DuckingStatus.NO_STREAMS))

            self._records.clear()
            self._original_volumes.clear()
            changed = 0
            failed = 0
            permission_denied = 0
            factor = (100.0 - self._duck_percent) / 100.0

            for stream in eligible:
                original = [int(value) for value in stream["channel_volumes"]]
                target = [max(0, round(value * factor)) for value in original]
                if target == original:
                    continue
                record = self._record_for(stream, target)
                sink_id = int(stream["id"])
                # Journal first: a crash after the set but before bookkeeping must
                # still be recoverable. Recovery only acts when current == target.
                self._records[sink_id] = record
                self._write_journal()
                result = _set_sink_input_channel_volumes(sink_id, target)
                if result.success:
                    changed += 1
                    self._original_volumes[sink_id] = round(
                        original[0] * 100 / PULSE_NORMAL_VOLUME
                    )
                else:
                    failed += 1
                    permission_denied += result.error_kind == "permission_denied"
                    self._records.pop(sink_id, None)
                    self._write_journal()

            self._is_ducked = changed > 0
            if not self._is_ducked:
                self._records.clear()
                self._original_volumes.clear()
                self._clear_journal()
                if failed and permission_denied == failed:
                    return self._remember(
                        DuckingResult(
                            DuckingStatus.PERMISSION_DENIED,
                            failed_count=failed,
                            message="Audio ducking is blocked by this system's audio permissions.",
                        )
                    )
                if failed:
                    return self._remember(
                        DuckingResult(
                            DuckingStatus.ERROR,
                            failed_count=failed,
                            message="Aura could not lower other application audio.",
                        )
                    )
                return self._remember(DuckingResult(DuckingStatus.NO_CHANGE))

            print(f"🔉 Ducked {changed} audio source(s) by {self._duck_percent:g}%")
            if failed:
                return self._remember(
                    DuckingResult(
                        DuckingStatus.PARTIAL,
                        changed_count=changed,
                        failed_count=failed,
                        message=f"Audio ducking worked for {changed} source(s), but failed for {failed}.",
                    )
                )
            return self._remember(DuckingResult(DuckingStatus.APPLIED, changed_count=changed))

    def restore(self) -> DuckingResult:
        """Restore only streams that are still at Aura's exact ducked target."""
        with self._lock:
            if not self._available:
                return self._remember(DuckingResult(DuckingStatus.UNAVAILABLE))
            if not self._is_ducked:
                return self._remember(DuckingResult(DuckingStatus.NOT_DUCKED))
            if self._use_macos:
                original = self._macos_original_volume
                target = self._macos_ducked_volume
                current = _get_macos_volume()
                if original is None or target is None:
                    self._is_ducked = False
                    self._clear_journal()
                    return self._remember(DuckingResult(DuckingStatus.NOT_DUCKED))
                if current is None:
                    return self._remember(DuckingResult(
                        DuckingStatus.ERROR,
                        failed_count=1,
                        message="Could not inspect system volume before restoring it.",
                    ))
                if abs(current - target) > 1:
                    # User changed volume while recording; never overwrite it.
                    # (±1: Core Audio may quantise to the device's volume steps.)
                    self._macos_original_volume = None
                    self._macos_ducked_volume = None
                    self._is_ducked = False
                    self._clear_journal()
                    return self._remember(DuckingResult(DuckingStatus.NO_CHANGE, skipped_count=1))
                if _set_macos_volume(original):
                    self._macos_original_volume = None
                    self._macos_ducked_volume = None
                    self._is_ducked = False
                    self._clear_journal()
                    print(f"🔊 Restored {_system_volume_name()} volume to {original}%")
                    return self._remember(DuckingResult(DuckingStatus.RESTORED, changed_count=1))
                return self._remember(DuckingResult(DuckingStatus.ERROR, failed_count=1, message="Could not restore system volume."))
            return self._remember(self._restore_linux())

    def _restore_linux(self) -> DuckingResult:
        streams, query = _query_sink_inputs()
        if not query.success:
            return self._failure_result(query)
        by_id = {int(stream["id"]): stream for stream in streams}
        restored = 0
        failed = 0
        skipped = 0

        for sink_id, record in list(self._records.items()):
            stream = by_id.get(sink_id)
            if stream is None or not self._same_stream(record, stream):
                # Gone/reused streams must never be targeted.
                skipped += 1
                self._records.pop(sink_id, None)
                continue
            current = [int(value) for value in stream["channel_volumes"]]
            original = [int(value) for value in record["original_volumes"]]
            target = [int(value) for value in record["ducked_volumes"]]
            if current == original:
                # Already restored externally.
                skipped += 1
                self._records.pop(sink_id, None)
                continue
            if current != target:
                # The user or another mixer changed it while Aura was recording.
                # Their newer choice wins, and the journal relinquishes ownership.
                skipped += 1
                self._records.pop(sink_id, None)
                continue
            result = _set_sink_input_channel_volumes(sink_id, original)
            if result.success:
                restored += 1
                self._records.pop(sink_id, None)
                self._original_volumes.pop(sink_id, None)
            elif result.error_kind == "stream_gone":
                skipped += 1
                self._records.pop(sink_id, None)
                self._original_volumes.pop(sink_id, None)
            else:
                failed += 1

        self._write_journal()
        self._is_ducked = bool(self._records)
        if not self._records:
            self._original_volumes.clear()

        if restored:
            print(f"🔊 Restored {restored} audio source(s)")
        if failed:
            return DuckingResult(
                DuckingStatus.PARTIAL if restored or skipped else DuckingStatus.ERROR,
                changed_count=restored,
                failed_count=failed,
                skipped_count=skipped,
                message="Some audio sources could not be restored; Aura will retry on exit or next launch.",
            )
        if restored:
            return DuckingResult(DuckingStatus.RESTORED, changed_count=restored, skipped_count=skipped)
        return DuckingResult(DuckingStatus.NO_CHANGE, skipped_count=skipped)

    @staticmethod
    def _failure_result(result: _PactlResult) -> DuckingResult:
        if result.error_kind == "permission_denied":
            return DuckingResult(
                DuckingStatus.PERMISSION_DENIED,
                failed_count=1,
                message="Audio ducking is blocked by this system's audio permissions.",
            )
        if result.error_kind == "unavailable":
            return DuckingResult(
                DuckingStatus.UNAVAILABLE,
                failed_count=1,
                message="Aura could not connect to PulseAudio or PipeWire.",
            )
        return DuckingResult(
            DuckingStatus.ERROR,
            failed_count=1,
            message=f"Audio ducking failed: {result.detail or result.error_kind}",
        )

    def _remember(self, result: DuckingResult) -> DuckingResult:
        self.last_result = result
        return result

    def close(self) -> DuckingResult:
        """Explicit shutdown hook; future queued duck actions become harmless."""
        with self._lock:
            result = self.restore() if self._is_ducked else DuckingResult(DuckingStatus.NOT_DUCKED)
            self._closed = True
            return self._remember(result)

    def __del__(self):
        # Best effort only. Application shutdown calls close() explicitly, while
        # the durable journal covers SIGKILL/interpreter crashes on next launch.
        try:
            self.close()
        except Exception:
            pass
