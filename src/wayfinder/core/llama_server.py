"""Resident llama.cpp server for post-processing cleanup.

Instead of spawning llama-simple per dictation (which reloads the whole model
every time — ~0.6s for a 4B on GPU, ~2.5s for caricature), this keeps
llama-server alive as a local HTTP service. The model is paid for once.

MEASURED on Qwen3-4B/Vulkan: warm request 0.147s vs 0.61s one-shot (4.1x). The
generation itself is ~0.09s of that, so the resident path is close to the floor
and the remaining cost is prompt eval plus loopback HTTP.

Two things the one-shot llama-simple path cannot do, both of which this fixes:

  * SAMPLING CONTROL. llama-simple exposes no sampling flags, so caricature ran
    greedy and fell into degenerate loops ("level 1000000...") that burned the
    whole token budget. repeat_penalty makes those stop.
  * AN EXACT TERMINATION SIGNAL. The response carries stop_type ("eos" | "limit"
    | "word"), so hitting the cap is observable rather than inferred from
    comparing an N-token and a 2N-token generation.

Modeled deliberately on WhisperServerBackend (transcriber.py): same class-level
single process, same bounded log drain, same honest-unknown adoption rule, same
disable-after-failure fallback to the per-call CLI.
"""
import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import urllib.error
import urllib.request
import weakref
from collections import deque
from pathlib import Path
from typing import Optional

from wayfinder.utils.child_supervisor import release_supervised, signal_supervised, spawn_supervised
from wayfinder.utils.hostexec import bundle_binary_env
from wayfinder.utils.loopback_http import urlopen_loopback


DEFAULT_PORT = 8179  # whisper-server owns 8178
_PORT_SCAN = 5


class LlamaServerError(RuntimeError):
    """Server could not be started or did not answer."""


# The config key a snapshot stamps LlamaServerManager.load_epoch() under
# (see transcriber.LOAD_EPOCH_KEY).
LOAD_EPOCH_KEY = "_llama_load_epoch"


class LlamaServerSuperseded(LlamaServerError):
    """A start cancelled by a CPU/GPU switch, model change or shutdown.

    Not a server failure: callers fall back without counting it, so cancelled
    starts never add up to a restart that kills the new mode's server.
    """


def resolve_server_binary(llama_binary: str, use_gpu: bool = True) -> Optional[str]:
    """Find llama-server next to whatever llama binary is configured.

    The config points at llama-simple/llama-cli; the server lives in the same
    build directory. When GPU is off, prefer the CPU-only twin: a Vulkan-linked
    binary can SIGSEGV during ggml device enumeration on broken drivers before
    any -ngl 0 flag is honored (the same failure whisper-server hits on the
    Deck's RDNA2 in-sandbox, which is why a separate CPU binary is shipped).
    """
    if not llama_binary:
        return None
    bin_dir = Path(os.path.expanduser(llama_binary)).parent
    names = ("llama-server-cpu", "llama-server") if not use_gpu else \
            ("llama-server", "llama-server-cpu")
    for name in names:
        cand = bin_dir / name
        if cand.exists():
            return str(cand)
    return None


