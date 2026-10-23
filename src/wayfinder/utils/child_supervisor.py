"""Keep long-lived native children bound to the macOS app lifetime.

Linux uses ``PR_SET_PDEATHSIG`` for its overlay. macOS has no equivalent, so
each helper stays this app's own child (spawn_supervised), started into the
group of a small watcher process (run_watcher) that ends it if the app dies
first, a crash included.

Windows needs no watcher: ``bind_to_app_lifetime`` puts the child in a job
object that the kernel closes when the app exits for any reason, crash
included, killing the child with it.
"""

from __future__ import annotations

import os
import re
import select
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Mapping, Sequence


# Set on the watcher (spawn_supervised): the pid it guards comes on its stdin.
WATCHER_ENV = "WAYFINDER_CHILD_SUPERVISOR"
# The watcher's one byte on its stdout once it ignores SIGTERM; the helper
# starts only after it (Sol mac r17). A fresh frozen app on a busy 8 GB Mac
# can take seconds to get there.
WATCHER_READY = b"r"
WATCHER_READY_TIMEOUT_S = 20.0


def parent_process_alive(pid: int) -> bool:
    """Return whether *pid* still exists (permission denied still means alive)."""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _watcher_command() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "--child-supervisor"]
    return [sys.executable, str(Path(__file__).resolve())]


_POPEN = subprocess.Popen


def _bound_waits(proc) -> None:
    """A macOS helper's untimed wait() polls, so no reap holds Popen's reap
    lock for longer than one non-blocking waitpid, and signal_supervised can
    always take that lock (Sol mac r8). Set on the instance, so Popen's own
    communicate() and __exit__ use it too."""
    timed_wait = proc.wait

    def wait(timeout=None):
        if timeout is not None:
            return timed_wait(timeout)
        while True:
            try:
                return timed_wait(0.05)
            except subprocess.TimeoutExpired:
                pass

    proc.wait = wait


def spawn_supervised(command: Sequence[str], *, env: Mapping[str, str] | None = None, popen=None,
                     platform_name: str | None = None, before_helper=None,
                     ready_within: float | None = None,
                     **popen_kwargs) -> subprocess.Popen:
    """Popen a native helper that must not outlive the app.

    macOS: its watcher starts first, the app's own child leading a new
    process group in the app's session, and the helper, the app's own child
    too, starts inside that group once the watcher reports it ignores
    SIGTERM, so the native binary never runs before its watcher watches
    (Sol mac r15, r17). The watcher learns the helper's pid on
    its lifeline pipe. A group keeps its id while it has a member, an exited
    one too until it is reaped (XNU's reap_child_locked leaves the group, as
    Linux's release_task does), so while either of the two is unreaped the
    group id is no one else's, however the other ended and whoever reaped it
    (Sol mac r14). stop_supervised() ends the helper and whatever it forked
    into that group, whatever the helper does with inherited descriptors
    (Sol mac r7); the watcher ends the group if the app dies first. No
    watcher, no helper: the spawn fails instead (Sol mac r15). Killing a
    supervisor in front of the helper used to orphan whisper-server, which
    then kept the model loaded and its port open.

    *before_helper*, if given, runs on macOS after the watcher's readiness
    wait and right before the helper starts; it raises to refuse the helper,
    and the watcher goes with it (Sol mac r18: that wait can take seconds,
    and a dictation retired or a deadline passed meanwhile must not get its
    helper). It runs too when the watcher never got ready, so the caller's
    own refusal wins over "no crash watcher". *ready_within* caps that wait
    at the caller's time left.

    Elsewhere a plain Popen: Linux binds by PR_SET_PDEATHSIG, Windows by
    bind_to_app_lifetime()."""
    if (platform_name or sys.platform) != "darwin":
        return (popen or subprocess.Popen)(command, env=env, **popen_kwargs)
    spawn = popen or subprocess.Popen
    if spawn is not _POPEN:
        # A test's stand-in: its pid is no child of ours, and a watcher on it
        # could signal a stranger's group.
        return spawn(command, env=env, process_group=0, **popen_kwargs)
    # The watcher's stdin is the app's lifeline: the app holds the only
    # write end, so the kernel closes it however the app exits.
    watcher = _POPEN(_watcher_command(), process_group=0, env={**os.environ, WATCHER_ENV: "1"},
                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                     stderr=subprocess.DEVNULL, bufsize=0)
    # Not before the watcher ignores SIGTERM: a stop's group SIGTERM landing
    # while it still started up would end it, and a helper that outlasts
    # SIGTERM would then run unwatched (Sol mac r17).
    wait = WATCHER_READY_TIMEOUT_S
    if ready_within is not None:
        wait = max(0.0, min(wait, ready_within))
    try:
        ready = _watcher_ready(watcher, wait)
        if before_helper is not None:
            before_helper()
        if not ready:
            raise OSError(f"no crash watcher for {Path(str(command[0])).name}")
        proc = _POPEN(command, env=env, process_group=watcher.pid, **popen_kwargs)
        proc.spawned_at = time.monotonic()
    except BaseException:
        _drop_watcher(watcher)
        raise
    _bound_waits(proc)
    proc._aura_group = True
    proc._aura_gid = watcher.pid
    proc._aura_watcher = watcher
    proc._aura_guarded = True
    try:
        watcher.stdin.write(b"%d\n" % proc.pid)
    except (OSError, ValueError) as exc:
        # The watcher is gone already: nothing would end the helper if the
        # app died, so it does not run.
        signal_supervised(proc, kill=True, platform_name="darwin")
        wait_supervised(proc, 5.0, platform_name="darwin")
        _drop_watcher(watcher)
        raise OSError(f"no crash watcher for {Path(str(command[0])).name}") from exc
    return proc


