#!/usr/bin/env python3
"""Wayfinder Aura host-side mode supervisor (Steam Deck).

Keeps wayfinder-aura.service running or stopped based on the SteamOS mode
(Desktop vs Game Mode) plus a user toggle, and publishes the current mode for
the app to read.

The point: in Game Mode you usually want the dictation app gone (free the RAM
its whisper/llama servers hold). But some users keybind dictation onto a
controller and want it alive in Game Mode too — so a toggle file decides. This
runs host-side (a systemd --user daemon) and is mode-agnostic by design: it
detects the mode itself rather than relying on a compositor that swaps out from
under it on Desktop<->Game transitions.

Decisions (see decide()):
* Desktop (or indeterminate) -> always RUNNING. Never stop outside Game Mode.
* Game Mode + toggle ON       -> RUNNING.
* Game Mode + toggle OFF      -> STOPPED.

Side effect every poll: writes "game"/"desktop" to
$XDG_RUNTIME_DIR/wayfinder-aura/mode so the app can read which mode it is in
(fail-safe: an indeterminate mode is published as "desktop").

Install:  ~/.local/bin/wayfinder-mode-supervisor.py
          + wayfinder-mode-supervisor.service
"""

import os
import signal
import subprocess
import sys
import tempfile
import time

SERVICE = "wayfinder-aura.service"

TOGGLE_PATH = os.path.join(
    os.path.expanduser("~"), ".config", "wayfinder-aura", "game-mode-dictation",
)

MODE_MARKER_PATH = os.path.join(
    os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"),
    "wayfinder-aura", "mode",
)

POLL_INTERVAL = 5.0    # seconds between polls
MIN_ACTUATE_GAP = 3.0  # min seconds between any two start/stop/restart actions

_SEED = object()  # sentinel for prev_mode before the first poll


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _run_systemctl(args) -> "tuple[int, str]":
    """Run `systemctl --user <args...>` and return (returncode, stdout).

    The single seam through which every systemctl call flows, so tests can
    monkeypatch it. Returns (-1, "") if systemctl itself can't be run.
    """
    try:
        proc = subprocess.run(
            ["systemctl", "--user", *args],
            capture_output=True, text=True, timeout=20, check=False,
        )
        return proc.returncode, (proc.stdout or "").strip()
    except Exception as e:  # noqa: BLE001 — never let systemctl failure escape
        log(f"systemctl {' '.join(args)} failed: {e}")
        return -1, ""


def detect_mode() -> "str | None":
    """Return "game", "desktop", or None (indeterminate).

    Primary: gamescope-session.service active state. "active" -> game; a clean
    inactive/unknown/failed -> desktop. Only when that call is indeterminate
    (systemctl couldn't run, or returned something unexpected) do we fall back
    to an exact-match scan of `ps -eo comm` for a process literally named
    "gamescope" (not a substring, not pgrep -f). If even that fails -> None.
    """
    rc, out = _run_systemctl(["is-active", "gamescope-session.service"])
    state = out.strip()
    # systemctl is-active exits non-zero for inactive units, so rc alone is not
    # a failure signal — the stdout word is what matters.
    if state == "active":
        return "game"
    if state in ("inactive", "unknown", "failed", "deactivating"):
        return "desktop"

    # Indeterminate (e.g. systemctl absent / unexpected output): fall back.
    try:
        proc = subprocess.run(
            ["ps", "-eo", "comm"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        if proc.returncode != 0:
            return None
        count = sum(
            1 for line in proc.stdout.splitlines() if line.strip() == "gamescope"
        )
        return "game" if count >= 1 else "desktop"
    except Exception as e:  # noqa: BLE001
        log(f"ps fallback failed: {e}")
        return None


def read_toggle() -> bool:
    """Read the game-mode-dictation toggle. True iff file content strips to "1".

    Missing / empty / garbage / any error -> False (default: no dictation in
    Game Mode).
    """
    try:
        with open(TOGGLE_PATH, encoding="utf-8") as f:
            return f.read().strip() == "1"
    except Exception:  # noqa: BLE001 — missing or unreadable -> off
        return False


def write_mode_marker(mode: "str | None") -> None:
    """Atomically publish the current mode to MODE_MARKER_PATH. Never raises.

    None (indeterminate) is written as "desktop" — fail-safe, since desktop is
    the "keep the app running" side.
    """
    value = "game" if mode == "game" else "desktop"
    try:
        directory = os.path.dirname(MODE_MARKER_PATH)
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".mode.")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(value)
            os.replace(tmp, MODE_MARKER_PATH)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except Exception as e:  # noqa: BLE001 — publishing the mode is best-effort
        log(f"could not write mode marker: {e}")


def decide(mode: "str | None", toggle_on: bool) -> str:
    """Pure decision: return "RUNNING" or "STOPPED". No I/O — trivially testable.

    * mode != "game" (desktop or None/indeterminate) -> RUNNING (never stop).
    * game + toggle on  -> RUNNING.
    * game + toggle off -> STOPPED.
    """
    if mode != "game":
        return "RUNNING"
    return "RUNNING" if toggle_on else "STOPPED"


def service_active() -> bool:
    """True iff wayfinder-aura.service is currently active."""
    _rc, out = _run_systemctl(["is-active", SERVICE])
    return out.strip() == "active"


def main() -> None:
    def shutdown(*_):
        log("received signal; exiting")
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    log("mode supervisor starting")

    prev_mode = _SEED
    last_actuate = 0.0

    # Publish the mode once before the loop so the app has a marker immediately.
    write_mode_marker(detect_mode())

    while True:
        mode = detect_mode()
        write_mode_marker(mode)
        toggle_on = read_toggle()
        desired = decide(mode, toggle_on)

        seeding = prev_mode is _SEED
        mode_changed = (not seeding) and (mode != prev_mode)

        active = service_active()

        if seeding:
            log(f"seed: mode={mode} toggle={'on' if toggle_on else 'off'} "
                f"desired={desired} service={'active' if active else 'inactive'}")
        elif mode_changed:
            log(f"mode change {prev_mode} -> {mode} "
                f"(toggle={'on' if toggle_on else 'off'} desired={desired})")

        now = time.time()
        throttled = (now - last_actuate) < MIN_ACTUATE_GAP

        def actuate(verb: str) -> None:
            nonlocal last_actuate
            log(f"-> systemctl {verb} {SERVICE}")
            _run_systemctl([verb, SERVICE])
            last_actuate = time.time()

        if desired == "STOPPED":
            # Defense-in-depth: never stop unless we are genuinely in Game Mode
            # with the toggle off, regardless of what `desired` claims.
            if not (mode == "game" and toggle_on is False):
                log("guard: refusing to stop outside game+toggle-off")
            elif active:
                if throttled:
                    log("throttled: skipping stop (min-interval guard)")
                else:
                    actuate("stop")
        else:  # RUNNING
            if not active:
                if throttled:
                    log("throttled: skipping start (min-interval guard)")
                else:
                    actuate("start")
            elif mode_changed:
                # Service is up but the mode flipped (e.g. Game->Desktop with
                # toggle on): restart so the app re-reads its mode marker.
                if throttled:
                    log("throttled: skipping restart (min-interval guard)")
                else:
                    actuate("restart")

        prev_mode = mode
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
