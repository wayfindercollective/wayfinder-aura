"""Prefer Core ML only after a local, accuracy-checked speed comparison."""
from __future__ import annotations

import json
import hashlib
import os
import platform
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path

TURBO_CHECKPOINT_SHA256 = "aff26ae408abcba5fbf8813c21e62b0941638c5f6eebfb145be0c9839262a19a"
COREML_MANIFEST = "aura-coreml.json"
# Loading the encoder hands it to Apple's ANECompilerService, a system daemon
# that keeps compiling after our probe exits and then discards the orphaned
# result. A cold compile takes ~125 s on an idle M1 and ~250 s under load; once
# one load completes, later loads take under a second. So preparation waits for
# a quiet spell, gives the device enough time to finish, and failed attempts
# back off persistently instead of repeating the compile after every dictation.
QUIET_BEFORE_PREPARATION = 300.0  # seconds without dictation before any work starts
METAL_READY_TIMEOUT = 120.0
COREML_READY_TIMEOUT = 600.0
# Wait before attempt n+1 once n attempts have failed, for the same model,
# binaries, chip and OS. After the last entry, Metal stays selected for good.
RETRY_AFTER_SECONDS = (0.0, 24 * 3600.0, 7 * 24 * 3600.0)
_READY_POLL_SECONDS = 0.5
_prepare_lock = threading.Lock()
_transition_lock = threading.RLock()
_dictation_active = threading.Event()
_activity_changed = threading.Event()  # wakes the worker's quiet-spell wait
_idle_since = time.monotonic()  # None while dictating
_dictations = 0  # dictations started so far; a pass yields if this moves
_children = set()  # live preparation processes; a starting dictation stops them
_worker_lock = threading.Lock()
_preparation_requested = threading.Event()
_worker = None
_latest_config = None
_SETTINGS_KEYS = ("model_path", "use_gpu", "whisper_coreml_encoder", "transcription_backend", "threads",
                  "beam_size", "best_of", "language", "prompt", "custom_vocabulary")


def set_dictation_active(active: bool) -> None:
    """Main-thread seam: arbitration takes milliseconds, never model loading.

    Starting a dictation signals every live preparation child (compiler, probe
    servers) at once, so none keeps loading the CPU while the microphone runs.
    """
    global _idle_since, _dictations
    with _transition_lock:
        if active:
            if not _dictation_active.is_set():
                _dictations += 1
            _dictation_active.set()
            _idle_since = None
            for proc in list(_children):
                _signal_stop(proc)
        elif _dictation_active.is_set() or _idle_since is None:
            _dictation_active.clear()
            _idle_since = time.monotonic()
    _activity_changed.set()


def _signal_stop(proc) -> None:
    """Non-blocking SIGTERM; the supervisor forwards it to the native child."""
    try:
        if proc.poll() is None:
            proc.terminate()
    except OSError:
        pass


def encoder_path(model_path: str) -> Path:
    """Mirror whisper.cpp's quantization-suffix stripping for sidecar lookup."""
    model = Path(os.path.expanduser(str(model_path)))
    stem = re.sub(r"-q\d_\d$", "", model.stem)
    return model.with_name(stem + "-encoder.mlmodelc")


def _conversion_root() -> Path:
    from wayfinder.utils.platform import get_cache_dir
    return get_cache_dir() / "coreml-models"


def converted_model_path(model_path: str) -> Path:
    """Separate precision/revision caches; never rewrite the user's download."""
    from wayfinder.utils.macos_coreml_convert import TEMPLATE_VERSION, model_identity
    model = Path(os.path.expanduser(model_path)).resolve()
    key = hashlib.sha256(json.dumps([str(model), model_identity(model), TEMPLATE_VERSION],
                                   sort_keys=True).encode()).hexdigest()
    return _conversion_root() / key / model.name


def _binary_twin(cli_binary: str) -> Path | None:
    cli = Path(cli_binary)
    twin = cli.parent / "whisper-coreml" / cli.name
    server = twin.with_name("whisper-server")
    return twin if all(p.is_file() and os.access(p, os.X_OK) for p in (twin, server)) else None