def _watcher_ready(watcher: subprocess.Popen, timeout: float) -> bool:
    """Whether *watcher* reported WATCHER_READY within *timeout*. End of file
    (it refused or died) or silence is False. Its stdout is closed either
    way: nothing else comes on it."""
    try:
        fd = watcher.stdout.fileno()
        # One select: it retries an interrupted wait itself with the time
        # left (PEP 475), and the first thing on the pipe settles it.
        readable, _, _ = select.select([fd], [], [], timeout)
        return bool(readable) and os.read(fd, len(WATCHER_READY)) == WATCHER_READY
    finally:
        try:
            watcher.stdout.close()
        except (OSError, ValueError, AttributeError):
            pass


def _drop_watcher(watcher: subprocess.Popen) -> None:
    """A watcher whose helper never ran: its group holds only itself."""
    try:
        watcher.kill()
    except OSError:
        pass
    try:
        watcher.stdin.close()
    except (OSError, ValueError, AttributeError):
        pass
    try:
        watcher.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def _owns_group(proc: subprocess.Popen, platform_name: str | None) -> bool:
    """macOS: *proc* runs in a group of its own with its watcher
    (spawn_supervised). Whether that group id is still theirs is only known
    under a member's reap lock (see _signal_group)."""
    return ((platform_name or sys.platform) == "darwin"
            and getattr(proc, "_aura_group", False) is True)


# Long enough for any reap of a helper (one non-blocking waitpid).
_REAP_LOCK_WAIT = 2.0


def _signal_group(proc: subprocess.Popen, sig: int) -> None:
    """killpg the helper's group (its watcher's pid, see spawn_supervised)
    while a member of it is provably unreaped: the watcher or the helper.
    Popen reaps only under its _waitpid_lock and sets returncode before
    letting go, so holding it, an unset returncode means that member, and so
    the group id, is still there (Sol mac r8: a reap on another thread could
    land between the check and the signal). Either member's proof holds
    after the other was reaped, by poll() or another thread (Sol mac r14)."""
    gid = getattr(proc, "_aura_gid", None)
    watcher = getattr(proc, "_aura_watcher", None)
    if not isinstance(gid, int):
        return
    members = [proc]
    if getattr(proc, "_aura_guarded", False) and isinstance(watcher, _POPEN):
        members.insert(0, watcher)
    busy = False
    for member in members:
        ran = _while_unreaped(member, lambda: os.killpg(gid, sig))
        if ran:
            return
        busy = busy or ran is None
    if busy:
        print(f"[Helpers] not signalling group {gid}: its reap lock is busy", flush=True)


