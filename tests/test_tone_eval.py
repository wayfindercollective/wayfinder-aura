"""Fast, GPU-free, mock-free tests for the tone work.

Covers:
- the re-tuned standard tone-guidance strings (the "guide, not rewrite" intent),
- the CLI backend's pure prompt builder + the tone-collapse regression canary,
- the deterministic eval metrics.

These need NO model and NO GPU — they assert on pure functions only.
Run on the host python:
  flatpak-spawn --host sh -c 'cd <repo>; PYTHONPATH=src \
      python3 -m pytest tests/test_tone_eval.py -q'
"""
import sys
from pathlib import Path

import pytest

# Make the scripts/ harness package importable (conftest already adds src/).
_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from wayfinder.core.postprocessor import (  # noqa: E402
    FILLER_RULES,
    FORMATTING_RULES,
    LlamaCppCliBackend,
    _is_reasoning_model,
    get_tone_guidance,
)
from tone_eval import metrics as M  # noqa: E402
from tone_eval.corpus import CORPUS  # noqa: E402


# ---------------------------------------------------------------------------
# (a) Re-tuned standard tone strings encode "guide, not rewrite"
# ---------------------------------------------------------------------------
class TestToneGuidanceStrings:
    def test_dev_standard_preserves_terms_and_phrasing(self):
        # Imperative phrasing (no bare "Developer context." opener, which Gemma-1B
        # echoed → dev had no output). Must still preserve technical terms + no-rewrite.
        g = (get_tone_guidance("dev", "standard")
             + " " + FORMATTING_RULES["dev"]["standard"]).lower()
        assert "code" in g and "technical terms" in g  # preserves dev vocabulary
        assert "restructure" in g  # "Do not restructure"
        assert not get_tone_guidance("dev", "standard").lower().startswith("developer context")

    def test_casual_standard_allows_lowercase_relaxed(self):
        g = FORMATTING_RULES["casual"]["standard"].lower()
        assert "lowercase" in g or "relaxed" in g
        assert "optional" in g  # periods optional

    def test_professional_standard_demands_caps_and_punct(self):
        g = FORMATTING_RULES["professional"]["standard"].lower()
        assert "capitalization" in g and "punctuation" in g

    def test_styled_standards_keep_no_rewrite_guard(self):
        for tone in ("professional", "casual", "dev"):
            g = get_tone_guidance(tone, "standard").lower()
            assert ("do not restructure" in g) or ("do not rewrite" in g)

    def test_dev_keeps_discourse_markers(self):
        assert "basically" in FILLER_RULES["dev"]["standard"].lower()


# ---------------------------------------------------------------------------
# (b) CLI backend routes tone correctly — the bug + the fix
# ---------------------------------------------------------------------------
class TestCliPromptRouting:
    @pytest.fixture
    def be(self):
        return LlamaCppCliBackend()  # no model needed; build_cli_prompt is pure

    def test_minimal_prompt_is_filler_only(self, be):
        p = be.build_cli_prompt("um i went to the store today", "minimal").lower()
        assert "remove only filler sounds" in p

    def test_dev_prompt_is_not_minimal(self, be):
        p = be.build_cli_prompt("commit the boolean fix on the branch", "dev").lower()
        assert "remove only filler sounds" not in p  # must NOT collapse to minimal
        assert "developer" in p or "git" in p

    def test_casual_prompt_relaxed(self, be):
        p = be.build_cli_prompt("gonna head out in like ten minutes", "casual").lower()
        assert "lowercase" in p or "relaxed" in p

    def test_professional_prompt_tidies(self, be):
        p = be.build_cli_prompt("oh thats tight bro the numbers", "professional").lower()
        assert "punctuation" in p or "capitaliz" in p

    def test_all_styled_prompts_keep_90_guard(self, be):
        for tone in ("dev", "casual", "professional"):
            p = be.build_cli_prompt("the boolean flag is false today", tone).lower()
            assert "90 percent" in p
            assert "do not rewrite" in p

    def test_three_tones_produce_three_distinct_prompts(self, be):
        # REGRESSION CANARY: before the fix, dev/casual/professional all
        # collapsed onto one of two generic prompts.
        ps = {t: be.build_cli_prompt("the boolean flag is false", t)
              for t in ("dev", "casual", "professional")}
        assert len(set(ps.values())) == 3


