#!/usr/bin/env python3
"""
Simple script to cycle Wayfinder Aura output style.
Can be bound to a KDE Global Shortcut.

Cycles through: Professional → AI Prompt → Casual → Personal

Tries the $XDG_RUNTIME_DIR socket first (where the Flatpak binds it — that dir is shared
host<->sandbox) and falls back to /tmp (from-source build), so one shortcut works for
whichever build is the live instance. Mirrors trigger_record.py's socket resolution.
Fires a desktop notification on failure so global-shortcut failures are visible.
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
    icon = "dialog-error" if urgency == "critical" else "preferences-desktop-keyboard-shortcuts"
    try:
        subprocess.run(
            ["notify-send", "-u", urgency, "-i", icon, "Wayfinder Aura", message],
            timeout=2,
            check=False,
        )
    except Exception:
        pass


def send_style(path: str) -> None:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(path)
        sock.send(b"style")
    finally:
        sock.close()


paths = candidate_sockets()
last_error = None
for sock_path in paths:
    try:
        send_style(sock_path)
        print(f"Style cycled! ({sock_path})")
        sys.exit(0)
    except (FileNotFoundError, ConnectionRefusedError) as e:
        last_error = e
        continue
    except Exception as e:
        last_error = e
        continue

msg = "Service not running. Start with: systemctl --user start wayfinder-aura.service"
notify(msg)
print(f"Wayfinder Aura not reachable (tried: {', '.join(paths)}; last error: {last_error})")
sys.exit(1)
