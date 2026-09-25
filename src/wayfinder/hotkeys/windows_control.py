"""Windows control channel: the counterpart of the AF_UNIX command socket.

CPython on Windows has no ``socket.AF_UNIX``, so the tray menu (hosted in the
Qt overlay process), a second launch ("show"), the CLI verbs and the headless
``inspect:`` check had no way to reach the running app. This listener accepts
the same verbs as ``hotkeys/socket.py`` on a loopback TCP port.

Only this user can drive it: each start picks a free 127.0.0.1 port and a
random token, and writes both to ``control.json`` in the per-user cache dir
(``%LOCALAPPDATA%``, which Windows keeps private to the account). A request is
``<token> <verb>``; anything without the current token is dropped unanswered,
so another local account or a web page cannot trigger dictation.

Windows-only; the Unix socket path on Linux and macOS is unchanged.
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
import socket
import threading
from pathlib import Path
from queue import Queue
from threading import Event
from typing import Callable, Optional

from .types import EventType

MAX_REQUEST = 512
# A client that connects and sends nothing must not hold up the tray or a
# second launch: each connection is served on its own short-lived thread, a
# few at a time, and must speak within READ_TIMEOUT_S.
MAX_CONCURRENT = 8
READ_TIMEOUT_S = 0.5


def endpoint_file() -> Path:
    from ..utils.platform import get_cache_dir

    return get_cache_dir() / "control.json"


def _write_endpoint(port: int, token: str) -> None:
    path = endpoint_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"port": port, "token": token, "pid": os.getpid()}),
                   encoding="utf-8")
    os.replace(tmp, path)


def _clear_endpoint(token: str) -> None:
    """Remove the endpoint file only if it is still ours (a newer run may own it)."""
    path = endpoint_file()
    try:
        if json.loads(path.read_text(encoding="utf-8")).get("token") == token:
            path.unlink()
    except Exception:
        pass


def dispatch(verb: str, event_queue: Queue, log: Callable[[str], None]) -> Optional[bytes]:
    """Queue the event for *verb*; return the reply bytes (None = no reply).

    Mirrors hotkeys/socket.py verb for verb (tests pin the two together),
    plus "update" (tray: Check for Updates…).
    """
    if verb == "ping":
        return b"pong"
    if verb == "toggle":
        log("🎯 Toggle received via control channel")
        event_queue.put((EventType.HOTKEY_PRESSED, None))
    elif verb == "style":
        log("✎ Style toggle received via control channel")
        event_queue.put((EventType.STYLE_TOGGLE, None))
    elif verb.startswith("style:"):
        style = verb.split(":", 1)[1]
        log(f"✎ Style set to '{style}' via control channel")
        event_queue.put((EventType.STYLE_TOGGLE, style))
    elif verb == "show":
        log("🪟 Show window received via control channel")
        event_queue.put((EventType.SHOW_WINDOW, None))
        return b"ok"
    elif verb == "hide":
        log("🫥 Hide to tray received via control channel")
        event_queue.put((EventType.HIDE_WINDOW, None))
        return b"ok"
    elif verb == "reset":
        log("🔄 Reset received via control channel")
        event_queue.put((EventType.FORCE_RESET, None))
    elif verb == "cancel":
        log("✕ Cancel recording received via control channel")
        event_queue.put((EventType.CANCEL_RECORDING, None))
    elif verb == "quit":
        log("👋 Quit received via control channel")
        event_queue.put((EventType.QUIT_APP, None))
    elif verb.startswith("tab:"):
        event_queue.put((EventType.SWITCH_TAB, verb.split(":", 1)[1]))
    elif verb.startswith("inspect:"):
        event_queue.put((EventType.INSPECT_UI, verb.split(":", 1)[1]))
        return b"ok"
    elif verb == "update":
        event_queue.put((EventType.CHECK_UPDATES, None))
        return b"ok"
    return None


def control_listener(
    event_queue: Queue,
    stop_event: Event,
    log_callback: Optional[Callable[[str], None]] = None,
) -> None:
    """Serve control verbs until *stop_event* is set (run on a daemon thread)."""
    def log(msg: str) -> None:
        if log_callback:
            try:
                log_callback(msg)
            except Exception:
                pass

    token = secrets.token_urlsafe(24)
    expected = token.encode("ascii")
    try:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        server.listen(4)
        server.settimeout(1.0)
        _write_endpoint(server.getsockname()[1], token)
    except Exception as e:
        log(f"⚠️ Control channel failed: {e}")
        return
    log("📡 Control channel ready (loopback)")
    slots = threading.BoundedSemaphore(MAX_CONCURRENT)

    def serve(conn) -> None:
        try:
            conn.settimeout(READ_TIMEOUT_S)
            data = conn.recv(MAX_REQUEST)
            given, _, verb = data.decode("utf-8", "replace").strip().partition(" ")
            if not hmac.compare_digest(given.encode("utf-8", "replace"), expected):
                return  # wrong/missing token: drop silently
            reply = dispatch(verb.strip(), event_queue, log)
            if reply:
                conn.sendall(reply)
        except (socket.timeout, OSError):
            pass
        except Exception as e:
            if not stop_event.is_set():
                log(f"⚠️ Control channel error: {e}")
        finally:
            try:
                conn.close()
            except Exception:
                pass
            slots.release()

    try:
        while not stop_event.is_set():
            try:
                conn, _ = server.accept()
            except socket.timeout:
                continue
            except OSError as e:
                if not stop_event.is_set():
                    log(f"⚠️ Control channel error: {e}")
                continue
            if not slots.acquire(blocking=False):
                conn.close()  # saturated: shed the connection, keep accepting
                continue
            threading.Thread(target=serve, args=(conn,), daemon=True,
                             name="wayfinder-control-conn").start()
    finally:
        try:
            server.close()
        except Exception:
            pass
        _clear_endpoint(token)


def send_command(verb: str, *, expect_reply: bool = False, timeout: float = 2.0) -> Optional[bytes]:
    """Send *verb* to the running app. The reply (b"" if none), or None if unreachable."""
    try:
        info = json.loads(endpoint_file().read_text(encoding="utf-8"))
        port, token = int(info["port"]), str(info["token"])
    except Exception:
        return None
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
            sock.sendall(f"{token} {verb}".encode("utf-8"))
            if not expect_reply:
                return b""
            try:
                return sock.recv(16)
            except socket.timeout:
                return b""
    except OSError:
        return None
