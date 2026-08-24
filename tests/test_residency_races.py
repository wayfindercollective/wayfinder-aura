"""Round-4 closures: every inference path holds the residency lock, a timed-out
model is quarantined rather than closed, released handles are never re-entered,
and a cold GPU probe cannot blow the cleanup budget.

The failure these guard is a native crash, not an exception: freeing llama.cpp
weights while a call is inside them corrupts the process. A deterministic probe
in the fourth review round showed the Python fallback and the warm-up both let
release_resident_models() close a model mid-call.
"""

import threading
import time

import pytest
from unittest.mock import patch

from wayfinder.core.postprocessor import (
    LlamaCppBackend,
    LlamaCppCliBackend,
    PostProcessingError,
    _GPU_PROBE_TIMEOUT,
)


@pytest.fixture(autouse=True)
def _clean_caches():
    saved_models = dict(LlamaCppBackend._model_cache)
    saved_resident = dict(LlamaCppCliBackend._resident_cache)
    saved_probe = dict(LlamaCppCliBackend._gpu_probe)
    saved_clamped = set(LlamaCppCliBackend._gpu_probe_clamped)
    saved_quarantine = list(LlamaCppBackend._quarantined)
    LlamaCppBackend._model_cache.clear()
    LlamaCppCliBackend._resident_cache.clear()
    LlamaCppCliBackend._gpu_probe.clear()
    LlamaCppCliBackend._gpu_probe_clamped.clear()
    LlamaCppBackend._quarantined.clear()
    yield
    LlamaCppBackend._model_cache.clear()
    LlamaCppBackend._model_cache.update(saved_models)
    LlamaCppCliBackend._resident_cache.clear()
    LlamaCppCliBackend._resident_cache.update(saved_resident)
    LlamaCppCliBackend._gpu_probe.clear()
    LlamaCppCliBackend._gpu_probe.update(saved_probe)
    LlamaCppCliBackend._gpu_probe_clamped.clear()
    LlamaCppCliBackend._gpu_probe_clamped.update(saved_clamped)
    LlamaCppBackend._quarantined.clear()
    LlamaCppBackend._quarantined.extend(saved_quarantine)


def _cli_backend(tmp_path, **kw):
    (tmp_path / "llama-simple").touch()
    model = tmp_path / "m.gguf"
    model.write_bytes(b"\x00")
    kw.setdefault("output_tone", "professional")
    return LlamaCppCliBackend(
        llama_binary=str(tmp_path / "llama-simple"),
        model_path=str(model), **kw)


def _py_backend(tmp_path, **kw):
    model = tmp_path / "m.gguf"
    model.write_bytes(b"\x00")
    return LlamaCppBackend(model_path=str(model), **kw)


def _lock_is_held_by_another_thread() -> bool:
    """True when this thread cannot take the residency lock — i.e. an
    inference elsewhere is holding it right now."""
    got = []

    def _try():
        ok = LlamaCppCliBackend._residency_lock.acquire(blocking=False)
        got.append(ok)
        if ok:
            LlamaCppCliBackend._residency_lock.release()

    t = threading.Thread(target=_try)
    t.start()
    t.join()
    return got == [False]


class TestEveryInferencePathHoldsTheLock:
    def test_python_fallback_infers_under_the_residency_lock(self, tmp_path):
        b = _py_backend(tmp_path, timeout=5.0)
        seen = {}

        def fake_model(prompt, **kw):
            seen["locked"] = _lock_is_held_by_another_thread()
            return {"choices": [{"text": "cleaned"}]}

        with patch.object(b, "is_available", return_value=True), \
             patch.object(b, "_get_model", return_value=fake_model):
            b.process("hello world", "{text}")
        assert seen["locked"] is True

    def test_warm_up_wheel_call_runs_under_the_lock(self, tmp_path):
        b = _cli_backend(tmp_path, n_gpu_layers=0)
        seen = {}

        def fake_model(prompt, **kw):
            seen["locked"] = _lock_is_held_by_another_thread()
            return {"choices": [{"text": "ok"}]}

        LlamaCppCliBackend._resident_cache[b._resident_cache_key()] = fake_model
        with patch.object(b, "_resident_model", return_value=fake_model):
            b.warm_up()
        assert seen["locked"] is True


