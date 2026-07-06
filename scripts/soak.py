#!/usr/bin/env python3
"""
Soak / stress + leak-detection harness for Wayfinder Aura.

Standalone tool (NOT imported by the test suite). It hammers the real
transcription -> post-processing pipeline for many iterations and watches for
the two things that leak on this app: the long-lived whisper warm-server
(``whisper-server``, ~1.5GB resident) and the llama post-processing subprocess
(``llama-simple`` / ``llama-cli`` / ``llama``). Both are meant to be reaped at
exit; a hard ``kill -9`` skips ``atexit``, which is the H1 orphan regression
this harness also probes.

Two modes:

  Mode A  (--mode inprocess, default)
      In ONE process, loop:
          transcribe_with_config(wav, config)  ->  process_with_config(text, config)
      against the REAL warm whisper-server + llama. Portable / automatable — the
      transcription runs against a WAV you supply (or 2s of generated silence).
      This exercises the class-level whisper-server lifecycle and VRAM residency
      plus the in-process llama memory footprint. --orphan-check here asserts
      WhisperServerBackend.shutdown() actually tears the server down.

  Mode B  (--mode socket)
      Drive a RUNNING app by sending b"toggle" to its control socket (start/stop
      a real recording). This is the true end-to-end soak: it records via the
      app's real mic + state machine. REQUIRES a running app AND audio playing
      into the mic (otherwise every recording transcribes to nothing). Pass
      --pid so RSS and the endgame orphan check can target the app process. The
      SIGKILL orphan check runs by default in this mode.

Per-iteration measurements (psutil used if importable; degrades gracefully):
    * submit->result wall latency, with post-processing latency broken out
    * process RSS (psutil.Process(pid).memory_info().rss)
    * child census: count of whisper-server and llama/llama-simple processes
    * temp-WAV residue: files matching *_chunk*.wav in the system temp dir
    * VRAM: /sys/class/drm/card*/device/mem_info_vram_used if present.
      BEST-EFFORT/ADVISORY ONLY — on the Steam Deck's shared-memory APU this
      metric is often missing or meaningless, so it NEVER fails the run.

Pass/fail (printed as a summary table; process exits non-zero on FAIL):
    * RSS: linear-fit slope over the post-warmup window (first ~3 iters skipped)
      < ~0.5 MB/iter AND final RSS < 1.5x the steady-state median.
    * child counts return to baseline at rest (<=1 whisper-server, 0 llama).
    * temp-WAV count returns to baseline (cleanup ran).
    * latency: p95 of the last-10 window <= 1.3x p95 of the first-10 window.
    * VRAM: advisory only — trend printed, never fails the run.

Usage:
    # quick smoke (portable, ~20 iters against generated silence)
    PYTHONPATH=src python scripts/soak.py --iters 20

    # nightly leak hunt
    PYTHONPATH=src python scripts/soak.py --iters 500 --wav sample_16k_mono.wav --json soak.json

    # overnight Steam Deck end-to-end soak (needs a running app + mic audio)
    PYTHONPATH=src python scripts/soak.py --mode socket --minutes 480 --pid $(pgrep -f wayfinder_main) --orphan-check

    # just verify the app reaps children on hard kill (H1 regression)
    PYTHONPATH=src python scripts/soak.py --mode socket --pid <APP_PID> --iters 3 --orphan-check
"""

import argparse
import glob
import json
import math
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

# Make the app's own modules importable when run as `python scripts/soak.py`
# (mirrors `PYTHONPATH=src`, but also works if the caller forgot to set it).
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SRC = _PROJECT_ROOT / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

# psutil is optional — everything degrades to pgrep/procfs without it.
try:
    import psutil  # type: ignore
    HAVE_PSUTIL = True
except Exception:  # pragma: no cover - import guard
    psutil = None  # type: ignore
    HAVE_PSUTIL = False


# =============================================================================
# Process-name matching for the child census
# =============================================================================

# The whisper warm-server (and its CPU-only sibling the Flatpak ships).
WHISPER_SERVER_NAMES = ("whisper-server", "whisper-server-cpu")
# The llama post-processing binaries (CLI backend). The Python binding path runs
# in-process and shows up in the harness's own RSS instead.
LLAMA_NAMES = ("llama", "llama-simple", "llama-cli", "llama-server")

