#!/usr/bin/env python3
"""
Simple script to trigger Wayfinder Aura recording toggle.
Can be bound to a KDE Global Shortcut.

Tries the $XDG_RUNTIME_DIR socket first (Flatpak; that dir is shared host<->sandbox),
then /tmp (from-source build). Prefer the repo-root trigger_record.py, which also raises
a desktop notification on failure; this is the minimal scripts/ variant.
"""
import os
import socket
import sys


def candidate_sockets():
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return [
        os.path.join(runtime_dir, "wayfinder-aura", "wayfinder-aura.sock"),
        "/tmp/wayfinder-aura.sock",
    ]


for sock_path in candidate_sockets():
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.connect(sock_path)
            sock.send(b"toggle")
        finally:
            sock.close()
        print(f"Toggle sent! ({sock_path})")
        sys.exit(0)
    except (FileNotFoundError, ConnectionRefusedError):
        continue
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)

print("Wayfinder Aura not running")
sys.exit(1)
