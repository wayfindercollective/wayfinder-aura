#!/usr/bin/env python3
"""Attach the notarized Mac DMG to every new Aura release, from this Mac.

Installed as a LaunchAgent by install_mac_release_watcher.sh (every 10
minutes). The signing certificate and notary profile stay in the Mac's login
Keychain: no signing secret is uploaded anywhere. Each run:

1. lists the wayfinder-aura releases from the last 7 days (stable, and the
   betas the Beta workflow makes) that carry no Mac DMG for their version, and
   attaches any DMG it already built for one (step 3) at once: no build, so
   no admission and no hold;
2. builds, notarizes and attaches the newest such release with
   attach_mac_dmg.sh (one build per run; the next run takes the next one);
3. otherwise, for a tag whose Release workflow is still running, builds the
   DMG ahead (AURA_DMG_ONLY) and keeps it in DMG_CACHE, so the release gets
   its DMG minutes after it appears instead of a build later;
4. gives up on a tag after 3 failed builds (the state file records them).

The Mac is shared with a local model (memory guard up to 160 GiB) and the Fox
Grid VM, so a build is admitted the way Fox Grid admits Mac CI jobs
(docs/CI.md): available memory (anonymous + wired + compressed pages count as
used) minus the model's unused allowance, minus the VM's unused commitment,
minus the 24 GiB protected headroom, must still cover BUILD_BYTES. The model's
allowance is its 160 GiB guard ceiling unless Fox Grid publishes a grant for
the model that is loaded right now (GRANT, written by Fox Grid; its smaller
measured reserve then applies). Memory pressure must be normal, no Mac CI job
may be running, and nobody else may hold the Mac CI queue.

Bounded by design: one instance (flock), one build per run, the build in its
own process group under a 2-hour wall clock and re-checked every 15 s (it is
stopped if memory pressure rises, a Mac CI job starts, the host loses its
protected headroom with the model's allowance re-read from a grant that must
still be valid, or the build passes BUILD_CAP_BYTES; admission itself is
checked again once the hold is taken), every process of that group stopped and gone
before the CI hold is released (a group that will not die keeps the hold),
the hold taken atomically and released only by its owner, and a hold left by
a killed watcher reclaimed once its owner and build are both gone.
Termination signals are held off while the hold is taken and the build is
started and recorded, so a stop can never land between the two and leave an
unrecorded build running. The build runs at background priority
(taskpolicy -b: efficiency cores, throttled I/O); its measured peak was
0.42 GB (2026-10-03).
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE_DIR = Path.home() / "Library" / "Application Support" / "wayfinder-aura-release"
STATE = STATE_DIR / "watcher-state.json"
LOCK = STATE_DIR / "watcher.lock"
DMG_CACHE = STATE_DIR / "dmg"   # DMGs built ahead of their release (step 3)
HOLD = Path.home() / ".cache" / "foxgrid" / "aura-mac-ci.hold"
GRANT = Path.home() / ".cache" / "foxgrid" / "aura-release-grant.json"
REPO = "wayfindercollective/wayfinder-aura"
AURA_REPO = Path(os.environ.get("AURA_REPO", Path.home() / "wayfinder-aura"))
MAX_TRIES = 3
WINDOW_DAYS = 7
GIB = 1024 ** 3
PROTECTED_HEADROOM = 24 * GIB   # docs/CI.md
MODEL_CEILING = 160 * GIB       # the local model's memory-guard ceiling
VM_COMMITMENT = 66 * GIB        # the Fox Grid VM: 64 GiB guest + host overhead
BUILD_BYTES = 2 * GIB           # admission size for one build (measured peak 0.42 GB)
BUILD_CAP_BYTES = 4 * GIB       # the build is stopped above this
BUILD_TIMEOUT_S = 2 * 3600      # notarytool's own wait is capped at 60 min
CHECK_EVERY_S = 15
TAG_RE = re.compile(r"^v(\d+\.\d+\.\d+(?:-beta\.\d+)?)$")
HOLD_TAG = "aura-mac-release"


class Stop(Exception):
    """A termination signal: unwind so the build and the hold are cleaned up."""


# getattr: Windows has no SIGHUP, and the tests import this module there.
STOP_SIGNALS = tuple(getattr(signal, name) for name in ("SIGTERM", "SIGINT", "SIGHUP")
                     if hasattr(signal, name))
_stopping = False   # Stop was raised: later signals are ignored while cleanup runs
_held = 0           # inside signals_held(): a signal is noted, not raised
_deferred: int | None = None


def _raise_stop(signum, _frame):
    """Raise Stop once, or note it for the end of a signals_held() block."""
    global _stopping, _deferred
    if _stopping:
        return
    if _held:
        _deferred = signum
        return
    _stopping = True
    raise Stop(signal.Signals(signum).name)


@contextmanager
def signals_held():
    """Defer Stop until the block has finished: a signal that arrives inside
    is raised right after it. A flag, not a signal mask: a mask would be
    inherited by the build started inside the block."""
    global _held, _deferred
    _held += 1
    try:
        yield
    finally:
        _held -= 1
    if not _held and _deferred is not None:
        signum, _deferred = _deferred, None
        _raise_stop(signum, None)


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


# --- what to build --------------------------------------------------------------------

def dmg_name(tag: str) -> str:
    return f"Wayfinder_Aura-{tag[1:]}-macOS-arm64.dmg"


def built_ahead(tag: str) -> Path:
    return DMG_CACHE / dmg_name(tag)


def tags_being_released(runs: list, releases: list) -> list[str]:
    """Tags (newest first) whose Release workflow is queued or running and
    whose release does not exist yet or has no Mac DMG: their DMG can be
    built now, while the Linux and Windows builds run."""
    with_dmg = {r.get("tag_name") for r in releases
                if dmg_name(r.get("tag_name") or "v") in
                {a.get("name") for a in r.get("assets") or [] if a.get("state") == "uploaded"}}
    tags = []
    for run in sorted(runs, key=lambda r: r.get("created_at") or "", reverse=True):
        tag = run.get("head_branch") or ""
        if (run.get("status") in ("queued", "in_progress", "waiting", "pending")
                and TAG_RE.match(tag) and tag not in with_dmg and tag not in tags):
            tags.append(tag)
    return tags


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
        names = {a.get("name") for a in release.get("assets") or [] if a.get("state") == "uploaded"}
        if dmg_name(tag) not in names:
            missing.append((when, tag))
    return [tag for _, tag in sorted(missing, reverse=True)]


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, indent=2), encoding="utf-8")


# --- host readings (the same counters Fox Grid's Mac admission uses) -------------------

# struct rusage_info_v4 (<libproc.h>) is a 16-byte UUID followed by 36 uint64
# counters (296 bytes). Read into a larger zeroed buffer so a newer kernel can
# never write past it; ri_phys_footprint is the 8th counter.
_RUSAGE_INFO_V4 = 4
_RUSAGE_WORDS = 64
_PHYS_FOOTPRINT_WORD = 2 + 7


def phys_footprint(pid: int) -> int | None:
    buffer = (ctypes.c_uint64 * _RUSAGE_WORDS)()
    libproc = ctypes.CDLL("/usr/lib/libproc.dylib")
    libproc.proc_pid_rusage.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
    if libproc.proc_pid_rusage(pid, _RUSAGE_INFO_V4, ctypes.byref(buffer)) != 0:
        return None
    return int(buffer[_PHYS_FOOTPRINT_WORD])


def pids_matching(pattern: str) -> list[int]:
    out = subprocess.run(["/usr/bin/pgrep", "-f", pattern], capture_output=True, text=True).stdout
    return [int(pid) for pid in out.split()]


def host_reading() -> dict:
    raw = subprocess.check_output(["/usr/bin/vm_stat"], text=True, timeout=10)
    page = int(re.search(r"page size of (\d+) bytes", raw)[1])
    pages = {k: int(v) for k, v in re.findall(r"^(.+?):\s+(\d+)\.", raw, re.M)}
    total = int(subprocess.check_output(["/usr/sbin/sysctl", "-n", "hw.memsize"], timeout=10))
    used = (pages["Anonymous pages"] + pages["Pages wired down"]
            + pages["Pages occupied by compressor"]) * page
    pressure = int(subprocess.check_output(
        ["/usr/sbin/sysctl", "-n", "kern.memorystatus_vm_pressure_level"], timeout=10))
    model_pids = pids_matching("omlx")
    vm_pids = pids_matching("com.apple.Virtualization.VirtualMachine")
    return {
        "availableBytes": total - used,
        "pressure": pressure,
        "modelPids": model_pids,
        "modelFootprintBytes": sum(phys_footprint(p) or 0 for p in model_pids),
        "vmFootprintBytes": sum(phys_footprint(p) or 0 for p in vm_pids),
        "ciJobRunning": bool(pids_matching("Runner.Worker")),
    }


def _positive_int(value) -> int | None:
    """A JSON integer above zero; never a bool, float, string or other coercion."""
    return value if type(value) is int and value > 0 else None


def model_reserve(reading: dict, grant_path: Path = GRANT) -> int:
    """The model's full allowance: the 160 GiB guard, or Fox Grid's measured
    reserve when its grant (owner-only, under a day old, well-formed) names
    the model process loaded right now. Anything else gets the guard."""
    try:
        info = grant_path.stat()
        if info.st_uid != os.getuid() or info.st_mode & 0o022:
            return MODEL_CEILING
        if not 0 <= time.time() - info.st_mtime <= 86400:
            return MODEL_CEILING
        grant = json.loads(grant_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return MODEL_CEILING
    if not isinstance(grant, dict):
        return MODEL_CEILING
    pid = _positive_int(grant.get("modelPid"))
    reserve = _positive_int(grant.get("modelReserveBytes"))
    if pid is None or reserve is None or reserve > MODEL_CEILING:
        return MODEL_CEILING
    return reserve if pid in reading["modelPids"] else MODEL_CEILING


def spare_bytes(reading: dict, reserve: int, job_bytes: int = BUILD_BYTES) -> int:
    """Fox Grid's admission sum; negative means the build does not fit."""
    model_growth = max(0, reserve - reading["modelFootprintBytes"])
    vm_growth = max(0, VM_COMMITMENT - reading["vmFootprintBytes"])
    return (reading["availableBytes"] - model_growth - vm_growth
            - PROTECTED_HEADROOM - job_bytes)