def prepared_twin(cli_binary: str, model_path: str) -> str | None:
    """Return the optional Core ML CLI only for an explicitly prepared Turbo asset.

    Called only AFTER the shared GPU entitlement check. A separate executable
    prevents an encoder sidecar from silently accelerating the free CPU path.
    Legacy/unverified conversions are deliberately not picked up automatically.
    """
    if sys.platform != "darwin" or not cli_binary or not model_path:
        return None
    from wayfinder.utils.macos_coreml_convert import TEMPLATE_VERSION, model_identity
    try:
        converted = converted_model_path(model_path)
    except (OSError, ValueError):
        return None
    asset = encoder_path(str(converted))
    if asset.name != "ggml-large-v3-turbo-encoder.mlmodelc":
        return None
    candidate = _binary_twin(cli_binary)
    try:
        if not candidate or not converted.samefile(Path(os.path.expanduser(model_path))):
            return None
        if not all((asset / name).is_file() for name in ("coremldata.bin", "model.mil", "weights/weight.bin")):
            return None
        manifest = json.loads((asset / COREML_MANIFEST).read_text(encoding="utf-8"))
        if not isinstance(manifest, dict) or manifest.get("format") != 2:
            return None
        if manifest.get("source") != model_identity(Path(os.path.expanduser(model_path))):
            return None
        if manifest.get("template") != TEMPLATE_VERSION:
            return None
    except (OSError, ValueError, TypeError):
        return None
    return str(candidate)


def prepare_encoder(cli_binary: str, model_path: str, *, cancelled=lambda: False) -> bool:
    """Convert locally in private staging, atomically publish only complete assets.

    Licensing is checked by the caller. This does not select an accelerator:
    the original Metal model stays active until a separate idle comparison.
    Raises InterruptedError when superseded or a dictation starts, and
    TimeoutError/RuntimeError when the compiler fails (a recorded attempt).
    """
    if sys.platform != "darwin" or not _binary_twin(cli_binary):
        return False
    if prepared_twin(cli_binary, model_path):
        return True
    from wayfinder.utils.macos_coreml_convert import load_bindings, populate_package, template_path
    model = Path(os.path.expanduser(model_path)).resolve()
    if encoder_path(str(model)).name != "ggml-large-v3-turbo-encoder.mlmodelc":
        return False
    compiler = Path(cli_binary).with_name("coreml-compile")
    if not compiler.is_file() or not os.access(compiler, os.X_OK):
        return False
    manifest = load_bindings(template_path())
    converted = converted_model_path(str(model))
    converted.parent.parent.mkdir(parents=True, exist_ok=True)
    # Source package + compiled result + compiler's temporary working files.
    if shutil.disk_usage(converted.parent.parent).free < manifest["weights_size"] * 3 + 256 * 1024 * 1024:
        print("[Whisper] Local encoder conversion deferred: insufficient temporary disk space; keeping Metal")
        return False
    from wayfinder.utils.child_supervisor import wrap_macos_child_command
    from wayfinder.utils.hostexec import bundle_binary_env
    print("[Whisper] Converting downloaded model locally in the background (no additional model download)")
    with tempfile.TemporaryDirectory(prefix=".conversion-", dir=converted.parent.parent) as staging:
        stage = Path(staging)
        package = stage / "encoder.mlpackage"
        marker = populate_package(model, package, cancelled=lambda: cancelled() or not _idle())
        asset = stage / encoder_path(str(converted)).name
        command, env = wrap_macos_child_command([str(compiler), str(package), str(asset)], bundle_binary_env())
        with _preparation_child(command, env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) as proc:
            try:
                deadline = time.monotonic() + 180
                while proc.poll() is None:
                    _ensure_idle(cancelled)
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Core ML package compilation timed out")
                    time.sleep(0.2)
                _ensure_idle(cancelled)
                if proc.returncode:
                    raise RuntimeError(f"Core ML package compiler exited with {proc.returncode}")
            finally:
                _stop_probe(proc)
        (asset / COREML_MANIFEST).write_text(json.dumps(marker))
        publish = stage / "ready"
        publish.mkdir(mode=0o700)
        asset.rename(publish / asset.name)
        (publish / model.name).symlink_to(model)
        _ensure_idle(cancelled)
        # Every source revision/precision has a separate namespace. Repair only
        # our own incomplete generated cache; never touch the GGML download.
        obsolete = stage / "incomplete-cache"
        if converted.parent.exists():
            if converted.parent.is_symlink() or not converted.parent.is_dir():
                return False
            converted.parent.rename(obsolete)
        try:
            publish.rename(converted.parent)
        except OSError:
            if obsolete.exists():
                obsolete.rename(converted.parent)
            raise
    print("[Whisper] Local encoder conversion complete; awaiting idle accuracy/speed comparison")
    return prepared_twin(cli_binary, model_path) is not None