def _while_unreaped(member: subprocess.Popen, act):
    """Run *act* under *member*'s reap lock if it is unreaped: True if it ran,
    False if *member* was reaped, None if its lock stayed busy (only a
    blocking wait() that _bound_waits did not bound holds it this long, and
    signalling then could race that reap)."""
    lock = getattr(member, "_waitpid_lock", None)
    if lock is None or not lock.acquire(timeout=_REAP_LOCK_WAIT):
        return None
    try:
        if member.returncode is not None:
            return False
        try:
            act()
        except OSError:
            pass   # every member has exited; they await their reap
        return True
    finally:
        lock.release()


def _release_watcher(proc: subprocess.Popen) -> None:
    """*proc* is reaped: its watcher lets go without signalling anything."""
    watcher = getattr(proc, "_aura_watcher", None)
    if not isinstance(watcher, subprocess.Popen):
        return
    proc._aura_watcher = None
    try:
        watcher.stdin.write(b"r")
    except (OSError, ValueError, AttributeError):
        pass  # it already left: it saw the helper exit
    try:
        watcher.stdin.close()
    except (OSError, ValueError, AttributeError):
        pass
    watcher.poll()   # reaped now if it already left; otherwise when collected


def release_supervised(proc) -> None:
    """For a caller that reaped *proc* itself: what it left in its group goes,
    then its watcher lets go."""
    if getattr(proc, "returncode", None) is not None:
        if getattr(proc, "_aura_guarded", False) and _owns_group(proc, "darwin"):
            _signal_group(proc, signal.SIGKILL)
        _release_watcher(proc)


def _exited_unreaped(proc: subprocess.Popen, timeout: float) -> bool:
    """Wait up to *timeout* for *proc* to exit without reaping it: until it is
    reaped, its pid, and so its group id, stay the helper's. kqueue on macOS,
    waitid(WNOWAIT) where that exists (the Linux tests). True once it exited
    (or another thread reaped it), False on a timeout."""
    if proc.returncode is not None:
        return True
    if hasattr(select, "kqueue"):
        kq = select.kqueue()
        try:
            try:
                kq.control([select.kevent(proc.pid, select.KQ_FILTER_PROC,
                                          select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                                          select.KQ_NOTE_EXIT)], 0, 0)
            except OSError:
                return True   # already exited: a zombie (or reaped) is not watchable
            return bool(kq.control(None, 1, timeout))
        finally:
            kq.close()
    deadline = time.monotonic() + timeout
    while True:
        try:
            if os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None:
                return True
        except ChildProcessError:
            return True   # reaped meanwhile
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)


def wait_supervised(proc: subprocess.Popen, timeout: float, *,
                    platform_name: str | None = None) -> bool:
    """Wait up to *timeout* for *proc* to exit. Once it is reaped, its watcher
    is released, so it signals nothing that later takes the pid.

    macOS: a helper that exited may leave what it forked behind (a worker
    that ignored SIGTERM, Sol mac r13). Its exit is awaited without the reap,
    and the group is SIGKILLed while a member still holds its id
    (_signal_group), before the reap: also when the helper exited on its own
    and poll() or another thread already reaped it (Sol mac r14)."""
    if _owns_group(proc, platform_name) and (hasattr(select, "kqueue") or hasattr(os, "waitid")):
        if not _exited_unreaped(proc, timeout):
            return False
        _signal_group(proc, signal.SIGKILL)   # what it left in its group
        timeout = _REAP_LOCK_WAIT             # the reap itself: it has exited
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        return False
    _release_watcher(proc)
    return True


def signal_supervised(proc: subprocess.Popen, *, kill: bool = False,
                      platform_name: str | None = None) -> None:
    """Terminate (or kill) a supervised child: its whole group on macOS."""
    sig = signal.SIGKILL if kill else signal.SIGTERM
    if _owns_group(proc, platform_name):
        _signal_group(proc, sig)
        return
    if proc.returncode is not None:
        return   # reaped: its pid may be someone else's
    try:
        proc.kill() if kill else proc.terminate()
    except OSError:
        pass


