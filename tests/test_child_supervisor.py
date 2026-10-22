"""Parent-lifetime supervision for native macOS helper processes.

The macOS shape (the helper as the app's own child, leading its group, plus a
watcher) runs on any POSIX host with platform_name="darwin"."""

from __future__ import annotations

import os
import select
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
def test_a_macos_helper_is_the_apps_own_child_in_its_watchers_group(tmp_path):
    proc = _helper(tmp_path)
    watcher = proc._aura_watcher
    try:
        # Both ours until reaped, whatever happens to anything else; the
        # watcher leads the group the helper was started into, and either of
        # them, unreaped, keeps the group's id.
        assert watcher.poll() is None and os.getpgid(watcher.pid) == watcher.pid
        assert os.getpgid(proc.pid) == watcher.pid == proc._aura_gid
        assert os.getsid(watcher.pid) == os.getsid(0)   # the app's session, so it can be joined
        assert proc._aura_guarded is True
        child_supervisor.stop_supervised(proc, term_wait=2, platform_name="darwin")
        assert proc.returncode is not None
        # Released once reaped: it leaves without signalling anything.
        watcher.wait(timeout=5)
        assert proc._aura_watcher is None
    finally:
        if proc.poll() is None:
            proc.kill()


class _Recording(subprocess.Popen):
    """Real processes, with each spawn's process group recorded."""
    spawned: list = []
    instances: list = []

    def __init__(self, args, **kwargs):
        _Recording.spawned.append((list(args), kwargs.get("process_group")))
        super().__init__(args, **kwargs)
        _Recording.instances.append(self)


def _record_spawns(monkeypatch):
    _Recording.spawned, _Recording.instances = [], []
    monkeypatch.setattr(child_supervisor, "_POPEN", _Recording)
    monkeypatch.setattr(child_supervisor.subprocess, "Popen", _Recording)
    return _Recording.spawned


@posix
def test_the_watcher_starts_before_the_helper_which_starts_in_its_group(monkeypatch):
    # Sol mac r15: a helper started first ran unwatched until its watcher did.
    spawned = _record_spawns(monkeypatch)
    proc = child_supervisor.spawn_supervised(["sleep", "30"], env=dict(os.environ),
                                             platform_name="darwin")
    try:
        watcher = proc._aura_watcher
        assert spawned == [(child_supervisor._watcher_command(), 0), (["sleep", "30"], watcher.pid)]
    finally:
        child_supervisor.stop_supervised(proc, term_wait=1, platform_name="darwin")


@posix
def test_no_watcher_no_helper(monkeypatch, tmp_path):
    marker = tmp_path / "ran"
    monkeypatch.setattr(child_supervisor, "_watcher_command", lambda: [str(tmp_path / "missing")])
    with pytest.raises(OSError):
        child_supervisor.spawn_supervised(["touch", str(marker)], env=dict(os.environ),
                                          platform_name="darwin")
    time.sleep(0.2)
    assert not marker.exists()


@posix
@pytest.mark.parametrize("watcher_body", ["pass", "import time; time.sleep(30)"],
                         ids=["exits-silently", "never-ready"])
def test_a_watcher_that_never_reports_ready_starts_no_helper(monkeypatch, tmp_path, watcher_body):
    # Sol mac r17: the helper waits for the watcher's word, not just its spawn.
    marker = tmp_path / "ran"
    _record_spawns(monkeypatch)
    monkeypatch.setattr(child_supervisor, "_watcher_command",
                        lambda: [sys.executable, "-c", watcher_body])
    monkeypatch.setattr(child_supervisor, "WATCHER_READY_TIMEOUT_S", 1.0)
    with pytest.raises(OSError, match="no crash watcher"):
        child_supervisor.spawn_supervised(["touch", str(marker)], env=dict(os.environ),
                                          platform_name="darwin")
    [watcher] = _Recording.instances                      # the helper was never spawned
    assert watcher.returncode is not None                 # stopped and reaped
    time.sleep(0.2)
    assert not marker.exists()