# Thresholds (see module docstring).
RSS_SLOPE_MAX_MB_PER_ITER = 0.5
RSS_FINAL_MAX_MULT = 1.5
LATENCY_P95_MAX_MULT = 1.3
WARMUP_ITERS = 3

# Files to clean up on interrupt.
_temp_files_to_cleanup: list = []


# =============================================================================
# Formatting helpers (mirrors benchmark.py)
# =============================================================================

def format_time(seconds: float) -> str:
    """Format a duration nicely."""
    if seconds < 0.001:
        return f"{seconds * 1_000_000:.0f}us"
    if seconds < 1:
        return f"{seconds * 1000:.0f}ms"
    if seconds < 10:
        return f"{seconds:.2f}s"
    return f"{seconds:.1f}s"


def format_mb(num_bytes) -> str:
    """Format a byte count as MB, or '?' if unknown."""
    if num_bytes is None:
        return "?"
    return f"{num_bytes / (1024 * 1024):.1f}MB"


# =============================================================================
# Math helpers (stdlib only — no numpy)
# =============================================================================

def linfit_slope(xs, ys) -> float:
    """Least-squares slope of ys over xs (units: y per x). 0.0 if degenerate."""
    n = len(xs)
    if n < 2:
        return 0.0
    mx = sum(xs) / n
    my = sum(ys) / n
    den = sum((x - mx) ** 2 for x in xs)
    if den == 0:
        return 0.0
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return num / den


def percentile(data, p: float) -> float:
    """Linear-interpolated p-th percentile (p in 0..100). 0.0 for empty input."""
    if not data:
        return 0.0
    s = sorted(data)
    if len(s) == 1:
        return float(s[0])
    k = (len(s) - 1) * (p / 100.0)
    lo = math.floor(k)
    hi = math.ceil(k)
    if lo == hi:
        return float(s[int(k)])
    return s[lo] * (hi - k) + s[hi] * (k - lo)


def median(data) -> float:
    if not data:
        return 0.0
    s = sorted(data)
    mid = len(s) // 2
    if len(s) % 2:
        return float(s[mid])
    return (s[mid - 1] + s[mid]) / 2.0


# =============================================================================
# System probes
# =============================================================================

def get_system_info() -> dict:
    """Best-effort hardware/runtime info for the report header."""
    info = {"psutil": HAVE_PSUTIL, "python": sys.version.split()[0]}
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if "model name" in line:
                    info["cpu"] = line.split(":", 1)[1].strip()
                    break
    except Exception:
        pass
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if "MemTotal" in line:
                    info["ram_gb"] = round(int(line.split()[1]) / 1024 / 1024, 1)
                    break
    except Exception:
        pass
    try:
        result = subprocess.run(["lspci"], capture_output=True, text=True, timeout=5)
        for line in result.stdout.splitlines():
            if "VGA" in line and any(v in line for v in ("AMD", "NVIDIA", "Intel")):
                info["gpu"] = line.split(":")[-1].strip()
                break
    except Exception:
        pass
    return info


def process_rss(pid: int):
    """Resident set size (bytes) for a pid, or None if unavailable."""
    if pid is None:
        return None
    if HAVE_PSUTIL:
        try:
            return psutil.Process(pid).memory_info().rss
        except Exception:
            return None
    # procfs fallback: VmRSS is in kB.
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024
    except Exception:
        return None
    return None


def _pgrep_count(pattern: str) -> int:
    """Count processes whose full command line matches an ERE pattern."""
    try:
        result = subprocess.run(
            ["pgrep", "-fc", pattern], capture_output=True, text=True, timeout=5
        )
        out = result.stdout.strip()
        return int(out) if out.isdigit() else 0
    except Exception:
        return 0


