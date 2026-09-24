"""Styles that can't work with the current cleanup setup are unavailable, with
a reason - and are never run (fallback to Standard, then Normal)."""
from __future__ import annotations

import pytest

from wayfinder.core import postprocessor as P


@pytest.fixture
def model(tmp_path):
    def make(name):
        path = tmp_path / name
        path.write_bytes(b"\0")
        return str(path)
    return make


def _cfg(model_path, **kw):
    cfg = {"post_processing_enabled": True, "post_processing_backend": "llama_cpp",
           "llama_cpp_model_path": model_path}
    cfg.update(kw)
    return cfg


def test_normal_is_always_available(tmp_path):
    table = P.style_availability({"post_processing_enabled": False})
    assert table["minimal"] == {"standard": (True, None), "strong": (True, None)}


def test_styles_need_cleanup_turned_on(model):
    table = P.style_availability(_cfg(model("google_gemma-3-1b-it-Q4_K_M.gguf"),
                                      post_processing_enabled=False))
    ok, reason = table["professional"]["standard"]
    assert ok is False and "Turn on Post-Processing" in reason


def test_styles_need_a_cleanup_model_on_disk(tmp_path):
    table = P.style_availability(_cfg(str(tmp_path / "missing.gguf")))
    ok, reason = table["casual"]["standard"]
    assert ok is False and "Download a cleanup model" in reason


def test_strong_needs_a_bigger_model(model):
    table = P.style_availability(_cfg(model("google_gemma-3-1b-it-Q4_K_M.gguf")))
    assert table["dev"]["standard"] == (True, None)
    ok, reason = table["dev"]["strong"]
    assert ok is False and "Qwen3 4B" in reason


def test_a_4b_model_offers_strong(model):
    table = P.style_availability(_cfg(model("Qwen_Qwen3-4B-Instruct-2507-Q4_K_M.gguf")))
    assert table["professional"]["strong"] == (True, None)


def test_graded_failures_are_unavailable(model, monkeypatch):
    monkeypatch.setitem(P.STYLE_SUPPORT, "tinytest", {
        "name": "Tiny Test", "unsupported": {("professional", "standard")}, "better": "Gemma 3 1B"})
    table = P.style_availability(_cfg(model("tinytest-1b.gguf")))
    ok, reason = table["professional"]["standard"]
    assert ok is False and reason == "Tiny Test can't do Professional reliably — try Gemma 3 1B."
    assert table["casual"]["standard"] == (True, None)


def test_effective_style_falls_back_to_standard_then_normal(model, monkeypatch):
    gemma = model("google_gemma-3-1b-it-Q4_K_M.gguf")
    assert P.effective_style(_cfg(gemma, output_tone="dev", strong_mode=True))[:2] == ("dev", "standard")
    monkeypatch.setitem(P.STYLE_SUPPORT, "gemma-3-1b", {
        "name": "Gemma 3 1B", "unsupported": {("dev", "standard")}})
    assert P.effective_style(_cfg(gemma, output_tone="dev", strong_mode=True))[:2] == ("minimal", "standard")


def test_cleanup_runs_the_effective_style(model, monkeypatch):
    import wayfinder.license as lic
    from types import SimpleNamespace

    monkeypatch.setattr(lic, "get_feature_gate",
                        lambda *a, **k: SimpleNamespace(is_premium=True, has_feature=lambda f: True))
    seen = {}

    class _Backend:
        def process(self, text):
            return text

    def fake_get_backend(cfg):
        seen["tone"], seen["strong"] = cfg.get("output_tone"), cfg.get("strong_mode")
        return _Backend()

    monkeypatch.setattr(P, "get_backend", fake_get_backend)
    cfg = _cfg(model("google_gemma-3-1b-it-Q4_K_M.gguf"), output_tone="professional",
               strong_mode=True)
    P.process_with_config("please send the quarterly report to the whole team today", cfg)
    assert seen == {"tone": "professional", "strong": False}
