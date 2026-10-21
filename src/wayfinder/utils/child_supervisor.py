"""Keep long-lived native children bound to the macOS app lifetime.

Linux uses ``PR_SET_PDEATHSIG`` for its overlay. macOS has no equivalent, so a
small Python supervisor watches the owning PID and terminates the native child
if the app crashes. Normal shutdown signals are forwarded through the same path.

Windows needs no supervisor process: ``bind_to_app_lifetime`` puts the child in
a job object that the kernel closes when the app exits for any reason, crash
included, killing the child with it.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Mapping, Sequence


COMMAND_ENV = "WAYFINDER_SUPERVISED_COMMAND"
PARENT_PID_ENV = "WAYFINDER_SUPERVISOR_PARENT_PID"


def parent_process_alive(pid: int) -> bool:
    """Return whether *pid* still exists (permission denied still means alive)."""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def wrap_macos_child_command(
    command: Sequence[str],
    env: Mapping[str, str],
    *,
    platform_name: str | None = None,
    parent_pid: int | None = None,
) -> tuple[list[str], dict[str, str]]:
    """Wrap a native child so it is reaped if the macOS owner disappears."""
    active_platform = platform_name or sys.platform
    child_env = dict(env)
    original = [str(part) for part in command]
    if active_platform != "darwin":
        return original, child_env

    child_env[COMMAND_ENV] = json.dumps(original)
    child_env[PARENT_PID_ENV] = str(parent_pid or os.getpid())
    if getattr(sys, "frozen", False):
        supervisor = [sys.executable, "--child-supervisor"]
    else:
        supervisor = [sys.executable, str(Path(__file__).resolve())]
    return supervisor, child_env


def supervised_popen_kwargs(*, platform_name: str | None = None) -> dict:
    """Popen options for a command from wrap_macos_child_command.

    macOS: the supervisor leads its own process group, which its native child
    inherits, so stop_supervised() can end both at once. Killing only the
    supervisor (SIGKILL cannot be forwarded) used to orphan whisper-server,
    which then kept the model loaded and port 8178 open.
    """
    return {"start_new_session": True} if (platform_name or sys.platform) == "darwin" else {}


def signal_supervised(proc: subprocess.Popen, *, kill: bool = False,
                      platform_name: str | None = None) -> None:
    """Terminate (or kill) a supervised child: its whole group on macOS."""
    if proc.returncode is not None:
        return  # reaped: its pid, and so its group id, may belong to someone else now
    if (platform_name or sys.platform) == "darwin":
        try:
            os.killpg(proc.pid, signal.SIGKILL if kill else signal.SIGTERM)
            return
        except OSError:
            pass  # no such group: spawned without supervised_popen_kwargs
    try:
        proc.kill() if kill else proc.terminate()
    except OSError:
        pass


def stop_supervised(proc: subprocess.Popen, term_wait: float = 5.0, kill_wait: float = 2.0, *,
                    platform_name: str | None = None) -> None:
    """SIGTERM, then SIGKILL after *term_wait*; on macOS to the whole group."""
    if proc.poll() is not None:
        return
    signal_supervised(proc, platform_name=platform_name)
    try:
        proc.wait(timeout=term_wait)
        return
    except subprocess.TimeoutExpired:
        pass
    signal_supervised(proc, kill=True, platform_name=platform_name)
    try:
        proc.wait(timeout=kill_wait)
    except subprocess.TimeoutExpired:
        pass


# Native helpers an older Aura could leave behind (its supervisor SIGKILLed,
# or a crash with llama-server, which is not supervised).
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


class HelpersClosed(RuntimeError):
    """A helper process was refused: the app is shutting down."""


class HelperStopped(HelpersClosed):
    """A terminal shutdown stopped this helper mid-run. Its output and exit
    status say nothing about the machine, even after a reopen, so no caller
    may cache or count them."""


def spawn_tracked(command: Sequence[str], *, stop=None, **popen_kwargs) -> subprocess.Popen:
    """Popen a helper that a terminal shutdown stops (with *stop*, default
    terminate then kill); refused once one has begun."""
    global _exit_hook_registered
    with _tracked_lock:
        if _tracked_closed:
            raise HelpersClosed("shutting down: no new helper process")
        if not _exit_hook_registered:
            # An interpreter exit with no server started (main.py, CLI verbs)
            # runs no server exit hook: this one stops the helpers itself.
            import atexit
            atexit.register(close_tracked)
            _exit_hook_registered = True
        proc = subprocess.Popen(command, **popen_kwargs)
        _tracked[proc] = stop or _terminate_child
    bind_to_app_lifetime(proc)
    return proc


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
    """subprocess.run() through spawn_tracked(): the same arguments and errors."""
    if kwargs.pop("capture_output", False):
        kwargs["stdout"] = kwargs["stderr"] = subprocess.PIPE
    if input is not None:
        kwargs["stdin"] = subprocess.PIPE
    proc = spawn_tracked(command, **kwargs)
    try:
        with proc:
            try:
                stdout, stderr = proc.communicate(input, timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                if cut_short(proc):
                    raise HelperStopped("stopped by a terminal shutdown") from None
                raise
            except BaseException as exc:
                proc.kill()
                if isinstance(exc, Exception) and cut_short(proc):
                    # Output cut off mid-character, a closed pipe: the stop's
                    # doing, not the helper's answer.
                    raise HelperStopped("stopped by a terminal shutdown") from exc
                raise
    finally:
        forget_tracked(proc)
    if cut_short(proc):
        raise HelperStopped("stopped by a terminal shutdown")
    if check and proc.returncode:
        raise subprocess.CalledProcessError(proc.returncode, proc.args, stdout, stderr)
    return subprocess.CompletedProcess(proc.args, proc.returncode, stdout, stderr)


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


def run_supervised_command(
    command: Sequence[str],
    parent_pid: int,
    *,
    poll_interval: float = 0.2,
) -> int:
    """Run *command* until it exits or its owning app process disappears."""
    if parent_pid <= 1 or not parent_process_alive(parent_pid):
        return 0

    stop = threading.Event()

    def request_stop(_signum, _frame):
        stop.set()

    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, request_stop)

    child_env = os.environ.copy()
    child_env.pop(COMMAND_ENV, None)
    child_env.pop(PARENT_PID_ENV, None)
    proc = subprocess.Popen([str(part) for part in command], env=child_env)
    while proc.poll() is None:
        if stop.is_set() or not parent_process_alive(parent_pid):
            _terminate_child(proc)
            return 0
        time.sleep(poll_interval)
    return int(proc.returncode or 0)


def run_from_env() -> int:
    """Supervisor entry point used by source and frozen application processes."""
    try:
        command = json.loads(os.environ[COMMAND_ENV])
        parent_pid = int(os.environ[PARENT_PID_ENV])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(f"Invalid child-supervisor request: {exc}", file=sys.stderr, flush=True)
        return 2
    if not isinstance(command, list) or not command or not all(
        isinstance(part, str) and part for part in command
    ):
        print("Invalid child-supervisor command", file=sys.stderr, flush=True)
        return 2
    return run_supervised_command(command, parent_pid)


if __name__ == "__main__":
    raise SystemExit(run_from_env())