@lru_cache(maxsize=1)
def _hardware_identity() -> dict:
    return {"hardware": subprocess.check_output(["sysctl", "-n", "hw.model"], text=True).strip(),
            "os": platform.mac_ver()[0], "arch": platform.machine()}


def _cache_path() -> Path:
    from wayfinder.utils.platform import get_cache_dir
    return get_cache_dir() / "coreml-selection.json"


def _comparison_key(cli_binary: str, model_path: str) -> str:
    twin = prepared_twin(cli_binary, model_path)
    if not twin:
        raise ValueError("No prepared Core ML encoder")
    files = [Path(cli_binary), Path(twin), Path(twin).with_name("whisper-server"),
             Path(os.path.expanduser(model_path)), encoder_path(str(converted_model_path(model_path))) / "weights/weight.bin"]
    stamps = [(p.name, p.stat().st_size, p.stat().st_mtime_ns) for p in files]
    return hashlib.sha256(json.dumps([_hardware_identity(), stamps], sort_keys=True).encode()).hexdigest()


def comparison(cli_binary: str, model_path: str) -> dict | None:
    try:
        key = _comparison_key(cli_binary, model_path)
        row = json.loads(_cache_path().read_text(encoding="utf-8")).get(key)
        if (isinstance(row, dict) and isinstance(row.get("preferred"), bool)
                and isinstance(row.get("same_words"), bool)):
            return row
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return None


def _read_rows(path: Path) -> dict:
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return rows if isinstance(rows, dict) else {}


def _write_rows(path: Path, rows: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=f".{path.stem}-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(rows, handle)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def record_comparison(cli_binary: str, model_path: str, metal_seconds: float,
                      coreml_seconds: float, same_words: bool) -> bool:
    """Cache no audio/text; require a meaningful win rather than measurement noise."""
    preferred = bool(same_words and 0 < coreml_seconds <= metal_seconds * 0.9
                     and metal_seconds - coreml_seconds >= 0.025)
    key = _comparison_key(cli_binary, model_path)
    path = _cache_path()
    rows = _read_rows(path)
    rows[key] = {"preferred": preferred, "metal_seconds": metal_seconds,
                 "coreml_seconds": coreml_seconds, "same_words": bool(same_words)}
    _write_rows(path, rows)
    return preferred


def _attempts_path() -> Path:
    from wayfinder.utils.platform import get_cache_dir
    return get_cache_dir() / "coreml-attempts.json"


def _attempt_key(cli_binary: str, model_path: str) -> str:
    """Failure history follows the source model, binaries, chip and OS.

    Unlike the comparison key it ignores the converted weights, so a
    reconversion does not reset the attempt count.
    """
    from wayfinder.utils.macos_coreml_convert import TEMPLATE_VERSION
    twin = _binary_twin(cli_binary)
    if not twin:
        raise ValueError("No Core ML twin")
    cli = Path(cli_binary)
    stamps = []
    for path in (cli, twin, twin.with_name("whisper-server"), cli.with_name("coreml-compile"),
                 Path(os.path.expanduser(model_path))):
        try:
            stat = path.stat()
            stamps.append((path.name, stat.st_size, stat.st_mtime_ns))
        except FileNotFoundError:
            stamps.append((path.name, None, None))
    return hashlib.sha256(json.dumps([_hardware_identity(), TEMPLATE_VERSION, stamps],
                                     sort_keys=True).encode()).hexdigest()


