"""Crash and error reports to Wayfinder (Beta: on by default; Stable: opt-in).

What a report holds: whether the app crashed or hit an unhandled error, where
(main thread, a worker thread, a Tk callback, a native crash), the app version
and update channel, OS name/version/architecture, package type, Python
version, a signature that groups the same bug, the time since launch, a
random install ID (not tied to a person or licence), and a scrubbed
traceback: the home folder becomes ``~``, user names in paths are dropped,
email addresses and quoted text longer than a few words are removed, and the
exception message is capped. Never: audio, dictated text, the clipboard,
settings, licence keys or API keys.

Capture:
- Python: sys.excepthook (a crash: the app is going down), threading.excepthook
  and Tk's report_callback_exception (errors the app survived).
- Native crashes (segfault, abort): faulthandler writes Python stacks to a
  per-launch file; the next launch turns a non-empty one into a report.
- macOS: the system's own crash log for Aura (~/Library/Logs/
  DiagnosticReports/Wayfinder Aura*.ips), summarised to its exception and
  top frames, on the next launch.

Reports are queued as small JSON files and sent in the background to the
Wayfinder backend (WAYFINDER_AURA_CRASH_URL overrides). The same bug is sent
once a day per version, at most 20 reports a day; unsent reports are kept
for 14 days. Turning reports off deletes the queue. Settings > System >
"Send crash reports" (config ``crash_reports``: "" follows the channel,
"on", "off").
"""

from __future__ import annotations

import faulthandler
import hashlib
import json
import os
import platform
import re
import sys
import threading
import time
import traceback
import uuid
from collections.abc import Callable
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

CRASH_API_URL = os.environ.get(
    "WAYFINDER_AURA_CRASH_URL",
    "https://fine-shrimp-886.convex.site/api/aura/crash",
)
HTTP_TIMEOUT = 10
MAX_TRACE_CHARS = 16000
MAX_MESSAGE_CHARS = 300
DAILY_CAP = 20
KEEP_DAYS = 14
MAX_QUEUE = 50

_started = time.monotonic()
_lock = threading.Lock()
_state: dict[str, Any] = {"app_version": "", "dir": None, "native_file": None}


# --- where things live ----------------------------------------------------------

def reports_dir() -> Path:
    if _state["dir"] is not None:
        return _state["dir"]
    from ..utils.platform import get_cache_dir

    return get_cache_dir() / "crash-reports"


def _queue_dir() -> Path:
    path = reports_dir() / "pending"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _load_state() -> dict[str, Any]:
    try:
        data = json.loads((reports_dir() / "state.json").read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(data: dict[str, Any]) -> None:
    try:
        reports_dir().mkdir(parents=True, exist_ok=True)
        tmp = reports_dir() / "state.json.tmp"
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, reports_dir() / "state.json")
    except OSError:
        pass


def install_id() -> str:
    data = _load_state()
    value = data.get("install_id")
    if not (isinstance(value, str) and re.fullmatch(r"[0-9a-f]{32}", value)):
        value = uuid.uuid4().hex
        data["install_id"] = value
        _save_state(data)
    return value


# --- consent ------------------------------------------------------------------------

def reports_enabled(config: dict[str, Any] | None = None, app_version: str = "") -> bool:
    """The user's choice; unset follows the update channel (Beta on, Stable off)."""
    if config is None:
        try:
            from ..config import load_config

            config = load_config()
        except Exception:
            return False
    choice = str(config.get("crash_reports") or "").strip().lower()
    if choice in ("on", "off"):
        return choice == "on"
    from .app_updates import effective_channel

    version = app_version or _state["app_version"]
    return effective_channel(version, config.get("update_channel")) == "prerelease"


def _channel(config: dict[str, Any]) -> str:
    from .app_updates import effective_channel

    internal = effective_channel(_state["app_version"], config.get("update_channel"))
    return "beta" if internal == "prerelease" else "stable"