# ---------------------------------------------------------------------------
# (c) Deterministic metrics behave
# ---------------------------------------------------------------------------
class TestMetrics:
    def test_retention_full_when_identical(self):
        m = M.compute_all({"id": "x", "stresses": []}, "dev",
                          "git commit the fix", "git commit the fix")
        assert m["word_retention"] == pytest.approx(1.0)

    def test_retention_ignores_filler(self):
        m = M.compute_all({"id": "x", "stresses": []}, "minimal",
                          "um i went to the store", "I went to the store.")
        assert m["passes"]["retention"]

    def test_order_lcs_drops_on_reorder(self):
        m = M.compute_all({"id": "x", "stresses": []}, "dev",
                          "first we commit then we push",
                          "then we push first we commit")
        assert m["order_lcs_ratio"] < 0.8

    def test_length_blowup_flagged(self):
        m = M.compute_all({"id": "x", "stresses": []}, "professional",
                          "send the deck",
                          "Please kindly send over the updated deck at your "
                          "earliest convenience thank you so much indeed today.")
        assert not m["passes"]["len_ratio"]

    def test_dev_term_preservation_passes_on_echo(self):
        s = next(s for s in CORPUS if s["id"] == "dev_01_git_flow")
        m = M.compute_all(s, "dev", s["text"], s["text"])
        assert m["passes"]["dev_term_preservation"]

    def test_dev_term_preservation_fails_when_dropped(self):
        s = next(s for s in CORPUS if s["id"] == "dev_01_git_flow")
        m = M.compute_all(s, "dev", s["text"], "I need to do the fix and review")
        assert not m["passes"]["dev_term_preservation"]

    def test_prof_slang_removal_metric(self):
        s = next(s for s in CORPUS if s["id"] == "prof_01_slang_to_tidy")
        m = M.compute_all(s, "professional", s["text"],
                          "The quarterly numbers came in better than expected.")
        assert m["passes"]["prof_slang_removal"]

    def test_prof_slang_remaining_flags_leftover(self):
        s = next(s for s in CORPUS if s["id"] == "prof_01_slang_to_tidy")
        m = M.compute_all(s, "professional", s["text"],
                          "oh thats tight bro the numbers came in.")
        assert not m["passes"]["prof_slang_removal"]

    def _noop(self):
        pass


# ---------------------------------------------------------------------------
# (d) Reasoning-model handling (Qwen3/3.5 emit <think> blocks)
# ---------------------------------------------------------------------------
class TestReasoningModel:
    @pytest.mark.parametrize("name,expected", [
        ("Qwen3.5-2B-Q4_K_M.gguf", True),
        ("Qwen3-1.7B-Q4_K_M.gguf", True),
        ("QwQ-32B.gguf", True),
        ("DeepSeek-R1-Distill.gguf", True),
        ("qwen2.5-1.5b-instruct-q4_k_m.gguf", False),
        ("google_gemma-3-1b-it-Q4_K_M.gguf", False),
        ("LFM2.5-1.2B-Instruct-Q4_K_M.gguf", False),
    ])
    def test_detection(self, name, expected):
        assert _is_reasoning_model(name) is expected

    def test_reasoning_backend_prefills_think_block(self):
        be = LlamaCppCliBackend(model_path="/x/Qwen3.5-2B-Q4_K_M.gguf")
        p = be.build_cli_prompt("clean this text up please ok", "professional")
        assert p.rstrip().endswith("</think>")  # think block pre-filled

    def test_nonreasoning_backend_has_no_prefill(self):
        be = LlamaCppCliBackend(model_path="/x/google_gemma-3-1b-it-Q4_K_M.gguf")
        p = be.build_cli_prompt("clean this text up please ok", "professional")
        assert "<think>" not in p
        assert p.rstrip().endswith("Cleaned text:")


# ---------------------------------------------------------------------------
# (e) CLI output extraction (split-on-prompt, think-strip, echo-reject)
# ---------------------------------------------------------------------------
class TestExtractCliOutput:
    @pytest.fixture
    def be(self):
        return LlamaCppCliBackend()

    def test_takes_text_after_prompt(self, be):
        prompt = "Task: clean.\n\nText: hello\n\nCleaned text:"
        stdout = prompt + " Hello there."
        assert be._extract_cli_output(stdout, prompt) == "Hello there."

    def test_strips_leading_think_block(self, be):
        prompt = "Cleaned text:"
        stdout = prompt + "<think>\nreasoning\n</think>\nThe cleaned text."
        assert be._extract_cli_output(stdout, prompt) == "The cleaned text."

    def test_cuts_trailing_annotation(self, be):
        prompt = "Cleaned text:"
        stdout = prompt + " The cleaned text.\n\n**Changes made:**\n- removed um"
        assert be._extract_cli_output(stdout, prompt) == "The cleaned text."

    def test_echo_of_guidance_is_rejected(self, be):
        prompt = "Cleaned text:"
        stdout = prompt + " Developer context. Keep code and git terms intact."
        assert be._extract_cli_output(stdout, prompt) == ""


class TestSummarizeMore:
    def test_summarize_rolls_up_per_tone(self):
        recs = [
            {"tone": "dev", "latency_s": 1.0,
             "metrics": M.compute_all({"id": "a", "stresses": []}, "dev",
                                      "git commit the fix", "git commit the fix.")},
            {"tone": "casual", "latency_s": 2.0,
             "metrics": M.compute_all({"id": "b", "stresses": []}, "casual",
                                      "gonna grab food later", "gonna grab food later")},
        ]
        s = M.summarize(recs)
        assert set(s.keys()) == {"dev", "casual"}
        assert s["dev"]["n"] == 1
        assert 0.0 <= s["dev"]["mean_guide_score"] <= 1.0
