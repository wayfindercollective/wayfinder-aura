"""Start-up warm-up timings (11 Nov 2026: the speech model took 2 min after
login and a dictation waited 73 s, with nothing saying which step was slow)."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_timing_line_lists_every_stage_that_ran_in_order():
    import wayfinder_main as wm

    line = wm._warm_up_timing_line(124.4, {
        "load": 121.8, "checks": 0.1, "lock": 2.0, "cleanup": 0.4})
    assert line == ("⏱ Start-up warm-up took 124s: checks 0s · waiting to load 2s · "
                    "loading the speech model 122s · cleanup model 0s")


def test_timing_line_skips_stages_that_did_not_run():
    import wayfinder_main as wm

    assert wm._warm_up_timing_line(3.0, {"checks": 0.0, "lock": 0.0}) == (
        "⏱ Start-up warm-up took 3s: checks 0s · waiting to load 0s")


def test_the_timing_line_is_windows_only_and_start_up_only():
    src = (ROOT / "wayfinder_main.py").read_text(encoding="utf-8")
    assert "if startup and IS_WINDOWS and not IS_MACOS and ready:" in src
    assert 'stages["load"] = time.monotonic() - load_started' in src