def census_children() -> dict:
    """Count whisper-server and llama processes running right now.

    Matches on the executable/argv0 basename (not the whole command line) so an
    unrelated python that merely imported llama_cpp is not miscounted.
    """
    if HAVE_PSUTIL:
        ws = ll = 0
        for proc in psutil.process_iter(["name", "cmdline"]):
            try:
                name = proc.info.get("name") or ""
                cmd = proc.info.get("cmdline") or []
                argv0 = os.path.basename(cmd[0]) if cmd else ""
                candidates = {name, argv0}
                if candidates & set(WHISPER_SERVER_NAMES):
                    ws += 1
                if candidates & set(LLAMA_NAMES):
                    ll += 1
            except (psutil.NoSuchProcess, psutil.AccessDenied, IndexError):
                continue
        return {"whisper_server": ws, "llama": ll}
    # Fallback: pgrep against the full command line (best-effort, may over-count).
    return {
        "whisper_server": _pgrep_count(r"whisper-server"),
        "llama": _pgrep_count(r"llama-(simple|cli|server)|/llama\b"),
    }


def count_temp_wavs() -> int:
    """Count leftover chunk WAVs (`*_chunk*.wav`) in the system temp dir."""
    tmp = tempfile.gettempdir()
    try:
        return len(glob.glob(os.path.join(tmp, "*_chunk*.wav")))
    except Exception:
        return 0


def read_vram():
    """Read total VRAM used from sysfs. Returns (bytes, note).

    ADVISORY ONLY. On the Steam Deck's shared-memory APU this node is frequently
    absent or reports a tiny carveout that does not reflect real model residency,
    so callers must never fail a run on it.
    """
    paths = sorted(glob.glob("/sys/class/drm/card*/device/mem_info_vram_used"))
    if not paths:
        return None, "no mem_info_vram_used sysfs node (shared-memory APU? VRAM advisory unavailable)"
    total = 0
    read_any = False
    for p in paths:
        try:
            with open(p) as f:
                total += int(f.read().strip())
                read_any = True
        except Exception:
            continue
    if not read_any:
        return None, "mem_info_vram_used present but unreadable"
    return total, ""


# =============================================================================
# WAV generation
# =============================================================================

def generate_silence_wav(duration_seconds: float = 2.0, sample_rate: int = 16000) -> str:
    """Write `duration_seconds` of 16kHz mono silence to a temp WAV; return path."""
    n_frames = int(duration_seconds * sample_rate)
    fd, path = tempfile.mkstemp(suffix=".wav", prefix="soak_silence_")
    os.close(fd)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(b"\x00\x00" * n_frames)
    return path


# =============================================================================
# Sample record
# =============================================================================

def make_sample(index: int, wall: float, trans: float, post: float, pid: int) -> dict:
    """Capture all per-iteration metrics into one record."""
    census = census_children()
    vram, vram_note = read_vram()
    return {
        "i": index,
        "wall": wall,
        "trans": trans,
        "post": post,
        "rss": process_rss(pid),
        "whisper_server": census["whisper_server"],
        "llama": census["llama"],
        "temp_wavs": count_temp_wavs(),
        "vram": vram,
        "vram_note": vram_note,
    }


def print_progress(sample: dict, total, prev_rss):
    """One readable progress line per iteration."""
    i = sample["i"]
    total_str = str(total) if total is not None else "?"
    rss = sample["rss"]
    if rss is not None and prev_rss is not None:
        drss = (rss - prev_rss) / (1024 * 1024)
        drss_str = f"{drss:+.1f}"
    else:
        drss_str = "  ?"
    vram_str = format_mb(sample["vram"]) if sample["vram"] is not None else "n/a"
    print(
        f"[{i + 1:>4}/{total_str:>4}] "
        f"wall={format_time(sample['wall']):>7} "
        f"(t {format_time(sample['trans']):>7} + p {format_time(sample['post']):>7}) "
        f"rss={format_mb(rss):>9} d{drss_str:>6}MB  "
        f"ws={sample['whisper_server']} llama={sample['llama']} "
        f"wav={sample['temp_wavs']} vram={vram_str:>9}"
    )


# =============================================================================
# Mode A: in-process pipeline soak
# =============================================================================