@posix
def test_an_early_group_sigterm_cannot_end_a_slow_watcher(monkeypatch, tmp_path):
    # Sol mac r17: a watcher still starting up (here 1 s) did not yet ignore
    # SIGTERM, so a stop's group SIGTERM right after the spawn ended it and
    # left a helper that outlasts SIGTERM unwatched.
    script = child_supervisor.__file__
    monkeypatch.setattr(child_supervisor, "_watcher_command", lambda: [
        sys.executable, "-c",
        f"import runpy, time; time.sleep(1.0); runpy.run_path({script!r}, run_name='__main__')"])
    proc = _helper(tmp_path, ignores_term=True)
    watcher = proc._aura_watcher
    try:
        os.killpg(proc._aura_gid, signal.SIGTERM)
        time.sleep(0.5)
        assert watcher.poll() is None                     # still watching
        assert proc.poll() is None                        # the helper shrugged it off
    finally:
        child_supervisor.stop_supervised(proc, term_wait=1, platform_name="darwin")
    assert proc.returncode is not None
    watcher.wait(timeout=5)


@posix
def test_the_watcher_reports_ready_only_once_it_ignores_sigterm(monkeypatch):
    ready_read, ready_write = os.pipe()
    seen_before_ignore = []

    def ignore(sig, how):
        readable, _, _ = select.select([ready_read], [], [], 0)
        seen_before_ignore.append((sig, how, bool(readable)))

    monkeypatch.setattr(child_supervisor.os, "getpgrp", lambda: os.getpid())
    monkeypatch.setattr(child_supervisor.os, "killpg", lambda pgid, sig: None)
    monkeypatch.setattr(child_supervisor.signal, "signal", ignore)
    pid_read, pid_write = os.pipe()
    os.close(pid_write)                                   # the app went before naming a helper
    try:
        assert child_supervisor.run_watcher(pipe_fd=pid_read, ready_fd=ready_write) == 0
        assert seen_before_ignore == [(signal.SIGTERM, signal.SIG_IGN, False)]
        assert os.read(ready_read, 8) == child_supervisor.WATCHER_READY
    finally:
        for fd in (ready_read, ready_write, pid_read):
            os.close(fd)


@posix
def test_a_helper_that_cannot_start_takes_its_watcher_with_it(monkeypatch, tmp_path):
    _record_spawns(monkeypatch)
    with pytest.raises(FileNotFoundError):
        child_supervisor.spawn_supervised([str(tmp_path / "missing-helper")], env=dict(os.environ),
                                          platform_name="darwin")
    [watcher] = _Recording.instances
    assert watcher.returncode is not None                 # stopped and reaped


@posix
def test_a_watcher_gone_before_it_is_told_the_helper_stops_it(monkeypatch):
    # Nothing would end the helper if the app died: it does not run.
    spawned = _record_spawns(monkeypatch)

    class _Closed:
        def __init__(self, pipe):
            self.pipe = pipe

        def write(self, data):
            raise BrokenPipeError(32, "Broken pipe")

        def close(self):
            self.pipe.close()

    real_init = _Recording.__init__

    def init(self, args, **kwargs):
        real_init(self, args, **kwargs)
        if kwargs.get("process_group") == 0:
            self.stdin = _Closed(self.stdin)

    monkeypatch.setattr(_Recording, "__init__", init)
    with pytest.raises(OSError, match="no crash watcher"):
        child_supervisor.spawn_supervised(["sleep", "30"], env=dict(os.environ),
                                          platform_name="darwin")
    watcher, helper = _Recording.instances
    assert [group for _args, group in spawned] == [0, watcher.pid]
    assert helper.returncode is not None and watcher.returncode is not None   # both reaped


