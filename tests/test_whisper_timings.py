"""wayfinder.core.whisper_timings — warm, load-free benchmark measurement."""

import subprocess
from types import SimpleNamespace

import pytest

from wayfinder.core.whisper_timings import (
    MeasureResult,
    format_bench_seconds,
    measure_transcription,
    parse_timings,
    steady_state_seconds,
    warm_benchmark_results,
)

# Captured verbatim from the bundled whisper-cli (v1.9.1, Flatpak sandbox).
REAL_STDERR = """\
whisper_init_state: compute buffer (decode) =   97.29 MB
read_audio_data: trying to decode with miniaudio
whisper_print_timings:     load time =    73.08 ms
whisper_print_timings:     fallbacks =   0 p /   0 h
whisper_print_timings:      mel time =     1.61 ms
whisper_print_timings:   sample time =     5.15 ms /    31 runs (     0.17 ms per run)
whisper_print_timings:   encode time =    24.47 ms /     1 runs (    24.47 ms per run)
whisper_print_timings:   decode time =     1.82 ms /     1 runs (     1.82 ms per run)
whisper_print_timings:    total time =   124.24 ms
"""


class TestParseTimings:
    def test_real_output_parses_load_and_total(self):
        timings = parse_timings(REAL_STDERR)
        assert timings["load"] == pytest.approx(0.07308)
        assert timings["total"] == pytest.approx(0.12424)

    def test_empty_and_none_are_safe(self):
        assert parse_timings("") == {}
        assert parse_timings(None) == {}

    def test_unrelated_text_yields_nothing(self):
        assert parse_timings("error: could not load model\n") == {}

    def test_integer_milliseconds_parse(self):
        text = "whisper_print_timings:     load time =    50 ms\n"
        assert parse_timings(text) == {"load": 0.05}

    def test_repeated_reports_use_the_last_complete_block(self):
        text = REAL_STDERR + (
            "whisper_print_timings:     load time =    10.00 ms\n"
            "whisper_print_timings:    total time =    30.00 ms\n"
        )
        assert parse_timings(text) == {"load": 0.01, "total": 0.03}

    def test_partial_final_block_does_not_mix_with_previous(self):
        # A truncated second report must not donate its load while the first
        # block donates its total — that would mix two runs (Codex review).
        text = REAL_STDERR + "whisper_print_timings:     load time =    10.00 ms\n"
        timings = parse_timings(text)
        assert timings["load"] == pytest.approx(0.07308)
        assert timings["total"] == pytest.approx(0.12424)


class TestFormatBenchSeconds:
    def test_fast_results_keep_two_decimals(self):
        # 0.06s rendered as "0.1s" is a 67% error; as "0.0s" it reads broken.
        assert format_bench_seconds(0.06) == "0.06s"
        assert format_bench_seconds(0.04) == "0.04s"

    def test_slow_results_keep_one_decimal(self):
        assert format_bench_seconds(1.234) == "1.2s"
        assert format_bench_seconds(9.8) == "9.8s"

    def test_boundary_and_none(self):
        assert format_bench_seconds(0.94) == "0.94s"
        assert format_bench_seconds(0.96) == "1.0s"
        assert format_bench_seconds(None) == "—"


class TestWarmBenchmarkResults:
    def test_only_warm_entries_survive(self):
        config = {
            "benchmark_results": {
                "base.en": {"gpu_10s": 0.06, "method": "warm"},
                "small.en": {"gpu_10s": 0.8},  # legacy cold single shot
                "junk": "not-a-dict",
            }
        }
        assert list(warm_benchmark_results(config)) == ["base.en"]

    def test_empty_and_missing_are_safe(self):
        assert warm_benchmark_results({}) == {}
        assert warm_benchmark_results({"benchmark_results": None}) == {}


class TestSteadyStateSeconds:
    def test_subtracts_load_from_total(self):
        seconds, load, source = steady_state_seconds(REAL_STDERR, wall_seconds=9.9)
        assert abs(seconds - 0.05116) < 1e-9  # total − load, NOT the wall clock
        assert abs(load - 0.07308) < 1e-9
        assert source == "report"

    def test_missing_report_falls_back_to_wall_clock(self):
        seconds, load, source = steady_state_seconds("no timings here", wall_seconds=1.23)
        assert seconds == 1.23
        assert load is None
        assert source == "wall"

    def test_missing_total_falls_back(self):
        text = "whisper_print_timings:     load time =    73.08 ms\n"
        seconds, load, source = steady_state_seconds(text, wall_seconds=2.0)
        assert seconds == 2.0
        assert load == pytest.approx(0.07308)
        assert source == "wall"

    def test_nonpositive_inference_falls_back(self):
        # A load >= total means the report can't be trusted — never show a
        # flattering nonsense number.
        text = (
            "whisper_print_timings:     load time =   200.00 ms\n"
            "whisper_print_timings:    total time =   100.00 ms\n"
        )
        seconds, _load, source = steady_state_seconds(text, wall_seconds=3.5)
        assert seconds == 3.5
        assert source == "wall"