def stop_supervised(proc: subprocess.Popen, term_wait: float = 5.0, kill_wait: float = 2.0, *,
                    platform_name: str | None = None) -> None:
    """SIGTERM, then SIGKILL after *term_wait*; on macOS to the whole group."""
    signal_supervised(proc, platform_name=platform_name)
    if wait_supervised(proc, term_wait, platform_name=platform_name):
        return
    signal_supervised(proc, kill=True, platform_name=platform_name)
    wait_supervised(proc, kill_wait, platform_name=platform_name)


# Native helpers an earlier Aura could leave behind: the app and its watcher
# both SIGKILLed, an older release's supervisor SIGKILLed, or a crash with
# llama-server, which is not supervised.
_ORPHAN_HELPERS = re.compile(
    r"^/.+/Wayfinder Aura[^/]*\.app/Contents/Frameworks/bin/"
    r"(?:whisper-coreml/)?(?:whisper-server|whisper-cli|llama-server|llama-simple|coreml-compile)(?: |$)"
)


def orphaned_helper_pids(ps_rows: Sequence[str], uid: int) -> list[int]:
    """PIDs in ``ps -axo uid=,pid=,ppid=,command=`` output that are this user's
    Aura helpers re-parented to launchd (no live app or supervisor owns them)."""
    found = []
    for row in ps_rows:
        parts = row.split(None, 3)
        if len(parts) != 4 or parts[0] != str(uid) or parts[2] != "1":
            continue
        if _ORPHAN_HELPERS.match(parts[3]):
            found.append(int(parts[1]))
    return found


def reap_orphaned_helpers(*, platform_name: str | None = None, grace: float = 3.0) -> list[int]:
    """macOS: stop helpers left by an earlier Aura. Returns the PIDs stopped."""
    if (platform_name or sys.platform) != "darwin":
        return []

    def scan() -> list[int]:
        try:
            rows = subprocess.check_output(
                ["ps", "-axo", "uid=,pid=,ppid=,command="], text=True, timeout=5,
            ).splitlines()
        except (OSError, subprocess.SubprocessError):
            return []
        return orphaned_helper_pids(rows, os.getuid())

    pids = scan()
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    deadline = time.monotonic() + grace
    while pids and time.monotonic() < deadline and any(parent_process_alive(p) for p in pids):
        time.sleep(0.1)
    # SIGKILL only what a fresh scan still shows as the same orphan.
    for pid in set(pids) & set(scan()) if pids else ():
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    return pids


def bind_to_app_lifetime(proc: subprocess.Popen, *, platform_name: str | None = None) -> bool:
    """Windows: make *proc* die with this app, even if the app crashes.

    See utils/windows_job.py. No-op (False) elsewhere; nothing Windows-specific
    is imported or built on other platforms.
    """
    if (platform_name or sys.platform) != "win32":
        return False
    try:
        from wayfinder.utils.windows_job import assign_to_kill_on_close_job
    except Exception:
        return False
    return assign_to_kill_on_close_job(proc)


def _terminate_child(proc: subprocess.Popen, timeout: float = 3.0) -> None:
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            pass
    except (OSError, ProcessLookupError):
        pass


# One-shot native helpers (whisper-cli, llama-simple and their probes) are not
# servers, so no server stop reaches them. They run through here: admitted
# only while the app is open and tracked until they exit, so a terminal
# shutdown (Quit, overlay restart, final cleanup) stops the running ones and
# refuses the rest. A fallback racing Quit otherwise outlived os._exit(), its
# timeout gone with the parent.
_tracked_lock = threading.Lock()
_tracked: dict = {}   # Popen -> its stop function
_tracked_closed = False
# Terminal shutdowns so far; a reopen never lowers it, so work that spans one
# can tell, however late it finishes (see shutdown_generation()).
_shutdowns = 0
_exit_hook_registered = False


def _darwin_spawn(command, **popen_kwargs):
    return spawn_supervised(command, platform_name="darwin", **popen_kwargs)


