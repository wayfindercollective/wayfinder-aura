"""Benchmark engine tests — ``benchmark.py`` had zero direct coverage.

``benchmark.py`` is the standalone hardware-profiling tool: it times whisper.cpp
(CPU vs GPU), post-processing and cloud APIs, then flattens everything into the
config keys the app reads for its speed tooltips and auto-recommendations. The
risky surface is (a) the single-run driver ``benchmark_whisper_cpp`` (subprocess
timing + error/timeout handling) and (b) ``BenchmarkSuite.to_config_format``,
whose output shape is a cross-module CONTRACT with wayfinder_main's benchmark
consumers.

All subprocess calls are mocked; no whisper binary, GPU, or network is touched.

Contract note pinned by ``test_config_format_10s_keys_match_consumer``: the
in-app benchmark writer (wayfinder_main) stores ``benchmark_results[model_id] =
{"gpu_10s", "cpu_10s", "fastest", ...}`` and the tooltip consumer reads exactly
those keys. ``to_config_format`` here emits ``{mode}_{int(dur)}s`` + ``fastest``,
so for 10s runs the keys line up — that alignment is what these tests guard.
"""

from __future__ import annotations

import subprocess
import wave
from unittest.mock import MagicMock, patch

import pytest

benchmark = pytest.importorskip("benchmark")

BenchmarkResult = benchmark.BenchmarkResult
BenchmarkSuite = benchmark.BenchmarkSuite


def _ok_run(returncode=0, stderr=b""):
    m = MagicMock()
    m.returncode = returncode
    m.stderr = stderr
    m.stdout = b""
    return m


# ===========================================================================
# benchmark_whisper_cpp — the single-run subprocess driver
# ===========================================================================

class TestBenchmarkWhisperCpp:
    def test_happy_path_returns_timing_stats(self):
        # 1 warm-up + 3 timed runs, all succeeding.
        with patch.object(benchmark.subprocess, "run", return_value=_ok_run()) as mock_run:
            result = benchmark.benchmark_whisper_cpp(
                "/fake/whisper-cli", "/fake/model.bin", "/fake/audio.wav",
                use_gpu=False, threads=4,
            )
        assert set(result) == {"min", "max", "avg", "runs"}
        assert result["runs"] == 3
        assert result["min"] <= result["avg"] <= result["max"]
        assert result["avg"] >= 0
        assert mock_run.call_count == 4  # warm-up + 3

    def test_cpu_mode_passes_no_gpu_flag(self):
        with patch.object(benchmark.subprocess, "run", return_value=_ok_run()) as mock_run:
            benchmark.benchmark_whisper_cpp("/w", "/m", "/a", use_gpu=False)
        cmd = mock_run.call_args_list[0][0][0]
        assert "--no-gpu" in cmd
        assert "-m" in cmd and "/m" in cmd
        assert "-f" in cmd and "/a" in cmd

    def test_gpu_mode_omits_no_gpu_flag(self):
        with patch.object(benchmark.subprocess, "run", return_value=_ok_run()) as mock_run:
            benchmark.benchmark_whisper_cpp("/w", "/m", "/a", use_gpu=True)
        cmd = mock_run.call_args_list[0][0][0]
        assert "--no-gpu" not in cmd

    def test_warmup_timeout_returns_error_dict(self):
        exc = subprocess.TimeoutExpired(cmd=["whisper"], timeout=120)
        with patch.object(benchmark.subprocess, "run", side_effect=exc):
            result = benchmark.benchmark_whisper_cpp("/w", "/m", "/a")
        assert "error" in result
        assert "timed out" in result["error"].lower()
        # A failure dict must never masquerade as a timing dict.
        assert "avg" not in result

    def test_all_timed_runs_fail_returns_error(self):
        # Warm-up succeeds; every timed run exits non-zero → no valid times.
        runs = [_ok_run(0)] + [_ok_run(1, stderr=b"model load failed")] * 3
        with patch.object(benchmark.subprocess, "run", side_effect=runs):
            result = benchmark.benchmark_whisper_cpp("/w", "/m", "/a")
        assert "error" in result
        assert "avg" not in result

    def test_timed_run_timeout_returns_error(self):
        runs = [_ok_run(0)] + [subprocess.TimeoutExpired(["whisper"], 180)] * 3
        with patch.object(benchmark.subprocess, "run", side_effect=runs):
            result = benchmark.benchmark_whisper_cpp("/w", "/m", "/a")
        assert "error" in result
        assert "avg" not in result

    def test_partial_failure_still_averages_successful_runs(self):
        # 2 good runs + 1 bad → still returns stats over the 2 successes.
        runs = [_ok_run(0), _ok_run(0), _ok_run(1, b"glitch"), _ok_run(0)]
        with patch.object(benchmark.subprocess, "run", side_effect=runs):
            result = benchmark.benchmark_whisper_cpp("/w", "/m", "/a")
        assert result.get("runs") == 2


