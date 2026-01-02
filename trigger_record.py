#!/usr/bin/env python3
"""
Simple script to trigger Wayfinder Aura recording toggle.
Can be bound to a KDE Global Shortcut.
"""
import socket
import sys

SOCKET_PATH = "/tmp/wayfinder-aura.sock"

try:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(SOCKET_PATH)
    sock.send(b"toggle")
    sock.close()
    print("Toggle sent!")
except FileNotFoundError:
    print("Wayfinder Aura not running")
    sys.exit(1)
except Exception as e:
    print(f"Error: {e}")
    sys.exit(1)

