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


def test_corrections_are_idempotent_when_the_spelling_contains_the_heard_phrase():
    pairs = [["aura", "Wayfinder Aura"]]
    once = T.apply_vocabulary_replacements("try aura today", pairs)
    assert once == "try Wayfinder Aura today"
    assert T.apply_vocabulary_replacements(once, pairs) == once
    assert T.apply_vocabulary_replacements("wayfinder aura rocks", pairs) == "Wayfinder Aura rocks"


def test_inserted_text_is_not_rescanned_by_other_corrections():
    pairs = [["git", "git commit"], ["commit", "Commit"]]
    out = T.apply_vocabulary_replacements("run git now", pairs)
    assert out == "run git commit now"
    assert T.apply_vocabulary_replacements(out, pairs) == out


def test_two_column_corrections_editor_rows():
    """Settings ▸ Vocabulary: one Heard and one Write-as field per correction."""
    import wayfinder_main as wm

    pairs, incomplete = wm._collect_vocabulary_corrections([
        ("Iran", "Arawn"),          # the field case: a name the model keeps mishearing
        ("  way finder ", "Wayfinder"),
        ("", ""),                   # blank row: ignored
        ("aura", ""),               # one side only: counted, not saved
        ("", "Cyrus"),
        ("iran", "Arawn"),          # duplicate heard (any case): first wins
        ("same", "same"),           # no-op correction: dropped
    ])
    assert pairs == [("Iran", "Arawn"), ("way finder", "Wayfinder")]
    assert incomplete == 2


# ------------------------------------------------------------ near-miss snapping
# Turbo Q5 wrote "Aeron" (and, on Linux, "Arrhawn") for "Arawn" even with the
# name in the prompt; an exact correction can't list every spelling.
TERMS = ["Daan", "Arawn", "Aura", "Wayfinder", "Cyrus", "Main", "Qwen"]


def test_sound_key_groups_spellings_that_sound_alike():
    assert {T.vocabulary_sound_key(w) for w in ("Arawn", "Aeron", "Arrhawn")} == {"*R*N"}
    assert T.vocabulary_sound_key("Claude") == T.vocabulary_sound_key("Klawd")
    # Vowel positions keep real words apart from a term (found on /usr/share/dict/words).
    assert T.vocabulary_sound_key("collide") != T.vocabulary_sound_key("Claude")
    assert T.vocabulary_sound_key("Philip") == T.vocabulary_sound_key("Filip")
    assert T.vocabulary_sound_key("Daan") == T.vocabulary_sound_key("Dahn")
    assert T.vocabulary_sound_key("Cyrus") != T.vocabulary_sound_key("Virus")


@pytest.mark.parametrize("heard", ["Aeron", "Arrhawn", "aeron"])
def test_near_misses_of_a_term_get_the_users_spelling(heard):
    out = T.snap_near_vocabulary(f"visual work, and {heard}, to blend in", TERMS)
    assert out == "visual work, and Arawn, to blend in"


def test_possessive_keeps_its_suffix():
    assert T.snap_near_vocabulary("Aeron's sword", TERMS) == "Arawn's sword"


@pytest.mark.parametrize("word", ["Aaron", "Iran", "irony", "error", "Cyprus", "Dawn", "Aryan"])
def test_ordinary_english_is_never_snapped(word):
    """Real words (top 60k) need an explicit correction instead."""
    assert T.snap_near_vocabulary(f"{word} today", TERMS) == f"{word} today"


def test_short_terms_need_the_same_first_letter_and_a_close_spelling():
    assert T.snap_near_vocabulary("Aira and Dahn", TERMS) == "Aura and Daan"
    assert T.snap_near_vocabulary("the Oura ring", TERMS) == "the Oura ring"


def test_acronyms_short_words_and_mixed_tokens_are_left_alone():
    """Found on the repo's docs: KWin -> Qwen, WayfinderAura -> Wayfinder, aren't."""
    text = "ARON arn Aeron2 Aeron-like KWin WayfinderAura aren't"
    assert T.snap_near_vocabulary(text, TERMS) == text


def test_a_tie_between_two_terms_changes_nothing():
    assert T.snap_near_vocabulary("Arun", ["Aran", "Aron"]) == "Arun"


def test_multi_word_and_non_letter_terms_do_not_snap():
    assert T.snap_near_vocabulary("Wayfindr", ["Way Finder", "k8s"]) == "Wayfindr"


def test_snapping_is_idempotent_and_runs_after_corrections():
    pairs = [["Iran", "Arawn"], ["Maine", "Main"]]
    once = T.apply_vocabulary_replacements("Iran met Aeron in Maine", pairs, TERMS)
    assert once == "Arawn met Arawn in Main"
    assert T.apply_vocabulary_replacements(once, pairs, TERMS) == once


