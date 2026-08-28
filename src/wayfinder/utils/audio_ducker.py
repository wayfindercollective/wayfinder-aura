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


def _get_macos_volume() -> int | None:
    """Get current macOS output volume (0-100). Returns None on failure."""
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
        os.kill(numeric_pid, 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


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
        self._is_ducked = False
        self._closed = False
        self._lock = threading.RLock()
        self._use_macos = is_macos() and not is_pactl_available()
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
            print("ℹ Using macOS osascript for audio ducking")
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
                current = _get_macos_volume()
                if current is None:
                    return self._remember(DuckingResult(DuckingStatus.ERROR, message="Could not read system volume."))
                target = round(current * (100 - self._duck_percent) / 100)
                self._macos_original_volume = current
                if not _set_macos_volume(target):
                    self._macos_original_volume = None
                    return self._remember(DuckingResult(DuckingStatus.ERROR, message="Could not lower system volume."))
                self._is_ducked = True
                print(f"🔉 Ducked macOS volume {current}% → {target}%")
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
                self._macos_original_volume = None
                self._is_ducked = False
                if original is not None and _set_macos_volume(original):
                    print(f"🔊 Restored macOS volume to {original}%")
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