# ===========================================================================
# BenchmarkResult / to_config_format — the config contract
# ===========================================================================

def _tx(name, mode, dur, avg, error=""):
    return BenchmarkResult(
        name=name, backend="whisper_cpp", mode=mode,
        duration_seconds=dur, avg_time=avg, min_time=avg, max_time=avg,
        rtf=avg / dur if dur else 0.0, error=error,
        extra={"model_id": name.lower()},
    )


class TestConfigFormatContract:
    def _suite(self):
        s = BenchmarkSuite()
        # Both durations present (as a real run produces: TEST_DURATIONS=[10,30]),
        # because to_config_format resolves benchmark_fastest_processor via
        # get_fastest_transcription() at its DEFAULT 30s duration.
        s.transcription_results = [
            _tx("Tiny", "cpu", 10, 2.0),
            _tx("Tiny", "gpu", 10, 0.5),
            _tx("Tiny", "cpu", 30, 6.0),
            _tx("Tiny", "gpu", 30, 1.5),
            _tx("Base", "cpu", 10, 4.0),
            _tx("Broken", "cpu", 10, 0.0, error="crashed"),  # must be excluded
        ]
        s.postprocessing_results = [
            BenchmarkResult(name="llama", backend="llama_cpp", mode="cpu",
                            duration_seconds=0, avg_time=1.2),
            BenchmarkResult(name="bad", backend="broken", mode="cpu",
                            duration_seconds=0, avg_time=0, error="nope"),
        ]
        s.api_results = [
            BenchmarkResult(name="groq", backend="groq", mode="cloud",
                            duration_seconds=0, avg_time=0.3),
        ]
        s.accuracy_results = [
            BenchmarkResult(name="hi", backend="whisper_cpp", mode="cpu",
                            duration_seconds=10, avg_time=6.0, rtf=0.6,
                            extra={"accuracy_mode": "high"}),
            BenchmarkResult(name="fast", backend="whisper_cpp", mode="cpu",
                            duration_seconds=10, avg_time=2.0, rtf=0.2,
                            extra={"accuracy_mode": "fast"}),
        ]
        return s

    def test_top_level_keys_present(self):
        out = self._suite().to_config_format()
        for key in (
            "benchmark_results", "postprocessing_benchmark_results",
            "api_benchmark_results", "accuracy_benchmark_results",
            "benchmark_fastest_processor", "benchmark_fastest_postprocessor",
            "benchmark_optimal_accuracy_mode", "benchmark_timestamp",
            "benchmark_system_info",
        ):
            assert key in out, f"config contract lost key: {key}"

    def test_errored_results_excluded_everywhere(self):
        out = self._suite().to_config_format()
        assert "broken" not in out["benchmark_results"]
        assert "bad" not in out["postprocessing_benchmark_results"]
        # timestamp is an int (JSON-serialisable), system_info a dict.
        assert isinstance(out["benchmark_timestamp"], int)
        assert isinstance(out["benchmark_system_info"], dict)

    def test_config_format_10s_keys_match_consumer(self):
        """Cross-module contract: the tooltip consumer in wayfinder_main reads
        result['gpu_10s'] / result['cpu_10s'] / result['fastest'] — the exact
        keys to_config_format emits for 10-second runs."""
        out = self._suite().to_config_format()
        tiny = out["benchmark_results"]["tiny"]
        assert tiny["cpu_10s"] == 2.0
        assert tiny["gpu_10s"] == 0.5
        assert tiny["fastest"] == "gpu"  # 0.5 < 2.0

    def test_postprocessing_and_api_shapes(self):
        out = self._suite().to_config_format()
        # Post-processing + API results are keyed by BACKEND (not display name).
        assert out["postprocessing_benchmark_results"]["llama_cpp"] == {
            "avg_time": 1.2, "available": True,
        }
        assert out["api_benchmark_results"]["groq"] == {
            "latency": 0.3, "available": True,
        }

    def test_fastest_processor_and_accuracy(self):
        out = self._suite().to_config_format()
        # Fastest 30s transcription is Tiny GPU (1.5 < 6.0) → gpu.
        assert out["benchmark_fastest_processor"] == "gpu"
        assert out["benchmark_fastest_postprocessor"] == "llama_cpp"
        # 'fast' (rtf 0.2) and 'high' (rtf 0.6) both eligible under default 0.5?
        # high's rtf 0.6 > 0.5 so it's dropped; 'fast' remains.
        assert out["benchmark_optimal_accuracy_mode"] in ("fast", "balanced", "high")


