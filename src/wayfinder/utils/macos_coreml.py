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
_prepare_lock = threading.Lock()
_transition_lock = threading.RLock()
_dictation_active = threading.Event()
_worker_lock = threading.Lock()
_preparation_requested = threading.Event()
_worker = None
_latest_config = None
# The first Core ML load compiles the encoder for the Neural Engine: about
# 2.5 min on an M1 MacBook Air (2026-10-21), 11 s once compiled. The old 120 s
# limit killed that compile every time, so the comparison restarted on every
# launch and never finished, holding an extra model copy while it ran.
COREML_FIRST_LOAD_DEADLINE_S = 600.0
METAL_PROBE_DEADLINE_S = 120.0
# A comparison that failed this often for the same model, app build and macOS
# release is not retried: dictation stays on Metal.
MAX_COMPARISON_FAILURES = 2


def set_dictation_active(active: bool) -> None:
    """Main-thread seam: arbitration takes milliseconds, never model loading."""
    with _transition_lock:
        (_dictation_active.set if active else _dictation_active.clear)()


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
    from wayfinder.utils.child_supervisor import (
        stop_supervised, supervised_popen_kwargs, wrap_macos_child_command,
    )
    from wayfinder.utils.hostexec import bundle_binary_env
    print("[Whisper] Converting downloaded model locally in the background (no additional model download)")
    with tempfile.TemporaryDirectory(prefix=".conversion-", dir=converted.parent.parent) as staging:
        stage = Path(staging)
        package = stage / "encoder.mlpackage"
        marker = populate_package(model, package, cancelled=cancelled)
        asset = stage / encoder_path(str(converted)).name
        command, env = wrap_macos_child_command([str(compiler), str(package), str(asset)], bundle_binary_env())
        proc = subprocess.Popen(command, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                **supervised_popen_kwargs())
        try:
            import time
            deadline = time.monotonic() + 180
            while proc.poll() is None:
                if cancelled():
                    return False
                if time.monotonic() >= deadline:
                    record_comparison_failure(cli_binary, model_path, "coreml-compile timed out")
                    return False
                time.sleep(0.2)
            if cancelled():
                return False
            if proc.returncode:
                record_comparison_failure(cli_binary, model_path, f"coreml-compile exited {proc.returncode}")
                return False
        finally:
            stop_supervised(proc)
        (asset / COREML_MANIFEST).write_text(json.dumps(marker))
        publish = stage / "ready"
        publish.mkdir(mode=0o700)
        asset.rename(publish / asset.name)
        (publish / model.name).symlink_to(model)
        if cancelled():
            return False
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


def _failure_path() -> Path:
    from wayfinder.utils.platform import get_cache_dir
    return get_cache_dir() / "coreml-failures.json"


def _failure_key(cli_binary: str, model_path: str) -> str:
    # Not _comparison_key: a failed conversion has no prepared twin to stamp.
    files = [Path(cli_binary), Path(os.path.expanduser(model_path))]
    stamps = [(p.name, p.stat().st_size, p.stat().st_mtime_ns) for p in files]
    return hashlib.sha256(json.dumps([_hardware_identity(), stamps], sort_keys=True).encode()).hexdigest()


def _read_failures() -> dict:
    try:
        rows = json.loads(_failure_path().read_text(encoding="utf-8"))
        return rows if isinstance(rows, dict) else {}
    except (OSError, ValueError):
        return {}


def comparison_failures(cli_binary: str, model_path: str) -> int:
    try:
        row = _read_failures().get(_failure_key(cli_binary, model_path))
        return int(row["count"]) if isinstance(row, dict) else 0
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
        return 0


def record_comparison_failure(cli_binary: str, model_path: str, reason: str) -> int:
    """Count a failed conversion or comparison; returns the new count."""
    try:
        key = _failure_key(cli_binary, model_path)
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0
    rows = _read_failures()
    previous = rows.get(key)
    count = (int(previous.get("count", 0)) if isinstance(previous, dict) else 0) + 1
    rows[key] = {"count": count, "reason": reason[:200], "at": int(time.time())}
    path = _failure_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".coreml-failures-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(rows, handle)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
    return count


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


def record_comparison(cli_binary: str, model_path: str, metal_seconds: float,
                      coreml_seconds: float, same_words: bool) -> bool:
    """Cache no audio/text; require a meaningful win rather than measurement noise."""
    preferred = bool(same_words and 0 < coreml_seconds <= metal_seconds * 0.9
                     and metal_seconds - coreml_seconds >= 0.025)
    key = _comparison_key(cli_binary, model_path)
    path = _cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(rows, dict):
            rows = {}
    except (OSError, ValueError):
        rows = {}
    rows[key] = {"preferred": preferred, "metal_seconds": metal_seconds,
                 "coreml_seconds": coreml_seconds, "same_words": bool(same_words)}
    fd, temp = tempfile.mkstemp(prefix=".coreml-selection-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(rows, handle)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
    return preferred


def coreml_twin(cli_binary: str, model_path: str) -> str | None:
    candidate = prepared_twin(cli_binary, model_path)
    row = comparison(cli_binary, model_path) if candidate else None
    return candidate if row and row["preferred"] and row["same_words"] else None


def _public_probe() -> Path:
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[3]))
    return root / "assets/benchmarks/coreml_probe.wav"