def run_inprocess(args, config, wav_path, stop_check):
    """Loop transcribe -> post-process in-process. Returns (samples, baseline)."""
    from wayfinder.core.transcriber import transcribe_with_config
    from wayfinder.core.postprocessor import process_with_config

    pid = os.getpid()
    print(f"[Mode A] In-process soak, measuring THIS pid {pid}")
    if not config.get("post_processing_enabled", True):
        print("[Mode A] NOTE: post_processing_enabled is False in config — the "
              "llama phase will be a fast no-op (nothing to leak there).")

    baseline = {
        "whisper_server": census_children()["whisper_server"],
        "llama": census_children()["llama"],
        "temp_wavs": count_temp_wavs(),
        "rss": process_rss(pid),
    }
    print(f"[Mode A] Baseline: whisper-server={baseline['whisper_server']} "
          f"llama={baseline['llama']} temp_wavs={baseline['temp_wavs']} "
          f"rss={format_mb(baseline['rss'])}")

    samples: list = []
    prev_rss = baseline["rss"]
    i = 0
    while not stop_check(i):
        t0 = time.perf_counter()
        # skip_post_processing=True keeps the transcription timing pure (no LLM
        # inside), then we drive process_with_config() explicitly so both phases
        # run exactly once and are measured separately.
        try:
            text = transcribe_with_config(
                wav_path, config, context="", skip_post_processing=True
            )
        except Exception as e:
            print(f"  ! transcribe iteration {i} failed: {e}")
            text = ""
        t_trans = time.perf_counter() - t0

        t1 = time.perf_counter()
        try:
            process_with_config(text, config)
        except Exception as e:
            print(f"  ! post-process iteration {i} failed: {e}")
        t_post = time.perf_counter() - t1

        sample = make_sample(i, t_trans + t_post, t_trans, t_post, pid)
        samples.append(sample)
        print_progress(sample, args.iters if args.minutes is None else None, prev_rss)
        prev_rss = sample["rss"]
        i += 1

    return samples, baseline


# =============================================================================
# Mode B: socket-driven end-to-end soak
# =============================================================================

def _send_toggle() -> bool:
    """Send b'toggle' to the app's control socket. Reuse send_toggle if importable."""
    try:
        from wayfinder.hotkeys.socket import send_toggle
        return bool(send_toggle())
    except Exception:
        pass
    # Raw AF_UNIX fallback.
    try:
        from wayfinder.config import SOCKET_PATH
    except Exception:
        SOCKET_PATH = os.path.join(
            os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "wayfinder-aura", "wayfinder-aura.sock"
        )
        if not os.path.exists(SOCKET_PATH):
            SOCKET_PATH = "/tmp/wayfinder-aura.sock"
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(SOCKET_PATH)
        s.send(b"toggle")
        s.close()
        return True
    except Exception as e:
        print(f"  ! toggle send failed ({SOCKET_PATH}): {e}")
        return False


def run_socket(args, stop_check):
    """Drive a running app via the toggle socket. Returns (samples, baseline)."""
    pid = args.pid
    print(f"[Mode B] Socket-driven soak. app pid={pid if pid else 'unknown'}")
    print("[Mode B] REQUIRES a running app AND audio playing into the mic.")
    if pid is None:
        print("[Mode B] WARNING: no --pid given — RSS will be unavailable and the "
              "orphan check cannot be scoped to the app.")

    baseline = {
        "whisper_server": census_children()["whisper_server"],
        "llama": census_children()["llama"],
        "temp_wavs": count_temp_wavs(),
        "rss": process_rss(pid),
    }
    print(f"[Mode B] Baseline: whisper-server={baseline['whisper_server']} "
          f"llama={baseline['llama']} temp_wavs={baseline['temp_wavs']} "
          f"rss={format_mb(baseline['rss'])}")

    samples: list = []
    prev_rss = baseline["rss"]
    i = 0
    while not stop_check(i):
        t0 = time.perf_counter()
        if not _send_toggle():
            print("  ! could not reach the app socket — is the app running?")
            break
        # Record for the configured window, then stop.
        time.sleep(args.record_seconds)
        t_stop = time.perf_counter()
        _send_toggle()

        # Approximate processing latency: wait for the pipeline to go idle
        # (llama subprocess gone AND chunk WAVs cleaned back to baseline), capped
        # by --grace * 10 as a safety timeout. There is no direct done-signal on
        # the socket, so this is an inferred, best-effort latency.
        deadline = t_stop + max(args.grace * 10, 15.0)
        while time.perf_counter() < deadline:
            c = census_children()
            if c["llama"] <= baseline["llama"] and count_temp_wavs() <= baseline["temp_wavs"]:
                break
            time.sleep(0.1)
        t_post = time.perf_counter() - t_stop
        wall = time.perf_counter() - t0

        sample = make_sample(i, wall, args.record_seconds, t_post, pid)
        samples.append(sample)
        print_progress(sample, args.iters if args.minutes is None else None, prev_rss)
        prev_rss = sample["rss"]
        i += 1
        time.sleep(args.gap_seconds)

    return samples, baseline