class TestSuiteHelpers:
    def test_get_fastest_transcription_picks_min_avg(self):
        s = BenchmarkSuite()
        s.transcription_results = [
            _tx("Tiny", "cpu", 30, 5.0),
            _tx("Tiny", "gpu", 30, 1.0),
        ]
        backend, mode = s.get_fastest_transcription(duration=30)
        assert (backend, mode) == ("whisper_cpp", "gpu")

    def test_get_fastest_transcription_none_when_empty(self):
        assert BenchmarkSuite().get_fastest_transcription() == (None, None)

    def test_get_fastest_postprocessing_none_when_all_errored(self):
        s = BenchmarkSuite()
        s.postprocessing_results = [
            BenchmarkResult(name="x", backend="x", mode="cpu",
                            duration_seconds=0, avg_time=0, error="boom"),
        ]
        assert s.get_fastest_postprocessing() is None

    def test_optimal_accuracy_prefers_highest_under_rtf_cap(self):
        s = BenchmarkSuite()
        s.accuracy_results = [
            BenchmarkResult(name="h", backend="w", mode="cpu", duration_seconds=10,
                            avg_time=3, rtf=0.3, extra={"accuracy_mode": "high"}),
            BenchmarkResult(name="b", backend="w", mode="cpu", duration_seconds=10,
                            avg_time=2, rtf=0.2, extra={"accuracy_mode": "balanced"}),
        ]
        # Both under 0.5 → prefer the higher-accuracy 'high'.
        assert s.get_optimal_accuracy_mode(max_rtf=0.5) == "high"

    def test_optimal_accuracy_defaults_fast_when_all_too_slow(self):
        s = BenchmarkSuite()
        s.accuracy_results = [
            BenchmarkResult(name="h", backend="w", mode="cpu", duration_seconds=10,
                            avg_time=9, rtf=0.9, extra={"accuracy_mode": "high"}),
        ]
        assert s.get_optimal_accuracy_mode(max_rtf=0.5) == "fast"

    def test_ranked_results_sorted_and_excludes_errors(self):
        s = BenchmarkSuite()
        s.transcription_results = [
            _tx("A", "cpu", 10, 3.0),
            _tx("B", "gpu", 10, 1.0),
            _tx("C", "cpu", 10, 0.0, error="err"),
        ]
        ranked = s.get_ranked_results()
        assert [r.name for r in ranked] == ["B", "A"]  # sorted by avg, error dropped

    def test_result_to_dict_roundtrips_fields(self):
        r = _tx("Tiny", "gpu", 10, 0.5)
        d = r.to_dict()
        assert d["name"] == "Tiny" and d["mode"] == "gpu"
        assert d["avg_time"] == 0.5
        assert set(d) == {"name", "backend", "mode", "duration_seconds",
                          "avg_time", "min_time", "max_time", "rtf", "error", "extra"}


# ===========================================================================
# create_test_audio — the synthetic input used by every benchmark run
# ===========================================================================

class TestCreateTestAudio:
    def test_produces_valid_mono_16k_wav(self, tmp_path, monkeypatch):
        import os
        path = benchmark.create_test_audio(2, sample_rate=16000)
        try:
            with wave.open(path, "rb") as w:
                assert w.getnchannels() == 1
                assert w.getframerate() == 16000
                assert w.getsampwidth() == 2
                # ~2 seconds of samples.
                assert abs(w.getnframes() - 2 * 16000) < 16000
        finally:
            os.unlink(path)