def prepare_comparison(config: dict, backend, *, cancelled=lambda: False) -> bool:
    if not _prepare_lock.acquire(blocking=False):
        return False
    try:
        return _prepare_comparison(config, backend, cancelled=cancelled)
    finally:
        _prepare_lock.release()


def _idle() -> bool:
    from wayfinder.utils.platform import get_cache_dir
    try:
        state = json.loads((get_cache_dir() / "runtime/status.json").read_text())
        return not _dictation_active.is_set() and state.get("pid") == os.getpid() and state.get("state") == "IDLE"
    except (OSError, ValueError):
        return False


def _wait_for_idle(cancelled) -> None:
    deadline = time.monotonic() + 600
    while not _idle():
        if cancelled() or time.monotonic() >= deadline:
            raise InterruptedError("Encoder comparison deferred")
        time.sleep(0.2)
    if cancelled():
        raise InterruptedError("Encoder preparation superseded")


def _stop_probe(proc) -> None:
    from wayfinder.utils.child_supervisor import stop_supervised
    stop_supervised(proc)


@contextmanager
def _probe_server(backend, binary: str, model: str, *, coreml: bool, cancelled):
    """Independent supervised probe; never queue synthetic audio on dictation's server."""
    import copy
    import socket
    from types import SimpleNamespace
    from wayfinder.utils.child_supervisor import supervised_popen_kwargs, wrap_macos_child_command
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
    proc = subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            **supervised_popen_kwargs())
    tail = deque(maxlen=100)
    reader = threading.Thread(target=backend._drain_server_output, args=(proc.stdout, tail), daemon=True)
    reader.start()
    probe = SimpleNamespace(proc=proc, port=port, tail=tail, reader=reader, adopted=False, backend=candidate)
    try:
        deadline = time.monotonic() + (COREML_FIRST_LOAD_DEADLINE_S if coreml else METAL_PROBE_DEADLINE_S)
        while time.monotonic() < deadline:
            if cancelled():
                raise InterruptedError("Encoder preparation superseded")
            if proc.poll() is not None:
                # A crash on load is a failure, not a pause: retrying it as an
                # interruption looped forever on a Mac where it always crashes.
                raise RuntimeError(f"probe exited with {proc.returncode}: {''.join(list(tail)[-3:])[-200:]}")
            # Device compilation may continue while recording; only public
            # inference is paused. Verify ownership before trusting loopback.
            rows = subprocess.check_output(["ps", "-axo", "uid=,pid=,ppid=,command="], text=True).splitlines()
            owned = False
            for row in rows:
                parts = row.split(None, 3)
                if (len(parts) == 4 and parts[0] == str(os.getuid()) and parts[2] == str(proc.pid)
                        and parts[3].startswith(binary + " ")):
                    owned = pid_listens_on_tcp(int(parts[1]), port) is True
            if owned:
                try:
                    with urlopen_loopback(f"http://127.0.0.1:{port}/health", timeout=1) as response:
                        ready = response.status == 200
                    if ready and (not coreml or any("Core ML model loaded" in line for line in list(tail))):
                        break
                except OSError:
                    pass
            time.sleep(0.2)
        else:
            raise TimeoutError("Encoder device preparation timed out")
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
        _wait_for_idle(cancelled)
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
        while not done.wait(0.025):
            if not _idle() or cancelled():
                # The context manager stops ONLY this probe, so the user's
                # already-warm dictation server can handle the next stop.
                raise InterruptedError("Public comparison yielded to dictation")
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
        finally:
            cls._server_lock.release()
    if old is not None and old is not probe.proc:
        _stop_probe(old)  # outside arbitration: a slow exit cannot hold the hotkey
    return preferred


