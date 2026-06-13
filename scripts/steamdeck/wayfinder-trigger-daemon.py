#!/usr/bin/env python3
"""Wayfinder Aura host-side trigger daemon (Steam Deck).

Watches input devices via evdev (host side — the Flatpak sandbox can't) and
drives the app through its Unix socket. No X11/Wayland involvement at all, so
it works identically in Desktop Mode and Game Mode — unlike XRecord/pynput,
which gamescope's input routing bypasses entirely (verified 2026-06-12: a
pynput listener on gamescope's Xwayland sees nothing).

Watched devices:

* The Corsair Scimitar's "Keyboard" HID interface — its side grid is
  programmed (iCUE Hardware Actions, onboard memory) to emit F3/F2. The
  device is GRABBED exclusively: presses reach only this daemon, so they
  cannot double-trigger the app's pynput listener in Desktop Mode and cannot
  leak F3/F2 to whatever window has focus. Any other key from the grabbed
  side grid is logged and dropped.
    F3 -> socket "toggle"   (start/stop dictation)
    F2 -> socket "style"    (cycle dictation style)

* Steam's virtual "Microsoft X-Box 360 pad 0" — NOT grabbed (Steam/games own
  it). BTN_THUMBR (right-stick click) -> "toggle". Dormant unless the Steam
  Desktop Layout binds R4 to Right Joystick Click; replaces the old
  r4-f3-bridge (which faked F3 via xdotool and therefore died wherever X
  listeners do). Do not run r4-f3-bridge.service alongside this daemon.

Replaces: r4-f3-bridge.py (kept in repo for history).
Install:  ~/.local/bin/wayfinder-trigger-daemon.py + wayfinder-trigger.service
"""

import os
import signal
import socket
import subprocess
import sys
import time

from evdev import InputDevice, ecodes, list_devices

SOCKET_PATH = os.path.join(
    os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"),
    "wayfinder-aura", "wayfinder-aura.sock",
)

# (human label, match function on device name, grab?, {keycode: socket command})
WATCHES = [
    (
        "scimitar-side-grid",
        lambda name: "SCIMITAR" in name.upper() and name.endswith("Keyboard"),
        True,
        {ecodes.KEY_F3: b"toggle", ecodes.KEY_F2: b"style"},
    ),
    (
        "steam-virtual-pad",
        lambda name: name == "Microsoft X-Box 360 pad 0",
        False,
        {ecodes.BTN_THUMBR: b"toggle"},
    ),
]

RESCAN_INTERVAL = 3.0  # seconds between hotplug rescans for missing devices


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


_notified_down = False


def send_command(cmd: bytes) -> bool:
    """Send a command to the app socket. Notifies (once per outage) on failure."""
    global _notified_down
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(2.0)
        sock.connect(SOCKET_PATH)
        sock.send(cmd)
        sock.close()
        log(f"sent '{cmd.decode()}'")
        _notified_down = False
        return True
    except OSError as e:
        log(f"socket send failed ({e}) — is wayfinder-aura.service running?")
        if not _notified_down:
            _notified_down = True
            try:
                subprocess.run(
                    ["notify-send", "-u", "critical", "-i", "dialog-error",
                     "Wayfinder Aura",
                     "Trigger pressed but the app socket is unreachable. "
                     "Restart: systemctl --user restart wayfinder-aura.service"],
                    timeout=2, check=False,
                )
            except Exception:
                pass
        return False


class Watch:
    def __init__(self, label, matcher, grab, keymap):
        self.label = label
        self.matcher = matcher
        self.grab = grab
        self.keymap = keymap
        self.device = None

    def attach(self, taken_paths) -> bool:
        for path in list_devices():
            if path in taken_paths:
                continue
            try:
                dev = InputDevice(path)
            except OSError:
                continue
            if not self.matcher(dev.name):
                dev.close()
                continue
            if self.grab:
                try:
                    dev.grab()
                except OSError as e:
                    log(f"{self.label}: could not grab {dev.name} ({e}); "
                        "watching without exclusivity")
            self.device = dev
            log(f"{self.label}: attached {dev.path} ({dev.name})"
                f"{' [grabbed]' if self.grab else ''}")
            return True
        return False

    def detach(self):
        if self.device is not None:
            try:
                self.device.ungrab()
            except OSError:
                pass
            try:
                self.device.close()
            except OSError:
                pass
            self.device = None


def main() -> None:
    watches = [Watch(*w) for w in WATCHES]

    def shutdown(*_):
        for w in watches:
            w.detach()
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    import select as _select

    last_scan = 0.0
    while True:
        now = time.time()
        if now - last_scan >= RESCAN_INTERVAL:
            last_scan = now
            taken = {w.device.path for w in watches if w.device is not None}
            for w in watches:
                if w.device is None:
                    w.attach(taken)

        fd_map = {w.device.fd: w for w in watches if w.device is not None}
        if not fd_map:
            time.sleep(RESCAN_INTERVAL)
            continue

        readable, _, _ = _select.select(list(fd_map), [], [], 1.0)
        for fd in readable:
            w = fd_map[fd]
            try:
                for ev in w.device.read():
                    if ev.type != ecodes.EV_KEY or ev.value != 1:
                        continue  # key-down only; ignore release + autorepeat
                    cmd = w.keymap.get(ev.code)
                    if cmd is not None:
                        log(f"{w.label}: keycode {ev.code} -> {cmd.decode()}")
                        send_command(cmd)
                    elif w.grab:
                        name = ecodes.KEY.get(ev.code, ev.code)
                        log(f"{w.label}: unmapped key {name} swallowed by grab")
            except OSError as e:
                log(f"{w.label}: device lost ({e}); will rescan")
                w.detach()


if __name__ == "__main__":
    main()
