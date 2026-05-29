"""Tests for the wall-clock timeout helper used to bound in-process inference backends."""

import time

import pytest

from wayfinder.utils.timeout import run_with_timeout, CallTimeout


class TestRunWithTimeout:
    def test_returns_result_when_fast(self):
        assert run_with_timeout(lambda: 42, 1.0) == 42

    def test_passes_args_and_kwargs(self):
        assert run_with_timeout(lambda a, b=0: a + b, 1.0, 3, b=4) == 7

    def test_raises_call_timeout_when_slow(self):
        def slow():
            time.sleep(5)
            return "never"

        start = time.time()
        with pytest.raises(CallTimeout):
            run_with_timeout(slow, 0.2)
        # Should give up promptly (not wait the full 5s).
        assert time.time() - start < 2.0

    def test_zero_timeout_runs_directly(self):
        assert run_with_timeout(lambda: "ok", 0) == "ok"

    def test_none_timeout_runs_directly(self):
        assert run_with_timeout(lambda: "ok", None) == "ok"

    def test_propagates_function_exception(self):
        def boom():
            raise ValueError("kaboom")

        with pytest.raises(ValueError, match="kaboom"):
            run_with_timeout(boom, 1.0)
