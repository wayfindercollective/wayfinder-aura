#!/usr/bin/env python3
"""
DEPRECATED: legacy R4 -> F3 bridge.

This script is kept only as historical reference for the old Steam Deck
workaround. The supported trigger path is now wayfinder-trigger-daemon.py,
which watches BTN_THUMBR directly and sends the "toggle" command to
$XDG_RUNTIME_DIR/wayfinder-aura/wayfinder-aura.sock.

By default this script exits without sending keys. Set
WAYFINDER_ALLOW_LEGACY_R4_F3=1 only when intentionally reproducing the old
X11-dependent path for debugging.

Steam Input's keyboard injection is broken on this SteamOS install; gamepad
emulation is fine. Pair this daemon with R4 bound to "Right Joystick Click"
(xinput_button JOYSTICK_RIGHT) in the Steam Desktop Layout. The actual right-
stick click is bound to mouse-left in the Default preset, so it does not
collide.

R4 -> Steam virtual X-Box pad emits BTN_THUMBR -> this daemon -> xdotool key F3
"""
import os
import signal
import subprocess
import sys
import time

PAD_NAME = "Microsoft X-Box 360 pad 0"
EVENT_TYPE_KEY = 1  # ecodes.EV_KEY
TRIGGER_CODE = 318  # ecodes.BTN_THUMBR = R3 / right stick click
LEGACY_ENABLE_ENV = "WAYFINDER_ALLOW_LEGACY_R4_F3"


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def legacy_enabled() -> bool:
    return os.environ.get(LEGACY_ENABLE_ENV) == "1"


def find_pad():
    from evdev import InputDevice, list_devices

    for path in list_devices():
        try:
            dev = InputDevice(path)
        except OSError:
            continue
        if dev.name == PAD_NAME:
            return dev
        dev.close()
    return None


def fire_f3() -> None:
    env = os.environ.copy()
    env.setdefault("DISPLAY", ":0")
    subprocess.Popen(
        ["/usr/bin/xdotool", "key", "F3"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def main() -> None:
    if not legacy_enabled():
        log(
            "r4-f3-bridge.py is superseded and disabled. "
            "Use wayfinder-trigger-daemon.py, or set "
            f"{LEGACY_ENABLE_ENV}=1 to reproduce the legacy xdotool path."
        )
        sys.exit(1)

    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    while True:
        dev = find_pad()
        if dev is None:
            log("virtual pad not found; sleeping 3s")
            time.sleep(3)
            continue
        log(f"listening on {dev.path} ({dev.name})")
        try:
            for ev in dev.read_loop():
                if (
                    ev.type == EVENT_TYPE_KEY
                    and ev.code == TRIGGER_CODE
                    and ev.value == 1
                ):
                    log("R3 press -> xdotool key F3")
                    fire_f3()
        except OSError as e:
            log(f"pad disconnected ({e}); reconnecting")
            time.sleep(1)


if __name__ == "__main__":
    main()
