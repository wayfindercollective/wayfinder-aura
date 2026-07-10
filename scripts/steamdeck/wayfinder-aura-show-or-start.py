#!/usr/bin/env python3
"""Restore a live Wayfinder window, or start the user service.

This is the taskbar fast path on Steam Deck. It talks to the app's existing Unix socket and
does not create a daemon. Older installed builds close the connection without acknowledging
``show``; that clean close is accepted as a successful legacy response.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
from pathlib import Path

SERVICE = "wayfinder-aura.service"


def socket_path() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return Path(runtime) / "wayfinder-aura" / "wayfinder-aura.sock"


def show_live_instance(timeout: float = 0.35) -> bool:
    """Return True when a current or legacy live app accepts ``show``."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout)
            client.connect(str(socket_path()))
            client.sendall(b"show")
            response = client.recv(16)
            # Current builds acknowledge with ``ok``. Pre-ACK builds process the command,
            # then immediately close their per-command connection, producing clean EOF.
            # Treating EOF as success avoids a redundant Flatpak activation that can start
            # a second legacy instance and steal the control-socket path.
            return response in (b"ok", b"")
    except OSError:
        return False


def service_is_active() -> bool:
    return subprocess.run(
        ["systemctl", "--user", "is-active", "--quiet", SERVICE],
        check=False,
    ).returncode == 0


def main() -> int:
    if show_live_instance():
        return 0

    if not service_is_active():
        return subprocess.run(
            ["systemctl", "--user", "start", SERVICE],
            check=False,
        ).returncode

    # Active service + unhealthy control socket: restart rather than leaving a dead taskbar
    # click. This is only the recovery path; healthy launches never restart the app.
    return subprocess.run(
        ["systemctl", "--user", "restart", SERVICE],
        check=False,
    ).returncode


if __name__ == "__main__":
    sys.exit(main())
