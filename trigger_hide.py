#!/usr/bin/env python3
"""Hide the main Wayfinder Aura window to the system tray (keep dictation running).

Bound as a .desktop Action so KDE/GNOME app-menu and some taskbar right-click
menus expose "Hide to tray". Same socket bridge as trigger_record.py.
"""
import os
import socket
import subprocess
import sys


def candidate_sockets():
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return [
        os.path.join(runtime_dir, "wayfinder-aura", "wayfinder-aura.sock"),
        "/tmp/wayfinder-aura.sock",
    ]


def notify(message: str, urgency: str = "critical") -> None:
    try:
        subprocess.run(
            ["notify-send", "-u", urgency, "-i", "window-minimize", "Wayfinder Aura", message],
            timeout=2,
            check=False,
        )
    except Exception:
        pass


def send_hide(path: str) -> None:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(2)
        sock.connect(path)
        sock.send(b"hide")
        try:
            sock.recv(16)
        except socket.timeout:
            pass
    finally:
        sock.close()


paths = candidate_sockets()
last_error = None
for sock_path in paths:
    try:
        send_hide(sock_path)
        print(f"Hide sent! ({sock_path})")
        sys.exit(0)
    except (FileNotFoundError, ConnectionRefusedError) as e:
        last_error = e
        continue
    except Exception as e:
        last_error = e
        continue

msg = "Service not running. Start Wayfinder Aura first."
notify(msg)
print(f"Wayfinder Aura not reachable (tried: {', '.join(paths)}; last error: {last_error})")
sys.exit(1)