# --- scrubbing --------------------------------------------------------------------

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_LONG_QUOTED = re.compile(r"""(['"])(?:(?!\1).){40,}?\1""")
_USER_PATHS = (
    re.compile(r"(/Users/)[^/\s'\"]+"),
    re.compile(r"(/home/)[^/\s'\"]+"),
    re.compile(r"([A-Za-z]:\\Users\\)[^\\\s'\"]+", re.IGNORECASE),
)


def scrub(text: str) -> str:
    """Remove what could identify the user or carry their words."""
    home = str(Path.home())
    if home and len(home) > 3:
        text = text.replace(home, "~")
    for pattern in _USER_PATHS:
        text = pattern.sub(r"\1<user>", text)
    text = _EMAIL.sub("<email>", text)
    return _LONG_QUOTED.sub(r"\1…\1", text)


def _message(exc: BaseException) -> str:
    message = str(exc)
    if len(message) > MAX_MESSAGE_CHARS:
        message = message[:MAX_MESSAGE_CHARS] + "…"
    return scrub(message)


def signature(kind: str, frames: list[str], exc_name: str) -> str:
    """Same bug, same signature: the exception type and the code location."""
    basis = "|".join([kind, exc_name, *frames[-8:]])
    return hashlib.sha256(basis.encode("utf-8", "replace")).hexdigest()[:16]


# --- building reports -------------------------------------------------------------

def _package() -> str:
    if sys.platform == "darwin":
        return "dmg" if getattr(sys, "frozen", False) else "source"
    if sys.platform == "win32":
        return "windows" if getattr(sys, "frozen", False) else "source"
    try:
        from ..utils.platform import is_appimage, is_flatpak

        if is_flatpak():
            return "flatpak"
        if is_appimage():
            return "appimage"
    except Exception:
        pass
    return "source"


def _os_fields() -> dict[str, str]:
    if sys.platform == "darwin":
        name, version = "macOS", platform.mac_ver()[0]
    elif sys.platform == "win32":
        name, version = "Windows", platform.version()
    else:
        name, version = "Linux", platform.release()
        try:
            info = platform.freedesktop_os_release()
            version = f"{info.get('PRETTY_NAME', '')} ({platform.release()})".strip()
        except (OSError, AttributeError):
            pass
    return {"os": name, "osVersion": version[:80], "arch": (platform.machine() or "")[:20]}