class TestReleasedModelsAreNeverReEntered:
    def test_warm_up_skips_a_model_released_in_the_window(self, tmp_path):
        """The lookup happened, then release closed the model, then the call
        would have entered freed weights. Identity against the cache under the
        lock is the proof of life."""
        b = _cli_backend(tmp_path, n_gpu_layers=0)
        calls = []

        def fake_model(prompt, **kw):
            calls.append(prompt)
            return {"choices": [{"text": "ok"}]}

        # NOT in the resident cache: released between lookup and lock.
        with patch.object(b, "_resident_model", return_value=fake_model):
            b.warm_up()
        assert calls == []

    def test_dictation_falls_to_the_next_rung_when_released_in_the_window(self, tmp_path):
        b = _cli_backend(tmp_path, n_gpu_layers=0)
        calls = []

        def fake_model(prompt, **kw):
            calls.append(prompt)
            return {"choices": [{"text": "never"}]}

        served = {"content": "hello world, served", "stop_type": "eos"}
        with patch.object(b, "_resident_model", return_value=fake_model), \
             patch.object(b, "_server_generate", return_value=served):
            out = b.process("hello world", "{text}")
        assert calls == []
        assert "served" in out

    def test_get_model_never_returns_a_released_handle(self, tmp_path):
        """self._model survived release_resident_models() as a pointer to
        CLOSED weights; the next call must reload, not re-enter it."""
        b = _py_backend(tmp_path)
        stale = object()
        b._model = stale
        key = (b.model_path, b.n_ctx, b.n_gpu_layers)

        # Cache holds a fresh model under this key: adopt it, never the handle.
        fresh = object()
        LlamaCppBackend._model_cache[key] = fresh
        assert b._get_model() is fresh

        # Cache empty (released): the stale handle must be dropped. Loading a
        # 1-byte gguf then fails either way — the point is it RAISES rather
        # than returning the stale handle.
        del LlamaCppBackend._model_cache[key]
        b._model = stale
        with pytest.raises(PostProcessingError):
            b._get_model()
        assert b._model is not stale


class TestTimedOutModelsAreQuarantined:
    def test_a_timed_out_model_is_pulled_from_the_cache_but_never_closed(self, tmp_path):
        """run_with_timeout bounds recovery, not the leaked native call — the
        worker thread is still inside the weights after the timeout, so close()
        (from release) or GC would free memory under a live call."""
        closed = []

        class FakeModel:
            def __call__(self, prompt, **kw):
                time.sleep(0.5)
                return {"choices": [{"text": "late"}]}

            def close(self):
                closed.append(1)

        b = _py_backend(tmp_path, timeout=0.05)
        fake = FakeModel()
        key = (b.model_path, b.n_ctx, b.n_gpu_layers)
        LlamaCppBackend._model_cache[key] = fake

        with patch.object(b, "is_available", return_value=True):
            with pytest.raises(PostProcessingError):
                b.process("hello world", "{text}")

        assert key not in LlamaCppBackend._model_cache
        assert any(m is fake for m, _w in LlamaCppBackend._quarantined)
        # The worker is still inside the weights: release must NOT close.
        LlamaCppBackend.release_resident_models()
        assert closed == []
        # Once the worker provably ends, the next sweep reclaims the memory —
        # quarantine is bounded by worker liveness, not append-only.
        for _m, w in LlamaCppBackend._quarantined:
            if w is not None:
                w.join(5)
        LlamaCppBackend.release_resident_models()
        assert closed == [1]
        assert not any(m is fake for m, _w in LlamaCppBackend._quarantined)


class TestLoadTimeCountsAgainstTheTimeout:
    def test_a_load_that_eats_the_budget_forfeits_the_inference(self, tmp_path):
        b = _py_backend(tmp_path, timeout=0.05)
        calls = []

        def slow_load():
            time.sleep(0.2)   # longer than the whole 0.05s budget

            def model(prompt, **kw):
                calls.append(prompt)
                return {"choices": [{"text": "x"}]}
            return model

        with patch.object(b, "_get_model", side_effect=slow_load):
            with pytest.raises(PostProcessingError):
                b.process("hello world", "{text}")
        assert calls == []

    def test_a_disabled_timeout_stays_disabled(self, tmp_path):
        b = _py_backend(tmp_path, timeout=0)

        def fake_model(prompt, **kw):
            return {"choices": [{"text": "hello world, cleaned"}]}

        with patch.object(b, "is_available", return_value=True), \
             patch.object(b, "_get_model", return_value=fake_model):
            assert "cleaned" in b.process("hello world", "{text}")


class TestTheGpuProbeRespectsTheBudget:
    def test_an_expired_deadline_routes_to_cpu_without_probing(self, tmp_path):
        b = _cli_backend(tmp_path, n_gpu_layers=-1)
        with patch.object(b, "_probe_gpu_ok",
                          side_effect=AssertionError("must not probe")):
            _binary, ngl = b._subprocess_target(deadline=time.monotonic() - 1)
        assert ngl == 0
        assert (b.llama_binary, b.model_path, 99) not in LlamaCppCliBackend._gpu_probe

    def test_a_budget_clamped_probe_failure_is_not_cached(self, tmp_path):
        """A probe cut short by the budget may have failed BECAUSE of the cut;
        caching that verdict would condemn a healthy GPU forever."""
        b = _cli_backend(tmp_path, n_gpu_layers=-1)
        with patch.object(b, "_probe_gpu_ok", return_value=False):
            _binary, ngl = b._subprocess_target(
                deadline=time.monotonic() + min(1.0, _GPU_PROBE_TIMEOUT / 2))
        assert ngl == 0
        assert (b.llama_binary, b.model_path, 99) not in LlamaCppCliBackend._gpu_probe

    def test_a_full_time_probe_failure_is_still_cached(self, tmp_path):
        b = _cli_backend(tmp_path, n_gpu_layers=-1)
        with patch.object(b, "_probe_gpu_ok", return_value=False):
            _binary, ngl = b._subprocess_target()
        assert ngl == 0
        assert LlamaCppCliBackend._gpu_probe[(b.llama_binary, b.model_path, 99)] is False


