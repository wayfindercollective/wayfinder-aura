#!/usr/bin/env python3
"""Attach the notarized Mac DMG to every new Aura release, from this Mac.

Installed as a LaunchAgent by install_mac_release_watcher.sh (hourly). The
signing certificate and notary profile stay in the Mac's login Keychain: no
signing secret is uploaded anywhere. Each run:

1. lists the wayfinder-aura releases from the last 7 days (stable, and the
   betas the Beta workflow makes) that carry no Mac DMG for their version;
2. builds, notarizes and attaches the newest such release with
   attach_mac_dmg.sh (one build per run; the next run takes the next one);
3. gives up on a release after 3 failed builds (the state file records them),
   so a broken tag can't keep the Mac busy every hour.

Bounded by design: one instance (non-blocking flock, released by the OS on
any exit), one build per run, a 2-hour wall clock for the build (its process
group is terminated, then killed), notarytool's wait capped at 60 minutes
(packaging/macos/build.py), and the Mac CI hold this run takes is lifted on
every exit path.

Nothing happens while another copy runs (lock file), while the Mac CI queue
is held by someone else (a DMG build or Infra Mac's own work), or while less
than MIN_FREE_GIB of memory is free: the Mac's protected 24 GiB headroom for
the local model, the Fox Grid VM and the OS, plus room for the build. The
build itself runs at background priority (taskpolicy -b: efficiency cores,
throttled disk I/O), and its peak memory is logged for Infra Mac's budget.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import signal
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE_DIR = Path.home() / "Library" / "Application Support" / "wayfinder-aura-release"
STATE = STATE_DIR / "watcher-state.json"
LOCK = STATE_DIR / "watcher.lock"
HOLD = Path.home() / ".cache" / "foxgrid" / "aura-mac-ci.hold"
REPO = "wayfindercollective/wayfinder-aura"
AURA_REPO = Path(os.environ.get("AURA_REPO", Path.home() / "wayfinder-aura"))
MAX_TRIES = 3
WINDOW_DAYS = 7
MIN_FREE_GIB = 32  # 24 GiB protected headroom (docs/CI.md) + the build
BUILD_TIMEOUT_S = 2 * 3600  # whole build; notarytool's own wait is capped at 60 min
TAG_RE = re.compile(r"^v(\d+\.\d+\.\d+(?:-beta\.\d+)?)$")


def log(message: str) -> None:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{stamp} {message}", flush=True)


def gh_env() -> dict:
    env = dict(os.environ)
    if not env.get("GH_TOKEN"):
        token = subprocess.run(["gh", "auth", "token", "--user", "wayfindercollective"],
                               capture_output=True, text=True).stdout.strip()
        if token:
            env["GH_TOKEN"] = token
    return env


def releases_missing_a_dmg(releases: list, now: datetime) -> list[str]:
    """Tags (newest first) of recent releases whose Mac DMG is missing."""
    missing = []
    for release in releases:
        tag = release.get("tag_name") or ""
        match = TAG_RE.match(tag)
        if not match or release.get("draft"):
            continue
        published = release.get("published_at") or release.get("created_at") or ""
        try:
            when = datetime.fromisoformat(published.replace("Z", "+00:00"))
        except ValueError:
            continue
        if now - when > timedelta(days=WINDOW_DAYS):
            continue
        dmg = f"Wayfinder_Aura-{match[1]}-macOS-arm64.dmg"
        names = {a.get("name") for a in release.get("assets") or [] if a.get("state") == "uploaded"}
        if dmg not in names:
            missing.append((when, tag))
    return [tag for _, tag in sorted(missing, reverse=True)]


def free_gib() -> float:
    """Free memory as macOS reports it (the memory_pressure percentage)."""
    def sysctl(name: str) -> int:
        return int(subprocess.run(["/usr/sbin/sysctl", "-n", name],
                                  capture_output=True, text=True).stdout.strip() or 0)
    return sysctl("kern.memorystatus_level") / 100 * sysctl("hw.memsize") / 1024**3


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def run_build(tag: str, env: dict) -> int:
    """attach_mac_dmg.sh under the Mac CI hold, background QoS and a wall clock."""
    HOLD.parent.mkdir(parents=True, exist_ok=True)
    HOLD.touch()  # main() saw no hold: this run owns it and always lifts it
    try:
        # Background QoS keeps the build on the efficiency cores; /usr/bin/time -l
        # logs its peak memory ("maximum resident set size") for the budget.
        proc = subprocess.Popen(
            ["/usr/sbin/taskpolicy", "-b", "/usr/bin/time", "-l",
             "/bin/bash", str(HERE / "attach_mac_dmg.sh"), tag],
            env={**env, "AURA_REPO": str(AURA_REPO), "AURA_HOLD_HELD": "1"},
            start_new_session=True)
        try:
            return proc.wait(timeout=BUILD_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            log(f"Mac DMG for {tag} passed {BUILD_TIMEOUT_S // 60} minutes: stopping it.")
            for sig, grace in ((signal.SIGTERM, 60), (signal.SIGKILL, 10)):
                try:
                    os.killpg(proc.pid, sig)
                    return proc.wait(timeout=grace)
                except (ProcessLookupError, subprocess.TimeoutExpired):
                    continue
            return -1
    finally:
        try:
            HOLD.unlink()
        except FileNotFoundError:
            pass


def main() -> int:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOCK, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0  # a build from the previous hour is still running
        if HOLD.exists():
            log("Mac CI queue is held by someone else: trying next hour.")
            return 0
        if "--dry-run" not in sys.argv and free_gib() < MIN_FREE_GIB:
            log(f"Only {free_gib():.0f} GiB free (need {MIN_FREE_GIB}): trying next hour.")
            return 0
        env = gh_env()
        listing = subprocess.run(
            ["gh", "api", f"repos/{REPO}/releases?per_page=30"],
            capture_output=True, text=True, env=env)
        if listing.returncode != 0:
            log(f"Could not list releases: {listing.stderr.strip()[:200]}")
            return 0
        state = load_state()
        tries = state.setdefault("tries", {})
        pending = [t for t in releases_missing_a_dmg(json.loads(listing.stdout), datetime.now(timezone.utc))
                   if tries.get(t, 0) < MAX_TRIES]
        if not pending:
            return 0
        if "--dry-run" in sys.argv:
            log(f"Would build the Mac DMG for: {', '.join(pending)}")
            return 0
        tag = pending[0]
        tries[tag] = tries.get(tag, 0) + 1
        save_state(state)
        log(f"Building the Mac DMG for {tag} (try {tries[tag]} of {MAX_TRIES}).")
        returncode = run_build(tag, env)
        if returncode == 0:
            tries.pop(tag, None)
            state.setdefault("attached", []).append(tag)
            state["attached"] = state["attached"][-20:]
            log(f"Attached the Mac DMG to {tag}.")
        else:
            log(f"Mac DMG for {tag} failed (exit {returncode}).")
        save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
