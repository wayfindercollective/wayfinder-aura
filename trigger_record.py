#!/usr/bin/env python3
"""
Simple script to trigger Wayfinder Aura recording toggle.
Can be bound to a KDE Global Shortcut (e.g. the Steam Deck R4 button → Insert).

Tries the $XDG_RUNTIME_DIR socket first — that is where the Flatpak binds it, and
that directory is bind-mounted host<->sandbox so a host process can reach it — then
falls back to /tmp, where the from-source build binds it. One trigger therefore works
for whichever build is the live instance.

Fires a desktop notification on failure so silent breakage (service down, socket
missing, etc.) is visible to the user — important when invoked from a global shortcut
or controller button binding where stdout goes nowhere.
"""
import os
import socket
import subprocess
import sys


def candidate_sockets():
    """Socket paths to try, in priority order (Flatpak runtime dir, then legacy /tmp)."""
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return [
        os.path.join(runtime_dir, "wayfinder-aura", "wayfinder-aura.sock"),
        "/tmp/wayfinder-aura.sock",
    ]


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


def send_toggle(path: str) -> None:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(path)
        sock.send(b"toggle")
    finally:
        sock.close()


paths = candidate_sockets()
last_error = None
for sock_path in paths:
    try:
        send_toggle(sock_path)
        print(f"Toggle sent! ({sock_path})")
        sys.exit(0)
    except (FileNotFoundError, ConnectionRefusedError) as e:
        # Not this one — socket missing, or present but no live listener. Try the next.
        last_error = e
        continue
    except Exception as e:
        last_error = e
        continue

# Every candidate failed — the service is not running (or its socket is dead).
msg = "Service not running. Start with: systemctl --user start wayfinder-aura.service"
notify(msg)
print(f"Wayfinder Aura not reachable (tried: {', '.join(paths)}; last error: {last_error})")
sys.exit(1)