def _is_helper_group(proc) -> bool:
    return getattr(proc, "_aura_group", False) is True


def _kill_helper(proc: subprocess.Popen) -> None:
    if _is_helper_group(proc):
        signal_supervised(proc, kill=True, platform_name="darwin")
    else:
        proc.kill()


class HelpersClosed(RuntimeError):
    """A helper process was refused: the app is shutting down."""


class HelperStopped(HelpersClosed):
    """A terminal shutdown stopped this helper mid-run. Its output and exit
    status say nothing about the machine, even after a reopen, so no caller
    may cache or count them."""


def _supervise_by_default() -> bool:
    """macOS: every tracked helper (whisper-cli, llama-simple, their probes)
    is a native binary that must not outlive a Force Quit either (Sol mac r8)."""
    return sys.platform == "darwin"


def _stop_helper_group(proc: subprocess.Popen) -> None:
    stop_supervised(proc, 3.0, 1.0, platform_name="darwin")


# Run under _tracked_lock right before every tracked spawn; each raises to
# refuse it. The transcriber registers one that refuses a helper for a
# dictation the app retired, however long the caller waited for the lock
# after its own check (Sol mac r10).
_spawn_checks: list = []


def refuse_spawns_when(check) -> None:
    """Register *check*: called under the lock before every tracked spawn,
    it raises to refuse that spawn."""
    with _tracked_lock:
        if check not in _spawn_checks:
            _spawn_checks.append(check)


def _run_spawn_checks() -> None:
    for check in _spawn_checks:
        check()


class HelperNotStarted(subprocess.TimeoutExpired):
    """Raised instead of a plain TimeoutExpired when the deadline passed
    before the helper was ever spawned: nothing ran, so nothing a caller
    caches, disables or counts as a broken binary may be based on it (Sol
    mac r20/r21). A subclass of TimeoutExpired, so an existing
    ``except subprocess.TimeoutExpired`` still catches it unchanged."""


def _refuse_past(command, deadline) -> None:
    if deadline is not None and time.monotonic() >= deadline:
        raise HelperNotStarted(list(command), 0.0)


def spawn_tracked(command: Sequence[str], *, stop=None, popen=None,
                  deadline: float | None = None, **popen_kwargs) -> subprocess.Popen:
    """Popen (or *popen*, such as spawn_supervised) a helper that a terminal
    shutdown stops (with *stop*, default terminate then kill, its whole group
    on macOS); refused once one has begun. macOS supervises it by default.

    *deadline* (time.monotonic()) bounds the wait for the lock and for the
    watcher: past it, subprocess.TimeoutExpired and no helper (Sol mac r19)."""
    global _exit_hook_registered
    if popen is None and _supervise_by_default():
        popen = _darwin_spawn
    if deadline is None:
        _tracked_lock.acquire()
    elif not _tracked_lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
        raise HelperNotStarted(list(command), 0.0)
    try:
        if _tracked_closed:
            raise HelpersClosed("shutting down: no new helper process")
        _run_spawn_checks()
        if not _exit_hook_registered:
            # An interpreter exit with no server started (main.py, CLI verbs)
            # runs no server exit hook: this one stops the helpers itself.
            import atexit
            atexit.register(close_tracked)
            _exit_hook_registered = True
        if popen is _darwin_spawn or popen is spawn_supervised:
            # Again after the watcher's readiness wait, right before the
            # helper starts: that wait can take seconds, and a dictation
            # retired meanwhile must not get its helper (Sol mac r18).
            # Shutdown cannot begin meanwhile: close_tracked needs this lock.
            own_check = popen_kwargs.pop("before_helper", None)

            def recheck():
                _run_spawn_checks()
                if own_check is not None:
                    own_check()
                _refuse_past(command, deadline)
            popen_kwargs["before_helper"] = recheck
            if deadline is not None:
                left = max(0.0, deadline - time.monotonic())
                given = popen_kwargs.get("ready_within")
                popen_kwargs["ready_within"] = left if given is None else min(given, left)
        _refuse_past(command, deadline)
        proc = (popen or subprocess.Popen)(command, **popen_kwargs)
        # Supervised spawns record this before their watcher handshake.
        if not isinstance(getattr(proc, "spawned_at", None), (int, float)):
            proc.spawned_at = time.monotonic()
        if stop is None:
            stop = _stop_helper_group if _is_helper_group(proc) else _terminate_child
        _tracked[proc] = stop
    finally:
        _tracked_lock.release()
    bind_to_app_lifetime(proc)
    return proc


