"""
Unix socket-based hotkey listener for Wayfinder Voice.

Allows external scripts to trigger recording via a simple socket connection.
Useful for KDE shortcuts on Wayland where direct input monitoring is restricted.
"""

import os
import socket
from queue import Queue
from threading import Event
from typing import Callable, Optional

from ..config import SOCKET_PATH
from .evdev import EventType


def socket_listener(
    event_queue: Queue,
    stop_event: Event,
    log_callback: Optional[Callable[[str], None]] = None
):
    """
    Listen for toggle commands on a Unix socket.
    
    This allows KDE shortcuts to trigger recording via a simple script:
    ```
    echo "toggle" | nc -U /tmp/wayfinder-voice.sock
    ```
    
    Args:
        event_queue: Queue to put events into
        stop_event: Threading event to signal shutdown
        log_callback: Optional function to call with log messages
    """
    def log(msg: str):
        if log_callback:
            try:
                log_callback(msg)
            except Exception:
                pass
    
    # Remove old socket if exists
    try:
        os.unlink(SOCKET_PATH)
    except FileNotFoundError:
        pass
    
    try:
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(SOCKET_PATH)
        server.listen(1)
        server.settimeout(1.0)
        
        log(f"📡 Socket listener ready: {SOCKET_PATH}")
        
        while not stop_event.is_set():
            try:
                conn, _ = server.accept()
                data = conn.recv(64)
                if data == b"toggle":
                    log("🎯 Toggle received via socket")
                    event_queue.put((EventType.HOTKEY_PRESSED, None))
                conn.close()
            except socket.timeout:
                continue
            except Exception as e:
                if not stop_event.is_set():
                    log(f"⚠️ Socket error: {e}")
        
        server.close()
        try:
            os.unlink(SOCKET_PATH)
        except Exception:
            pass
    except Exception as e:
        log(f"⚠️ Socket listener failed: {e}")


def send_toggle():
    """
    Send a toggle command to a running Wayfinder Voice instance.
    
    This can be used from external scripts to trigger recording.
    
    Returns:
        True if command was sent successfully, False otherwise.
    """
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(SOCKET_PATH)
        sock.send(b"toggle")
        sock.close()
        return True
    except Exception:
        return False




