"""Parent-lifetime supervision for native macOS helper processes.

The macOS shape (the helper as the app's own child, leading its group, plus a
watcher) runs on any POSIX host with platform_name="darwin"."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time

import pytest

from wayfinder.utils import child_supervisor

posix = pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")


def _state(pid: int) -> str:
    """ps state letter, or "" once the process is gone (zombies count as gone)."""
    out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout
    return "" if out.strip().startswith("Z") else out.strip()


def _gone(pid: int, within: float = 3.0) -> bool:
    deadline = time.monotonic() + within
    while _state(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    return _state(pid) == ""


def _wait_for(path, what: str):
    deadline = time.monotonic() + 10
    while not (path.exists() and path.read_text().strip()):
        assert time.monotonic() < deadline, f"{what} never started"
        time.sleep(0.05)
    return path.read_text().split()


def _helper(tmp_path, body: str = "", *, ignores_term: bool = False, name: str = "server"):
    """A real helper spawned the macOS way; returns its Popen once it runs."""
    pid_file = tmp_path / f"{name}.pid"
    script = ("import os, signal, sys, time\n"
              + ("signal.signal(signal.SIGTERM, signal.SIG_IGN)\n" if ignores_term else "")
              + body
              + f"open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
              + "time.sleep(60)\n")
    proc = child_supervisor.spawn_supervised([sys.executable, "-c", script], env=dict(os.environ),
                                             platform_name="darwin")
    assert _wait_for(pid_file, "the helper") == [str(proc.pid)]
    return proc


def _boot(seconds: float = 1.0) -> None:
    time.sleep(seconds)   # the watcher is a fresh Python: let it start watching


def test_other_platforms_spawn_plainly():
    made = []

    def popen(command, **kwargs):
        made.append((command, kwargs))
        return subprocess.Popen([sys.executable, "-c", "pass"])

    proc = child_supervisor.spawn_supervised(["whisper-server"], env={"A": "1"}, popen=popen,
                                             platform_name="linux")
    proc.wait()
    assert made == [(["whisper-server"], {"env": {"A": "1"}})]
    assert getattr(proc, "_aura_watcher", None) is None


def test_the_watcher_command_reexecs_the_app_or_runs_this_module(monkeypatch):
    monkeypatch.setattr(child_supervisor.sys, "executable", "/python312")
    monkeypatch.delattr(child_supervisor.sys, "frozen", raising=False)
    command = child_supervisor._watcher_command()
    assert command[0] == "/python312" and command[1].endswith("child_supervisor.py")
    monkeypatch.setattr(child_supervisor.sys, "frozen", True, raising=False)
    assert child_supervisor._watcher_command() == ["/python312", "--child-supervisor"]


@posix
def test_a_macos_helper_is_the_apps_own_child_and_leads_its_group(tmp_path):
    proc = _helper(tmp_path)
    watcher = proc._aura_watcher
    try:
        # Ours until reaped, whatever happens to anything else; its watcher,
        # a member of its group, keeps the group's id until it is reaped too.
        assert os.getpgid(proc.pid) == proc.pid
        assert os.getsid(proc.pid) == os.getsid(0)   # the app's session, so it can be joined
        assert watcher.poll() is None and os.getpgid(watcher.pid) == proc.pid
        assert proc._aura_guarded is True
        child_supervisor.stop_supervised(proc, term_wait=2, platform_name="darwin")
        assert proc.returncode is not None
        # Released once reaped: it leaves without signalling anything.
        watcher.wait(timeout=5)
        assert proc._aura_watcher is None
    finally:
        if proc.poll() is None:
            proc.kill()


@posix
@pytest.mark.parametrize("ignores_term", [False, True])
def test_the_watcher_ends_the_helper_when_the_app_dies(tmp_path, ignores_term):
    # The app SIGKILLed (a crash, Force Quit): no Python code of its runs, and
    # the helper must not keep the model loaded and the port open.
    app = subprocess.Popen(
        [sys.executable, "-c",
         "import os, sys, time\n"
         f"sys.path.insert(0, {str(child_supervisor.Path(child_supervisor.__file__).parents[2])!r})\n"
         "from wayfinder.utils import child_supervisor as cs\n"
         "body = 'import signal; signal.signal(signal.SIGTERM, signal.SIG_IGN); ' if "
         f"{ignores_term!r} else ''\n"
         "proc = cs.spawn_supervised([sys.executable, '-c', body + 'import time; time.sleep(60)'],"
         " env=dict(os.environ), platform_name='darwin')\n"
         "print(proc.pid, proc._aura_watcher.pid, flush=True)\n"
         "time.sleep(60)\n"],
        stdout=subprocess.PIPE, text=True)
    helper, watcher = map(int, app.stdout.readline().split())
    try:
        _boot()
        app.kill()
        app.wait()
        # SIGTERM, then SIGKILL after the watcher's 3 s grace.
        assert _gone(helper, within=7), "the helper outlived the app"
        assert _gone(watcher, within=3)
    finally:
        for pid in (helper, watcher):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass


@posix
@pytest.mark.parametrize("ignores_term", [False, True])
def test_a_helper_whose_watcher_died_is_still_stopped(tmp_path, ignores_term):
    # Sol mac r6: a supervisor SIGKILLed first used to leave its server
    # running after Quit. Now only the crash cover goes with the watcher.
    proc = _helper(tmp_path, ignores_term=ignores_term)
    proc._aura_watcher.kill()
    proc._aura_watcher.wait()
    child_supervisor.stop_supervised(proc, term_wait=0.5, kill_wait=2, platform_name="darwin")
    assert proc.returncode is not None
    assert _gone(proc.pid, within=1)


@posix
def test_a_helper_that_closes_inherited_descriptors_is_still_ours(tmp_path):
    # Sol mac r7: ownership must not rest on a descriptor the helper keeps.
    proc = _helper(tmp_path, "os.closerange(3, 4096)\n")
    _boot()
    os.kill(proc._aura_watcher.pid, signal.SIGKILL)
    child_supervisor.stop_supervised(proc, term_wait=2, platform_name="darwin")
    assert _gone(proc.pid, within=1)


@posix
@pytest.mark.parametrize("fork_ignores_term", [False, True])
def test_the_group_signal_reaches_what_the_helper_forked(tmp_path, fork_ignores_term):
    # Sol mac r13: the leader leaving on SIGTERM ended the stop (and released
    # the watcher) while a worker it forked, which ignored SIGTERM, ran on.
    grandchild_file = tmp_path / "grandchild.pid"
    proc = _helper(tmp_path,
                   "if os.fork() == 0:\n"
                   + ("    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n" if fork_ignores_term else "")
                   + f"    open({str(grandchild_file)!r}, 'w').write(str(os.getpid()))\n"
                   "    time.sleep(60)\n"
                   "    os._exit(0)\n")
    grandchild = int(_wait_for(grandchild_file, "the fork")[0])
    try:
        child_supervisor.stop_supervised(proc, term_wait=2, platform_name="darwin")
        assert proc.returncode == -signal.SIGTERM        # the leader left on SIGTERM
        assert _gone(grandchild, within=2)
    finally:
        try:
            os.kill(grandchild, signal.SIGKILL)
        except OSError:
            pass


@posix
def test_the_group_kill_waits_for_the_leaders_own_shutdown(tmp_path):
    # The group SIGKILL follows the leader's exit and never replaces it: a
    # server finishing its shutdown on SIGTERM gets to finish it.
    done = tmp_path / "done"
    proc = _helper(tmp_path,
                   "def leave(*_):\n"
                   "    time.sleep(0.3)\n"
                   f"    open({str(done)!r}, 'w').write('clean')\n"
                   "    os._exit(0)\n"
                   "signal.signal(signal.SIGTERM, leave)\n")
    child_supervisor.signal_supervised(proc, platform_name="darwin")
    assert child_supervisor.wait_supervised(proc, 5, platform_name="darwin")
    assert proc.returncode == 0 and done.read_text() == "clean"


@posix
def test_a_leader_still_running_when_the_wait_ends_is_left_to_its_caller(tmp_path):
    # A wait that times out kills nothing: the caller decides on SIGKILL.
    proc = _helper(tmp_path, ignores_term=True)
    try:
        child_supervisor.signal_supervised(proc, platform_name="darwin")
        assert not child_supervisor.wait_supervised(proc, 0.3, platform_name="darwin")
        assert proc.poll() is None
    finally:
        child_supervisor.stop_supervised(proc, term_wait=0.2, kill_wait=2, platform_name="darwin")
        assert _gone(proc.pid, within=2)


@posix
def test_the_watcher_ends_what_the_helper_forked_when_the_app_dies(tmp_path):
    # Sol mac r13, Force Quit: the leader leaves on the watcher's SIGTERM, and
    # the worker it forked, which ignores SIGTERM, must not outlive it.
    worker_file = tmp_path / "worker.pid"
    body = ("import os, signal, time\n"
            "if os.fork() == 0:\n"
            "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            f"    open({str(worker_file)!r}, 'w').write(str(os.getpid()))\n"
            "    time.sleep(60)\n"
            "    os._exit(0)\n"
            "time.sleep(60)\n")
    app = subprocess.Popen(
        [sys.executable, "-c",
         "import os, sys, time\n"
         f"sys.path.insert(0, {str(child_supervisor.Path(child_supervisor.__file__).parents[2])!r})\n"
         "from wayfinder.utils import child_supervisor as cs\n"
         f"proc = cs.spawn_supervised([sys.executable, '-c', {body!r}],"
         " env=dict(os.environ), platform_name='darwin')\n"
         "print(proc.pid, proc._aura_watcher.pid, flush=True)\n"
         "time.sleep(60)\n"],
        stdout=subprocess.PIPE, text=True)
    helper, watcher = map(int, app.stdout.readline().split())
    worker = int(_wait_for(worker_file, "the worker")[0])
    try:
        _boot()
        app.kill()
        app.wait()
        assert _gone(helper, within=3)
        assert _gone(worker, within=2), "the forked worker outlived the app"
        assert _gone(watcher, within=3)
    finally:
        for pid in (helper, worker, watcher):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass


def _forking_helper(tmp_path, *, leader_leaves: bool = False):
    """A helper that forks a worker ignoring SIGTERM; the leader exits by
    itself first if *leader_leaves*. Returns (proc, worker pid)."""
    worker_file = tmp_path / "worker.pid"
    proc = _helper(tmp_path,
                   "if os.fork() == 0:\n"
                   "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                   f"    open({str(worker_file)!r}, 'w').write(str(os.getpid()))\n"
                   "    time.sleep(60)\n"
                   "    os._exit(0)\n")
    worker = int(_wait_for(worker_file, "the worker")[0])
    if leader_leaves:
        os.kill(proc.pid, signal.SIGUSR1)   # the default action: it leaves
    return proc, worker


def _kill_quietly(*pids):
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


@posix
@pytest.mark.parametrize("stop", ["llama", "stop_supervised"])
def test_a_leader_that_left_on_its_own_and_was_reaped_still_takes_its_worker(tmp_path, monkeypatch, stop):
    # Sol mac r14: poll() reaped a leader that exited by itself, every group
    # signal then refused, and the worker it forked ran on. The watcher is
    # killed first (unreaped, it still holds the group), so this is the
    # app's own sweep.
    from wayfinder.core.llama_server import LlamaServerManager
    proc, worker = _forking_helper(tmp_path)
    os.kill(proc._aura_watcher.pid, signal.SIGKILL)
    os.kill(proc.pid, signal.SIGUSR1)
    try:
        deadline = time.monotonic() + 5
        while proc.poll() is None:                     # the competing reaper
            assert time.monotonic() < deadline
            time.sleep(0.02)
        assert _state(worker), "the worker should still run here"
        if stop == "llama":
            monkeypatch.setattr(child_supervisor.sys, "platform", "darwin")
            LlamaServerManager._kill(proc)
        else:
            child_supervisor.stop_supervised(proc, term_wait=1, platform_name="darwin")
        assert _gone(worker, within=2), "the worker outlived its reaped leader"
    finally:
        _kill_quietly(worker)


@posix
def test_a_reaper_racing_the_stop_cannot_save_the_worker(tmp_path):
    # Sol mac r14: a concurrent poll() reaping the leader between its exit
    # and the stop's sweep made the sweep refuse.
    proc, worker = _forking_helper(tmp_path)
    os.kill(proc._aura_watcher.pid, signal.SIGKILL)   # the app's sweep alone
    done = threading.Event()

    def reap():
        while not done.is_set() and proc.poll() is None:
            time.sleep(0.001)

    reaper = threading.Thread(target=reap, daemon=True)
    reaper.start()
    try:
        child_supervisor.stop_supervised(proc, term_wait=2, platform_name="darwin")
        assert proc.returncode == -signal.SIGTERM
        assert _gone(worker, within=2)
    finally:
        done.set()
        reaper.join(2)
        _kill_quietly(worker)


@posix
def test_the_watcher_ends_the_worker_of_a_leader_that_left_on_its_own(tmp_path):
    # With no stop from the app at all: the watcher, a member, sees the
    # leader leave and ends its group, itself included.
    proc, worker = _forking_helper(tmp_path, leader_leaves=True)
    watcher = proc._aura_watcher
    try:
        assert _gone(worker, within=3), "the worker outlived its leader"
        assert watcher.wait(timeout=3) == -signal.SIGKILL
        proc.wait(timeout=3)
        child_supervisor.release_supervised(proc)
    finally:
        _kill_quietly(worker)


@posix
def test_the_group_keeps_its_id_while_the_watcher_lives(tmp_path):
    # Sol mac r14: the id a watcher signals must be the helper's group even
    # after the leader is gone and reaped. A member keeps it: no process can
    # be given that number, nor make a group of it, meanwhile.
    proc = child_supervisor.spawn_supervised(["true"], env=dict(os.environ), platform_name="darwin")
    watcher = proc._aura_watcher
    proc.wait()
    try:
        os.killpg(proc.pid, 0)                          # still there: the watcher
        assert os.getpgid(watcher.pid) == proc.pid
    finally:
        child_supervisor.release_supervised(proc)
    assert watcher.wait(timeout=3) is not None


@posix
@pytest.mark.parametrize("case", ["app-died", "helper-left", "app-died-after-it-left"])
def test_a_member_watcher_signals_its_group_by_zero_never_by_number(monkeypatch, case):
    # Sol mac r14: once the leader is reaped by launchd its number can be
    # handed out again; a member signals its own group (killpg 0) instead.
    sent, ignored = [], []
    helper = subprocess.Popen(["sleep", "30"] if case == "app-died" else ["true"])
    if case != "app-died":
        helper.wait()                                   # gone and reaped
    monkeypatch.setattr(child_supervisor.os, "getpgrp", lambda: helper.pid)
    monkeypatch.setattr(child_supervisor.os, "killpg", lambda pgid, sig: sent.append((pgid, sig)))
    monkeypatch.setattr(child_supervisor.signal, "signal", lambda sig, how: ignored.append(sig))
    read_end, write_end = os.pipe()
    if case != "helper-left":
        os.close(write_end)                             # the app died
    try:
        assert child_supervisor.run_watcher(helper.pid, pipe_fd=read_end, term_wait=0.2) == 0
    finally:
        os.close(read_end)
        if case == "helper-left":
            os.close(write_end)
        helper.kill()
        helper.wait()
    assert ignored == [signal.SIGTERM]
    expected = [(0, signal.SIGTERM), (0, signal.SIGKILL)] if case == "app-died" else [(0, signal.SIGKILL)]
    assert sent == expected


@posix
def test_a_reaped_helpers_group_is_signalled_only_while_its_watcher_holds_it(monkeypatch):
    # Sol mac r14: poll() reaping a leader that left on its own made every
    # later group signal refuse, and what it forked ran on. The watcher's
    # membership still proves the group; released, nothing does.
    sent = []
    monkeypatch.setattr(child_supervisor.os, "killpg", lambda pid, sig: sent.append((pid, sig)))
    proc = child_supervisor.spawn_supervised(["true"], env=dict(os.environ), platform_name="darwin")
    proc.wait()
    child_supervisor.signal_supervised(proc, kill=True, platform_name="darwin")
    assert sent == [(proc.pid, signal.SIGKILL)]
    child_supervisor.release_supervised(proc)          # sweeps, then lets go
    assert sent == [(proc.pid, signal.SIGKILL)] * 2 and proc._aura_watcher is None
    sent.clear()
    child_supervisor.signal_supervised(proc, kill=True, platform_name="darwin")
    child_supervisor.signal_supervised(proc, platform_name="darwin")
    assert sent == []


@posix
def test_signal_supervised_never_targets_a_reaped_pid_without_its_watcher(monkeypatch):
    sent = []
    monkeypatch.setattr(child_supervisor.os, "killpg", lambda pid, sig: sent.append((pid, sig)))
    proc = child_supervisor.spawn_supervised(["true"], env=dict(os.environ), platform_name="darwin")
    proc._aura_watcher.kill()
    proc._aura_watcher.wait()                           # its watcher reaped: no member is ours
    proc.wait()
    child_supervisor.signal_supervised(proc, kill=True, platform_name="darwin")
    child_supervisor.signal_supervised(proc, platform_name="darwin")
    child_supervisor.release_supervised(proc)
    assert sent == []


def _exited_unreaped(proc) -> None:
    deadline = time.monotonic() + 5
    while _state(proc.pid):   # a zombie counts as gone
        assert time.monotonic() < deadline, "the helper never exited"
        time.sleep(0.02)
    assert proc.returncode is None


@posix
@pytest.mark.parametrize("member", ["watcher", "helper"])
def test_a_reap_on_another_thread_cannot_land_between_the_check_and_the_signal(monkeypatch, member):
    # Sol mac r8: the unreaped check and the killpg hold the member's reap
    # lock, so a wait() racing them reaps only after the signal, never before.
    proc = child_supervisor.spawn_supervised(["true"], env=dict(os.environ), platform_name="darwin")
    watcher = proc._aura_watcher
    if member == "watcher":
        proc.wait()                    # the helper reaped: the watcher holds the group
        os.kill(watcher.pid, signal.SIGKILL)
        _exited_unreaped(watcher)
        held = watcher
    else:
        watcher.kill()
        watcher.wait()                 # no watcher: the helper's own proof
        _exited_unreaped(proc)
        held = proc
    during = []

    def killpg(pid, sig):
        reaper = threading.Thread(target=held.wait, kwargs={"timeout": 5})
        reaper.start()
        reaper.join(0.3)   # an unlocked reap lands well within this
        during.append(held.returncode)

    monkeypatch.setattr(child_supervisor.os, "killpg", killpg)
    child_supervisor.signal_supervised(proc, kill=True, platform_name="darwin")
    assert during == [None]
    held.wait(timeout=5)
    proc.wait(timeout=5)
    child_supervisor.release_supervised(proc)


@posix
def test_a_group_whose_reap_lock_stays_busy_is_not_signalled(monkeypatch):
    sent = []
    monkeypatch.setattr(child_supervisor.os, "killpg", lambda pid, sig: sent.append((pid, sig)))
    monkeypatch.setattr(child_supervisor, "_REAP_LOCK_WAIT", 0.1)
    proc = child_supervisor.spawn_supervised(["sleep", "30"], env=dict(os.environ), platform_name="darwin")
    try:
        # A reap in progress on both members, for all anyone knows.
        with proc._waitpid_lock, proc._aura_watcher._waitpid_lock:
            child_supervisor.signal_supervised(proc, kill=True, platform_name="darwin")
        assert sent == []
        with proc._waitpid_lock:   # the watcher's proof is enough alone
            child_supervisor.signal_supervised(proc, kill=True, platform_name="darwin")
        assert sent == [(proc.pid, signal.SIGKILL)]
    finally:
        proc.kill()
        proc.wait()
        child_supervisor.release_supervised(proc)


@posix
def test_a_blocking_wait_on_a_helper_leaves_its_group_stoppable(monkeypatch):
    # An untimed wait() would otherwise sit in waitpid() under the reap lock,
    # and the stop would be refused as a reap in progress.
    monkeypatch.setattr(child_supervisor, "_REAP_LOCK_WAIT", 0.5)
    proc = child_supervisor.spawn_supervised(["sleep", "30"], env=dict(os.environ), platform_name="darwin")
    waiter = threading.Thread(target=proc.wait, daemon=True)
    waiter.start()
    time.sleep(0.3)
    child_supervisor.signal_supervised(proc, kill=True, platform_name="darwin")
    waiter.join(5)
    assert not waiter.is_alive() and proc.returncode == -signal.SIGKILL
    child_supervisor.release_supervised(proc)


def test_a_stand_in_popen_gets_no_watcher_or_group(monkeypatch):
    # A test's fake pid is no child of ours: a watcher on it could, once the
    # fake is collected, signal a stranger's group.
    started = []
    monkeypatch.setattr(child_supervisor.subprocess, "Popen",
                        lambda *a, **k: started.append(a) or type("Fake", (), {"pid": 1})())
    proc = child_supervisor.spawn_supervised(["whisper-server"], platform_name="darwin")
    assert len(started) == 1 and not hasattr(proc, "_aura_group") and not hasattr(proc, "_aura_watcher")


@posix
def test_macos_supervises_every_tracked_helper_by_default(monkeypatch):
    # Sol mac r8: whisper-cli and llama-simple die with a Force Quit too.
    monkeypatch.setattr(child_supervisor, "_supervise_by_default", lambda: True)
    proc = child_supervisor.spawn_tracked(["sleep", "30"])
    try:
        assert proc._aura_group is True and os.getpgid(proc.pid) == proc.pid
        assert isinstance(proc._aura_watcher, subprocess.Popen)
        assert child_supervisor._tracked[proc] is child_supervisor._stop_helper_group
    finally:
        child_supervisor._stop_helper_group(proc)
        child_supervisor.forget_tracked(proc)
    assert proc.returncode is not None and proc._aura_watcher is None


@posix
def test_a_timed_out_tracked_helper_is_killed_with_its_group(monkeypatch, tmp_path):
    monkeypatch.setattr(child_supervisor, "_supervise_by_default", lambda: True)
    pid_file = tmp_path / "grandchild.pid"
    script = (f"import subprocess, time\n"
              f"g = subprocess.Popen(['sleep', '60'])\n"
              f"open({str(pid_file)!r}, 'w').write(str(g.pid))\n"
              f"time.sleep(60)\n")
    with pytest.raises(subprocess.TimeoutExpired):
        child_supervisor.run_tracked([sys.executable, "-c", script], timeout=3)
    grandchild = int(_wait_for(pid_file, "the grandchild")[0])
    assert _gone(grandchild, within=2)


@posix
def test_a_child_outside_its_own_group_is_never_group_signalled(monkeypatch):
    # Its pid is no group id of ours: a group of that number could be anyone's.
    sent = []
    monkeypatch.setattr(child_supervisor.os, "killpg", lambda pid, sig: sent.append((pid, sig)))
    proc = subprocess.Popen(["sleep", "30"])  # not through spawn_supervised
    child_supervisor.stop_supervised(proc, term_wait=2, platform_name="darwin")
    assert proc.returncode is not None and sent == []


@posix
def test_only_macos_signals_a_group(monkeypatch):
    sent = []
    monkeypatch.setattr(child_supervisor.os, "killpg", lambda pid, sig: sent.append((pid, sig)))
    proc = subprocess.Popen(["sleep", "30"], start_new_session=True)
    proc._aura_group = True
    try:
        child_supervisor.signal_supervised(proc, platform_name="linux")
        proc.wait(timeout=5)
        assert sent == []
    finally:
        if proc.poll() is None:
            proc.kill()


# The watcher itself, in this process: a pipe stands for the app's lifeline.

@posix
def test_the_watcher_signals_nothing_once_the_helper_has_exited(monkeypatch):
    sent = []
    monkeypatch.setattr(child_supervisor.os, "killpg", lambda pid, sig: sent.append((pid, sig)))
    helper = subprocess.Popen(["true"])
    helper.wait()                      # exited and reaped: its pid is free
    read_end, write_end = os.pipe()
    os.close(write_end)                # and then the app died
    try:
        assert child_supervisor.run_watcher(helper.pid, pipe_fd=read_end) == 0
    finally:
        os.close(read_end)
    assert sent == []


@pytest.mark.skipif(not hasattr(os, "waitid"), reason="needs a wait that leaves the zombie")
def test_a_helper_that_exits_with_the_app_is_not_signalled(monkeypatch):
    # Both ends at once: the exit is seen with the app's, so its pid (free
    # once reaped) is never signalled.
    sent = []
    monkeypatch.setattr(child_supervisor.os, "killpg", lambda pid, sig: sent.append((pid, sig)))
    helper = subprocess.Popen(["true"], start_new_session=True)
    os.waitid(os.P_PID, helper.pid, os.WEXITED | os.WNOWAIT)   # exited, not reaped
    read_end, write_end = os.pipe()
    os.close(write_end)
    try:
        assert child_supervisor.run_watcher(helper.pid, pipe_fd=read_end) == 0
    finally:
        os.close(read_end)
        helper.wait()
    assert sent == []


@posix
def test_a_released_watcher_signals_nothing(monkeypatch):
    sent = []
    monkeypatch.setattr(child_supervisor.os, "killpg", lambda pid, sig: sent.append((pid, sig)))
    helper = subprocess.Popen(["sleep", "30"], start_new_session=True)
    read_end, write_end = os.pipe()
    try:
        os.write(write_end, b"r")
        os.close(write_end)
        assert child_supervisor.run_watcher(helper.pid, pipe_fd=read_end) == 0
        assert sent == []
    finally:
        os.close(read_end)
        helper.kill()
        helper.wait()


@posix
def test_a_watcher_whose_app_died_ends_the_helper_group():
    helper = subprocess.Popen(["sleep", "30"], start_new_session=True)
    read_end, write_end = os.pipe()
    os.close(write_end)
    try:
        started = time.monotonic()
        assert child_supervisor.run_watcher(helper.pid, pipe_fd=read_end, term_wait=2) == 0
        assert helper.wait(timeout=2) == -signal.SIGTERM
        assert time.monotonic() - started < 2
    finally:
        os.close(read_end)
        if helper.poll() is None:
            helper.kill()


def test_a_watcher_without_a_helper_refuses(monkeypatch):
    monkeypatch.delenv(child_supervisor.CHILD_PID_ENV, raising=False)
    assert child_supervisor.run_from_env() == 2
    monkeypatch.setenv(child_supervisor.CHILD_PID_ENV, "1")
    assert child_supervisor.run_from_env() == 2


def test_orphaned_helpers_are_only_launchd_parented_aura_binaries():
    app = "/Applications/Wayfinder Aura.app/Contents/Frameworks/bin"
    moved = "/Users/p/Downloads/Wayfinder Aura 2.app/Contents/Frameworks/bin"
    rows = [
        "501 55437     1 /Applications/Wayfinder Aura.app/Contents/MacOS/Wayfinder Aura",
        f"501 55485     1 {app}/whisper-server -m /Users/p/models/ggml-large-v3-turbo-q5_0.bin --port 8178",
        f"501 55493 55492 {app}/whisper-server -m /Users/p/models/ggml-large-v3-turbo-q5_0.bin --port 8178",
        f"501 55500     1 {app}/whisper-coreml/whisper-server -m /x --port 50123",
        f"501 55501     1 {moved}/llama-server -m /Users/p/llm/gemma.gguf",
        f"501 55502     1 {app}/coreml-compile /tmp/a /tmp/b",
        f"502 55503     1 {app}/whisper-server -m /other/user",
        f"501 55504     1 /Users/p/dev/wayfinder-aura/bin/whisper-server -m /x",
        f"501 55505     1 {app}/whisper-server-helper-unrelated",
        "garbage",
    ]
    assert child_supervisor.orphaned_helper_pids(rows, 501) == [55485, 55500, 55501, 55502]


def test_reaping_is_macos_only():
    assert child_supervisor.reap_orphaned_helpers(platform_name="linux") == []


@posix
def test_llama_servers_stop_ends_what_its_helper_forked(tmp_path, monkeypatch):
    # Sol mac r13: llama-server's own stop waited on the leader alone, so a
    # worker it forked that ignores SIGTERM outlived it.
    from wayfinder.core.llama_server import LlamaServerManager
    worker_file = tmp_path / "worker.pid"
    proc = _helper(tmp_path,
                   "if os.fork() == 0:\n"
                   "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                   f"    open({str(worker_file)!r}, 'w').write(str(os.getpid()))\n"
                   "    time.sleep(60)\n"
                   "    os._exit(0)\n")
    worker = int(_wait_for(worker_file, "the worker")[0])
    monkeypatch.setattr(child_supervisor.sys, "platform", "darwin")
    try:
        LlamaServerManager._kill(proc)
        assert proc.returncode == -signal.SIGTERM
        assert _gone(worker, within=2)
    finally:
        try:
            os.kill(worker, signal.SIGKILL)
        except OSError:
            pass
