"""
Unix socket-based hotkey listener for Wayfinder Aura.

Allows external scripts to trigger recording via a simple socket connection.
Useful for KDE shortcuts on Wayland where direct input monitoring is restricted.
"""

import os
import socket
from queue import Queue
from threading import Event
from typing import Callable, Optional

from ..config import SOCKET_PATH
from .types import EventType


def socket_listener(
    event_queue: Queue,
    stop_event: Event,
    log_callback: Optional[Callable[[str], None]] = None
):
    """
    Listen for toggle commands on a Unix socket.
    
    This allows KDE shortcuts to trigger recording via a simple script. The socket path
    is SOCKET_PATH from wayfinder.config (under $XDG_RUNTIME_DIR in a Flatpak, else /tmp),
    e.g. with SOCK set to that path:
    ```
    echo "toggle"    | nc -U "$SOCK"   # Toggle recording
    echo "style"     | nc -U "$SOCK"   # Cycle styles
    echo "style:dev" | nc -U "$SOCK"   # Set a specific style
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
    
    # Ensure the socket's parent dir exists (e.g. $XDG_RUNTIME_DIR/wayfinder-aura). The
    # Flatpak manifest also creates it via --filesystem=xdg-run/...:create, but do it here
    # too so a freshly-booted runtime dir and the non-Flatpak path both work.
    try:
        os.makedirs(os.path.dirname(SOCKET_PATH), exist_ok=True)
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
                data_str = data.decode("utf-8").strip() if data else ""
                
                if data_str == "toggle":
                    log("🎯 Toggle received via socket")
                    event_queue.put((EventType.HOTKEY_PRESSED, None))
                elif data_str == "style":
                    # Cycle to next style
                    log("✎ Style toggle received via socket")
                    event_queue.put((EventType.STYLE_TOGGLE, None))
                elif data_str.startswith("style:"):
                    # Set specific style (style:professional, style:dev, style:casual, style:minimal, style:personal)
                    style = data_str.split(":", 1)[1]
                    log(f"✎ Style set to '{style}' via socket")
                    event_queue.put((EventType.STYLE_TOGGLE, style))
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
    Send a toggle command to a running Wayfinder Aura instance.
    
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


def send_style(style: Optional[str] = None):
    """
    Send a style command to a running Wayfinder Aura instance.
    
    Args:
        style: Optional style name ("minimal", "professional", "casual", "dev", "personal").
               If None, cycles to the next style.
    
    Returns:
        True if command was sent successfully, False otherwise.
    """
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(SOCKET_PATH)
        if style:
            sock.send(f"style:{style}".encode("utf-8"))
        else:
            sock.send(b"style")
        sock.close()
        return True
    except Exception:
        return False