def test_correction_spellings_are_snap_targets(ultra):
    cfg = {"custom_vocabulary": ["Daan"], "vocabulary_replacements": [["Iran", "Arawn"]]}
    assert T.licensed_vocabulary_terms(cfg) == ["Daan", "Arawn"]


def test_snapping_is_ultra_only(free):
    cfg = {"custom_vocabulary": ["Arawn"]}
    assert T.licensed_vocabulary_terms(cfg) == []


def test_transcription_snaps_near_misses(ultra, monkeypatch, tmp_path):
    class _Backend:
        custom_vocabulary = []

        def transcribe(self, path, context=""):
            return "and aeron to really blend in"

    monkeypatch.setattr(T, "get_backend", lambda cfg: _Backend())
    cfg = {"post_processing_enabled": False, "ensure_punctuation": False,
           "custom_vocabulary": ["Arawn"]}
    out = T.transcribe_with_config(str(tmp_path / "a.wav"), cfg, skip_post_processing=True)
    assert "Arawn" in out and "aeron" not in out.lower()


def test_cleanup_output_gets_near_misses_snapped(ultra, monkeypatch):
    from wayfinder.core import postprocessor as P

    monkeypatch.setattr(P, "_process_with_config", lambda text, cfg: "Ask Arrhawn.")
    assert P.process_with_config("x", {"custom_vocabulary": ["Arawn"]}) == "Ask Arawn."


def test_free_transcription_is_not_snapped(free, monkeypatch, tmp_path):
    class _Backend:
        custom_vocabulary = []

        def transcribe(self, path, context=""):
            return "and aeron to really blend in"

    monkeypatch.setattr(T, "get_backend", lambda cfg: _Backend())
    cfg = {"post_processing_enabled": False, "ensure_punctuation": False,
           "custom_vocabulary": ["Arawn"]}
    out = T.transcribe_with_config(str(tmp_path / "a.wav"), cfg, skip_post_processing=True)
    assert "Arawn" not in out


# ------------------------------------------------------------ style lists (Dev, Casual)
def test_dev_style_names_snap():
    assert T.snap_near_vocabulary("switched to Kwenn", [], T.DEV_VOCABULARY) == "switched to Qwen"


def test_sound_alike_names_in_one_list_never_replace_each_other():
    assert T.snap_near_vocabulary("Groq and Grok", [], T.DEV_VOCABULARY) == "Groq and Grok"


def test_common_style_words_are_not_snap_targets_but_user_terms_are():
    """Whisper spells "commit" right; "committ" is only fixed if the user asked."""
    assert T.snap_near_vocabulary("committ it", [], ["commit"]) == "committ it"
    assert T.snap_near_vocabulary("committ it", ["commit"]) == "commit it"


def test_a_user_term_wins_over_the_style_list():
    assert T.snap_near_vocabulary("Kwenn", ["Kwen"], ["Qwen"]) == "Kwen"
    assert T.snap_near_vocabulary("Kwenn", [], ["Qwen"]) == "Qwen"


def test_style_list_follows_the_selected_style(ultra):
    assert T.licensed_style_vocabulary({"output_tone": "dev"}) == T.DEV_VOCABULARY
    assert T.licensed_style_vocabulary({"output_tone": "casual"}) == T.CASUAL_VOCABULARY
    assert T.licensed_style_vocabulary({"output_tone": "professional"}) == []


def test_style_list_is_ultra_only(free):
    assert T.licensed_style_vocabulary({"output_tone": "dev"}) == []


def test_dev_ai_names_fit_the_prompt_next_to_user_terms(ultra, tmp_path):
    model = tmp_path / "ggml-base.en.bin"
    model.write_bytes(b"\0")
    cfg = {"transcription_backend": "whisper_cpp", "model_path": str(model),
           "whisper_server_mode": False, "output_tone": "dev",
           "custom_vocabulary": ["Daan", "Arawn", "Aura", "Wayfinder", "Cyrus"]}
    vocab = T.get_backend(cfg).custom_vocabulary
    assert {"Qwen", "Claude", "Ollama", "kubectl"} <= set(vocab)
    assert vocab[-5:] == ["Daan", "Arawn", "Aura", "Wayfinder", "Cyrus"]


def test_dev_style_transcription_snaps_tool_names(ultra, monkeypatch, tmp_path):
    class _Backend:
        custom_vocabulary = []

        def transcribe(self, path, context=""):
            return "pull it with olamma and ask kwenn"

    monkeypatch.setattr(T, "get_backend", lambda cfg: _Backend())
    cfg = {"post_processing_enabled": False, "ensure_punctuation": False, "output_tone": "dev"}
    out = T.transcribe_with_config(str(tmp_path / "a.wav"), cfg, skip_post_processing=True)
    assert "Ollama" in out and "Qwen" in out
