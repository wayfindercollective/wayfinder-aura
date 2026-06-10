"""Feral GameMode integration — pause Wayfinder hotkeys while a game is running.

When a Lutris/Steam game registers with the ``gamemoded`` D-Bus service, this
listener bumps a process-wide active-game counter. The event dispatcher checks
``is_hotkeys_paused()`` and silently drops HOTKEY_PRESSED / STYLE_TOGGLE events
while the counter is > 0. This avoids F-key collisions with in-game keybinds
without per-user, per-game window rules.

Cross-DE: works wherever ``gamemoded`` runs. Lutris activates it automatically;
Steam triggers it via Proton when ``gamemode`` is enabled. KDE and GNOME both
ignore the daemon — only this integration cares about its signals.

Graceful when gamemoded is absent: the listener thread logs a one-shot notice
and exits cleanly; the pause flag stays False, so hotkeys behave exactly as
before. Same applies on systems missing ``python-dbus`` / PyGObject.
"""

from __future__ import annotations

import threading
from typing import Callable, Optional

try:
    import dbus
    from dbus.mainloop.glib import DBusGMainLoop
    from gi.repository import GLib  # type: ignore[import-not-found]
    _GAMEMODE_DEPS_OK = True
except ImportError:
    _GAMEMODE_DEPS_OK = False

_GM_SERVICE = "com.feralinteractive.GameMode"
_GM_PATH = "/com/feralinteractive/GameMode"
_GM_IFACE = "com.feralinteractive.GameMode"

# Process-wide active-game counter. Readers check is_hotkeys_paused().
# The lock guards reads + writes so the dispatcher (in the UI thread) never
# observes a torn update from the D-Bus mainloop thread.
_pause_lock = threading.Lock()
_active_games = 0


def is_gamemode_available() -> bool:
    """True if the dbus + PyGObject deps imported. Says nothing about the daemon."""
    return _GAMEMODE_DEPS_OK


def is_hotkeys_paused() -> bool:
    """True iff at least one game is currently registered with gamemoded."""
    with _pause_lock:
        return _active_games > 0


def _set_active_count(count: int) -> None:
    global _active_games
    with _pause_lock:
        _active_games = max(0, int(count))


def _bump(delta: int) -> int:
    global _active_games
    with _pause_lock:
        _active_games = max(0, _active_games + delta)
        return _active_games


def gamemode_pause_listener(
    stop_event: threading.Event,
    log_callback: Optional[Callable[[str], None]] = None,
) -> None:
    """Run a D-Bus GLib mainloop that tracks gamemoded's active-game count.

    Intended target for ``threading.Thread(target=gamemode_pause_listener, ...)``.
    Returns silently when:
      - python-dbus / PyGObject are missing,
      - the gamemoded service can't be contacted (no error path raised),
      - ``stop_event`` is set (clean shutdown).

    The pause flag is process-wide; readers use :func:`is_hotkeys_paused`.
    """
    def log(msg: str) -> None:
        if log_callback is not None:
            try:
                log_callback(msg)
            except Exception:
                # A logging failure must never crash the listener thread.
                pass

    if not _GAMEMODE_DEPS_OK:
        log("ℹ️  GameMode integration disabled (python-dbus / PyGObject not importable)")
        return

    try:
        DBusGMainLoop(set_as_default=True)
        bus = dbus.SessionBus()
    except Exception as e:
        log(f"⚠️  GameMode listener: SessionBus unavailable — {e}")
        return

    # Initial snapshot. Gamemoded exposes ClientCount as a D-Bus property.
    # If the daemon isn't on the bus yet, the GetNameOwner/Properties.Get will
    # raise — we treat that as "no games active" and still register the signal
    # receivers so a later daemon start is handled.
    try:
        gm = bus.get_object(_GM_SERVICE, _GM_PATH)
        props = dbus.Interface(gm, "org.freedesktop.DBus.Properties")
        count = int(props.Get(_GM_IFACE, "ClientCount"))
        _set_active_count(count)
        if count > 0:
            log(f"🎮 GameMode: {count} game(s) already active; hotkeys paused")
        else:
            log("✓ GameMode listener attached (no games active)")
    except dbus.DBusException:
        # Daemon not up yet — fine, we still want to react when it starts.
        _set_active_count(0)
        log("ℹ️  GameMode daemon not yet on bus; will react when a game registers")

    def on_game_registered(pid, _dbus_path) -> None:
        count = _bump(+1)
        log(f"🎮 GameMode: game registered (pid={pid}) — hotkeys paused (active={count})")

    def on_game_unregistered(pid, _dbus_path) -> None:
        count = _bump(-1)
        if count == 0:
            log("🎮 GameMode: last game ended — hotkeys resumed")
        else:
            log(f"🎮 GameMode: game ended (pid={pid}) — {count} still active")

    try:
        bus.add_signal_receiver(
            on_game_registered,
            signal_name="GameRegistered",
            dbus_interface=_GM_IFACE,
        )
        bus.add_signal_receiver(
            on_game_unregistered,
            signal_name="GameUnregistered",
            dbus_interface=_GM_IFACE,
        )
    except Exception as e:
        log(f"⚠️  GameMode listener: failed to attach signal receivers — {e}")
        return

    loop = GLib.MainLoop()

    def _shutdown_watcher() -> None:
        stop_event.wait()
        # Quit must happen on the loop's thread; idle_add schedules it safely.
        GLib.idle_add(loop.quit)

    threading.Thread(target=_shutdown_watcher, daemon=True).start()

    try:
        loop.run()
    except Exception as e:
        log(f"❌ GameMode listener mainloop crashed: {e}")
    finally:
        # Releasing the pause flag on shutdown prevents a permanently-paused state
        # if the listener exits while a game is still registered (e.g. app quit
        # before game exit).
        _set_active_count(0)
