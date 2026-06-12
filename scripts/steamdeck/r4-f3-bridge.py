#!/usr/bin/env python3
"""
R4 -> F3 bridge.

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

from evdev import InputDevice, list_devices, ecodes

PAD_NAME = "Microsoft X-Box 360 pad 0"
TRIGGER_CODE = ecodes.BTN_THUMBR  # 318 = R3 / right stick click


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def find_pad() -> InputDevice | None:
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
                    ev.type == ecodes.EV_KEY
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