class TestLoadOnlyExpiryIsNotATimeout:
    def test_a_load_that_eats_the_budget_quarantines_nothing(self, tmp_path):
        """No worker ever entered the model, so there is nothing to protect:
        quarantining here pinned one healthy multi-GB model per dictation."""
        b = _py_backend(tmp_path, timeout=0.05)
        key = (b.model_path, b.n_ctx, b.n_gpu_layers)

        def slow_load():
            time.sleep(0.2)

            def model(prompt, **kw):
                raise AssertionError("inference must not start")
            LlamaCppBackend._model_cache[key] = model
            return model

        with patch.object(b, "is_available", return_value=True), \
             patch.object(b, "_get_model", side_effect=slow_load):
            with pytest.raises(PostProcessingError):
                b.process("hello world", "{text}")
        assert LlamaCppBackend._quarantined == []
        # The freshly loaded weights are healthy — they stay cached so the
        # NEXT dictation gets them warm instead of paying the load again.
        assert key in LlamaCppBackend._model_cache


class TestLoadsCannotPublishAfterRelease:
    def test_a_load_in_flight_when_release_runs_is_closed_not_cached(self, tmp_path):
        """Sequence: lookup misses -> release sweeps (empty) and returns ->
        load finishes. Publishing now would resurrect weights 'Save memory'
        just swept, and the identity re-check cannot catch it — a model that
        inserts itself IS the cached one. The epoch check must close it."""
        b = _cli_backend(tmp_path, n_gpu_layers=0)
        closed = []

        class FakeLlama:
            def __init__(self, **kw):
                # The release fires DURING the load, after the epoch was read.
                LlamaCppCliBackend.release_resident_models()

            def close(self):
                closed.append(1)

        import wayfinder.core.postprocessor as pp
        with patch.object(pp, "_wheel_supports_gpu_offload", return_value=True), \
             patch.dict("sys.modules", {"llama_cpp": type("M", (), {"Llama": FakeLlama})}):
            result = b._resident_model()
        assert result is None
        assert closed == [1]
        assert LlamaCppCliBackend._resident_cache == {}


class TestClampedProbeBurnsAtMostOnce:
    def test_second_budgeted_call_skips_the_probe(self, tmp_path):
        b = _cli_backend(tmp_path, n_gpu_layers=-1)
        deadline = lambda: time.monotonic() + min(1.0, _GPU_PROBE_TIMEOUT / 2)
        with patch.object(b, "_probe_gpu_ok", return_value=False) as probe:
            b._subprocess_target(deadline=deadline())
            b._subprocess_target(deadline=deadline())
        assert probe.call_count == 1

    def test_a_full_time_probe_settles_a_clamp_burned_key(self, tmp_path):
        b = _cli_backend(tmp_path, n_gpu_layers=-1)
        key = (b.llama_binary, b.model_path, 99)
        with patch.object(b, "_probe_gpu_ok", return_value=False):
            b._subprocess_target(
                deadline=time.monotonic() + min(1.0, _GPU_PROBE_TIMEOUT / 2))
        assert key in LlamaCppCliBackend._gpu_probe_clamped
        with patch.object(b, "_probe_gpu_ok", return_value=True):
            _binary, ngl = b._subprocess_target()   # warm-up: no deadline
        assert ngl == 99
        assert LlamaCppCliBackend._gpu_probe[key] is True
        assert key not in LlamaCppCliBackend._gpu_probe_clamped

    def test_the_verdict_is_per_layer_count(self, tmp_path):
        """A pass at 10 layers says nothing about 99 — each requested count
        gets its own probe and its own cached verdict."""
        b10 = _cli_backend(tmp_path, n_gpu_layers=10)
        with patch.object(b10, "_probe_gpu_ok", return_value=True):
            b10._subprocess_target()
        b99 = LlamaCppCliBackend(
            llama_binary=b10.llama_binary, model_path=b10.model_path,
            output_tone="professional", n_gpu_layers=-1)
        with patch.object(b99, "_probe_gpu_ok", return_value=False) as probe:
            _binary, ngl = b99._subprocess_target()
        probe.assert_called_once()
        assert ngl == 0
        assert LlamaCppCliBackend._gpu_probe[(b10.llama_binary, b10.model_path, 10)] is True
        assert LlamaCppCliBackend._gpu_probe[(b10.llama_binary, b10.model_path, 99)] is False


class TestTheBudgetIsHonoredExactly:
    def test_a_tiny_budget_is_not_re_floored(self, tmp_path):
        b = _cli_backend(tmp_path, total_budget=0.05)
        assert b.CLEANUP_TOTAL_BUDGET == 0.05
