"""Ultra Custom Vocabulary: Whisper prompt budget + "heard -> write" corrections."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from wayfinder.core import transcriber as T


@pytest.fixture
def ultra(monkeypatch):
    import wayfinder.license as lic

    gate = SimpleNamespace(is_premium=True, has_feature=lambda f: True)
    monkeypatch.setattr(lic, "get_feature_gate", lambda *a, **k: gate)
    return gate


@pytest.fixture
def free(monkeypatch):
    import wayfinder.license as lic

    gate = SimpleNamespace(is_premium=False, has_feature=lambda f: False)
    monkeypatch.setattr(lic, "get_feature_gate", lambda *a, **k: gate)
    return gate


# ------------------------------------------------------------ prompt budget
def test_terms_are_trimmed_and_deduplicated_case_insensitively():
    assert T.normalize_vocabulary_terms(["  Wayfinder ", "wayfinder", "", "Kubernetes"]) == [
        "Wayfinder", "Kubernetes"]


def test_user_terms_go_last_so_whisper_keeps_them():
    order = T.vocabulary_prompt_terms(["Wayfinder", "Aura"], ["git", "commit"])
    assert order[-2:] == ["Wayfinder", "Aura"]
    assert order[:2] == ["git", "commit"]


def test_user_terms_take_the_budget_before_builtin_lists():
    user = [f"Name{i:03d}" for i in range(30)]          # 30 x 9 chars
    builtin = [f"builtin{i:03d}" for i in range(200)]
    order = T.vocabulary_prompt_terms(user, builtin, budget=300)
    assert [t for t in order if t.startswith("Name")] == user
    assert sum(len(t) + 2 for t in order) <= 300


def test_budget_keeps_the_users_first_terms_when_they_alone_overflow():
    user = [f"Term{i:03d}" for i in range(100)]
    order = T.vocabulary_prompt_terms(user, [], budget=90)
    assert order == user[:len(order)] and len(order) == 90 // 9


def test_builtin_duplicates_of_user_terms_are_dropped():
    assert T.vocabulary_prompt_terms(["Git"], ["git", "branch"]) == ["branch", "Git"]


# --------------------------------------------------------------- corrections
def test_replacement_formats_are_all_accepted():
    pairs = T.parse_vocabulary_replacements([
        ["way finder", "Wayfinder"], {"heard": "cube cuddle", "write": "kubectl"},
        "get hub -> GitHub", "eye oh ess → iOS", "bad", ["same", "same"], ["", "x"],
    ])
    assert pairs == [("way finder", "Wayfinder"), ("cube cuddle", "kubectl"),
                     ("get hub", "GitHub"), ("eye oh ess", "iOS")]


def test_replacements_match_whole_words_in_any_case_and_spacing():
    pairs = [["way finder", "Wayfinder"], ["aura", "Aura"]]
    out = T.apply_vocabulary_replacements("I love Way  Finder and aura, not auras.", pairs)
    assert out == "I love Wayfinder and Aura, not auras."


def test_longest_phrase_wins():
    pairs = [["way finder", "Wayfinder"], ["way finder aura", "Wayfinder Aura"]]
    assert T.apply_vocabulary_replacements("open way finder aura now", pairs) == \
        "open Wayfinder Aura now"


def test_replacement_text_is_literal_not_a_regex_template():
    assert T.apply_vocabulary_replacements("price is five", [["five", r"\\1 $5"]]) == \
        r"price is \\1 $5"


def test_corrections_are_ultra_only(free):
    cfg = {"vocabulary_replacements": [["way finder", "Wayfinder"]]}
    assert T.licensed_vocabulary_replacements(cfg) == []


def test_corrections_apply_with_ultra(ultra):
    cfg = {"vocabulary_replacements": [["way finder", "Wayfinder"]]}
    assert T.licensed_vocabulary_replacements(cfg) == [("way finder", "Wayfinder")]


# ------------------------------------------------------------ wiring (Ultra)
def test_backend_gets_budgeted_vocabulary_with_user_terms_last(ultra, tmp_path):
    model = tmp_path / "ggml-base.en.bin"
    model.write_bytes(b"\0")
    cfg = {"transcription_backend": "whisper_cpp", "model_path": str(model),
           "whisper_server_mode": False, "output_tone": "dev",
           "custom_vocabulary": ["Wayfinder"],
           "vocabulary_replacements": [["cube cuddle", "kubectl"]]}
    backend = T.get_backend(cfg)
    vocab = backend.custom_vocabulary
    assert vocab[-2:] == ["Wayfinder", "kubectl"]
    assert "git" in vocab  # built-in dev list still there, in front
    assert sum(len(t) + 2 for t in vocab) <= T.VOCAB_PROMPT_CHAR_BUDGET


def test_free_backend_gets_no_user_vocabulary(free, tmp_path):
    model = tmp_path / "ggml-base.en.bin"
    model.write_bytes(b"\0")
    cfg = {"transcription_backend": "whisper_cpp", "model_path": str(model),
           "whisper_server_mode": False, "custom_vocabulary": ["Wayfinder"],
           "vocabulary_replacements": [["way finder", "Wayfinder"]]}
    assert T.get_backend(cfg).custom_vocabulary == []


def test_transcription_applies_corrections_after_caps_fixes(ultra, monkeypatch, tmp_path):
    class _Backend:
        custom_vocabulary = []

        def transcribe(self, path, context=""):
            return "we pushed it to get hub and way finder"

    monkeypatch.setattr(T, "get_backend", lambda cfg: _Backend())
    cfg = {"post_processing_enabled": False, "output_tone": "professional",
           "ensure_punctuation": False,
           "vocabulary_replacements": [["get hub", "GitHub"], ["way finder", "Wayfinder"]]}
    out = T.transcribe_with_config(str(tmp_path / "a.wav"), cfg, skip_post_processing=True)
    assert "GitHub" in out and "Wayfinder" in out


def test_cleanup_output_gets_corrections_reapplied(ultra, monkeypatch):
    from wayfinder.core import postprocessor as P

    monkeypatch.setattr(P, "_process_with_config", lambda text, cfg: "Ship it on Way Finder.")
    cfg = {"vocabulary_replacements": [["way finder", "Wayfinder"]]}
    assert P.process_with_config("x", cfg) == "Ship it on Wayfinder."


def test_cleanup_leaves_text_alone_on_free(free, monkeypatch):
    from wayfinder.core import postprocessor as P

    monkeypatch.setattr(P, "_process_with_config", lambda text, cfg: "Ship it on Way Finder.")
    cfg = {"vocabulary_replacements": [["way finder", "Wayfinder"]]}
    assert P.process_with_config("x", cfg) == "Ship it on Way Finder."
