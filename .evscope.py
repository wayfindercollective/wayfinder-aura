"""Temp diagnostic: exact codes from the Corsair side-grid + any F3 (60s)."""
import select
import time

import evdev

devs = {}
for p in evdev.list_devices():
    try:
        d = evdev.InputDevice(p)
        # Only the Corsair interfaces and any virtual keyboard — not real keyboards.
        if "corsair" in d.name.lower() or "virtual" in d.name.lower():
            devs[d.fd] = d
    except Exception:
        pass
print(f"watching {len(devs)} devices", flush=True)

end = time.time() + 60
while time.time() < end:
    r, _, _ = select.select(list(devs), [], [], 1)
    for fd in r:
        try:
            for ev in devs[fd].read():
                if ev.type == evdev.ecodes.EV_KEY and ev.value == 1:
                    name = evdev.ecodes.KEY.get(ev.code) or evdev.ecodes.BTN.get(ev.code) or ev.code
                    print(f"{devs[fd].name} | {name} (code={ev.code})", flush=True)
        except OSError:
            pass
print("done", flush=True)