def running_refusal(reading: dict) -> str | None:
    """Re-checked every CHECK_EVERY_S while a build runs (the build's own
    memory is already in the reading, so no allowance is added for it)."""
    if reading["pressure"] != 1:
        return f"memory pressure rose to level {reading['pressure']}"
    if reading["ciJobRunning"]:
        return "a Mac CI job started"
    if spare_bytes(reading, model_reserve(reading), job_bytes=0) < 0:
        return "the host lost its protected headroom"
    return None


def admission_refusal(reading: dict, reserve: int) -> str | None:
    if reading["pressure"] != 1:
        return f"memory pressure level {reading['pressure']}"
    if reading["ciJobRunning"]:
        return "a Mac CI job is running"
    spare = spare_bytes(reading, reserve)
    if spare < 0:
        return f"{-spare / GIB:.1f} GiB short of protected headroom"
    return None


# --- the Mac CI hold ------------------------------------------------------------------

def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def group_members(pgid: int) -> list[int]:
    """Live (non-zombie) processes of a process group. macOS refuses signals
    (EPERM) to a group whose only member is an unreaped zombie, so the kernel's
    answer to killpg(pgid, 0) can't tell a finished build from a running one."""
    out = subprocess.run(["/bin/ps", "-A", "-o", "pid=,pgid=,stat="],
                         capture_output=True, text=True).stdout
    members = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[1] == str(pgid) and not parts[2].startswith("Z"):
            members.append(int(parts[0]))
    return members