def build_report(kind: str, where: str, title: str, trace: str, frames: list[str],
                 exc_name: str, config: dict[str, Any]) -> dict[str, Any]:
    trace = scrub(trace)
    if len(trace) > MAX_TRACE_CHARS:
        trace = trace[: MAX_TRACE_CHARS // 2] + "\n…\n" + trace[-MAX_TRACE_CHARS // 2:]
    return {
        "reportId": uuid.uuid4().hex,
        "installId": install_id(),
        "kind": kind,
        "signature": signature(kind, frames, exc_name),
        "title": scrub(title)[:200],
        "where": where[:60],
        "appVersion": (_state["app_version"] or "unknown")[:40],
        "channel": _channel(config),
        **_os_fields(),
        "package": _package(),
        "python": platform.python_version()[:20],
        "uptimeSeconds": max(0, int(time.monotonic() - _started)),
        "occurredAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "trace": trace,
    }


def _frames(tb) -> list[str]:
    return [f"{Path(f.filename).name}:{f.name}" for f in traceback.extract_tb(tb)]


def record_exception(exc_type, exc, tb, *, where: str, kind: str = "error") -> bool:
    """Queue a report for an exception (any thread). Never raises."""
    try:
        if exc_type is None or issubclass(exc_type, (KeyboardInterrupt, SystemExit)):
            return False
        config = _config()
        if not reports_enabled(config):
            return False
        frames = _frames(tb)
        location = frames[-1] if frames else "unknown"
        title = f"{exc_type.__name__} in {location}"
        body = "".join(traceback.format_tb(tb)) + f"{exc_type.__name__}: {_message(exc)}\n"
        return _enqueue(build_report(kind, where, title, body, frames, exc_type.__name__, config))
    except Exception:
        return False


def _config() -> dict[str, Any]:
    getter = _state.get("config_getter")
    if getter is not None:
        try:
            config = getter()
            if isinstance(config, dict):
                return config
        except Exception:
            pass
    try:
        from ..config import load_config

        return load_config()
    except Exception:
        return {}


def _enqueue(report: dict[str, Any]) -> bool:
    """Write the report unless the same bug was queued today or the cap is hit."""
    with _lock:
        state = _load_state()
        today = date.today().isoformat()
        if state.get("day") != today:
            state["day"], state["count"] = today, 0
        seen = state.setdefault("seen", {})
        key = f"{report['signature']}@{report['appVersion']}"
        if seen.get(key) == today or state.get("count", 0) >= DAILY_CAP:
            return False
        seen[key] = today
        for old in [k for k, day in seen.items() if day != today]:
            del seen[old]  # keep the dedupe map to today's entries
        state["count"] = state.get("count", 0) + 1
        _save_state(state)
        path = _queue_dir() / f"{int(time.time())}-{report['reportId']}.json"
        path.write_text(json.dumps(report), encoding="utf-8")
    _send_soon()
    return True


# --- native crashes ---------------------------------------------------------------

def _native_file_for(pid: int) -> Path:
    return reports_dir() / f"native-{pid}.log"


def collect_native_crashes() -> int:
    """Turn the faulthandler files of earlier launches into reports."""
    found = 0
    mine = _state.get("native_file")
    for path in sorted(reports_dir().glob("native-*.log")):
        if mine is not None and path == mine:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if text.strip() and _record_native(text, where="native"):
            found += 1
        try:
            path.unlink()
        except OSError:
            pass
    return found


def _record_native(text: str, *, where: str) -> bool:
    config = _config()
    if not reports_enabled(config):
        return False
    first = next((line for line in text.splitlines() if line.strip()), "Native crash")
    frames = re.findall(r'File "([^"]+)", line \d+ in (\S+)', text)
    names = [f"{Path(f).name}:{fn}" for f, fn in frames[:8]]
    title = scrub(first.strip())[:200]
    return _enqueue(build_report("crash", where, title, text, names, first.strip()[:60], config))


def collect_macos_crash_logs(directory: Path | None = None) -> int:
    """Summarise new macOS crash logs (.ips) for Aura into reports."""
    if sys.platform != "darwin" and directory is None:
        return 0
    directory = directory or Path.home() / "Library" / "Logs" / "DiagnosticReports"
    state = _load_state()
    seen = set(state.get("ips_seen", []))
    first_run = "ips_seen" not in state
    found = 0
    try:
        logs = sorted(directory.glob("Wayfinder Aura*.ips"))
    except OSError:
        return 0
    for path in logs:
        if path.name in seen:
            continue
        seen.add(path.name)
        if first_run:
            continue  # crashes from before reports existed are history, not news
        summary = summarize_ips(path)
        if summary and _record_native(summary, where="macos-crash-log"):
            found += 1
    state = _load_state()
    state["ips_seen"] = sorted(seen)[-200:]
    _save_state(state)
    return found


def summarize_ips(path: Path) -> str:
    """Exception and top frames of the crashed thread from an .ips crash log."""
    try:
        header, _, body = path.read_text(encoding="utf-8", errors="replace").partition("\n")
        meta, report = json.loads(header), json.loads(body)
    except (OSError, ValueError):
        return ""
    exception = report.get("exception", {}) or {}
    images = report.get("usedImages", []) or []
    threads = report.get("threads", []) or []
    index = report.get("faultingThread", 0)
    lines = [
        f"macOS crash: {exception.get('type', '?')} ({exception.get('signal', '?')})",
        f"app {meta.get('app_version', '?')} / macOS {report.get('osVersion', {}).get('train', '?')}",
    ]
    termination = report.get("termination", {}) or {}
    if termination.get("indicator"):
        lines.append(f"termination: {termination.get('indicator')}")
    if 0 <= index < len(threads):
        for frame in (threads[index].get("frames") or [])[:16]:
            image = images[frame.get("imageIndex", -1)] if 0 <= frame.get("imageIndex", -1) < len(images) else {}
            lines.append(f'  File "{image.get("name", "?")}", line 0 in {frame.get("symbol", "?")}')
    return "\n".join(lines)


# --- sending ------------------------------------------------------------------------

_send_scheduled = threading.Event()


def _send_soon() -> None:
    if _send_scheduled.is_set():
        return
    _send_scheduled.set()

    def run() -> None:
        time.sleep(5)  # batch a burst of errors into one pass
        _send_scheduled.clear()
        send_pending()

    threading.Thread(target=run, daemon=True, name="crash-report-send").start()


def send_pending() -> int:
    """POST queued reports. Returns how many were delivered. Never raises."""
    try:
        queue = sorted(_queue_dir().glob("*.json"))
    except OSError:
        return 0
    if not reports_enabled(_config()):
        for path in queue:  # the user turned reports off: forget them
            _unlink(path)
        return 0
    cutoff = time.time() - KEEP_DAYS * 86400
    for path in queue[:-MAX_QUEUE]:
        _unlink(path)
    sent = 0
    try:
        import requests
    except Exception:
        return 0
    for path in queue[-MAX_QUEUE:]:
        try:
            if path.stat().st_mtime < cutoff:
                _unlink(path)
                continue
            report = json.loads(path.read_text(encoding="utf-8"))
            response = requests.post(CRASH_API_URL, json=report, timeout=HTTP_TIMEOUT)
        except ValueError:
            _unlink(path)  # unreadable report
            continue
        except Exception:
            break  # offline: try again next launch
        if 200 <= response.status_code < 300 or response.status_code in (400, 413):
            _unlink(path)  # delivered, or one the server will never take
            sent += 200 <= response.status_code < 300
        elif response.status_code == 429:
            break
        # 404 (endpoint not live yet) and 5xx: keep for a later launch
    return sent


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


# --- install ------------------------------------------------------------------------

def install(app_version: str, config_getter: Callable[[], dict[str, Any]] | None = None) -> None:
    """Hook exceptions and native crashes for this process (GUI process only)."""
    _state["app_version"] = app_version
    _state["config_getter"] = config_getter
    try:
        reports_dir().mkdir(parents=True, exist_ok=True)
        native = _native_file_for(os.getpid())
        handle = open(native, "w", encoding="utf-8")
        faulthandler.enable(file=handle, all_threads=True)
        _state["native_file"], _state["native_handle"] = native, handle
        import atexit

        atexit.register(_clean_exit)
    except Exception:
        pass

    previous = sys.excepthook

    def excepthook(exc_type, exc, tb):
        record_exception(exc_type, exc, tb, where="main", kind="crash")
        previous(exc_type, exc, tb)

    sys.excepthook = excepthook
    previous_thread_hook = threading.excepthook

    def thread_hook(args):
        name = getattr(args.thread, "name", "thread") if args.thread else "thread"
        record_exception(args.exc_type, args.exc_value, args.exc_traceback,
                         where=f"thread:{name}"[:60])
        previous_thread_hook(args)

    threading.excepthook = thread_hook


def collect_and_send() -> None:
    """Startup pass (background thread): earlier crashes, then the queue."""
    try:
        collect_native_crashes()
        collect_macos_crash_logs()
    except Exception:
        pass
    send_pending()


def set_config_getter(getter: Callable[[], dict[str, Any]]) -> None:
    """Read consent from the running app's live config from now on."""
    _state["config_getter"] = getter


def clear_queue() -> None:
    """Delete unsent reports (the user turned reports off)."""
    try:
        for path in _queue_dir().glob("*.json"):
            _unlink(path)
    except OSError:
        pass


def mark_clean_exit() -> None:
    """Call on a normal quit, before os._exit: this launch's native crash file
    goes away whatever it holds, so only a crashed launch leaves one behind.
    (Windows' faulthandler can log exceptions the app survived.)"""
    handle = _state.pop("native_handle", None)
    native = _state.pop("native_file", None)
    try:
        faulthandler.disable()
        if handle is not None:
            handle.close()
        if native is not None:
            native.unlink()
    except Exception:
        pass


def _clean_exit() -> None:
    mark_clean_exit()