class LlamaServerManager:
    """Owns the lifecycle of one resident llama-server process.

    All state is class-level: get_backend() builds a fresh backend object per
    dictation, so per-instance state would start a new server every time.
    """

    _process: Optional[subprocess.Popen] = None
    # A child that has been spawned but not yet published. It lives here, not
    # only in ensure()'s frame, so shutdown() can reach it: ensure() holds the
    # lock for its whole readiness wait (up to STARTUP_TIMEOUT), and shutdown()
    # gives up on the lock after 5s. Quit inside that gap used to leave a
    # multi-GB llama-server running with nobody holding a handle to it, because
    # the app calls os._exit(0) and the startup thread never reached its epoch
    # guard. (PR_SET_PDEATHSIG is the obvious reflex here and is WRONG: it fires
    # on the death of the parent THREAD, and we spawn from short-lived worker
    # threads, so it would kill the server moments after a successful start.)
    _starting: Optional[subprocess.Popen] = None
    _port: int = 0
    _lock = threading.Lock()
    # Identity of the RUNNING server. Every field is fixed at spawn time, so a
    # change in any of them must miss reuse and respawn rather than silently
    # serve requests from a server configured for something else.
    # What was ASKED for. Reuse is decided against this, never against what was
    # actually spawned: a GPU request that legitimately degraded to a CPU rung
    # recorded n_gpu_layers=0, so the next identical request saw a "mismatch",
    # killed the healthy CPU server and retried the broken GPU ladder — on a
    # broken-Vulkan machine that reloaded the model on EVERY dictation and made
    # the startup warm-up worthless.
    _requested: Optional[tuple] = None
    # What was actually spawned. Drives the server-GPU/server-CPU label only.
    _identity: Optional[tuple] = None
    # Bumped by every shutdown. A startup that finishes after a shutdown began
    # must not publish its child, or Quit leaves a multi-GB process behind.
    _epoch: int = 0
    # The switch the published server's configuration was validated under,
    # by the start that published it or by the latest request that reused it
    # (see supersede() and shutdown(before_epoch=...)).
    _process_switch: int = 0
    # Bumped only by supersede(), never by a stop: the switch a configuration
    # was read under (see load_epoch()). _epoch also moves on every internal
    # stop, so it cannot say whether a configuration is still current.
    _switch_epoch: int = 0
    _log_tail: Optional[deque] = None
    _log_thread: Optional[threading.Thread] = None
    # Set when every startup ladder rung failed. The caller then goes straight
    # to the per-call CLI instead of paying a failed startup on every dictation.
    _disabled: bool = False
    # The switch the failed start was configured under (None: unscoped). The
    # failure belongs to that mode: after a switch the new mode gets its own
    # try, and a start the switch cancelled cannot disable the mode after it.
    _disabled_switch: Optional[int] = None
    # Per thread: the child the last ensure() on this thread handed out, set
    # under the lock, so a failure is pinned to the child that request reached
    # even if another thread replaces it before the caller reads it.
    _served = threading.local()
    # Set by close() (Quit, exit): no start is admitted after it, so a request
    # that read its settings just before cannot leave a child behind.
    _closed: bool = False
    _atexit_registered: bool = False
    # Consecutive request failures that were NOT timeouts. A server that stays
    # alive but always answers HTTP 500 / malformed JSON passes the poll()+
    # identity reuse check forever, so every dictation would pay the failure
    # before falling back. Restarting on the FIRST such error would instead
    # thrash a multi-GB model load on a one-off blip; this bounds both.
    _consecutive_failures: int = 0
    # The child those failures came from: a count belongs to one child, so an
    # old child's failures never bring a restart of its replacement closer.
    _failing_child: Optional[subprocess.Popen] = None
    MAX_CONSECUTIVE_FAILURES = 3
    # macOS and Linux: a fresh random key per spawn, handed to the child through
    # its environment (not argv, which any local process can read with ps). The
    # server accepts CORS from every origin, so without a key any web page could
    # drive the resident model over loopback. /health stays public.
    _api_key: Optional[str] = None
    # Each child's own key, so a request keeps using the key of the child it
    # reached: a replacement on the same port then refuses it (HTTP 401)
    # instead of answering it.
    _child_keys: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()

    STARTUP_TIMEOUT = 90.0   # cold load of a 4B GGUF from disk
    # A WARM server answers in ~0.15s (GPU) and ~3.6s (CPU caricature, measured).
    # 30s is therefore already pathological, and waiting the config's 60s CLI
    # timeout on a wedged-but-listening server would freeze a dictation for a
    # minute. On timeout the caller restarts the server and falls back to the
    # CLI, which is the recovery WhisperServerBackend gets from its deadline.
    REQUEST_TIMEOUT = 30.0
    # How often a request waiting on _lock re-checks that its configuration
    # is still current, so a switch during the wait cancels it promptly.
    _LOCK_POLL_S = 0.25

    # ---------------- identity / health ----------------

    @staticmethod
    def _identity_of(binary: str, model_path: str, n_ctx: int, n_threads: int,
                     n_gpu_layers: int) -> tuple:
        """Identity of a running server.

        n_gpu_layers is the LAYER COUNT, not a bool: collapsing it lost partial
        offload, so a deliberate 10-layer setting became "all layers" and could
        reproduce the exact VRAM exhaustion the setting exists to avoid. It also
        means a 10-layer and a 99-layer server are correctly different servers.
        """
        return (binary, model_path, int(n_ctx), int(n_threads), int(n_gpu_layers))

    @classmethod
    def _alive(cls) -> bool:
        return cls._process is not None and cls._process.poll() is None

    @classmethod
    def _auth_headers(cls, child: Optional[subprocess.Popen] = None) -> dict:
        """Authorization for a keyed (macOS/Linux) server; empty on Windows.

        child: use that child's key rather than the published server's."""
        key = cls._api_key if child is None else cls._child_keys.get(child)
        return {"Authorization": f"Bearer {key}"} if key else {}

    @classmethod
    def _props(cls, port: int, timeout: float = 2.0) -> Optional[dict]:
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/props", headers=cls._auth_headers())
            resp = urlopen_loopback(req, timeout=timeout)
            return json.loads(resp.read().decode("utf-8", errors="replace"))
        except Exception:
            return None

    @classmethod
    def _serving_model(cls, port: int, model_path: str, timeout: float = 2.0) -> bool:
        """True when the llama-server on `port` reports exactly this model.

        Used as a post-spawn self-check on OUR OWN child, never to decide whether
        to trust a stranger: /props is unauthenticated, so it can only confirm
        what a process we started says about itself, and cannot establish
        ownership. See _find_port() for why adoption was removed.
        """
        props = cls._props(port, timeout=timeout)
        if not props:
            return False
        served = str(props.get("model_path") or "")
        return bool(served) and os.path.realpath(served) == os.path.realpath(model_path)

    # ---------------- log drain ----------------

    @staticmethod
    def _drain(stream, tail: deque) -> None:
        """Consume server output forever without growing memory.

        Not optional: an unread Popen stdout pipe fills the kernel buffer and
        blocks the server inside write(), which presents as a random hang after
        the app has handled enough dictations.
        """
        try:
            while True:
                line = stream.readline()
                if not line:
                    break
                if not isinstance(line, (bytes, bytearray)):
                    break  # test double violating the binary-pipe contract
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    tail.append(text)
        except (OSError, ValueError):
            pass  # normal at shutdown when the pipe closes underneath us
        finally:
            try:
                stream.close()
            except Exception:
                pass

    @classmethod
    def _start_drain(cls, proc: subprocess.Popen) -> None:
        tail: deque = deque(maxlen=200)
        thread = threading.Thread(
            target=cls._drain, args=(proc.stdout, tail),
            name="wayfinder-llama-server-log", daemon=True)
        cls._log_tail = tail
        cls._log_thread = thread
        thread.start()

    @classmethod
    def spawned_gpu_layers(cls) -> Optional[int]:
        """-ngl the RUNNING server was actually spawned with, or None if unknown.

        Callers must label a request from this, never from what they requested: a
        GPU request that fell to a CPU rung is a CPU server, and reporting it as
        "server-GPU" made the eval matrix unable to establish that a server-gpu
        row really used the GPU.
        """
        ident = cls._identity
        return None if ident is None else ident[4]

    @classmethod
    def log_tail(cls) -> str:
        thread = cls._log_thread
        if thread is not None:
            thread.join(timeout=0.2)  # let it observe EOF on a just-dead child
        return "\n".join(cls._log_tail or ())

    # ---------------- ports ----------------

    @classmethod
    def _find_port(cls) -> Optional[int]:
        """First free loopback port in the scan range, or None if all are taken.

        We deliberately do NOT adopt an existing listener. /props is
        unauthenticated, so "it says it is serving our model path" is a claim any
        local process can make — and adopting it would hand that process every
        subsequent dictation. An occupied port is skipped, never trusted; if the
        whole range is occupied we fail and the caller falls back to the CLI.
        """
        for offset in range(_PORT_SCAN):
            port = DEFAULT_PORT + offset
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                if sock.connect_ex(("127.0.0.1", port)) != 0:
                    return port
            finally:
                sock.close()
        return None

    @staticmethod
    def _listener_inodes(port: int) -> set:
        """Socket inodes LISTENing on `port` on loopback, from /proc/net/tcp{,6}."""
        inodes = set()
        for name in ("tcp", "tcp6"):
            try:
                with open(f"/proc/net/{name}") as fh:
                    next(fh, None)  # header
                    for line in fh:
                        f = line.split()
                        if len(f) < 10 or f[3] != "0A":  # 0A == TCP_LISTEN
                            continue
                        local = f[1]
                        if ":" not in local:
                            continue
                        _addr_hex, port_hex = local.rsplit(":", 1)
                        if int(port_hex, 16) != port:
                            continue
                        # EVERY listener on the port, whatever address it claims.
                        # Filtering to loopback was the bug: a socket bound to
                        # 0.0.0.0 (or dual-stack ::) also answers a connection to
                        # 127.0.0.1, but its hex address is all zeroes, so the
                        # filter skipped it, the scan came back empty, and
                        # ownership read as "cannot determine" -- which proceeds.
                        # A stranger holding the wildcard was therefore invisible
                        # to exactly the check meant to catch it. (The v6 loopback
                        # pattern was wrong too: ::1 strips to "1000000", not "1".)
                        # A listener on some other interface can only add an inode
                        # we do not own, which costs nothing: we require that one
                        # of them IS ours, not that all of them are.
                        inodes.add(f[9])
            except (OSError, ValueError, StopIteration):
                continue
        return inodes

    @classmethod
    def _owns_listener(cls, proc: subprocess.Popen, port: int) -> Optional[bool]:
        """True/False if we could prove ownership; None if /proc could not tell us.

        _find_port() only proves nobody was listening a moment ago. Between that
        probe and llama-server's bind, any local process can take the port, answer
        /health, and echo our model path back from /props -- at which point we
        would hand it every dictation. Since /props is unauthenticated, no reply
        it makes can be trusted; the only real proof is that the kernel says the
        listening socket belongs to the child WE spawned.

        macOS has no /proc; libproc asks the kernel the same question there.
        """
        if sys.platform == "darwin":
            from wayfinder.utils.macos_procinfo import pid_listens_on_tcp
            return pid_listens_on_tcp(proc.pid, port)
        inodes = cls._listener_inodes(port)
        if not inodes:
            return None
        fd_dir = Path(f"/proc/{proc.pid}/fd")
        try:
            entries = list(fd_dir.iterdir())
        except OSError:
            return None
        for fd in entries:
            try:
                target = os.readlink(fd)
            except OSError:
                continue
            if target.startswith("socket:[") and target[8:-1] in inodes:
                return True
        return False

    # ---------------- lifecycle ----------------

    @classmethod
    def _spawn_attempts(cls, binary: str, model_path: str, n_ctx: int,
                        n_threads: int, n_gpu_layers: int, port: int) -> list:
        """Ordered spawn ladder: requested mode first, then CPU rescue.

        Mirrors whisper-server's ladder. A Vulkan build can fail at library init
        on a broken driver, and no flag rescues that — only a different binary
        does, so the CPU twin is the last rung.
        """
        base = [
            "-m", model_path,
            "--host", "127.0.0.1",
            "--port", str(port),
            "-c", str(n_ctx),
            "-t", str(n_threads),
            "--parallel", "1",   # one dictation at a time; keeps the KV cache whole
            "--no-webui",        # no reason to serve a UI from a dictation app
        ]
        if sys.platform != "win32":
            # /slots would expose the cached prompt (the last dictation).
            base.append("--no-slots")
        attempts = [[binary] + base + ["-ngl", str(n_gpu_layers)]]
        if n_gpu_layers != 0:
            attempts.append([binary] + base + ["-ngl", "0"])
            cpu_twin = binary.replace("llama-server", "llama-server-cpu")
            if cpu_twin != binary and Path(cpu_twin).exists():
                attempts.append([cpu_twin] + base + ["-ngl", "0"])
        return attempts

    @classmethod
    def _spawn_env_overrides(cls) -> Optional[dict]:
        """Per-spawn environment. None (inherit unchanged) on Windows.

        macOS and Linux: a new API key per spawn (see _api_key). macOS also
        drops the Metal residency heartbeat, which otherwise wakes an idle
        resident server ~170x/s. The Metal device itself stays registered:
        Free's -ngl 0 still offloads prompt processing to the GPU, which is
        intended.
        """
        if sys.platform == "win32":
            return None
        cls._api_key = secrets.token_urlsafe(24)
        env = {"LLAMA_API_KEY": cls._api_key}
        if sys.platform == "darwin":
            env["GGML_METAL_NO_RESIDENCY"] = "1"
        return env

    @classmethod
    def _wait_ready(cls, proc: subprocess.Popen, port: int, deadline: float,
                    epoch: Optional[int] = None) -> bool:
        """Poll /health until the model is loaded, the child dies, or a shutdown
        or supersede() moves the epoch away from ``epoch``."""
        import time
        while time.monotonic() < deadline:
            if epoch is not None and cls._epoch != epoch:
                return False  # no longer wanted; the caller stops the child
            if proc.poll() is not None:
                return False  # died during load
            try:
                resp = urlopen_loopback(
                    f"http://127.0.0.1:{port}/health", timeout=1.0)
                if resp.status == 200:
                    return True
            except urllib.error.HTTPError as e:
                if e.code != 503:  # 503 == still loading, keep waiting
                    return False
            except Exception:
                pass
            time.sleep(0.25)
        return False

    @classmethod
    def ensure(cls, binary: str, model_path: str, n_ctx: int = 2048,
               n_threads: int = 4, n_gpu_layers: int = 99,
               deadline: Optional[float] = None,
               load_epoch: Optional[int] = None) -> int:
        """Return a port serving `model_path`, starting the server if needed.

        deadline: absolute time.monotonic() budget covering EVERY blocking step —
        lock acquisition and all ladder rungs together. Without it the ladder
        could run 3 attempts x STARTUP_TIMEOUT (~270s) before the CLI fallback
        even began, and the app's 120s PROCESSING watchdog would have already
        bumped the session generation and DISCARDED the eventual output. The
        background warm-up passes None (nobody is waiting); a live dictation
        passes a real budget. Same shape as WhisperServerBackend's deadline.

        load_epoch: the load_epoch() the request's configuration was read
        under. Once a supersede() has moved it, the request is for a mode the
        user has left: it starts nothing and leaves the running server alone.
        """
        import atexit
        import time

        def _left() -> float:
            return float("inf") if deadline is None else deadline - time.monotonic()

        def _superseded() -> bool:
            return load_epoch is not None and cls._switch_epoch != load_epoch

        cls._served.proc = None   # this request has reached no child yet
        # First, before anything that can fail or wait: a request for a mode
        # the user has left is cancelled, never counted as a server failure.
        if _superseded():
            raise LlamaServerSuperseded("configuration superseded by a switch")
        if cls._closed:
            raise LlamaServerSuperseded("shutting down")
        configured = cls._switch_epoch if load_epoch is None else load_epoch
        if cls._disabled and cls._disabled_switch in (None, cls._switch_epoch):
            raise LlamaServerError("llama-server disabled after a previous failure")
        if not binary or not Path(binary).exists():
            raise LlamaServerError(f"llama-server binary not found: {binary or '<empty>'}")
        if not model_path or not Path(model_path).exists():
            raise LlamaServerError(f"model not found: {model_path or '<empty>'}")

        want = cls._identity_of(binary, model_path, n_ctx, n_threads, n_gpu_layers)

        # Bounded lock acquisition: a competing startup can hold this for its
        # whole readiness wait, so a deadline must clamp the wait for it too.
        # Taken in slices: the holder is often the NEW mode's start, and a
        # stale request timing out behind it would count as a server failure
        # (three of those restart the new mode's server).
        while True:
            rem = _left()
            if _superseded():
                raise LlamaServerSuperseded("configuration superseded by a switch")
            if rem <= 0:
                raise LlamaServerError("deadline hit acquiring the server lock")
            if cls._lock.acquire(timeout=min(rem, cls._LOCK_POLL_S)):
                break
        try:
            if _superseded():
                raise LlamaServerSuperseded("configuration superseded by a switch")
            # Again under the lock: a request that passed the entry check
            # before a Quit must not start a child once the Quit's stop ran.
            if cls._closed:
                raise LlamaServerSuperseded("shutting down")
            if cls._alive() and cls._requested == want:
                # Validated for this request's switch, so it is that mode's
                # server now: a teardown from an earlier switch (GPU off then
                # on again before it ran) must not stop it under the request.
                if load_epoch is not None:
                    cls._process_switch = max(cls._process_switch, load_epoch)
                cls._served.proc = cls._process
                return cls._port

            # Stopping a stale server can wait ~10s in _kill() for a child
            # that ignores SIGTERM. A caller whose deadline is already gone
            # must not pay that just to fail the first rung check anyway.
            # (A LIVE deadline still pays the stop if the child stalls — the
            # alternative, clamping _kill(), would leak a multi-GB process.)
            if deadline is not None and _left() <= 0:
                raise LlamaServerError("deadline hit before stopping the old server")
            cls._stop_locked()
            # Captured AFTER our own stop, which bumps the epoch itself —
            # otherwise a startup would always invalidate itself.
            epoch = cls._epoch
            # A supersede() before the capture moved _switch_epoch first; one
            # after it moves _epoch past ours, which every check below sees.
            if _superseded():
                raise LlamaServerSuperseded("configuration superseded by a switch")
            # A close() that ran while our own stop was terminating the old
            # child (its lock wait can run out first) already moved _epoch,
            # so the capture cannot see it; its flag can, since close() sets
            # it before superseding. One landing after the capture moves
            # _epoch past ours, which the checks below see.
            if cls._closed:
                raise LlamaServerSuperseded("shutting down")

            if not cls._atexit_registered:
                atexit.register(cls.close)
                cls._atexit_registered = True

            port = cls._find_port()
            if port is None:
                raise LlamaServerError(
                    f"no free port in {DEFAULT_PORT}-{DEFAULT_PORT + _PORT_SCAN - 1}")

            errors = []
            try:
                for cmd in cls._spawn_attempts(binary, model_path, n_ctx,
                                               n_threads, n_gpu_layers, port):
                    # Re-check before EVERY rung, not just before publishing. A
                    # shutdown partway down the ladder would otherwise keep spawning
                    # fresh children it has already been told to stop wanting.
                    if cls._epoch != epoch:
                        raise LlamaServerSuperseded("shut down while starting")
                    # ...and the caller's deadline binds the LADDER, not just each
                    # rung: checking only per-attempt let a ladder that had already
                    # overrun start yet another multi-GB spawn.
                    if _left() <= 0:
                        raise LlamaServerError("deadline hit before the next rung")
                    try:
                        # macOS: its own group, with a watcher for a Force
                        # Quit (Sol mac r8); a plain Popen elsewhere.
                        proc = spawn_supervised(
                            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            env=bundle_binary_env(cls._spawn_env_overrides()),
                        )
                    except Exception as e:
                        errors.append(f"{Path(cmd[0]).name}: {type(e).__name__}: {e}")
                        continue
                    if cls._api_key is not None:
                        try:
                            cls._child_keys[proc] = cls._api_key
                        except TypeError:   # a handle that takes no weak reference
                            pass
                    # Publish the handle FIRST so a concurrent shutdown can kill it.
                    cls._starting = proc
                    # ...then re-check, because a shutdown could have run in the
                    # window between the epoch check above and this assignment:
                    # it would have bumped the epoch, looked at _starting, found
                    # None, and returned satisfied -- while Popen was still
                    # returning the child it was supposed to kill. Without this
                    # the next epoch test is on the far side of the readiness
                    # wait, and Quit calls os._exit() long before that.
                    if cls._epoch != epoch:
                        cls._starting = None
                        cls._kill(proc)
                        raise LlamaServerSuperseded("shut down while starting")
                    cls._start_drain(proc)
                    # Readiness waits inside the caller's budget, never past it.
                    attempt_deadline = time.monotonic() + cls.STARTUP_TIMEOUT
                    if deadline is not None:
                        attempt_deadline = min(attempt_deadline, deadline)
                    ready = cls._wait_ready(proc, port, attempt_deadline, epoch)
                    if cls._epoch != epoch:
                        # Cancelled mid-load: not a failed rung, so the ladder must
                        # not run on into _disabled.
                        cls._kill(proc)
                        raise LlamaServerSuperseded("shut down while starting")
                    if ready:
                        # Confirm the child actually loaded the file we named. A
                        # mismatch means silently cleaning every dictation with the
                        # wrong model, which is invisible in the output.
                        # Ownership BEFORE identity: _serving_model believes what
                        # the listener says about itself, so it must only ever be
                        # asked of a listener we already proved is ours.
                        # Fail CLOSED on anything short of proof. Proceeding on
                        # "cannot determine" was the hole: _wait_ready only proves
                        # SOMETHING answered /health on this port -- our child may
                        # have failed to bind and exited -- and _serving_model then
                        # believes whatever that something claims about itself. An
                        # unverified listener is exactly what must not be handed a
                        # dictation. Refusing costs the resident server, not
                        # cleanup: ensure() raises and _server_generate falls
                        # through to the CLI path.
                        owned = cls._owns_listener(proc, port)
                        if owned is not True:
                            why = ("is held by another process, not our child"
                                   if owned is False else
                                   "could not be confirmed as our child")
                            errors.append(
                                f"{Path(cmd[0]).name}: the listener on port {port} {why}")
                            try:
                                cls._signal(proc, kill=True)
                                proc.wait(timeout=5)
                                release_supervised(proc)
                            except Exception:
                                pass
                            continue
                        if not cls._serving_model(port, model_path):
                            errors.append(f"{Path(cmd[0]).name}: serving an unexpected model")
                            try:
                                cls._signal(proc, kill=True)
                                proc.wait(timeout=5)
                                release_supervised(proc)
                            except Exception:
                                pass
                            continue
                        # A shutdown that began while we were loading wins. Publishing
                        # here anyway is how Quit-during-warm-up orphaned a multi-GB
                        # child: shutdown had already returned, having seen no process.
                        if cls._epoch != epoch:
                            cls._kill(proc)
                            raise LlamaServerSuperseded("shut down while starting")
                        cls._process_switch = configured
                        cls._process = proc
                        cls._port = port
                        # Reuse keys off the REQUEST; the label keys off the spawn.
                        # Recording only the spawned value made a healthy CPU-rescue
                        # server look like a mismatch to the next identical request.
                        cls._requested = want
                        # Read the -ngl VALUE, never `"99" in cmd`, which scans the
                        # whole argv and would let n_threads=99 mark a CPU server GPU.
                        spawned_ngl = int(cmd[cmd.index("-ngl") + 1])
                        cls._identity = cls._identity_of(
                            cmd[0], model_path, n_ctx, n_threads, spawned_ngl)
                        cls._served.proc = proc
                        return port
                    errors.append(f"{Path(cmd[0]).name}: not ready "
                                  f"(rc={proc.poll()}) {cls.log_tail()[-300:]}")
                    try:
                        cls._signal(proc, kill=True)
                        proc.wait(timeout=5)
                        release_supervised(proc)
                    except Exception:
                        pass

                # A switch or shutdown during the last rung's reap (or behind
                # spawn errors) is a cancellation, not a machine that cannot
                # run the server.
                if cls._epoch != epoch or _superseded():
                    raise LlamaServerSuperseded("shut down while starting")
                cls._disabled = True
                cls._disabled_switch = configured
                raise LlamaServerError("every llama-server start attempt failed: "
                                       + " | ".join(errors))
            finally:
                # Whatever happened, this frame no longer owns a child: it was
                # either published into _process or killed by one of the paths
                # above. Leaving a stale handle would let a LATER shutdown kill
                # a healthy, published server.
                cls._starting = None
        finally:
            cls._lock.release()

    @classmethod
    def still_ours(cls, port: int, child: Optional[subprocess.Popen] = None) -> bool:
        """Re-prove, right before we hand over a prompt, that the port is ours.

        Startup-time proof is not enough: the verified child can exit after
        /props, and the completion request travels over a NEW connection, so a
        process that binds the freed port in between would receive the prompt.
        Ownership is a property of the moment you use it, not of the moment you
        checked it — so it is checked again per request. Cost is one read of
        /proc/net/tcp plus a directory scan, against a request that takes ~1s.

        child: the child the request reached (serving()); it must still be the
        published server, so a replacement on the same port is never mistaken
        for it.
        """
        proc = cls._process
        if proc is None or proc.poll() is not None:
            return False
        if child is not None and proc is not child:
            return False
        return cls._owns_listener(proc, port) is True

    @classmethod
    def complete(cls, prompt: str, n_predict: int, temperature: float = 0.1,
                 repeat_penalty: float = 1.1, repeat_last_n: int = 64,
                 top_p: float = 0.95, seed: Optional[int] = None,
                 cache_prompt: bool = True, stop: Optional[list] = None,
                 timeout: Optional[float] = None, port: Optional[int] = None,
                 child: Optional[subprocess.Popen] = None) -> dict:
        """POST /completion and return the parsed response.

        child: send that child's key (see _child_keys), so the request can be
        answered only by the child it was bound to.

        The response carries `content` ALREADY STRIPPED of the prompt, so the
        CLI extractor's prompt-splitting is unnecessary here — but control-atom
        stripping still is, since a model can emit them mid-answer.
        """
        payload = {
            "prompt": prompt,
            "n_predict": int(n_predict),
            "temperature": float(temperature),
            "repeat_penalty": float(repeat_penalty),
            "repeat_last_n": int(repeat_last_n),
            "top_p": float(top_p),
            "cache_prompt": bool(cache_prompt),
            "stream": False,
        }
        if seed is not None:
            payload["seed"] = int(seed)
        if stop:
            payload["stop"] = list(stop)

        use_port = cls._port if port is None else port
        req = urllib.request.Request(
            f"http://127.0.0.1:{use_port}/completion",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", **cls._auth_headers(child)},
        )
        resp = urlopen_loopback(
            req, timeout=cls.REQUEST_TIMEOUT if timeout is None else timeout)
        return json.loads(resp.read().decode("utf-8", errors="replace"))

    # ---------------- shutdown ----------------

    @classmethod
    def _stop_locked(cls) -> None:
        """Stop the server. Caller MUST hold _lock (or be at interpreter exit)."""
        proc = cls._process
        cls._process = None
        cls._identity = None
        cls._requested = None
        cls._port = 0
        cls._epoch += 1
        cls._kill(proc)
        # An unpublished child from an in-flight ensure() is just as expensive to
        # leak as a published one. The epoch bump above tells that thread to stop,
        # but it cannot act on it if the interpreter exits first.
        starting, cls._starting = cls._starting, None
        cls._kill(starting)

    @classmethod
    def note_success(cls) -> None:
        cls._consecutive_failures = 0
        cls._failing_child = None

    @classmethod
    def note_failure(cls, failed: Optional[subprocess.Popen] = None) -> bool:
        """Record a non-timeout request failure. True when a restart was begun.

        failed: the child the request reached (serving()), None if it never
        reached one; the restart stops only that child (see restart()).
        The restart runs on its own daemon thread: stopping can wait ~5s for
        the lock plus ~10s terminating a wedged child, and note_failure() is
        called from inside a dictation's cleanup budget."""
        if failed is not cls._failing_child:
            cls._consecutive_failures = 0
            cls._failing_child = failed
        cls._consecutive_failures += 1
        if cls._consecutive_failures >= cls.MAX_CONSECUTIVE_FAILURES:
            cls._consecutive_failures = 0
            cls._failing_child = None
            threading.Thread(
                target=cls.restart, args=(failed,), daemon=True,
                name="llama-server-failure-restart",
            ).start()
            return True
        return False

    @classmethod
    def serving(cls) -> Optional[subprocess.Popen]:
        """The child the last successful ensure() on this thread handed out."""
        return getattr(cls._served, "proc", None)

    @classmethod
    def replaced(cls, child: Optional[subprocess.Popen]) -> bool:
        """Whether `child` was stopped or replaced since a request reached it.

        Its request's failure is then the stop's doing, not the server's: it
        counts toward no restart, and the replacement is left alone.
        """
        return child is not None and cls._process is not child

    @classmethod
    def restart(cls, failed: Optional[subprocess.Popen]) -> None:
        """Stop `failed` if it is still the published server; the next request
        starts a fresh one.

        For the failure and timeout restarts, which run later on their own
        thread: by then a switch may have published a replacement, which is
        not the server that failed. Leaves _switch_epoch alone, so pending
        configurations stay current. If the lock is held, a start owns the
        state and has already stopped or replaced this child itself.
        """
        if failed is None or not cls._lock.acquire(timeout=5):
            return
        try:
            if cls._process is failed:
                cls._stop_locked()
        finally:
            cls._lock.release()

    @classmethod
    def supersede(cls) -> int:
        """Cancel any startup in flight without waiting; returns the new switch.

        Lock-free, for the Tk thread. Pass the result to
        shutdown(before_epoch=...) to stop only a server whose configuration
        was last validated before it.
        """
        # _switch_epoch first: see the ordering note in ensure().
        cls._switch_epoch += 1
        switch = cls._switch_epoch
        cls._epoch += 1
        return switch

    @classmethod
    def load_epoch(cls) -> int:
        """The epoch to read a configuration under; pass it to ensure()."""
        return cls._switch_epoch

    @classmethod
    def shutdown(cls, before_epoch: Optional[int] = None) -> None:
        """Stop the resident server (atexit, GPU toggle, model switch).

        before_epoch: stop it only if its configuration was last validated
        (started, or reused by a request) before that supersede(), so a server
        a request in the new mode started or reused survives the old mode's
        cleanup. A startup holding the lock then either predates the supersede
        (it stops its own child on seeing the new epoch) or follows it (new
        mode), so neither needs us.

        Bounded lock acquisition, because a startup can hold _lock for its whole
        readiness wait and atexit must not hang the app on exit. If the lock is
        NOT acquired we still terminate the child — leaking a process holding
        several GB is worse than a racing spawn — but we do not clear the shared
        state, because another thread inside ensure() owns it and would then
        write its own results over ours. That thread's own _stop_locked/identity
        write leaves the state consistent.
        """
        acquired = cls._lock.acquire(timeout=5)
        try:
            if before_epoch is not None:
                if acquired and cls._process is not None and cls._process_switch < before_epoch:
                    cls._stop_locked()
            elif acquired:
                cls._stop_locked()
            else:
                # Could not take the lock: another thread owns the shared state,
                # so do not write it — but DO bump the epoch, which is what stops
                # that thread publishing a child after we were asked to stop.
                cls._epoch += 1
                proc, cls._process = cls._process, None
                cls._kill(proc)
                # The whole reason this branch exists: ensure() holds the lock for
                # its entire readiness wait, so the child we most need to kill on a
                # Quit is precisely the one it has not published yet.
                starting, cls._starting = cls._starting, None
                cls._kill(starting)
        finally:
            if acquired:
                cls._lock.release()

    @classmethod
    def close(cls) -> None:
        """Terminal stop for Quit and exit: refuse every later start, then
        supersede and stop everything, a start in flight too."""
        cls._closed = True   # before the supersede: see ensure()
        cls.supersede()
        # The per-call CLI (llama-simple, its GPU probe) too: a cleanup that
        # fell back just before this must not outlive the app.
        from wayfinder.utils.child_supervisor import close_tracked
        close_tracked()
        cls.shutdown()

    @classmethod
    def reopen(cls) -> None:
        """Admit starts again after a close() that did not exit."""
        from wayfinder.utils.child_supervisor import reopen_tracked
        cls._closed = False
        reopen_tracked()

    @staticmethod
    def _kill(proc: Optional[subprocess.Popen]) -> None:
        """Terminate, then kill, a child that ignores SIGTERM."""
        if proc is None:
            return
        try:
            if proc.poll() is None:
                LlamaServerManager._signal(proc)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    LlamaServerManager._signal(proc, kill=True)
                    proc.wait(timeout=5)
            release_supervised(proc)
        except Exception:
            pass

    @staticmethod
    def _signal(proc: subprocess.Popen, kill: bool = False) -> None:
        """SIGTERM or SIGKILL: on macOS to its whole group (spawn_supervised)."""
        if getattr(proc, "_aura_group", False) is True:
            signal_supervised(proc, kill=kill)
        elif kill:
            proc.kill()
        else:
            proc.terminate()

    @classmethod
    def reset_for_tests(cls) -> None:
        cls.shutdown()
        cls._disabled = False
        cls._disabled_switch = None
        cls._served = threading.local()
        cls._closed = False
        cls._requested = None
        cls._consecutive_failures = 0
        cls._failing_child = None
        cls._log_tail = None
        cls._log_thread = None