def _group_alive(pgid: int) -> bool:
    return bool(group_members(pgid))


def acquire_hold(build_pgid: int = 0) -> bool:
    """Create the hold atomically, naming this watcher (and later its build)."""
    HOLD.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(HOLD, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w") as fh:
        fh.write(f"{HOLD_TAG} pid={os.getpid()} pgid={build_pgid}\n")
    return True


def _hold_owner(text: str) -> tuple[int, int] | None:
    match = re.match(rf"^{HOLD_TAG} pid=(\d+) pgid=(\d+)", text)
    return (int(match[1]), int(match[2])) if match else None


def note_build_in_hold(pgid: int) -> None:
    HOLD.write_text(f"{HOLD_TAG} pid={os.getpid()} pgid={pgid}\n", encoding="utf-8")


def release_hold() -> None:
    """Remove the hold only if it is still this watcher's."""
    try:
        owner = _hold_owner(HOLD.read_text(encoding="utf-8"))
        if owner and owner[0] == os.getpid():
            HOLD.unlink()
    except FileNotFoundError:
        pass


def reclaim_stale_hold() -> bool:
    """A hold this watcher left behind (it was killed) whose owner and build
    are both gone is removed. Anyone else's hold is never touched."""
    try:
        owner = _hold_owner(HOLD.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False
    if owner is None:
        return False
    pid, pgid = owner
    if _alive(pid) or (pgid and _group_alive(pgid)):
        return False
    HOLD.unlink(missing_ok=True)
    log(f"Removed a stale Mac CI hold left by watcher {pid}.")
    return True


# --- the build ------------------------------------------------------------------------

def group_footprint(pgid: int) -> int:
    return sum(phys_footprint(pid) or 0 for pid in group_members(pgid))


def stop_group(pgid: int, grace: float = 40, proc: subprocess.Popen | None = None) -> bool:
    """SIGTERM the whole group, wait until every live member is gone, then
    SIGKILL whatever is left. The leader is reaped as it exits. True when
    nothing of the group is left."""
    for sig, wait in ((signal.SIGTERM, grace), (signal.SIGKILL, 10)):
        members = group_members(pgid)
        if not members:
            break
        try:
            os.killpg(pgid, sig)
        except (ProcessLookupError, PermissionError):
            for pid in members:  # the group refused (zombie leader): one by one
                try:
                    os.kill(pid, sig)
                except (ProcessLookupError, PermissionError):
                    pass
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if proc is not None:
                proc.poll()
            if not group_members(pgid):
                break
            time.sleep(0.5)
    if proc is not None:
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
    return not group_members(pgid)


def build_command(tag: str) -> list[str]:
    return ["/usr/sbin/taskpolicy", "-b", "/usr/bin/time", "-l",
            "/bin/bash", str(HERE / "attach_mac_dmg.sh"), tag]


def run_build(tag: str, env: dict, build: dict, ahead: bool = False) -> int:
    """attach_mac_dmg.sh under the hold this run already took: background QoS,
    a wall clock, and memory re-checked every CHECK_EVERY_S seconds (with the
    model's allowance re-read each time: a grant that expired or no longer
    names the loaded model falls back to the 160 GiB guard mid-build).

    ``build["pgid"]`` records the build's process group the moment it exists;
    the caller keeps the hold until that group is gone."""
    proc = None
    try:
        with signals_held():  # a stop lands after the build is recorded, never between
            extra = {"AURA_DMG_ONLY": str(built_ahead(tag))} if ahead else {}
            proc = subprocess.Popen(
                build_command(tag),
                env={**env, "AURA_REPO": str(AURA_REPO), "AURA_HOLD_HELD": "1",
                     "AURA_BUILD_DEADLINE_S": str(BUILD_TIMEOUT_S + 300), **extra},
                start_new_session=True)
            build["pgid"] = proc.pid
            note_build_in_hold(proc.pid)
        pgid = proc.pid
        deadline = time.monotonic() + BUILD_TIMEOUT_S
        while True:
            try:
                return proc.wait(timeout=CHECK_EVERY_S)
            except subprocess.TimeoutExpired:
                pass
            reason = None
            if time.monotonic() > deadline:
                reason = f"passed {BUILD_TIMEOUT_S // 60} minutes"
            else:
                used = group_footprint(pgid)
                if used > BUILD_CAP_BYTES:
                    reason = f"used {used / GIB:.1f} GiB (cap {BUILD_CAP_BYTES / GIB:.0f})"
                else:
                    reason = running_refusal(host_reading())
            if reason:
                log(f"Stopping the Mac DMG build for {tag}: it {reason}.")
                return -1
    finally:
        if proc is not None:
            with signals_held():
                stop_group(proc.pid, proc=proc)


def finish_hold(build: dict) -> None:
    """Release the CI hold once the build's group is gone. A group that
    survives SIGKILL keeps the hold (it names the group, so the next run
    reclaims it only after the group is gone)."""
    pgid = build.get("pgid")
    if pgid and group_members(pgid) and not stop_group(pgid):
        log(f"The Mac DMG build (process group {pgid}) would not stop: "
            "the Mac CI hold stays until it is gone.")
        return
    release_hold()


def attach_built_ahead(tag: str, env: dict) -> bool:
    """Upload the DMG built ahead for ``tag`` once its release exists: checked
    again (Gatekeeper, stapled ticket) right before the upload, then removed."""
    dmg = built_ahead(tag)
    if not dmg.is_file():
        return False
    checks = (["/usr/sbin/spctl", "--assess", "--type", "open",
               "--context", "context:primary-signature", str(dmg)],
              ["/usr/bin/xcrun", "stapler", "validate", str(dmg)])
    if any(subprocess.run(c, capture_output=True).returncode != 0 for c in checks):
        log(f"The DMG built ahead for {tag} failed its checks: discarding it.")
        dmg.unlink(missing_ok=True)
        return False
    upload = subprocess.run(["gh", "release", "upload", tag, str(dmg), "--repo", REPO,
                             "--clobber"], capture_output=True, text=True, env=env)
    if upload.returncode != 0:
        return False  # no release yet (or GitHub hiccup): the next run tries again
    dmg.unlink(missing_ok=True)
    return True


def prune_built_ahead(now: float | None = None) -> None:
    """DMGs built ahead for tags that never got a release go after WINDOW_DAYS."""
    now = time.time() if now is None else now
    for dmg in DMG_CACHE.glob("*.dmg*"):
        try:
            if now - dmg.stat().st_mtime > WINDOW_DAYS * 86400:
                dmg.unlink()
        except OSError:
            pass


def note_attached(state: dict, tag: str) -> None:
    state.setdefault("tries", {}).pop(tag, None)
    state.setdefault("attached", []).append(tag)
    state["attached"] = state["attached"][-20:]


def main() -> int:
    for sig in STOP_SIGNALS:
        signal.signal(sig, _raise_stop)
    try:
        return watch()
    except Stop as stop:
        log(f"Stopped by {stop}.")
        return 0


def watch() -> int:
    import fcntl  # macOS only; imported here so the module loads on Windows test runs

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOCK, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0  # a build from the previous hour is still running
        reclaim_stale_hold()
        env = gh_env()
        listing = subprocess.run(
            ["gh", "api", f"repos/{REPO}/releases?per_page=30"],
            capture_output=True, text=True, env=env)
        if listing.returncode != 0:
            log(f"Could not list releases: {listing.stderr.strip()[:200]}")
            return 0
        releases = json.loads(listing.stdout)
        state = load_state()
        tries = state.setdefault("tries", {})
        missing = releases_missing_a_dmg(releases, datetime.now(timezone.utc))
        prune_built_ahead()
        attached_now = set()
        for tag in list(missing):  # step 1: no build, so no admission or hold needed
            if attach_built_ahead(tag, env):
                missing.remove(tag)
                attached_now.add(tag)
                note_attached(state, tag)
                save_state(state)
                log(f"Attached the Mac DMG built ahead to {tag}.")
        if HOLD.exists():
            if missing:
                log("The Mac CI queue is held by someone else: trying next run.")
            return 0
        runs = subprocess.run(
            ["gh", "api", f"repos/{REPO}/actions/workflows/release.yml/runs?per_page=20",
             "--jq", ".workflow_runs"], capture_output=True, text=True, env=env)
        try:
            # A tag attached a moment ago still looks unfinished in the listing
            # taken before step 1 (its Release run may still be publishing):
            # never build it again.
            ahead = [t for t in tags_being_released(json.loads(runs.stdout or "[]"), releases)
                     if t not in missing and t not in attached_now
                     and t not in state.get("attached", [])
                     and not built_ahead(t).is_file()]
        except ValueError:
            ahead = []
        # A DMG already built ahead is never rebuilt: its upload is retried next run.
        pending = [t for t in missing + ahead
                   if tries.get(t, 0) < MAX_TRIES and not built_ahead(t).is_file()]
        if not pending:
            return 0
        reading = host_reading()
        reserve = model_reserve(reading)
        refusal = admission_refusal(reading, reserve)
        if "--dry-run" in sys.argv:
            log(f"Would build the Mac DMG for: {', '.join(pending)} "
                f"({'admitted' if refusal is None else 'not now: ' + refusal}).")
            return 0
        if refusal:
            log(f"Not building {pending[0]} yet: {refusal}. Trying next run.")
            return 0
        held = False
        build: dict = {}
        try:
            with signals_held():  # taken and owned with no gap a stop could land in
                held = acquire_hold()
            if not held:
                log("The Mac CI queue was just held by someone else: trying next run.")
                return 0
            # Admitted again under the hold: a CI job (or memory use) that
            # started since the first check refuses the build here.
            reading = host_reading()
            refusal = admission_refusal(reading, model_reserve(reading))
            if refusal:
                log(f"Not building {pending[0]} yet: {refusal}. Trying next run.")
                return 0
            tag = pending[0]
            early = tag not in missing
            tries[tag] = tries.get(tag, 0) + 1
            save_state(state)
            log(f"Building the Mac DMG for {tag} (try {tries[tag]} of {MAX_TRIES})"
                + (", ahead of its release." if early else "."))
            returncode = run_build(tag, env, build, ahead=early)
            if returncode == 0 and not early:
                note_attached(state, tag)
                log(f"Attached the Mac DMG to {tag}.")
            elif returncode == 0:
                tries.pop(tag, None)
                if attach_built_ahead(tag, env):  # the release may have appeared meanwhile
                    note_attached(state, tag)
                    log(f"Attached the Mac DMG to {tag}.")
                else:
                    log(f"Built the Mac DMG for {tag}; attaching it when the release appears.")
            else:
                log(f"Mac DMG for {tag} failed (exit {returncode}).")
            save_state(state)
        except Stop as stop:
            log(f"Stopped by {stop}: ending the build before releasing the hold.")
        finally:
            if held:
                with signals_held():
                    finish_hold(build)
    return 0


if __name__ == "__main__":
    sys.exit(main())