def _attempt_row(rows: dict, key: str) -> tuple[dict, int, float]:
    row = rows.get(key)
    row = dict(row) if isinstance(row, dict) else {}
    attempts, last = row.get("attempts"), row.get("last_attempt")
    attempts = attempts if isinstance(attempts, int) and attempts > 0 else 0
    last = float(last) if isinstance(last, (int, float)) else 0.0
    return row, attempts, last


def _attempt_allowed(key: str) -> bool:
    """Below the cap, and past the backoff since the last counted attempt."""
    _, attempts, last = _attempt_row(_read_rows(_attempts_path()), key)
    if attempts >= len(RETRY_AFTER_SECONDS):
        return False
    elapsed = time.time() - last
    return not 0 <= elapsed < RETRY_AFTER_SECONDS[attempts]  # a clock moved back never blocks


def _note_attempt(key: str, *, count: bool, failure: str | None = None) -> int:
    """Persist one attempt (counted BEFORE uncancellable device work) or its failure."""
    path = _attempts_path()
    rows = _read_rows(path)
    row, attempts, _ = _attempt_row(rows, key)
    if count:
        attempts += 1
        row.update(attempts=attempts, last_attempt=time.time())
    if failure:
        row["last_failure"] = failure
    rows[key] = row
    _write_rows(path, rows)
    return attempts


def coreml_twin(cli_binary: str, model_path: str) -> str | None:
    candidate = prepared_twin(cli_binary, model_path)
    row = comparison(cli_binary, model_path) if candidate else None
    return candidate if row and row["preferred"] and row["same_words"] else None


def _public_probe() -> Path:
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[3]))
    return root / "assets/benchmarks/coreml_probe.wav"


def prepare_comparison(config: dict, backend, *, cancelled=lambda: False,
                       on_device_attempt=lambda: None) -> bool:
    if not _prepare_lock.acquire(blocking=False):
        return False
    try:
        return _prepare_comparison(config, backend, cancelled=cancelled, on_device_attempt=on_device_attempt)
    finally:
        _prepare_lock.release()


def _idle() -> bool:
    from wayfinder.utils.platform import get_cache_dir
    try:
        state = json.loads((get_cache_dir() / "runtime/status.json").read_text())
        return not _dictation_active.is_set() and state.get("pid") == os.getpid() and state.get("state") == "IDLE"
    except (OSError, ValueError):
        return False


def _ensure_idle(cancelled) -> None:
    """Yield at once: every phase checks this, so recording never waits on preparation."""
    if cancelled():
        raise InterruptedError("Encoder preparation superseded")
    if not _idle():
        raise InterruptedError("Encoder preparation yielded to dictation")


def _quiet_seconds() -> float:
    since = _idle_since
    return 0.0 if since is None or not _idle() else time.monotonic() - since


def _wait_for_quiet(cancelled, seconds: float | None = None) -> int:
    """Block until nobody has dictated for ``seconds``; each dictation restarts the clock.

    Dictation changes and superseding requests wake it; a slow re-check covers
    the status breadcrumb. No work starts while a dictation is active or likely.
    Returns the dictation count, so later work can tell if anyone dictated since.
    """
    seconds = QUIET_BEFORE_PREPARATION if seconds is None else seconds
    while True:
        _activity_changed.clear()
        if cancelled():
            raise InterruptedError("Encoder preparation superseded")
        count = _dictations
        remaining = seconds - _quiet_seconds()
        if remaining <= 0:
            return count
        _activity_changed.wait(min(remaining, 30.0))


@contextmanager
def _preparation_child(command, env, **popen):
    """Spawn a preparation child that a starting dictation stops at once."""
    if _dictation_active.is_set():
        raise InterruptedError("Encoder preparation yielded to dictation")
    proc = subprocess.Popen(command, env=env, **popen)
    with _transition_lock:
        _children.add(proc)
        if _dictation_active.is_set():  # recording began during the spawn
            _signal_stop(proc)
    try:
        yield proc
    finally:
        with _transition_lock:
            _children.discard(proc)


def _stop_probe(proc) -> None:
    if proc.poll() is None:
        proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=2)