def track_existing(proc: subprocess.Popen, *, stop=None) -> bool:
    """Track a child started elsewhere (a server being retired) so a terminal
    shutdown stops it; False once one has begun, and the caller must stop it."""
    with _tracked_lock:
        if _tracked_closed:
            return False
        _tracked[proc] = stop or _terminate_child
    return True


def forget_tracked(proc: subprocess.Popen) -> None:
    with _tracked_lock:
        _tracked.pop(proc, None)


def cut_short(proc: subprocess.Popen) -> bool:
    """Whether a terminal shutdown stopped this tracked child: its exit says
    nothing about the machine, before or after a reopen."""
    return getattr(proc, "_aura_cut_short", False)


def shutdown_generation() -> int:
    """A token for work made of several helpers: a different value at its end
    means a terminal shutdown ran meanwhile, so the work is no result, even if
    the errors it met were swallowed on the way."""
    return _shutdowns


def run_tracked(command: Sequence[str], *, input=None, timeout: float | None = None,
                check: bool = False, **kwargs) -> subprocess.CompletedProcess:
    """subprocess.run() through spawn_tracked(): the same arguments and errors.
    *timeout* counts from this call, the wait for the lock and the watcher
    included, so a deadline's time left really bounds the helper (Sol mac r19).

    The returned CompletedProcess carries ``spawned_at`` (time.monotonic(),
    the moment the helper actually started, past admission) when it was
    spawned at all; a subprocess.TimeoutExpired raised after that point
    carries the same attribute, so a caller whose verdict depends on how
    long the helper ran can measure from there instead of from this call's
    own start, which would count admission as the helper's own time (Sol mac
    r21, Finding 1). HelperNotStarted (admission itself timed out, nothing
    ever spawned) carries no such attribute: there is nothing to measure."""
    deadline = None if timeout is None else time.monotonic() + timeout
    if kwargs.pop("capture_output", False):
        kwargs["stdout"] = kwargs["stderr"] = subprocess.PIPE
    if input is not None:
        kwargs["stdin"] = subprocess.PIPE
    try:
        proc = spawn_tracked(command, deadline=deadline, **kwargs)
    except HelperNotStarted:
        raise HelperNotStarted(command, timeout) from None
    except subprocess.TimeoutExpired:
        raise subprocess.TimeoutExpired(command, timeout) from None
    try:
        with proc:
            try:
                left = None if deadline is None else max(0.0, deadline - time.monotonic())
                stdout, stderr = proc.communicate(input, timeout=left)
                finished_at = time.monotonic()
            except subprocess.TimeoutExpired as exc:
                _kill_helper(proc)
                proc.wait()
                if cut_short(proc):
                    raise HelperStopped("stopped by a terminal shutdown") from None
                exc.spawned_at = getattr(proc, "spawned_at", None)
                exc.execution_budget = left
                raise
            except BaseException as exc:
                _kill_helper(proc)
                if isinstance(exc, Exception) and cut_short(proc):
                    # Output cut off mid-character, a closed pipe: the stop's
                    # doing, not the helper's answer.
                    raise HelperStopped("stopped by a terminal shutdown") from exc
                raise
    finally:
        forget_tracked(proc)
        release_supervised(proc)   # reaped by the with block
    if cut_short(proc):
        raise HelperStopped("stopped by a terminal shutdown")
    if check and proc.returncode:
        raise subprocess.CalledProcessError(proc.returncode, proc.args, stdout, stderr)
    result = subprocess.CompletedProcess(proc.args, proc.returncode, stdout, stderr)
    result.spawned_at = getattr(proc, "spawned_at", None)
    result.execution_seconds = max(0.0, finished_at - result.spawned_at)
    result.execution_budget = left
    return result


