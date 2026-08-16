"""Warm, load-free speech-to-text benchmark measurement.

The problem this solves: timing one cold `whisper-cli` run per configuration
measures binary startup + model load + GPU pipeline compilation + inference as
a single number, once. That punishes the GPU (which pays a large one-time
setup) exactly where the benchmark is supposed to showcase it, and it measures
a state real dictation never runs in — the app keeps a resident whisper-server
loaded and warm, so a dictation costs inference only.

The recipe here mirrors how dictation actually runs:

1. An untimed warmup run on a short clip first — it warms the OS file cache
   and the GPU driver's shader/pipeline cache the same way the resident
   server is warm during real use. Its failure is ignored; it is only a cache
   primer.
2. The measured run then reports whisper.cpp's own timing breakdown:
   ``total time − load time`` is the steady-state cost of transcribing the
   clip, and ``load time`` is surfaced separately as the one-time launch
   cost. When the timing report is missing or malformed the wall clock is
   used unchanged, which can only make the number MORE conservative.
"""

from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass
from typing import Optional

# whisper.cpp v1.9.x prints (to stderr):
#   whisper_print_timings:     load time =    73.08 ms
#   whisper_print_timings:    total time =   124.24 ms
# `total` includes `load` (verified empirically against the bundled binary).
_TIMING_RE = re.compile(
    r"whisper_print_timings:\s+(load|total)\s+time\s*=\s*([0-9]+(?:\.[0-9]+)?)\s*ms"
)


def parse_timings(stderr_text: str) -> dict:
    """``{"load": seconds, "total": seconds}`` for whichever lines are present.

    Matches are grouped into report blocks (a repeated key starts a new
    block) and the LAST COMPLETE block wins — a truncated final report must
    not donate one value while the previous block donates the other, mixing
    two different runs into one measurement (Codex review).
    """
    blocks = [{}]
    for name, ms in _TIMING_RE.findall(stderr_text or ""):
        if name in blocks[-1]:
            blocks.append({})
        blocks[-1][name] = float(ms) / 1000.0
    for block in reversed(blocks):
        if "load" in block and "total" in block:
            return block
    return blocks[-1]


def format_bench_seconds(seconds: "Optional[float]") -> str:
    """Adaptive precision so a fast result never renders as a false ``0.0s``.

    A warm GPU run of the base model is ~0.06s; one-decimal formatting shows
    that as 0.1s (67% error) and anything under 50ms as a flat 0.0s.
    """
    if seconds is None:
        return "—"
    if seconds < 0.95:
        return f"{seconds:.2f}s"
    return f"{seconds:.1f}s"


def steady_state_seconds(
    stderr_text: str, wall_seconds: float
) -> "tuple[float, Optional[float], str]":
    """(dictation seconds, one-time load seconds or None, timing source).

    ``total − load`` when both parsed and the difference is positive
    (source ``"report"``); the wall clock otherwise (source ``"wall"``).
    A nonpositive difference means the report is not trustworthy (clock
    quirk, format drift) — fall back rather than show a flattering
    nonsense number.
    """
    timings = parse_timings(stderr_text)
    load = timings.get("load")
    total = timings.get("total")
    if load is not None and total is not None:
        inference = total - load
        if inference > 0:
            return inference, load, "report"
    return wall_seconds, load, "wall"


def warm_benchmark_results(config: dict) -> dict:
    """Stored speech benchmark results measured with the warm recipe only.

    Legacy entries were cold single shots (model load + GPU pipeline compile
    lumped in, once) — every consumer retires them rather than presenting
    them alongside warm numbers they are not comparable with.
    """
    return {
        mid: r
        for mid, r in (config.get("benchmark_results", {}) or {}).items()
        if isinstance(r, dict) and r.get("method") == "warm"
    }


@dataclass(frozen=True)
class MeasureResult:
    ok: bool
    seconds: Optional[float] = None       # steady-state transcription seconds
    load_seconds: Optional[float] = None  # one-time model load, when parsed
    wall_seconds: Optional[float] = None  # full process wall clock
    returncode: Optional[int] = None
    stderr_tail: str = ""                 # for diagnostics on failure
    # Provenance (Codex review): "report" = load-free number from the
    # binary's timing report; "wall" = fallback wall clock (load included).
    timing_source: str = ""
    warmed_ok: bool = False               # the warmup pass actually succeeded


def measure_transcription(
    argv: list,
    audio_path: str,
    warmup_audio_path: Optional[str],
    *,
    env: Optional[dict] = None,
    timeout: float = 60,
    warmup_timeout: Optional[float] = None,
) -> MeasureResult:
    """Warmup once (untimed), then measure ``argv + ["-f", audio_path]``.

    ``argv`` is the transcription command WITHOUT the ``-f`` argument.
    ``--no-prints`` is stripped defensively — the timing report it suppresses
    is the whole point of the measured run. The warmup uses the same argv on
    the short clip; any warmup failure is ignored (the measured run will
    surface a real problem on its own).
    """
    argv = [a for a in argv if a != "--no-prints"]

    warmed_ok = False
    if warmup_audio_path:
        try:
            warmup = subprocess.run(
                argv + ["-f", warmup_audio_path],
                capture_output=True,
                timeout=warmup_timeout if warmup_timeout is not None else timeout,
                env=env,
            )
            warmed_ok = warmup.returncode == 0
        except Exception:
            pass

    try:
        start = time.perf_counter()
        result = subprocess.run(
            argv + ["-f", audio_path],
            capture_output=True,
            timeout=timeout,
            env=env,
        )
        wall = time.perf_counter() - start
    except subprocess.TimeoutExpired:
        return MeasureResult(ok=False, stderr_tail=f"timed out after {timeout}s")
    except Exception as exc:
        return MeasureResult(ok=False, stderr_tail=f"{type(exc).__name__}: {exc}")

    stderr_text = (result.stderr or b"").decode("utf-8", errors="replace")
    if result.returncode != 0:
        return MeasureResult(
            ok=False,
            wall_seconds=wall,
            returncode=result.returncode,
            stderr_tail=stderr_text[-400:],
        )

    seconds, load, source = steady_state_seconds(stderr_text, wall)
    return MeasureResult(
        ok=True,
        seconds=seconds,
        load_seconds=load,
        wall_seconds=wall,
        returncode=0,
        timing_source=source,
        warmed_ok=warmed_ok,
    )