def _supervised_child_pid(supervisor_pid: int, binary: str) -> int | None:
    """The native server our supervisor started (same user, exact binary)."""
    rows = subprocess.check_output(["ps", "-axo", "uid=,pid=,ppid=,command="], text=True).splitlines()
    for row in rows:
        parts = row.split(None, 3)
        if (len(parts) == 4 and parts[0] == str(os.getuid()) and parts[2] == str(supervisor_pid)
                and parts[3].startswith(binary + " ")):
            return int(parts[1])
    return None


@contextmanager
def _probe_server(backend, binary: str, model: str, *, coreml: bool, cancelled, on_spawn=lambda: None):
    """Independent supervised probe; never queue synthetic audio on dictation's server.

    A starting dictation stops the probe at once (see set_dictation_active)
    and this raises InterruptedError; ``on_spawn`` runs just before the spawn.
    """
    import copy
    import socket
    from types import SimpleNamespace
    from wayfinder.utils.child_supervisor import wrap_macos_child_command
    from wayfinder.utils.hostexec import bundle_binary_env
    from wayfinder.utils.loopback_http import urlopen_loopback
    from wayfinder.utils.macos_ggml_env import whisper_metal_env
    from wayfinder.utils.macos_procinfo import pid_listens_on_tcp

    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    candidate = copy.copy(backend)
    candidate.whisper_server_binary = binary
    candidate.model_path = model
    command = candidate._server_cmd_attempts(port)[0] + ["--host", "127.0.0.1"]
    command, env = wrap_macos_child_command(command, bundle_binary_env(whisper_metal_env(gpu=True)))
    _ensure_idle(cancelled)
    on_spawn()
    with _preparation_child(command, env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT) as proc:
        tail = deque(maxlen=100)
        reader = threading.Thread(target=backend._drain_server_output, args=(proc.stdout, tail), daemon=True)
        reader.start()
        probe = SimpleNamespace(proc=proc, port=port, tail=tail, reader=reader, adopted=False, backend=candidate)
        try:
            # The device compile happens inside Core ML's model load, before
            # the server listens; it needs minutes on older chips.
            deadline = time.monotonic() + (COREML_READY_TIMEOUT if coreml else METAL_READY_TIMEOUT)
            native = None
            while True:
                _ensure_idle(cancelled)
                if proc.poll() is not None:
                    _ensure_idle(cancelled)  # stopped by a dictation, not a crash
                    raise RuntimeError("Encoder probe exited during device preparation")
                if coreml and any("failed to load Core ML model" in line for line in list(tail)):
                    raise RuntimeError("Core ML encoder failed to load")
                if time.monotonic() >= deadline:
                    raise TimeoutError("Encoder device preparation timed out")
                # Verify ownership before trusting loopback.
                native = native or _supervised_child_pid(proc.pid, binary)
                if native and pid_listens_on_tcp(native, port) is True:
                    try:
                        with urlopen_loopback(f"http://127.0.0.1:{port}/health", timeout=1) as response:
                            ready = response.status == 200
                        if ready and (not coreml or any("Core ML model loaded" in line for line in list(tail))):
                            break
                    except OSError:
                        pass
                time.sleep(_READY_POLL_SECONDS)
            yield probe
        finally:
            if not probe.adopted:
                _stop_probe(proc)
                reader.join(timeout=1)