def helper_elapsed(result, fallback: float) -> float:
    """Helper runtime excluding admission, or a legacy runner's measurement."""
    elapsed = getattr(result, "execution_seconds", None)
    return elapsed if isinstance(elapsed, (int, float)) else fallback


def helper_budget_shortened(error, required: float, *, deadline=None) -> bool:
    """Whether admission materially shortened a timed-out helper's window.

    Process creation and the watcher handshake always consume some time.
    Allow five percent of the nominal window, capped at one second, so an
    ordinary near-full timeout still drives normal fallback and backoff.
    """
    budget = getattr(error, "execution_budget", None)
    if not isinstance(budget, (int, float)) and deadline is not None:
        spawned_at = getattr(error, "spawned_at", None)
        if isinstance(spawned_at, (int, float)):
            budget = deadline - spawned_at
    allowance = min(1.0, required * 0.05)
    return isinstance(budget, (int, float)) and budget < required - allowance


def close_tracked(timeout: float = 6.0) -> None:
    """Terminal shutdown: refuse new helpers and stop every tracked one, in
    parallel so Quit waits for the slowest, not the sum."""
    global _tracked_closed, _shutdowns
    with _tracked_lock:
        _tracked_closed = True
        _shutdowns += 1
        running = list(_tracked.items())
        for proc, _stop in running:
            # Marked on the child itself, so it holds after a reopen too.
            proc._aura_cut_short = True
    stoppers = [threading.Thread(target=stop, args=(proc,), daemon=True,
                                 name="helper-stop") for proc, stop in running]
    for thread in stoppers:
        thread.start()
    deadline = time.monotonic() + timeout
    for thread in stoppers:
        thread.join(max(0.0, deadline - time.monotonic()))


def reopen_tracked() -> None:
    """Admit helpers again after a terminal shutdown that did not exit."""
    global _tracked_closed
    with _tracked_lock:
        _tracked_closed = False


class _Watch:
    """Wakes on news from the app's pipe (a byte, or end of file) or on the
    helper's exit. kqueue on macOS; a pidfd and select() where only that
    exists (the Linux tests); polling otherwise."""

    def __init__(self, pipe_fd: int, child_pid: int):
        self.pipe_fd = pipe_fd
        self.child_pid = child_pid
        self.pipe_open = True
        self.child_gone = False
        self._kq = self._pidfd = None
        if hasattr(select, "kqueue"):
            self._kq = select.kqueue()
            self._kq.control([select.kevent(pipe_fd, select.KQ_FILTER_READ, select.KQ_EV_ADD)], 0, 0)
            try:
                self._kq.control([select.kevent(
                    child_pid, select.KQ_FILTER_PROC, select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                    select.KQ_NOTE_EXIT)], 0, 0)
            except OSError:
                self.child_gone = True   # already exited
        elif hasattr(os, "pidfd_open"):
            try:
                self._pidfd = os.pidfd_open(child_pid)
            except OSError:
                self.child_gone = True
        elif not parent_process_alive(child_pid):
            self.child_gone = True

    def close_pipe(self) -> None:
        self.pipe_open = False
        if self._kq is not None:
            try:
                self._kq.control([select.kevent(self.pipe_fd, select.KQ_FILTER_READ,
                                                select.KQ_EV_DELETE)], 0, 0)
            except OSError:
                pass

    def wait(self, timeout: float | None) -> bool:
        """Block up to *timeout* (None: no limit); True when the pipe has news.
        A helper exit found on the way sets child_gone."""
        if self._kq is not None:
            readable = False
            for event in self._kq.control(None, 4, timeout):
                if event.filter == select.KQ_FILTER_PROC:
                    self.child_gone = True
                elif event.filter == select.KQ_FILTER_READ:
                    readable = True
            return readable
        fds = [self.pipe_fd] if self.pipe_open else []
        if self._pidfd is not None and not self.child_gone:
            fds.append(self._pidfd)
        if self._pidfd is None:   # no exit notice: look every 0.2 s
            timeout = 0.2 if timeout is None else min(timeout, 0.2)
        if fds:
            ready = select.select(fds, [], [], timeout)[0]
        else:
            time.sleep(timeout or 0)
            ready = []
        if self._pidfd is not None and self._pidfd in ready:
            self.child_gone = True
        if self._pidfd is None and not parent_process_alive(self.child_pid):
            self.child_gone = True
        return self.pipe_fd in ready


