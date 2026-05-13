#!/usr/bin/env python3
"""
Simple script to trigger Wayfinder Aura recording toggle.
Can be bound to a KDE Global Shortcut.

Fires a desktop notification on failure so silent breakage (service down,
socket missing, etc.) is visible to the user — important when invoked from
a global shortcut or controller button binding where stdout goes nowhere.
"""
import socket
import subprocess
import sys

SOCKET_PATH = "/tmp/wayfinder-aura.sock"


def notify(message: str, urgency: str = "critical") -> None:
    icon = "dialog-error" if urgency == "critical" else "audio-input-microphone"
    try:
        subprocess.run(
            ["notify-send", "-u", urgency, "-i", icon, "Wayfinder Aura", message],
            timeout=2,
            check=False,
        )
    except Exception:
        pass  # best-effort — never let notification failure mask the real error


try:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(SOCKET_PATH)
    sock.send(b"toggle")
    sock.close()
    print("Toggle sent!")
except FileNotFoundError:
    msg = "Service not running. Start with: systemctl --user start wayfinder-aura.service"
    notify(msg)
    print(f"Wayfinder Aura not running ({SOCKET_PATH} missing)")
    sys.exit(1)
except ConnectionRefusedError:
    msg = "Service running but socket dead. Restart: systemctl --user restart wayfinder-aura.service"
    notify(msg)
    print(f"Socket connection refused at {SOCKET_PATH}")
    sys.exit(1)
except Exception as e:
    notify(f"Trigger failed: {e}")
    print(f"Error: {e}")
    sys.exit(1)

