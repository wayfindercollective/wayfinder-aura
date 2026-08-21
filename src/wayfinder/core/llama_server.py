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
import socket
import subprocess
import threading
import urllib.error
import urllib.request
from collections import deque
from pathlib import Path
from typing import Optional

from wayfinder.utils.hostexec import bundle_binary_env


DEFAULT_PORT = 8179  # whisper-server owns 8178
_PORT_SCAN = 5


class LlamaServerError(RuntimeError):
    """Server could not be started or did not answer."""


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
    _port: int = 0
    _lock = threading.Lock()
    # Identity of the RUNNING server. Every field is fixed at spawn time, so a
    # change in any of them must miss reuse and respawn rather than silently
    # serve requests from a server configured for something else.
    _identity: Optional[tuple] = None
    _log_tail: Optional[deque] = None
    _log_thread: Optional[threading.Thread] = None
    # Set when every startup ladder rung failed. The caller then goes straight
    # to the per-call CLI instead of paying a failed startup on every dictation.
    _disabled: bool = False
    _atexit_registered: bool = False

    STARTUP_TIMEOUT = 90.0   # cold load of a 4B GGUF from disk
    # A WARM server answers in ~0.15s (GPU) and ~3.6s (CPU caricature, measured).
    # 30s is therefore already pathological, and waiting the config's 60s CLI
    # timeout on a wedged-but-listening server would freeze a dictation for a
    # minute. On timeout the caller restarts the server and falls back to the
    # CLI, which is the recovery WhisperServerBackend gets from its deadline.
    REQUEST_TIMEOUT = 30.0

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

    @staticmethod
    def _props(port: int, timeout: float = 2.0) -> Optional[dict]:
        try:
            resp = urllib.request.urlopen(
                f"http://127.0.0.1:{port}/props", timeout=timeout)
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
        attempts = [[binary] + base + ["-ngl", str(n_gpu_layers)]]
        if n_gpu_layers != 0:
            attempts.append([binary] + base + ["-ngl", "0"])
            cpu_twin = binary.replace("llama-server", "llama-server-cpu")
            if cpu_twin != binary and Path(cpu_twin).exists():
                attempts.append([cpu_twin] + base + ["-ngl", "0"])
        return attempts

    @classmethod
    def _wait_ready(cls, proc: subprocess.Popen, port: int, deadline: float) -> bool:
        """Poll /health until the model is loaded, or the child dies."""
        import time
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                return False  # died during load
            try:
                resp = urllib.request.urlopen(
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
               n_threads: int = 4, n_gpu_layers: int = 99) -> int:
        """Return a port serving `model_path`, starting the server if needed."""
        import atexit
        import time

        if cls._disabled:
            raise LlamaServerError("llama-server disabled after a previous failure")
        if not binary or not Path(binary).exists():
            raise LlamaServerError(f"llama-server binary not found: {binary or '<empty>'}")
        if not model_path or not Path(model_path).exists():
            raise LlamaServerError(f"model not found: {model_path or '<empty>'}")

        want = cls._identity_of(binary, model_path, n_ctx, n_threads, n_gpu_layers)

        with cls._lock:
            if cls._alive() and cls._identity == want:
                return cls._port

            cls._stop_locked()

            if not cls._atexit_registered:
                atexit.register(cls.shutdown)
                cls._atexit_registered = True

            port = cls._find_port()
            if port is None:
                raise LlamaServerError(
                    f"no free port in {DEFAULT_PORT}-{DEFAULT_PORT + _PORT_SCAN - 1}")

            errors = []
            for cmd in cls._spawn_attempts(binary, model_path, n_ctx,
                                           n_threads, n_gpu_layers, port):
                try:
                    proc = subprocess.Popen(
                        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        env=bundle_binary_env(),
                    )
                except Exception as e:
                    errors.append(f"{Path(cmd[0]).name}: {type(e).__name__}: {e}")
                    continue
                cls._start_drain(proc)
                if cls._wait_ready(proc, port, time.monotonic() + cls.STARTUP_TIMEOUT):
                    # Confirm the child actually loaded the file we named. A
                    # mismatch means silently cleaning every dictation with the
                    # wrong model, which is invisible in the output.
                    if not cls._serving_model(port, model_path):
                        errors.append(f"{Path(cmd[0]).name}: serving an unexpected model")
                        try:
                            proc.kill()
                            proc.wait(timeout=5)
                        except Exception:
                            pass
                        continue
                    cls._process = proc
                    cls._port = port
                    # Identity records the mode ACTUALLY spawned, not the one
                    # requested: a GPU request that fell to a CPU rung must not
                    # claim GPU, or the next GPU request reuses a CPU server.
                    # Record the layer count ACTUALLY spawned, read from the
                    # -ngl argument (never `"99" in cmd`, which scans the whole
                    # argv and would let n_threads=99 mark a CPU server as GPU).
                    # A GPU request that fell to a CPU rung records 0, so the
                    # next GPU request misses reuse and retries GPU.
                    spawned_ngl = int(cmd[cmd.index("-ngl") + 1])
                    cls._identity = cls._identity_of(
                        cmd[0], model_path, n_ctx, n_threads, spawned_ngl)
                    return port
                errors.append(f"{Path(cmd[0]).name}: not ready "
                              f"(rc={proc.poll()}) {cls.log_tail()[-300:]}")
                try:
                    proc.kill()
                    proc.wait(timeout=5)
                except Exception:
                    pass

            cls._disabled = True
            raise LlamaServerError("every llama-server start attempt failed: "
                                   + " | ".join(errors))

    @classmethod
    def complete(cls, prompt: str, n_predict: int, temperature: float = 0.1,
                 repeat_penalty: float = 1.1, repeat_last_n: int = 64,
                 top_p: float = 0.95, seed: Optional[int] = None,
                 cache_prompt: bool = True, stop: Optional[list] = None,
                 timeout: Optional[float] = None, port: Optional[int] = None) -> dict:
        """POST /completion and return the parsed response.

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
            headers={"Content-Type": "application/json"},
        )
        resp = urllib.request.urlopen(
            req, timeout=cls.REQUEST_TIMEOUT if timeout is None else timeout)
        return json.loads(resp.read().decode("utf-8", errors="replace"))

    # ---------------- shutdown ----------------

    @classmethod
    def _stop_locked(cls) -> None:
        """Stop the server. Caller MUST hold _lock (or be at interpreter exit)."""
        proc = cls._process
        cls._process = None
        cls._identity = None
        cls._port = 0
        cls._kill(proc)

    @classmethod
    def shutdown(cls) -> None:
        """Stop the resident server (atexit, GPU toggle, model switch).

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
            if acquired:
                cls._stop_locked()
            else:
                proc, cls._process = cls._process, None
                cls._kill(proc)
        finally:
            if acquired:
                cls._lock.release()

    @staticmethod
    def _kill(proc: Optional[subprocess.Popen]) -> None:
        """Terminate, then kill, a child that ignores SIGTERM."""
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        except Exception:
            pass

    @classmethod
    def reset_for_tests(cls) -> None:
        cls.shutdown()
        cls._disabled = False
        cls._log_tail = None
        cls._log_thread = None
