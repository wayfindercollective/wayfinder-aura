"""Short controller rumble pulses for Game Mode dictation feedback.

In SteamOS Game Mode the PyQt overlay cannot render over a fullscreen game, so
audio cues are the primary feedback channel. A brief force-feedback pulse on a
rumble-capable pad (Steam Deck controls, Xbox pad, etc.) is a useful second
channel when game audio is loud.

Design constraints (mirror feedback/audio.py):

* Fire-and-forget and NON-blocking: pulses run on a daemon thread so the Tk UI
  thread is never held up.
* SILENT no-op on any failure (no pad, no FF support, permission denied, missing
  evdev, Flatpak sandbox without /dev/input). A missing rumble must never break
  dictation.
* Prefer the python-evdev force-feedback API when available; never depend on a
  host binary or Steam IPC.
"""

from __future__ import annotations

import threading
import time

# Cue name -> (strong_magnitude 0..0xFFFF, weak_magnitude, duration_ms)
_PULSES: dict[str, tuple[int, int, int]] = {
    "start": (0xC000, 0x8000, 80),
    "stop": (0x6000, 0x4000, 50),
    "done": (0xA000, 0xA000, 100),
    "error": (0xFFFF, 0xFFFF, 160),
}

_CUES = frozenset(_PULSES)


def _find_ff_device():
    """Return an open EV_FF InputDevice, or None.

    Best-effort: any failure (import, permission, no devices) yields None.
    Caller owns closing the device.
    """
    try:
        from evdev import InputDevice, ecodes, list_devices
    except Exception:
        return None

    try:
        paths = list(list_devices())
    except Exception:
        return None

    # Prefer pads / joysticks; fall back to any FF-capable device.
    preferred: list = []
    others: list = []
    for path in paths:
        try:
            dev = InputDevice(path)
        except Exception:
            continue
        try:
            caps = dev.capabilities()
            if ecodes.EV_FF not in caps:
                dev.close()
                continue
            name = (dev.name or "").lower()
            if any(
                token in name
                for token in (
                    "x-box",
                    "xbox",
                    "steam",
                    "gamepad",
                    "joystick",
                    "controller",
                    "dualshock",
                    "dualsense",
                    "wireless controller",
                )
            ):
                preferred.append(dev)
            else:
                others.append(dev)
        except Exception:
            try:
                dev.close()
            except Exception:
                pass

    for group in (preferred, others):
        if group:
            # Close unused candidates; keep the first of this group.
            keep = group[0]
            for extra in group[1:]:
                try:
                    extra.close()
                except Exception:
                    pass
            # And close the other group entirely.
            other_group = others if group is preferred else preferred
            for extra in other_group:
                try:
                    extra.close()
                except Exception:
                    pass
            return keep

    return None


def _pulse_blocking(name: str) -> None:
    """Upload and play a short rumble effect. Never raises."""
    pulse = _PULSES.get(name)
    if pulse is None:
        return
    strong, weak, duration_ms = pulse
    dev = None
    effect_id = None
    try:
        from evdev import ecodes, ff

        dev = _find_ff_device()
        if dev is None:
            return

        effect = ff.Effect(
            ecodes.FF_RUMBLE,
            -1,  # id assigned by kernel
            0,   # direction
            ff.Trigger(0, 0),
            ff.Replay(duration_ms, 0),
            ff.EffectType(
                ff_rumble_effect=ff.Rumble(
                    strong_magnitude=strong, weak_magnitude=weak
                )
            ),
        )
        effect_id = dev.upload_effect(effect)
        dev.write(ecodes.EV_FF, effect_id, 1)
        # Hold the process open long enough for the effect to finish, then stop.
        time.sleep(max(duration_ms, 1) / 1000.0 + 0.02)
        try:
            dev.write(ecodes.EV_FF, effect_id, 0)
        except Exception:
            pass
    except Exception:
        pass
    finally:
        if dev is not None and effect_id is not None:
            try:
                dev.erase_effect(effect_id)
            except Exception:
                pass
        if dev is not None:
            try:
                dev.close()
            except Exception:
                pass


def pulse_for_cue(name: str) -> None:
    """Fire a short rumble for a named cue (start/stop/done/error).

    Never blocks the caller and never raises — a failed pulse is silent.
    """
    if name not in _CUES:
        return
    threading.Thread(
        target=_pulse_blocking,
        args=(name,),
        name=f"wf-rumble-{name}",
        daemon=True,
    ).start()