def _measure_probe(probe, clip: Path, cancelled) -> tuple[list, list]:
    from urllib.request import Request
    from wayfinder.utils.loopback_http import urlopen_loopback

    boundary = "AuraPublicEncoderComparison"
    body = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="public-probe.wav"\r\nContent-Type: audio/wav\r\n\r\n').encode() + clip.read_bytes() + b"\r\n"
    backend = probe.backend
    for name, value in (("response_format", "json"), ("no_timestamps", "true"),
                        ("beam_size", backend.beam_size), ("best_of", backend.best_of),
                        ("temperature_inc", "0.0"), ("prompt", backend._build_prompt())):
        body += f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
    body += f"--{boundary}--\r\n".encode()
    timings, words = [], []
    for trial in range(3):
        _ensure_idle(cancelled)
        done = threading.Event()
        result = []

        def infer():
            try:
                request = Request(f"http://127.0.0.1:{probe.port}/inference", data=body,
                                  headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
                with urlopen_loopback(request, timeout=15) as response:
                    result.append(json.loads(response.read()).get("text", ""))
            except Exception as exc:
                result.append(exc)
            finally:
                done.set()

        start = time.perf_counter()
        threading.Thread(target=infer, daemon=True).start()
        # wait() returns the moment inference finishes, so the tick does not
        # quantize timings; it only bounds how late a superseding change is seen.
        while not done.wait(0.1):
            # The context manager stops ONLY this probe, so the user's
            # already-warm dictation server can handle the next stop.
            _ensure_idle(cancelled)
        if isinstance(result[0], Exception):
            raise result[0]
        if trial:
            timings.append(time.perf_counter() - start)
            words.append(re.findall(r"\w+", result[0].casefold()))
    return timings, words


def _activate_probe(probe, cli, source_model, metal, coreml, same_words, cancelled) -> bool:
    """Publish + hand off an already-warm child, never restart one on a hotkey."""
    from wayfinder.core.transcriber import WhisperServerBackend
    cls = WhisperServerBackend
    old = None
    with _transition_lock:
        if cancelled() or not _idle() or not cls._server_lock.acquire(blocking=False):
            raise InterruptedError("Accelerator switch deferred until idle")
        try:
            if probe.proc.poll() is not None:
                return False
            preferred = record_comparison(cli, source_model, metal, coreml, same_words)
            if preferred:
                old = cls._server_process
                cls._server_process = probe.proc
                cls._server_port = probe.port
                cls._server_model_path = probe.backend.model_path
                cls._server_binary = probe.backend.whisper_server_binary
                cls._server_use_gpu = True
                cls._server_threads = probe.backend.threads
                cls._server_log_tail = probe.tail
                cls._server_log_thread = probe.reader
                cls._server_disabled = False
                probe.adopted = True
                _children.discard(probe.proc)  # now the dictation server: recording must not stop it
        finally:
            cls._server_lock.release()
    if old is not None and old is not probe.proc:
        _stop_probe(old)  # outside arbitration: a slow exit cannot hold the hotkey
    return preferred


def _prepare_comparison(config: dict, backend, *, cancelled, on_device_attempt=lambda: None) -> bool:
    """Startup worker only: never benchmark on a hotkey or use the user's audio.

    The caller has already selected a licensed GPU backend and waited for a
    quiet spell. Metal stays live while a separately supervised Core ML child
    is prepared. If the user starts dictating, every phase is abandoned and its
    child stopped at once, without interrupting their server.
    ``on_device_attempt`` runs just before the Core ML probe starts, because
    the system's ANE compile cannot be cancelled after that.
    """
    if sys.platform != "darwin" or not backend.use_gpu:
        return False
    from wayfinder.core.transcriber import _resolve_whisper_cli_binary
    from wayfinder.license import get_feature_gate
    cli = _resolve_whisper_cli_binary(config.get("whisper_binary", ""))
    source_model = str(config.get("model_path", ""))
    if not source_model or Path(backend.whisper_server_binary).parent.name == "whisper-coreml":
        return False
    superseded = cancelled
    cancelled = lambda: superseded() or not get_feature_gate().has_feature("gpu_acceleration")
    _ensure_idle(cancelled)
    if not prepare_encoder(cli, source_model, cancelled=cancelled):
        return False
    twin = prepared_twin(cli, source_model)
    if not twin or comparison(cli, source_model) is not None:
        return False
    clip = _public_probe()
    if not clip.is_file():
        return False
    _ensure_idle(cancelled)
    print("[Whisper] Background encoder comparison started (public synthetic audio, no microphone)")
    with _probe_server(backend, str(Path(cli).with_name("whisper-server")), backend.model_path,
                       coreml=False, cancelled=cancelled) as baseline_probe:
        baseline, baseline_words = _measure_probe(baseline_probe, clip, cancelled)
    with _probe_server(backend, str(Path(twin).with_name("whisper-server")), str(converted_model_path(source_model)),
                       coreml=True, cancelled=cancelled, on_spawn=on_device_attempt) as probe:
        timings, outputs = _measure_probe(probe, clip, cancelled)
        same_words = bool(baseline_words[0] and all(words == baseline_words[0] for words in baseline_words + outputs))
        metal, coreml = statistics.median(baseline), statistics.median(timings)
        preferred = _activate_probe(probe, cli, source_model, metal, coreml, same_words, cancelled)
        print(f"[Whisper] Encoder comparison: Metal={metal:.3f}s CoreML={coreml:.3f}s; selected={'CoreML' if preferred else 'Metal'}")
        return preferred


def schedule_preparation(config: dict, backend) -> None:
    """Single parked daemon; model/settings changes supersede its pending work."""
    global _worker, _latest_config
    if sys.platform != "darwin" or not backend.use_gpu:
        return
    from wayfinder.core.transcriber import _resolve_whisper_cli_binary
    cli = _resolve_whisper_cli_binary(config.get("whisper_binary", ""))
    model = Path(os.path.expanduser(str(config.get("model_path", ""))))
    if not model.is_file() or not _binary_twin(cli):
        return
    with _worker_lock:
        _latest_config = config
        _preparation_requested.set()
        _activity_changed.set()  # a quiet-spell wait re-checks for supersession
        if _worker is not None and _worker.is_alive():
            return
        _worker = threading.Thread(target=_preparation_worker, name="wayfinder-local-encoder", daemon=True)
        _worker.start()


def _preparation_worker() -> None:
    while True:
        # One parked daemon, no idle polling and no lost request at thread
        # teardown. A newer model/settings request cancels old work.
        _preparation_requested.wait()
        _preparation_requested.clear()
        while _preparation_pass():
            pass


def _preparation_pass() -> bool:
    """At most one attempt. True: it yielded before finishing, so try again
    after the next quiet spell (the attempt history decides whether it may)."""
    from wayfinder.core.transcriber import _resolve_whisper_cli_binary, get_backend
    live = _latest_config
    snapshot = dict(live)
    superseded = lambda: (_preparation_requested.is_set() or live is not _latest_config
                          or any(live.get(k) != snapshot.get(k) for k in _SETTINGS_KEYS))
    cli = _resolve_whisper_cli_binary(snapshot.get("whisper_binary", ""))
    model = str(snapshot.get("model_path", ""))
    try:
        key = _attempt_key(cli, model)
    except (OSError, ValueError, subprocess.SubprocessError):
        return False
    counted = False

    def device_attempt():
        nonlocal counted
        _note_attempt(key, count=True)
        counted = True

    try:
        if comparison(cli, model) is not None:
            return False
        if not _attempt_allowed(key):
            print("[Whisper] Local encoder preparation postponed: an earlier attempt on this Mac "
                  "did not finish; keeping Metal")
            return False
        dictations = _wait_for_quiet(superseded)
        # Any dictation from here on abandons the pass, even a tap too short
        # for a poll to see: its stopped probe must not count as a crash.
        abandoned = lambda: superseded() or _dictations != dictations
        current = get_backend(snapshot)
        if not current.use_gpu or not snapshot.get("whisper_coreml_encoder", True):
            return False
        from wayfinder.utils import macos_activity
        with macos_activity.held("coreml-preparation", "Converting local speech model"):
            prepare_comparison(snapshot, current, cancelled=abandoned, on_device_attempt=device_attempt)
        if superseded():
            _preparation_requested.set()
        return False
    except InterruptedError:
        if superseded():
            _preparation_requested.set()
            return False
        if counted:  # the device compile had started; the backoff applies
            try:
                _note_attempt(key, count=False, failure="yielded to dictation")
            except OSError:
                pass
        return True  # recording won; nothing restarts until the next quiet spell
    except Exception as exc:
        try:
            attempts = _note_attempt(key, count=not counted, failure=type(exc).__name__)
        except OSError:
            attempts = 0
        if attempts < len(RETRY_AFTER_SECONDS):
            later = f"next attempt no sooner than {RETRY_AFTER_SECONDS[attempts] / 3600:.0f} h from now"
        else:
            later = "no further attempts for this model on this Mac"
        print(f"[Whisper] Local encoder preparation failed ({type(exc).__name__}); keeping Metal; {later}")
        return False