@posix
def test_an_app_killed_before_naming_its_helper_leaves_nothing_running(tmp_path):
    # Sol mac r15: Force Quit between the helper's start and its watcher's
    # used to leave the helper running for good. Now the watcher is already
    # there, and the app dies before it can name the helper.
    app = subprocess.Popen(
        [sys.executable, "-c",
         "import os, signal, subprocess, sys\n"
         f"sys.path.insert(0, {str(child_supervisor.Path(child_supervisor.__file__).parents[2])!r})\n"
         "from wayfinder.utils import child_supervisor as cs\n"
         "class Dying(subprocess.Popen):\n"
         "    def __init__(self, args, **kwargs):\n"
         "        super().__init__(args, **kwargs)\n"
         "        if list(args)[1:2] == ['-c']:   # the helper, not the watcher\n"
         "            print(self.pid, os.getpgid(self.pid), flush=True)\n"
         "            os.kill(os.getpid(), signal.SIGKILL)\n"
         "cs._POPEN = cs.subprocess.Popen = Dying\n"
         "cs.spawn_supervised([sys.executable, '-c', 'import signal, time; "
         "signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)'],"
         " env=dict(os.environ), platform_name='darwin')\n"],
        stdout=subprocess.PIPE, text=True)
    helper, group = map(int, app.stdout.readline().split())
    watcher = group if group != helper else None
    app.wait()
    try:
        assert app.returncode == -signal.SIGKILL
        assert _gone(helper, within=5), "the helper outlived the app"
        assert watcher is not None and _gone(watcher, within=3)
    finally:
        _kill_quietly(helper, *([watcher] if watcher else []))


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
@pytest.mark.parametrize("reaped", ["helper", "watcher"])
def test_the_group_keeps_its_id_while_either_member_lives(tmp_path, reaped):
    # Sol mac r14: the id signalled must be the helper's group even after one
    # of its two members is gone and reaped. The other keeps it: no process
    # can be given that number, nor make a group of it, meanwhile.
    proc = child_supervisor.spawn_supervised(["sleep", "30"], env=dict(os.environ),
                                             platform_name="darwin")
    watcher, gid = proc._aura_watcher, proc._aura_gid
    gone, kept = (proc, watcher) if reaped == "helper" else (watcher, proc)
    try:
        gone.kill()
        gone.wait()
        os.killpg(gid, 0)                               # still there: the other member
        assert os.getpgid(kept.pid) == gid
    finally:
        child_supervisor.stop_supervised(proc, term_wait=1, platform_name="darwin")
    assert watcher.wait(timeout=3) is not None


@posix
@pytest.mark.parametrize("case", ["app-died", "helper-left", "app-died-after-it-left",
                                  "app-died-before-naming-it", "released", "released-with-its-name"])
def test_the_watcher_signals_its_group_by_zero_never_by_number(monkeypatch, case):
    # Sol mac r14: once a member is reaped by launchd its number can be
    # handed out again; the watcher signals its own group (killpg 0) instead.
    # Sol mac r15: an app gone before naming the helper ends the group too.
    sent, ignored = [], []
    helper = subprocess.Popen(["sleep", "30"] if case in ("app-died", "released") else ["true"])
    if case not in ("app-died", "released"):
        helper.wait()                                   # gone and reaped
    monkeypatch.setattr(child_supervisor.os, "getpgrp", lambda: os.getpid())   # it leads
    monkeypatch.setattr(child_supervisor.os, "killpg", lambda pgid, sig: sent.append((pgid, sig)))
    monkeypatch.setattr(child_supervisor.signal, "signal", lambda sig, how: ignored.append(sig))
    read_end, write_end = os.pipe()
    if case != "app-died-before-naming-it":
        os.write(write_end, b"%d\n" % helper.pid)
    if case.startswith("released"):
        os.write(write_end, b"r")
    if case != "helper-left":
        os.close(write_end)                             # the app died, or let go
    try:
        assert child_supervisor.run_watcher(pipe_fd=read_end, term_wait=0.2) == 0
    finally:
        os.close(read_end)
        if case == "helper-left":
            os.close(write_end)
        helper.kill()
        helper.wait()
    assert ignored == [signal.SIGTERM]
    expected = {"app-died": [(0, signal.SIGTERM), (0, signal.SIGKILL)],
                "released": [], "released-with-its-name": []}.get(case, [(0, signal.SIGKILL)])
    assert sent == expected