class _FakeRun:
    """Records subprocess.run calls; scripted (returncode, stderr) per call."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def __call__(self, argv, capture_output=None, timeout=None, env=None):
        self.calls.append({"argv": list(argv), "timeout": timeout, "env": env})
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        returncode, stderr = outcome
        return SimpleNamespace(returncode=returncode, stderr=stderr, stdout=b"")


class TestMeasureTranscription:
    def test_warmup_runs_first_and_untimed_failure_is_ignored(self, monkeypatch):
        fake = _FakeRun([(1, b"warmup exploded"), (0, REAL_STDERR.encode())])
        monkeypatch.setattr(subprocess, "run", fake)

        result = measure_transcription(
            ["whisper-cli", "-m", "model.bin"], "clip10s.wav", "clip1s.wav"
        )

        assert result.ok
        assert [c["argv"][-1] for c in fake.calls] == ["clip1s.wav", "clip10s.wav"]
        assert abs(result.seconds - 0.05116) < 1e-9
        assert abs(result.load_seconds - 0.07308) < 1e-9

    def test_no_prints_is_stripped(self, monkeypatch):
        # --no-prints suppresses the timing report that IS the measurement.
        fake = _FakeRun([(0, b""), (0, REAL_STDERR.encode())])
        monkeypatch.setattr(subprocess, "run", fake)

        measure_transcription(
            ["whisper-cli", "--no-prints", "-m", "model.bin"], "a.wav", "w.wav"
        )

        for call in fake.calls:
            assert "--no-prints" not in call["argv"]

    def test_no_warmup_audio_skips_warmup(self, monkeypatch):
        fake = _FakeRun([(0, REAL_STDERR.encode())])
        monkeypatch.setattr(subprocess, "run", fake)

        result = measure_transcription(["whisper-cli"], "a.wav", None)

        assert result.ok
        assert len(fake.calls) == 1

    def test_measured_failure_reports_stderr_tail(self, monkeypatch):
        fake = _FakeRun([(0, b""), (-11, b"ggml_vulkan: device init failed")])
        monkeypatch.setattr(subprocess, "run", fake)

        result = measure_transcription(["whisper-cli"], "a.wav", "w.wav")

        assert not result.ok
        assert result.returncode == -11
        assert "vulkan" in result.stderr_tail.lower()

    def test_timeout_is_a_clean_failure(self, monkeypatch):
        fake = _FakeRun([(0, b""), subprocess.TimeoutExpired(cmd="x", timeout=60)])
        monkeypatch.setattr(subprocess, "run", fake)

        result = measure_transcription(["whisper-cli"], "a.wav", "w.wav", timeout=60)

        assert not result.ok
        assert "timed out" in result.stderr_tail

    def test_missing_timings_fall_back_to_wall_clock(self, monkeypatch):
        fake = _FakeRun([(0, b""), (0, b"no report at all")])
        monkeypatch.setattr(subprocess, "run", fake)

        result = measure_transcription(["whisper-cli"], "a.wav", "w.wav")

        assert result.ok
        assert result.seconds == result.wall_seconds
        assert result.load_seconds is None
        assert result.timing_source == "wall"

    def test_provenance_records_report_source_and_warmup_outcome(self, monkeypatch):
        fake = _FakeRun([(0, b""), (0, REAL_STDERR.encode())])
        monkeypatch.setattr(subprocess, "run", fake)

        result = measure_transcription(["whisper-cli"], "a.wav", "w.wav")

        assert result.timing_source == "report"
        assert result.warmed_ok is True

    def test_failed_warmup_is_recorded(self, monkeypatch):
        fake = _FakeRun([(1, b"boom"), (0, REAL_STDERR.encode())])
        monkeypatch.setattr(subprocess, "run", fake)

        result = measure_transcription(["whisper-cli"], "a.wav", "w.wav")

        assert result.ok
        assert result.warmed_ok is False

    def test_result_is_a_frozen_record(self):
        record = MeasureResult(ok=True, seconds=1.0)
        try:
            record.seconds = 2.0
            raised = False
        except Exception:
            raised = True
        assert raised
