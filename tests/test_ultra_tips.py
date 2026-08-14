"""Tests for wayfinder.core.ultra_tips — Ultra under-utilization detection."""

from types import SimpleNamespace

from wayfinder.core.ultra_tips import (
    SIGNAL_PHRASES,
    nudge_text,
    underutilization_signals,
)


def _gate(premium=True, features=None):
    """Gate stub: features is a set of granted feature ids (None = grant all)."""
    return SimpleNamespace(
        is_premium=premium,
        has_feature=lambda f: True if features is None else f in features,
    )


FRESH_ULTRA_CONFIG = {
    # Exactly what an install looks like right after activation: everything
    # still at the Free defaults.
    "model_path": "/models/ggml-base.en.bin",
    "use_gpu": False,
    "llama_cpp_model_path": "/models/Qwen3.5-2B-Q4_K_M.gguf",  # a Free model
    "llama_cpp_model_requires_feature": None,
    "chunked_mode": "off",
}


class TestUnderutilizationSignals:
    def test_free_user_gets_no_signals(self):
        assert underutilization_signals(FRESH_ULTRA_CONFIG, _gate(premium=False), True) == []

    def test_fresh_ultra_install_flags_everything(self):
        signals = underutilization_signals(dict(FRESH_ULTRA_CONFIG), _gate(), True)
        assert signals == ["model", "gpu", "cleanup", "chunking"]

    def test_no_gpu_hardware_suppresses_only_the_gpu_signal(self):
        signals = underutilization_signals(dict(FRESH_ULTRA_CONFIG), _gate(), False)
        assert "gpu" not in signals
        assert "model" in signals

    def test_each_signal_clears_when_the_upgrade_is_in_use(self):
        cfg = dict(FRESH_ULTRA_CONFIG)
        cfg["model_path"] = "/models/ggml-large-v3-turbo.bin"
        cfg["use_gpu"] = True
        cfg["llama_cpp_model_requires_feature"] = "large_cleanup_models"
        cfg["chunked_mode"] = "auto"
        assert underutilization_signals(cfg, _gate(), True) == []

    def test_custom_premium_cleanup_model_does_not_nag(self):
        # Browse stores the catalog marker as None for CUSTOM models, but the
        # shared tier logic still recognizes 3B+/4B names as premium — a user
        # already running one is utilizing Ultra, not under-utilizing it
        # (Codex review regression).
        cfg = dict(FRESH_ULTRA_CONFIG)
        cfg["llama_cpp_model_path"] = "/models/custom-Qwen3-4B-Instruct.gguf"
        cfg["llama_cpp_model_requires_feature"] = None
        assert "cleanup" not in underutilization_signals(cfg, _gate(), True)

    def test_ungranted_features_never_signal(self):
        # An Ultra plan variant without these features must not nag about them.
        signals = underutilization_signals(
            dict(FRESH_ULTRA_CONFIG), _gate(features=set()), True
        )
        assert signals == []

    def test_broken_gate_is_skipped_not_raised(self):
        def _boom(_f):
            raise RuntimeError("gate exploded")

        gate = SimpleNamespace(is_premium=True, has_feature=_boom)
        assert underutilization_signals(dict(FRESH_ULTRA_CONFIG), gate, True) == []


class TestNudgeText:
    def test_empty_signals_yield_empty_text(self):
        assert nudge_text([]) == ""
        assert nudge_text(["not-a-signal"]) == ""

    def test_single_signal_reads_naturally(self):
        text = nudge_text(["gpu"])
        assert text == "Ultra tip 😇 — you can switch on GPU acceleration."

    def test_two_signals_join_with_and(self):
        text = nudge_text(["model", "gpu"])
        assert " and " in text
        assert ", and " not in text  # serial comma is for 3+ items only

    def test_many_signals_use_serial_comma(self):
        text = nudge_text(["model", "gpu", "cleanup", "chunking"])
        for phrase in SIGNAL_PHRASES.values():
            assert phrase in text
        assert ", and " in text
