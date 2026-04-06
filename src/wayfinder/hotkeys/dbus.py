"""
D-Bus GlobalShortcuts portal listener for Wayfinder Aura.

Uses the XDG GlobalShortcuts portal for Wayland-compatible global hotkeys.
This is the proper way to do global hotkeys on Wayland/KDE.
"""

import os
from queue import Queue
from threading import Event
from typing import Callable, Optional

from .types import EventType

# Check if D-Bus is available
try:
    import dbus
    from dbus.mainloop.glib import DBusGMainLoop
    from gi.repository import GLib
    DBUS_AVAILABLE = True
except ImportError:
    DBUS_AVAILABLE = False

# Application ID for portal registration
# This must match the desktop file name (without .desktop extension)
APP_ID = os.environ.get("FLATPAK_ID", "wayfinder-aura")


def is_dbus_available() -> bool:
    """Check if D-Bus support is available."""
    return DBUS_AVAILABLE


def wayland_hotkey_listener(
    event_queue: Queue,
    hotkey_display: str,
    stop_event: Event,
    log_callback: Optional[Callable[[str], None]] = None
) -> bool:
    """
    Wayland-compatible hotkey listener using XDG GlobalShortcuts portal.
    
    This is the proper way to do global hotkeys on Wayland/KDE.
    
    Args:
        event_queue: Queue to put events into
        hotkey_display: Human-readable hotkey string (e.g., "F9")
        stop_event: Threading event to signal shutdown
        log_callback: Optional function to call with log messages
        
    Returns:
        True if listener started successfully, False otherwise.
    """
    def log(msg: str):
        if log_callback:
            try:
                log_callback(msg)
            except Exception:
                pass
    
    if not DBUS_AVAILABLE:
        log("⚠️ D-Bus not available for Wayland shortcuts")
        return False
    
    try:
        DBusGMainLoop(set_as_default=True)
        bus = dbus.SessionBus()
        
        portal = bus.get_object(
            "org.freedesktop.portal.Desktop",
            "/org/freedesktop/portal/desktop"
        )
        
        shortcuts_iface = dbus.Interface(
            portal,
            "org.freedesktop.portal.GlobalShortcuts"
        )
        
        # Create session with proper app identification
        log(f"🔗 Connecting to GlobalShortcuts portal as '{APP_ID}'...")
        
        # Use app_id-based tokens for proper KDE Global Shortcuts integration
        session_token = APP_ID.replace(".", "_").replace("-", "_")
        
        session_options = dbus.Dictionary({
            "handle_token": dbus.String(f"{session_token}_session"),
            "session_handle_token": dbus.String(session_token),
        }, signature="sv")
        
        request_path = shortcuts_iface.CreateSession(session_options)
        log("✓ Session created")
        
        # Wait for session to be ready
        import time
        time.sleep(0.5)
        
        # Try to get the session
        user = os.environ.get('USER', 'user')
        session_path = f"/org/freedesktop/portal/desktop/session/{user}/{session_token}"
        
        # Bind shortcuts
        shortcuts = dbus.Array([
            dbus.Struct([
                dbus.String("record-toggle"),
                dbus.Dictionary({
                    "description": dbus.String("Toggle voice recording"),
                    "preferred_trigger": dbus.String(hotkey_display),
                }, signature="sv")
            ], signature="(sa{sv})")
        ], signature="(sa{sv})")
        
        bind_options = dbus.Dictionary({
            "handle_token": dbus.String(f"{session_token}_bind"),
        }, signature="sv")
        
        try:
            shortcuts_iface.BindShortcuts(
                dbus.ObjectPath(session_path),
                shortcuts,
                "",  # parent_window
                bind_options
            )
            log(f"✓ Shortcut registered: {hotkey_display}")
        except dbus.exceptions.DBusException as e:
            log(f"⚠️ Could not bind shortcut: {e}")
            log("   You may need to set it manually in System Settings")
        
        # Listen for activation signals
        def on_activated(session, shortcut_id, timestamp, options):
            if shortcut_id == "record-toggle":
                log("🎯 Hotkey activated!")
                event_queue.put((EventType.HOTKEY_PRESSED, None))
        
        def on_deactivated(session, shortcut_id, timestamp, options):
            pass
        
        bus.add_signal_receiver(
            on_activated,
            signal_name="Activated",
            dbus_interface="org.freedesktop.portal.GlobalShortcuts",
            path="/org/freedesktop/portal/desktop"
        )
        
        log("🎧 Listening for Wayland global shortcuts...")
        
        # Run GLib main loop
        loop = GLib.MainLoop()
        
        def check_stop():
            if stop_event.is_set():
                loop.quit()
                return False
            return True
        
        GLib.timeout_add(500, check_stop)
        loop.run()
        
        return True
        
    except Exception as e:
        log(f"⚠️ Wayland hotkey setup failed: {e}")
        return False