@posix
@pytest.mark.parametrize("line", [b"0\n", b"1\n", b"-5\n", b"x\n"])
def test_a_watcher_told_no_real_helper_ends_its_group(monkeypatch, line):
    sent = []
    monkeypatch.setattr(child_supervisor.os, "getpgrp", lambda: os.getpid())
    monkeypatch.setattr(child_supervisor.os, "killpg", lambda pgid, sig: sent.append((pgid, sig)))
    monkeypatch.setattr(child_supervisor.signal, "signal", lambda sig, how: None)
    read_end, write_end = os.pipe()
    os.write(write_end, line)
    try:
        assert child_supervisor.run_watcher(pipe_fd=read_end) == 0
    finally:
        os.close(read_end)
        os.close(write_end)
    assert sent == [(0, signal.SIGKILL)]


@posix
def test_a_watcher_that_does_not_lead_its_group_signals_nothing(monkeypatch):
    sent = []
    monkeypatch.setattr(child_supervisor.os, "getpgrp", lambda: os.getpid() + 1)
    monkeypatch.setattr(child_supervisor.os, "killpg", lambda pgid, sig: sent.append((pgid, sig)))
    read_end, write_end = os.pipe()
    os.close(write_end)
    try:
        assert child_supervisor.run_watcher(pipe_fd=read_end) == 2
    finally:
        os.close(read_end)
    assert sent == []


@posix
def test_a_reaped_helpers_group_is_signalled_only_while_its_watcher_holds_it(monkeypatch):
    # Sol mac r14: poll() reaping a helper that left on its own made every
    # later group signal refuse, and what it forked ran on. The watcher's
    # membership still proves the group; released, nothing does.
    sent = []
    monkeypatch.setattr(child_supervisor.os, "killpg", lambda pid, sig: sent.append((pid, sig)))
    proc = child_supervisor.spawn_supervised(["true"], env=dict(os.environ), platform_name="darwin")
    proc.wait()
    child_supervisor.signal_supervised(proc, kill=True, platform_name="darwin")
    assert sent == [(proc._aura_gid, signal.SIGKILL)]
    child_supervisor.release_supervised(proc)          # sweeps, then lets go
    assert sent == [(proc._aura_gid, signal.SIGKILL)] * 2 and proc._aura_watcher is None
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
        assert sent == [(proc._aura_gid, signal.SIGKILL)]
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
        assert isinstance(proc._aura_watcher, subprocess.Popen)
        assert proc._aura_group is True
        assert os.getpgid(proc.pid) == proc._aura_gid == proc._aura_watcher.pid
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
def test_a_watcher_whose_app_died_waits_only_for_the_helper_to_go(monkeypatch):
    sent = []
    helper = subprocess.Popen(["sleep", "30"])

    def killpg(pgid, sig):
        sent.append((pgid, sig))
        if sig == signal.SIGTERM:
            helper.terminate()                          # it heeds the group's SIGTERM

    monkeypatch.setattr(child_supervisor.os, "getpgrp", lambda: os.getpid())
    monkeypatch.setattr(child_supervisor.os, "killpg", killpg)
    monkeypatch.setattr(child_supervisor.signal, "signal", lambda sig, how: None)
    read_end, write_end = os.pipe()
    os.write(write_end, b"%d\n" % helper.pid)
    os.close(write_end)
    try:
        started = time.monotonic()
        assert child_supervisor.run_watcher(pipe_fd=read_end, term_wait=5) == 0
        assert time.monotonic() - started < 2
    finally:
        os.close(read_end)
        helper.kill()
        helper.wait()
    assert sent == [(0, signal.SIGTERM), (0, signal.SIGKILL)]


def test_a_watcher_started_outside_the_protocol_refuses(monkeypatch):
    watched = []
    monkeypatch.setattr(child_supervisor, "run_watcher", lambda **kwargs: watched.append(kwargs) or 0)
    monkeypatch.delenv(child_supervisor.WATCHER_ENV, raising=False)
    assert child_supervisor.run_from_env() == 2 and watched == []
    monkeypatch.setenv(child_supervisor.WATCHER_ENV, "1")
    assert child_supervisor.run_from_env() == 0 and watched == [{"ready_fd": 1}]


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
