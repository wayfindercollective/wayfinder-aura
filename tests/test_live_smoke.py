"""Live socket smoke test — drives a RUNNING app over its control socket.

Skipped by default; run against a live instance with::

    WAYFINDER_LIVE=1 python3 -m pytest tests/test_live_smoke.py -v

Protocol reality (read from ``socket_listener`` in wayfinder_main.py): ``ping``
is the health-check command and replies ``pong``. User commands are
fire-and-forget: the listener accepts a connection, ``recv``s one command,
enqueues the matching EventType, and immediately ``conn.close()``s.

To prove the app actually *acted* on it, the app now writes a small status
breadcrumb (``config.STATUS_PATH`` = ``$XDG_RUNTIME_DIR/wayfinder-aura/status.json``)
on every tab/state change. These tests send a ``tab:<name>`` command and then poll
that file until it reflects the switch — upgrading the assertion from "accepted"
to "state actually changed". (Against an older app with no breadcrumb the check
degrades to a skip.)

Each command needs its own connection because the server handles one accept at a
time and closes after each recv.
"""
from __future__ import annotations

import json
import os
import socket
import time

import pytest

from wayfinder.config import SOCKET_PATH, STATUS_PATH  # single source of truth

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("WAYFINDER_LIVE") != "1",
        reason="live socket smoke — set WAYFINDER_LIVE=1 with the app running",
    ),
]


def _require_running_app():
    if not os.path.exists(SOCKET_PATH):
        pytest.skip(f"no app instance (socket absent: {SOCKET_PATH})")


def _send(command: str) -> None:
    """Connect, send one command, wait for the listener to consume + close it.

    The listener ``recv``s the command then ``close``s the connection with no
    reply, so ``recv`` here returns b"" the instant the server is done. Reading
    until close is the ACK *and* it paces us to the server — the listener runs
    ``listen(1)``, so firing the next connect before this one is accepted would
    overflow the backlog. No sleeps, no races.
    """
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(3.0)
        sock.connect(str(SOCKET_PATH))
        sock.sendall(command.encode("utf-8"))
        try:
            sock.recv(16)  # returns b"" when the listener closes the connection
        except socket.timeout:
            pass


def _ping() -> bytes:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(3.0)
        sock.connect(str(SOCKET_PATH))
        sock.sendall(b"ping")
        return sock.recv(16)


def _read_status():
    try:
        with open(STATUS_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _await_tab(tab: str, timeout: float = 3.0):
    """Poll the status breadcrumb until it reports ``tab`` (or timeout).

    Returns "ok" (switched), "stale" (breadcrumb seen but never this tab), or
    "absent" (no breadcrumb at all → older app, caller should skip)."""
    deadline = time.time() + timeout
    seen_any = False
    while time.time() < deadline:
        s = _read_status()
        if s is not None:
            seen_any = True
            if s.get("tab") == tab:
                return "ok"
        time.sleep(0.05)
    return "stale" if seen_any else "absent"


def test_show_command_accepted():
    _require_running_app()
    _send("show")  # raises if the live listener won't accept the command


def test_ping_replies_pong():
    _require_running_app()
    assert _ping() == b"pong"


@pytest.mark.parametrize("tab", ["dictate", "settings", "style", "history"])
def test_tab_switch_actually_switches(tab):
    """Send tab:<name> and verify via the status breadcrumb that the app REALLY
    switched — not merely that the socket accepted the bytes."""
    _require_running_app()
    _send(f"tab:{tab}")
    result = _await_tab(tab)
    if result == "absent":
        pytest.skip("app predates the status breadcrumb (no status.json)")
    assert result == "ok", f"tab never switched to {tab} (last status: {_read_status()})"


def test_full_tab_sweep_verified_in_sequence():
    """Sweep every tab back-to-back, verifying each switch through the breadcrumb."""
    _require_running_app()
    _send("show")
    saw_breadcrumb = False
    for tab in ("dictate", "settings", "style", "history"):
        _send(f"tab:{tab}")
        result = _await_tab(tab)
        if result == "absent":
            pytest.skip("app predates the status breadcrumb (no status.json)")
        saw_breadcrumb = True
        assert result == "ok", f"tab never switched to {tab}: {_read_status()}"
    assert saw_breadcrumb