def _read_child_pid(pipe_fd: int):
    """The helper's pid, the first line on the lifeline (spawn_supervised),
    and whatever came after it; (None, b"") if the app went first."""
    data = b""
    while b"\n" not in data:
        try:
            more = os.read(pipe_fd, 64)
        except OSError:
            more = b""
        if not more:
            return None, b""
        data += more
    line, rest = data.split(b"\n", 1)
    return (int(line) if line.isdigit() and int(line) > 1 else None), rest


def run_watcher(*, pipe_fd: int = 0, term_wait: float = 3.0, ready_fd: int | None = None) -> int:
    """The helper's watcher (see spawn_supervised): end its group once the app
    is gone.

    It leads the group the helper is started into. The app holds the only
    write end of *pipe_fd*: first the helper's pid, then one byte when it
    reaped the helper and swept the group; end of file means it exited
    without letting go, a crash or SIGKILL included. It signals only its own
    group (killpg 0), never a number that could have been reused: SIGTERM
    then SIGKILL when the app dies, SIGKILL as soon as the helper leaves
    what it forked behind (Sol mac r13, r14), and SIGKILL straight away if
    the app died before naming the helper (Sol mac r15). With *ready_fd* it
    writes WATCHER_READY there once SIGTERM is ignored, the app's cue to
    start the helper (Sol mac r17)."""
    if os.getpgrp() != os.getpid():
        print("child-supervisor: not leading its own group", file=sys.stderr, flush=True)
        return 2
    signal.signal(signal.SIGTERM, signal.SIG_IGN)   # its own group's SIGTERM
    if ready_fd is not None:
        # Only now may the helper start (Sol mac r17).
        try:
            os.write(ready_fd, WATCHER_READY)
        except OSError:
            pass   # the app stopped waiting; its lifeline says the rest
    try:
        child_pid, rest = _read_child_pid(pipe_fd)
        if child_pid is None:
            os.killpg(0, signal.SIGKILL)   # a helper that may have started goes
            return 0
        if rest:
            return 0              # released already
        watch = _Watch(pipe_fd, child_pid)
        while not watch.child_gone:
            if not watch.wait(None):
                continue
            try:
                news = os.read(pipe_fd, 1)
            except OSError:
                news = b""
            if news:
                return 0          # released: the app reaped it
            watch.close_pipe()
            watch.wait(0)         # an exit that came with the app's own
            _end_own_group(watch, term_wait)
            return 0
    except BaseException:
        # Whatever stops the watching ends the helper: a watcher gone while
        # its helper runs is the orphan it exists to prevent (Sol mac r18).
        os.killpg(0, signal.SIGKILL)
        raise
    os.killpg(0, signal.SIGKILL)   # the helper left: what it forked goes
    return 0


def _end_own_group(watch: _Watch, term_wait: float) -> None:
    """The watcher signals its own group (killpg 0, no number that could be
    reused): SIGTERM, which it ignores, while the helper
    runs, and after up to *term_wait* SIGKILL, which ends the watcher too,
    with whatever the helper forked (Sol mac r13, r14)."""
    if not watch.child_gone:
        try:
            os.killpg(0, signal.SIGTERM)
        except OSError:
            pass
        deadline = time.monotonic() + term_wait
        while not watch.child_gone:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            watch.wait(left)
    os.killpg(0, signal.SIGKILL)


def run_from_env() -> int:
    """Watcher entry point used by source and frozen application processes."""
    if os.environ.get(WATCHER_ENV) != "1":
        print("Invalid child-supervisor request", file=sys.stderr, flush=True)
        return 2
    return run_watcher(ready_fd=1)


if __name__ == "__main__":
    raise SystemExit(run_from_env())