# =============================================================================
# Orphan checks
# =============================================================================

def orphan_check_inprocess() -> dict:
    """Mode A: assert WhisperServerBackend.shutdown() tears the server down."""
    result = {"kind": "inprocess-shutdown", "passed": None, "detail": ""}
    try:
        from wayfinder.core.transcriber import WhisperServerBackend
    except Exception as e:
        result["passed"] = None
        result["detail"] = f"could not import WhisperServerBackend: {e}"
        return result

    before = census_children()["whisper_server"]
    WhisperServerBackend.shutdown()
    time.sleep(2.0)
    after = census_children()["whisper_server"]
    result["before"] = before
    result["after"] = after
    if before == 0:
        result["passed"] = None
        result["detail"] = ("no whisper-server was running before shutdown() — "
                            "nothing to verify (server never started this run).")
    else:
        result["passed"] = after < before or after == 0
        result["detail"] = f"whisper-server count {before} -> {after} after shutdown()"
    return result


def _descendant_target_pids(app_pid: int):
    """PIDs of whisper-server/llama processes that are descendants of app_pid."""
    targets = []
    if HAVE_PSUTIL:
        try:
            parent = psutil.Process(app_pid)
            procs = parent.children(recursive=True)
        except Exception:
            procs = []
        for p in procs:
            try:
                name = p.name()
                cmd = p.cmdline()
                argv0 = os.path.basename(cmd[0]) if cmd else ""
                if {name, argv0} & (set(WHISPER_SERVER_NAMES) | set(LLAMA_NAMES)):
                    targets.append(p.pid)
            except Exception:
                continue
    return targets


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def orphan_check_socket(app_pid, grace: float) -> dict:
    """Mode B: SIGKILL the app, assert no whisper-server/llama survive.

    atexit does NOT fire on -9, so this catches the H1 regression where the app
    fails to reap its children on a hard kill. Any survivor is a FAIL and gets
    cleaned up so the harness doesn't leave ~1.5GB dangling.
    """
    result = {"kind": "socket-sigkill", "passed": None, "detail": ""}
    if app_pid is None:
        result["passed"] = None
        result["detail"] = "no --pid given; cannot run the SIGKILL orphan check."
        return result
    if not _pid_alive(app_pid):
        result["passed"] = None
        result["detail"] = f"app pid {app_pid} is not alive; nothing to kill."
        return result

    # Prefer descendant-scoped tracking (psutil); else fall back to a system-wide
    # before/after diff of whisper-server/llama pids.
    tracked = _descendant_target_pids(app_pid)
    system_before = _all_target_pids()
    result["tracked_children"] = tracked

    print(f"[Orphan] SIGKILL app pid {app_pid} (children tracked: {tracked or 'via system diff'})")
    try:
        os.kill(app_pid, signal.SIGKILL)
    except OSError as e:
        result["passed"] = None
        result["detail"] = f"could not SIGKILL pid {app_pid}: {e}"
        return result

    time.sleep(grace)

    if tracked:
        survivors = [p for p in tracked if _pid_alive(p)]
    else:
        # System-wide diff: targets that existed before and still exist. May
        # include an unrelated whisper-server the user runs (documented caveat).
        system_after = _all_target_pids()
        survivors = sorted(system_before & system_after)

    result["survivors"] = survivors
    result["passed"] = len(survivors) == 0
    if survivors:
        result["detail"] = (f"{len(survivors)} orphaned child(ren) survived the -9: "
                            f"{survivors} — cleaning them up now.")
        for p in survivors:
            try:
                os.kill(p, signal.SIGKILL)
            except OSError:
                pass
    else:
        result["detail"] = "no orphaned whisper-server/llama survived the -9."
    return result


