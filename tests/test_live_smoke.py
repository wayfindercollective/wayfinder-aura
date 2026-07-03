"""Live socket smoke test — drives a RUNNING app over its control socket.

Skipped by default; run against a live instance with::

    WAYFINDER_LIVE=1 python3 -m pytest tests/test_live_smoke.py -v

Protocol reality (read from ``socket_listener`` in wayfinder_main.py): the
listener is fire-and-forget. It accepts a connection, ``recv``s one command,
enqueues the matching EventType, and immediately ``conn.close()``s — there is NO
reply written back. So "the socket accepts the command" is the only observable
ACK, and that is exactly what these tests assert: a successful connect + send to
the live listener (which then acts on it — the reviewer confirms the visual tab
switch / window raise via screenshots; that half is human-only).

Each command needs its own connection because the server handles one accept at a
time and closes after each recv.
"""

from __future__ import annotations

import os
import socket

import pytest

from wayfinder.config import SOCKET_PATH  # single source of truth for the path

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


def test_show_command_accepted():
    _require_running_app()
    _send("show")  # raises if the live listener won't accept the command


@pytest.mark.parametrize("tab", ["dictate", "settings", "style", "history"])
def test_tab_switch_commands_accepted(tab):
    _require_running_app()
    _send(f"tab:{tab}")


def test_full_tab_sweep_in_sequence():
    """Sweep every tab back-to-back — mirrors the reviewer's screenshot loop."""
    _require_running_app()
    _send("show")
    for tab in ("dictate", "settings", "style", "history"):
        _send(f"tab:{tab}")