def _prepare_comparison(config: dict, backend, *, cancelled) -> bool:
    """Startup worker only: never benchmark on a hotkey or use the user's audio.

    The caller has already selected a licensed GPU backend. Metal stays live
    while a separately supervised Core ML child is prepared. If the user starts
    dictating, comparison is abandoned without interrupting their server.
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
    failures = comparison_failures(cli, source_model)
    if failures >= MAX_COMPARISON_FAILURES:
        print(f"[Whisper] Core ML comparison failed {failures} times on this Mac; keeping Metal")
        return False
    if cancelled() or not prepare_encoder(cli, source_model, cancelled=cancelled):
        return False
    twin = prepared_twin(cli, source_model)
    if not twin or comparison(cli, source_model) is not None:
        return False
    clip = _public_probe()
    if not clip.is_file():
        return False
    try:
        return _compare_with_probes(config, backend, cli, source_model, twin, clip, cancelled)
    except InterruptedError:
        raise
    except Exception as exc:
        count = record_comparison_failure(cli, source_model, f"{type(exc).__name__}: {exc}")
        print(f"[Whisper] Core ML comparison failed ({type(exc).__name__}, attempt {count} of "
              f"{MAX_COMPARISON_FAILURES}); keeping Metal")
        raise


def _compare_with_probes(config: dict, backend, cli: str, source_model: str, twin: str,
                         clip: Path, cancelled) -> bool:
    _wait_for_idle(cancelled)
    print("[Whisper] Background encoder comparison started (public synthetic audio, no microphone)")
    with _probe_server(backend, str(Path(cli).with_name("whisper-server")), backend.model_path,
                       coreml=False, cancelled=cancelled) as baseline_probe:
        baseline, baseline_words = _measure_probe(baseline_probe, clip, cancelled)
    with _probe_server(backend, str(Path(twin).with_name("whisper-server")), str(converted_model_path(source_model)),
                       coreml=True, cancelled=cancelled) as probe:
        timings, outputs = _measure_probe(probe, clip, cancelled)
        same_words = bool(baseline_words[0] and all(words == baseline_words[0] for words in baseline_words + outputs))
        metal, coreml = statistics.median(baseline), statistics.median(timings)
        preferred = _activate_probe(probe, cli, source_model, metal, coreml, same_words, cancelled)
        print(f"[Whisper] Encoder comparison: Metal={metal:.3f}s CoreML={coreml:.3f}s; selected={'CoreML' if preferred else 'Metal'}")
        return preferred


def cancel_preparation(config: dict) -> None:
    """A switch to *config* (CPU mode) stops the comparison in flight.

    schedule_preparation() is never called in CPU mode, so without this a
    GPU-mode comparison started before the switch would finish and hot-swap a
    Core ML server in. The parked worker wakes, sees CPU, and parks again.
    """
    global _latest_config
    with _worker_lock:
        if _worker is not None and _worker.is_alive():
            _latest_config = dict(config)
            _preparation_requested.set()


def schedule_preparation(config: dict, backend) -> None:
    """Single daemon job, retried after dictation; model/settings changes supersede it."""
    global _worker, _latest_config
    if sys.platform != "darwin" or not backend.use_gpu:
        return
    from wayfinder.core.transcriber import _resolve_whisper_cli_binary
    cli = _resolve_whisper_cli_binary(config.get("whisper_binary", ""))
    model = Path(os.path.expanduser(str(config.get("model_path", ""))))
    if not model.is_file() or not _binary_twin(cli):
        return
    keys = ("model_path", "use_gpu", "whisper_coreml_encoder", "transcription_backend", "threads",
            "beam_size", "best_of", "language", "prompt", "custom_vocabulary")
    with _worker_lock:
        _latest_config = config
        _preparation_requested.set()
        if _worker is not None and _worker.is_alive():
            return

        def run():
            from wayfinder.core.transcriber import get_backend
            while True:
                # One parked daemon, no idle polling and no lost request at
                # thread teardown. A newer model/settings request cancels old work.
                _preparation_requested.wait()
                _preparation_requested.clear()
                attempts = 0
                while attempts < 3:
                    live = _latest_config
                    snapshot = dict(live)
                    cancelled = lambda: (_preparation_requested.is_set() or live is not _latest_config
                                         or any(live.get(k) != snapshot.get(k) for k in keys))
                    try:
                        current = get_backend(snapshot)
                        if not current.use_gpu or not snapshot.get("whisper_coreml_encoder", True):
                            break
                        from wayfinder.utils import macos_activity
                        with macos_activity.held("coreml-preparation", "Converting local speech model"):
                            prepare_comparison(snapshot, current, cancelled=cancelled)
                        if cancelled():
                            _preparation_requested.set()
                        break
                    except InterruptedError:
                        if cancelled():
                            _preparation_requested.set()
                            break
                        # Recording wins. Resume only when quiet; compilation
                        # is cached and need not be repeated.
                        try:
                            _wait_for_idle(cancelled)
                        except InterruptedError:
                            if cancelled():
                                _preparation_requested.set()
                            break
                    except Exception as exc:
                        print(f"[Whisper] Local encoder preparation deferred ({type(exc).__name__}); keeping Metal")
                        attempts += 1
                        time.sleep(1)

        _worker = threading.Thread(target=run, name="wayfinder-local-encoder", daemon=True)
        _worker.start()