def _all_target_pids() -> set:
    """System-wide set of whisper-server/llama pids (for the non-psutil fallback)."""
    pids = set()
    if HAVE_PSUTIL:
        for proc in psutil.process_iter(["name", "cmdline"]):
            try:
                name = proc.info.get("name") or ""
                cmd = proc.info.get("cmdline") or []
                argv0 = os.path.basename(cmd[0]) if cmd else ""
                if {name, argv0} & (set(WHISPER_SERVER_NAMES) | set(LLAMA_NAMES)):
                    pids.add(proc.pid)
            except Exception:
                continue
        return pids
    for pat in ("whisper-server", "llama-simple", "llama-cli", "llama-server"):
        try:
            out = subprocess.run(["pgrep", "-f", pat], capture_output=True, text=True, timeout=5)
            for line in out.stdout.split():
                if line.isdigit():
                    pids.add(int(line))
        except Exception:
            continue
    return pids


# =============================================================================
# Evaluation + summary
# =============================================================================

def evaluate(samples, baseline) -> dict:
    """Apply thresholds, return a structured verdict dict."""
    verdict = {"checks": [], "passed": True}

    def add(name, passed, detail, advisory=False):
        verdict["checks"].append(
            {"name": name, "passed": passed, "detail": detail, "advisory": advisory}
        )
        if not advisory and passed is False:
            verdict["passed"] = False

    if not samples:
        add("data", False, "no iterations ran")
        return verdict

    post = samples[WARMUP_ITERS:] if len(samples) > WARMUP_ITERS else samples
    idxs = [s["i"] for s in post]

    # --- RSS leak ---
    rss_vals = [s["rss"] for s in post if s["rss"] is not None]
    if len(rss_vals) >= 2:
        rss_mb = [v / (1024 * 1024) for v in rss_vals]
        rss_idx = [s["i"] for s in post if s["rss"] is not None]
        slope = linfit_slope(rss_idx, rss_mb)
        steady_median = median(rss_mb)
        final = rss_mb[-1]
        slope_ok = slope < RSS_SLOPE_MAX_MB_PER_ITER
        final_ok = final < RSS_FINAL_MAX_MULT * steady_median if steady_median else True
        add(
            "rss_leak",
            slope_ok and final_ok,
            f"slope={slope:+.3f} MB/iter (limit <{RSS_SLOPE_MAX_MB_PER_ITER}); "
            f"final={final:.1f}MB vs {RSS_FINAL_MAX_MULT}x median "
            f"({RSS_FINAL_MAX_MULT * steady_median:.1f}MB); "
            f"steady_median={steady_median:.1f}MB",
        )
        verdict["rss"] = {"slope_mb_per_iter": slope, "steady_median_mb": steady_median,
                          "final_mb": final}
    else:
        add("rss_leak", None, "RSS unavailable (no psutil/procfs reading) — skipped",
            advisory=True)

    # --- child census at rest ---
    max_ws = max((s["whisper_server"] for s in post), default=0)
    max_ll = max((s["llama"] for s in post), default=0)
    add(
        "child_census",
        max_ws <= 1 and max_ll <= baseline.get("llama", 0),
        f"max whisper-server at rest={max_ws} (limit <=1); "
        f"max llama at rest={max_ll} (limit <={baseline.get('llama', 0)})",
    )

    # --- temp WAV cleanup ---
    base_wavs = baseline.get("temp_wavs", 0)
    max_wavs = max((s["temp_wavs"] for s in post), default=0)
    add(
        "temp_wav_cleanup",
        max_wavs <= base_wavs,
        f"max chunk WAVs during run={max_wavs} (baseline={base_wavs})",
    )

    # --- latency drift ---
    lat = [s["wall"] for s in samples]
    w = min(10, max(1, len(lat) // 2))
    first = lat[:w]
    last = lat[-w:]
    p95_first = percentile(first, 95)
    p95_last = percentile(last, 95)
    drift_ok = p95_last <= LATENCY_P95_MAX_MULT * p95_first if p95_first > 0 else True
    add(
        "latency_drift",
        drift_ok,
        f"p95 last-{w}={format_time(p95_last)} vs "
        f"{LATENCY_P95_MAX_MULT}x p95 first-{w}={format_time(LATENCY_P95_MAX_MULT * p95_first)}",
    )
    verdict["latency"] = {"p95_first": p95_first, "p95_last": p95_last, "window": w}

    # --- VRAM (advisory) ---
    vram_vals = [(s["i"], s["vram"]) for s in post if s["vram"] is not None]
    if vram_vals:
        vidx = [i for i, _ in vram_vals]
        vmb = [v / (1024 * 1024) for _, v in vram_vals]
        vslope = linfit_slope(vidx, vmb)
        add(
            "vram_trend",
            None,
            f"advisory: {vmb[0]:.0f}->{vmb[-1]:.0f}MB, slope={vslope:+.3f}MB/iter "
            "(never fails the run)",
            advisory=True,
        )
    else:
        note = samples[-1].get("vram_note", "unavailable")
        add("vram_trend", None, f"advisory: VRAM unavailable — {note}", advisory=True)

    return verdict


def print_summary(verdict, orphan_result):
    print("\n" + "=" * 72)
    print("SOAK SUMMARY")
    print("=" * 72)
    for c in verdict["checks"]:
        if c["advisory"]:
            tag = "ADVISORY"
        elif c["passed"] is None:
            tag = "SKIP"
        elif c["passed"]:
            tag = "PASS"
        else:
            tag = "FAIL"
        print(f"  [{tag:>8}] {c['name']:<18} {c['detail']}")

    if orphan_result is not None:
        p = orphan_result["passed"]
        tag = "PASS" if p is True else "FAIL" if p is False else "SKIP"
        print(f"  [{tag:>8}] {'orphan_check':<18} "
              f"({orphan_result['kind']}) {orphan_result['detail']}")

    overall_ok = verdict["passed"] and (
        orphan_result is None or orphan_result["passed"] is not False
    )
    print("-" * 72)
    print(f"  OVERALL: {'PASS' if overall_ok else 'FAIL'}")
    print("=" * 72)
    return overall_ok


# =============================================================================
# Cleanup / signals
# =============================================================================

def _cleanup_temp_files():
    for path in _temp_files_to_cleanup:
        try:
            os.unlink(path)
        except Exception:
            pass


def _signal_handler(signum, frame):
    print("\n\nInterrupted — cleaning up...")
    _cleanup_temp_files()
    # Best-effort: tear down any warm server we started in-process.
    try:
        from wayfinder.core.transcriber import WhisperServerBackend
        WhisperServerBackend.shutdown()
    except Exception:
        pass
    sys.exit(1)


# =============================================================================
# Main
# =============================================================================

def main():
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    parser = argparse.ArgumentParser(
        description="Soak / stress + leak-detection harness for Wayfinder Aura.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  quick smoke:    PYTHONPATH=src python scripts/soak.py --iters 20\n"
            "  nightly:        PYTHONPATH=src python scripts/soak.py --iters 500 --json soak.json\n"
            "  overnight Deck: PYTHONPATH=src python scripts/soak.py --mode socket "
            "--minutes 480 --pid <APP_PID> --orphan-check\n"
        ),
    )
    parser.add_argument("--mode", choices=["inprocess", "socket"], default="inprocess",
                        help="inprocess (Mode A, default) or socket (Mode B).")
    parser.add_argument("--iters", type=int, default=20,
                        help="Number of iterations (default 20). Ignored if --minutes set.")
    parser.add_argument("--minutes", type=float, default=None,
                        help="Alternative stop condition: run for this many minutes.")
    parser.add_argument("--wav", type=str, default=None,
                        help="16kHz mono WAV to transcribe each iter (Mode A). "
                             "Generated 2s silence if omitted.")
    parser.add_argument("--pid", type=int, default=None,
                        help="App PID for RSS + the SIGKILL orphan check (Mode B/orphan).")
    parser.add_argument("--orphan-check", action="store_true",
                        help="Run the endgame orphan check. In Mode A asserts "
                             "WhisperServerBackend.shutdown() cleans up; in Mode B "
                             "SIGKILLs the app and asserts no children survive "
                             "(runs by default in Mode B).")
    parser.add_argument("--json", type=str, default=None,
                        help="Write a machine-readable JSON report to this path.")
    # Additive Mode-B / timing knobs (safe defaults).
    parser.add_argument("--record-seconds", type=float, default=4.0,
                        help="Mode B: seconds to hold each recording (default 4.0).")
    parser.add_argument("--gap-seconds", type=float, default=1.0,
                        help="Mode B: idle gap between iterations (default 1.0).")
    parser.add_argument("--grace", type=float, default=3.0,
                        help="Seconds to wait after SIGKILL before checking for "
                             "orphans (default 3.0).")
    args = parser.parse_args()

    # --- header ---
    info = get_system_info()
    print("=" * 72)
    print("WAYFINDER AURA — SOAK / LEAK HARNESS")
    print("=" * 72)
    print(f"mode={args.mode}  iters={args.iters}  minutes={args.minutes}  "
          f"psutil={'yes' if HAVE_PSUTIL else 'NO (degraded)'}")
    if "cpu" in info:
        print(f"CPU: {info['cpu']}")
    if "gpu" in info:
        print(f"GPU: {info['gpu']}")
    if "ram_gb" in info:
        print(f"RAM: {info['ram_gb']:.0f} GB")
    _, vram_note = read_vram()
    if vram_note:
        print(f"VRAM: unavailable — {vram_note}")
    print()

    # --- stop condition ---
    start = time.time()
    deadline = start + args.minutes * 60 if args.minutes is not None else None

    def stop_check(i):
        if deadline is not None:
            return time.time() >= deadline
        return i >= args.iters

    # --- config (Mode A needs it; Mode B drives the running app) ---
    config = None
    if args.mode == "inprocess":
        try:
            from wayfinder.config import load_config
            config = load_config()
        except Exception as e:
            print(f"[Config] load_config() failed ({e}); using a minimal fallback dict.")
            config = {
                "transcription_backend": "whisper_cpp",
                "whisper_server_mode": True,
                "post_processing_enabled": True,
                "post_processing_backend": "llama_cpp",
                "output_tone": "professional",
                "language": "en",
            }

    # --- WAV for Mode A ---
    wav_path = args.wav
    if args.mode == "inprocess":
        if wav_path and not Path(wav_path).exists():
            print(f"ERROR: --wav not found: {wav_path}")
            return 2
        if not wav_path:
            wav_path = generate_silence_wav(2.0)
            _temp_files_to_cleanup.append(wav_path)
            print(f"[Mode A] No --wav given; generated 2s silence at {wav_path}\n")

    samples = []
    baseline = {}
    orphan_result = None
    try:
        if args.mode == "inprocess":
            samples, baseline = run_inprocess(args, config, wav_path, stop_check)
            if args.orphan_check:
                print()
                orphan_result = orphan_check_inprocess()
        else:
            samples, baseline = run_socket(args, stop_check)
            # Orphan check runs by default in Mode B (or when explicitly asked).
            print()
            orphan_result = orphan_check_socket(args.pid, args.grace)

        verdict = evaluate(samples, baseline)
        overall_ok = print_summary(verdict, orphan_result)

        if args.json:
            report = {
                "mode": args.mode,
                "system_info": info,
                "iters_run": len(samples),
                "elapsed_seconds": round(time.time() - start, 1),
                "baseline": baseline,
                "samples": samples,
                "verdict": verdict,
                "orphan_check": orphan_result,
                "overall_pass": overall_ok,
            }
            try:
                with open(args.json, "w") as f:
                    json.dump(report, f, indent=2)
                print(f"\nJSON report written to {args.json}")
            except Exception as e:
                print(f"\n! Could not write JSON report: {e}")

        return 0 if overall_ok else 1
    finally:
        _cleanup_temp_files()
        # Mode A started a warm server in this process — always tear it down so
        # the harness never leaves ~1.5GB resident behind.
        if args.mode == "inprocess":
            try:
                from wayfinder.core.transcriber import WhisperServerBackend
                WhisperServerBackend.shutdown()
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(main())
