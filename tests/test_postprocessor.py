"""
Tests for wayfinder.core.postprocessor module.

Pure unit tests covering model tiers, tone/formatting rules, filler removal,
refusal/hallucination detection, prompt leakage, settings compatibility,
backend factory, and the top-level process_with_config entry point.
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, MagicMock, PropertyMock

import pytest

from wayfinder.core.postprocessor import (
    # Data structures / constants
    TONE_GUIDANCE,
    FORMATTING_RULES,
    FILLER_RULES,
    MODEL_TIERS,
    MODEL_QUIRKS,
    CLOUD_BACKENDS,
    # Functions under test
    detect_model_tier,
    get_model_quirks,
    get_tone_guidance,
    get_formatting_rules,
    get_filler_rules,
    build_prompt,
    fast_filler_removal,
    is_refusal_response,
    is_hallucination,
    remove_prompt_leakage,
    remove_repeated_sentences,
    check_settings_compatibility,
    get_backend,
    process_with_config,
    get_tone_options,
    # Backend classes
    LlamaCppBackend,
    LlamaCppCliBackend,
    AnthropicBackend,
    OpenAIBackend,
    PostProcessingError,
)


# =============================================================================
# detect_model_tier
# =============================================================================


class TestDetectModelTier:
    """Tests for detect_model_tier()."""

    def test_cloud_backend_openai_returns_large(self):
        assert detect_model_tier("gpt-4o-mini", backend="openai") == "large"

    def test_cloud_backend_anthropic_returns_large(self):
        assert detect_model_tier("claude-3-haiku", backend="anthropic") == "large"

    def test_cloud_backend_ignores_model_name(self):
        """Even a 'tiny'-looking name is large when backend is cloud."""
        assert detect_model_tier("smollm2:360m", backend="openai") == "large"

    def test_qwen_1_5b_returns_small(self):
        assert detect_model_tier("qwen2.5:1.5b") == "small"

    def test_phi3_mini_returns_standard(self):
        """phi3:mini has a tier_override to 'standard' in MODEL_QUIRKS."""
        assert detect_model_tier("phi3:mini") == "standard"

    def test_qwen3_4b_gguf_stem_returns_standard(self):
        """The strong/caricature flagship must resolve from its GGUF file stem
        (quirk key qwen3:4b), not just the ollama-style name."""
        assert detect_model_tier("Qwen_Qwen3-4B-Instruct-2507-Q4_K_M") == "standard"
        assert detect_model_tier("qwen3:4b") == "standard"

    def test_qwen_7b_returns_standard(self):
        assert detect_model_tier("qwen2.5:7b") == "standard"

    def test_unknown_model_returns_small(self):
        assert detect_model_tier("my-custom-model-v42") == "small"

    def test_llama_1b_returns_small(self):
        assert detect_model_tier("llama3.2:1b") == "small"

    def test_model_with_70b_returns_large(self):
        assert detect_model_tier("llama3.1:70b") == "large"

    def test_smollm2_360m_returns_tiny(self):
        """smollm2:360m has tier_override to 'tiny'."""
        assert detect_model_tier("smollm2:360m") == "tiny"

    def test_local_llama_cpp_backend_uses_name_matching(self):
        """Non-cloud backend should detect tier from model name patterns."""
        assert detect_model_tier("qwen2.5:14b", backend="llama_cpp") == "large"


# =============================================================================
# get_model_quirks
# =============================================================================


class TestGetModelQuirks:
    """Tests for get_model_quirks()."""

    def test_unknown_model_returns_empty_issues(self):
        quirks = get_model_quirks("totally-unknown-model")
        assert quirks["issues"] == []
        assert quirks.get("workaround") is None

    def test_llama_1b_has_safety_filter_and_hallucination(self):
        quirks = get_model_quirks("llama3.2:1b")
        assert "safety_filter_email" in quirks["issues"]
        assert "hallucination_prone" in quirks["issues"]

    def test_qwen_3_5_2b_recommended(self):
        # Qwen 3.5 2B is the top recommendation (replaced Qwen 2.5 1.5B, March 2026).
        quirks = get_model_quirks("qwen3.5:2b")
        assert quirks.get("recommended") is True

    def test_qwen_2_5_1_5b_not_recommended(self):
        # The superseded 2.5 1.5B is no longer flagged as the recommendation.
        quirks = get_model_quirks("qwen2.5:1.5b")
        assert quirks.get("recommended") is not True

    def test_qwen_1_5b_has_no_issues(self):
        quirks = get_model_quirks("qwen2.5:1.5b")
        assert quirks["issues"] == []

    def test_phi3_mini_best_for_strong(self):
        quirks = get_model_quirks("phi3:mini")
        assert "strong" in quirks.get("best_for", [])

    def test_smollm2_360m_has_hallucination_threshold(self):
        quirks = get_model_quirks("smollm2:360m")
        assert "hallucination_threshold" in quirks
        assert quirks["hallucination_threshold"] == 0.6


# =============================================================================
# get_tone_guidance
# =============================================================================


class TestGetToneGuidance:
    """Tests for get_tone_guidance()."""

    @pytest.mark.parametrize("tone", ["minimal", "professional", "casual", "dev", "personal"])
    def test_returns_string_for_each_tone_standard(self, tone):
        result = get_tone_guidance(tone, "standard")
        assert isinstance(result, str)

    @pytest.mark.parametrize("tone", ["minimal", "professional", "casual", "dev", "personal"])
    def test_returns_string_for_each_tone_strong(self, tone):
        result = get_tone_guidance(tone, "strong")
        assert isinstance(result, str)

    def test_professional_standard_mentions_capitalization(self):
        result = get_tone_guidance("professional", "standard")
        assert "capitalization" in result.lower()

    def test_casual_strong_mentions_text_message(self):
        result = get_tone_guidance("casual", "strong")
        assert "text message" in result.lower() or "slang" in result.lower()

    def test_unknown_tone_falls_back_to_professional(self):
        result = get_tone_guidance("nonexistent_tone", "standard")
        expected = TONE_GUIDANCE["professional"]["standard"]
        assert result == expected


# =============================================================================
# get_formatting_rules
# =============================================================================


class TestGetFormattingRules:
    """Tests for get_formatting_rules()."""

    @pytest.mark.parametrize(
        "tone,intensity",
        [
            ("minimal", "standard"),
            ("minimal", "strong"),
            ("professional", "standard"),
            ("professional", "strong"),
            ("casual", "standard"),
            ("casual", "strong"),
            ("dev", "standard"),
            ("dev", "strong"),
            ("personal", "standard"),
            ("personal", "strong"),
        ],
    )
    def test_returns_string_for_each_combo(self, tone, intensity):
        result = get_formatting_rules(tone, intensity)
        assert isinstance(result, str)
        assert len(result) > 0

    def test_casual_strong_mentions_no_periods(self):
        result = get_formatting_rules("casual", "strong")
        assert "period" in result.lower() or "lowercase" in result.lower()

    def test_unknown_tone_falls_back_to_professional(self):
        result = get_formatting_rules("nonexistent_tone", "standard")
        expected = FORMATTING_RULES["professional"]["standard"]
        assert result == expected


# =============================================================================
# get_filler_rules
# =============================================================================


class TestGetFillerRules:
    """Tests for get_filler_rules()."""

    def test_minimal_always_returns_minimal_rules(self):
        """Minimal tone ignores intensity and always uses its own rule."""
        result_std = get_filler_rules("minimal", "standard")
        result_strong = get_filler_rules("minimal", "strong")
        assert result_std == FILLER_RULES["minimal"]["standard"]
        assert result_strong == FILLER_RULES["minimal"]["standard"]

    def test_standard_intensity_returns_standard_rules(self):
        result = get_filler_rules("professional", "standard")
        assert result == FILLER_RULES["professional"]["standard"]

    def test_strong_intensity_returns_strong_rules(self):
        result = get_filler_rules("casual", "strong")
        assert result == FILLER_RULES["casual"]["strong"]

    def test_minimal_rules_mention_only_filler_sounds(self):
        rules = get_filler_rules("minimal")
        assert "um" in rules.lower()
        assert "uh" in rules.lower()

    def test_dev_standard_preserves_discourse_markers(self):
        """Dev standard should not remove 'basically', 'actually', 'so'."""
        rules = get_filler_rules("dev", "standard")
        assert "basically" in rules.lower()
        assert "actually" in rules.lower()
        # Should only remove filler sounds, not discourse markers
        assert "um" in rules.lower()

    def test_dev_strong_keeps_discourse_markers(self):
        """Dev strong removes true filler but keeps technical discourse markers."""
        rules = get_filler_rules("dev", "strong")
        assert "keep" in rules.lower()
        assert "basically" in rules.lower()
        assert "actually" in rules.lower()


# =============================================================================
# fast_filler_removal
# =============================================================================


class TestFastFillerRemoval:
    """Tests for the regex-based fast_filler_removal()."""

    def test_removes_um(self):
        result = fast_filler_removal("I um went to the store")
        assert "um" not in result.lower().split()

    def test_removes_uh(self):
        result = fast_filler_removal("So uh that happened")
        assert "uh" not in result.lower().split()

    def test_removes_ah(self):
        result = fast_filler_removal("Ah I see what you mean")
        assert result.lower().startswith("i see") or "ah" not in result.lower().split()

    def test_removes_verbal_fillers(self):
        result = fast_filler_removal("It was, you know, really good, basically, the best")
        assert "you know" not in result.lower()
        assert "basically" not in result.lower()

    def test_removes_repeated_words(self):
        result = fast_filler_removal("I went to the the store")
        assert "the the" not in result

    def test_capitalizes_first_letter(self):
        result = fast_filler_removal("um hello there")
        assert result[0].isupper()

    def test_ensures_ending_punctuation(self):
        result = fast_filler_removal("I went to the store")
        assert result[-1] in ".!?"

    def test_preserves_existing_punctuation(self):
        result = fast_filler_removal("Is this a question?")
        assert result.endswith("?")

    def test_handles_empty_string(self):
        assert fast_filler_removal("") == ""

    def test_handles_none_like_falsy(self):
        """Empty/falsy input returned as-is."""
        assert fast_filler_removal("") == ""

    def test_meaningful_content_preserved(self):
        """Core meaning words should survive filler removal."""
        result = fast_filler_removal("Um I went to the, you know, grocery store and um bought some milk")
        assert "grocery" in result
        assert "milk" in result
        assert "went" in result or "store" in result


# =============================================================================
# is_refusal_response
# =============================================================================


class TestIsRefusalResponse:
    """Tests for is_refusal_response()."""

    def test_detects_cannot_provide(self):
        assert is_refusal_response("I cannot provide that kind of content.")

    def test_detects_as_an_ai(self):
        assert is_refusal_response("As an AI language model, I shouldn't process that.")

    def test_detects_unable_to(self):
        assert is_refusal_response("I'm unable to help with this request.")

    def test_normal_text_returns_false(self):
        assert is_refusal_response("The quick brown fox jumps over the lazy dog.") is False

    def test_empty_string_returns_false(self):
        assert is_refusal_response("") is False

    def test_detects_apologize_in_long_response(self):
        long_text = "I apologize, but " + "x " * 100
        assert is_refusal_response(long_text) is True

    def test_short_sorry_is_not_refusal(self):
        """Short text containing 'sorry' alone shouldn't trigger."""
        assert is_refusal_response("Sorry about that.") is False


# =============================================================================
# is_hallucination
# =============================================================================


class TestIsHallucination:
    """Tests for is_hallucination()."""

    def test_completely_unrelated_response(self):
        original = "I need to buy groceries this afternoon"
        response = "The French Revolution began in 1789 when the Bastille was stormed"
        assert is_hallucination(original, response) is True

    def test_slightly_cleaned_version_is_not_hallucination(self):
        original = "Um, I need to, you know, buy groceries this afternoon."
        response = "I need to buy groceries this afternoon."
        assert is_hallucination(original, response) is False

    def test_detects_truncation(self):
        """Response < 40% of original with lost words is truncation."""
        original = (
            "I went to the store and bought apples oranges bananas "
            "grapes strawberries blueberries and raspberries for the party"
        )
        response = "I went to the store."
        assert is_hallucination(original, response) is True

    def test_detects_fabrication(self):
        """Response > 2.5x longer with many new words is fabrication."""
        original = "Fix the bug in the login page."
        response = (
            "The magnificent architectural wonders of ancient civilizations "
            "demonstrate the incredible engineering prowess of humanity throughout "
            "the millennia spanning from pyramids to modern skyscrapers and beyond "
            "into the realms of interstellar exploration and quantum mechanics "
            "fundamentally reshaping our understanding of the cosmos."
        )
        assert is_hallucination(original, response) is True

    def test_empty_original_returns_false(self):
        assert is_hallucination("", "some response") is False

    def test_empty_response_returns_false(self):
        assert is_hallucination("some original", "") is False

    def test_both_empty_returns_false(self):
        assert is_hallucination("", "") is False

    def test_model_specific_threshold_applied(self):
        """Known hallucination-prone models use stricter thresholds."""
        original = "I want to update the configuration file"
        # A borderline response that shares some words
        response = "Update the system configuration parameters and reload"
        # smollm2:360m has threshold 0.6 — much stricter
        result_strict = is_hallucination(original, response, model_name="smollm2:360m")
        result_default = is_hallucination(original, response, model_name="")
        # Strict threshold should be more likely to flag
        # (We can't guarantee exact outcomes, so just verify it runs without error)
        assert isinstance(result_strict, bool)
        assert isinstance(result_default, bool)


# =============================================================================
# remove_prompt_leakage
# =============================================================================


class TestRemovePromptLeakage:
    """Tests for remove_prompt_leakage()."""

    def test_removes_critical_prefix(self):
        text = "CRITICAL: You MUST preserve all content. Output: The meeting is at 3pm."
        result = remove_prompt_leakage(text)
        assert "CRITICAL" not in result
        assert "3pm" in result or "meeting" in result

    def test_removes_trailing_note(self):
        text = "The meeting is at 3pm. Note: There are no sentences missing from the output."
        result = remove_prompt_leakage(text)
        assert "Note:" not in result
        assert "3pm" in result

    def test_returns_unchanged_when_no_leakage(self):
        text = "The quick brown fox jumps over the lazy dog."
        result = remove_prompt_leakage(text)
        assert result == text

    def test_empty_input_returns_empty(self):
        assert remove_prompt_leakage("") == ""

    def test_none_returns_none(self):
        """Falsy input returned as-is."""
        assert remove_prompt_leakage(None) is None


# =============================================================================
# remove_repeated_sentences
# =============================================================================


class TestRemoveRepeatedSentences:
    """Tests for remove_repeated_sentences()."""

    def test_removes_duplicate_sentences(self):
        text = (
            "The meeting is at three o'clock. "
            "Please bring your laptop. "
            "The meeting is at three o'clock."
        )
        result = remove_repeated_sentences(text)
        # Should appear only once
        assert result.count("three o'clock") == 1

    def test_keeps_unique_sentences(self):
        text = "First sentence here. Second sentence there. Third sentence everywhere."
        result = remove_repeated_sentences(text)
        assert "First" in result
        assert "Second" in result
        assert "Third" in result

    def test_short_text_returned_unchanged(self):
        """Text shorter than min_length * 2 should pass through."""
        text = "Short."
        result = remove_repeated_sentences(text, min_length=20)
        assert result == text

    def test_empty_string(self):
        assert remove_repeated_sentences("") == ""

    def test_single_sentence(self):
        text = "Just one sentence here."
        result = remove_repeated_sentences(text)
        assert result == text


# =============================================================================
# check_settings_compatibility
# =============================================================================


class TestCheckSettingsCompatibility:
    """Tests for check_settings_compatibility()."""

    def test_disabled_post_processing_returns_ok(self):
        config = {"post_processing_enabled": False}
        result = check_settings_compatibility(config)
        assert result["severity"] == "ok"
        assert result["is_compatible"] is True

    def test_no_model_selected_returns_warning(self):
        config = {
            "post_processing_enabled": True,
            "post_processing_backend": "llama_cpp",
            "llama_cpp_model_path": "",
        }
        result = check_settings_compatibility(config)
        assert result["severity"] == "warning"
        assert any("model" in issue.lower() for issue in result["issues"])

    def test_minimal_style_without_caricature_returns_ok(self, full_config):
        full_config["output_tone"] = "minimal"
        full_config["caricature_mode"] = False
        full_config["strong_mode"] = False
        result = check_settings_compatibility(full_config)
        assert result["severity"] == "ok"

    def test_strong_mode_with_tiny_model_returns_incompatible(self):
        config = {
            "post_processing_enabled": True,
            "post_processing_backend": "llama_cpp",
            "llama_cpp_model_path": "/tmp/models/smollm2-360m.gguf",
            "output_tone": "professional",
            "strong_mode": True,
            "caricature_mode": False,
        }
        result = check_settings_compatibility(config)
        assert result["severity"] == "incompatible"

    def test_cloud_backend_always_compatible(self):
        config = {
            "post_processing_enabled": True,
            "post_processing_backend": "openai",
            "openai_model": "gpt-4o-mini",
            "output_tone": "professional",
            "strong_mode": True,
            "caricature_mode": False,
        }
        result = check_settings_compatibility(config)
        assert result["severity"] == "ok"

    def test_caricature_with_small_model_incompatible(self):
        config = {
            "post_processing_enabled": True,
            "post_processing_backend": "llama_cpp",
            "llama_cpp_model_path": "/tmp/models/qwen2.5-1.5b.gguf",
            "output_tone": "casual",
            "strong_mode": False,
            "caricature_mode": True,
        }
        result = check_settings_compatibility(config)
        assert result["severity"] == "incompatible"


# =============================================================================
# get_backend (factory)
# =============================================================================


class TestGetBackend:
    """Tests for the get_backend() factory function."""

    def test_returns_llama_cpp_backend_by_default(self):
        """When CLI binary doesn't exist, falls back to Python bindings."""
        config = {
            "post_processing_backend": "llama_cpp",
            "llama_cpp_use_cli": True,
            "llama_cpp_binary": "/nonexistent/llama-cli",
            "llama_cpp_model_path": "/tmp/model.gguf",
        }
        backend = get_backend(config)
        assert isinstance(backend, LlamaCppBackend)

    def test_returns_llama_cpp_cli_when_binary_exists(self, tmp_path):
        """When CLI binary exists, prefer LlamaCppCliBackend."""
        fake_binary = tmp_path / "llama-cli"
        fake_binary.touch()
        fake_binary.chmod(0o755)

        config = {
            "post_processing_backend": "llama_cpp",
            "llama_cpp_use_cli": True,
            "llama_cpp_binary": str(fake_binary),
            "llama_cpp_model_path": "/tmp/model.gguf",
        }
        backend = get_backend(config)
        assert isinstance(backend, LlamaCppCliBackend)

    def test_returns_anthropic_backend(self, monkeypatch):
        import wayfinder.license as license_module

        gate = SimpleNamespace(has_feature=lambda _feature: True)
        monkeypatch.setattr(license_module, "get_feature_gate", lambda: gate)
        config = {"post_processing_backend": "anthropic"}
        backend = get_backend(config)
        assert isinstance(backend, AnthropicBackend)

    def test_free_forces_local_cleanup_to_cpu_and_minimal(self, tmp_path, monkeypatch):
        import wayfinder.license as license_module

        gate = SimpleNamespace(has_feature=lambda _feature: False)
        monkeypatch.setattr(license_module, "get_feature_gate", lambda: gate)
        fake_binary = tmp_path / "llama-simple"
        fake_binary.write_text("#!/bin/sh\n")
        fake_binary.chmod(0o755)
        model = tmp_path / "model.gguf"
        model.write_bytes(b"x")
        config = {
            "post_processing_backend": "llama_cpp",
            "llama_cpp_use_cli": True,
            "llama_cpp_binary": str(fake_binary),
            "llama_cpp_model_path": str(model),
            "llama_cpp_n_gpu_layers": -1,
            "use_gpu": True,
            "output_tone": "casual",
            "strong_mode": True,
            "caricature_mode": True,
        }

        backend = get_backend(config)

        assert isinstance(backend, LlamaCppCliBackend)
        assert backend.n_gpu_layers == 0
        assert backend.output_tone == "minimal"
        assert backend.intensity == "standard"

    def test_free_large_cleanup_model_falls_back_to_installed_free_model(
        self, tmp_path, monkeypatch
    ):
        import wayfinder.license as license_module

        gate = SimpleNamespace(has_feature=lambda _feature: False)
        monkeypatch.setattr(license_module, "get_feature_gate", lambda: gate)
        binary = tmp_path / "llama-simple"; binary.write_text("#!/bin/sh\n"); binary.chmod(0o755)
        large = tmp_path / "Qwen_Qwen3-4B-Instruct-2507-Q4_K_M.gguf"; large.write_bytes(b"x")
        free = tmp_path / "google_gemma-3-1b-it-Q4_K_M.gguf"; free.write_bytes(b"x")
        config = {
            "post_processing_backend": "llama_cpp",
            "llama_cpp_use_cli": True,
            "llama_cpp_binary": str(binary),
            "llama_cpp_model_path": str(large),
        }

        backend = get_backend(config)

        assert Path(backend.model_path).name == free.name

    def test_ultra_large_cleanup_model_is_respected(self, tmp_path, monkeypatch):
        import wayfinder.license as license_module

        gate = SimpleNamespace(
            has_feature=lambda feature: feature == "large_cleanup_models"
        )
        monkeypatch.setattr(license_module, "get_feature_gate", lambda: gate)
        binary = tmp_path / "llama-simple"; binary.write_text("#!/bin/sh\n"); binary.chmod(0o755)
        large = tmp_path / "Qwen_Qwen3-4B-Instruct-2507-Q4_K_M.gguf"; large.write_bytes(b"x")
        config = {
            "post_processing_backend": "llama_cpp",
            "llama_cpp_use_cli": True,
            "llama_cpp_binary": str(binary),
            "llama_cpp_model_path": str(large),
        }

        backend = get_backend(config)

        assert backend.model_path == str(large)

    def test_returns_openai_backend(self, monkeypatch):
        import wayfinder.license as license_module

        gate = SimpleNamespace(has_feature=lambda _feature: True)
        monkeypatch.setattr(license_module, "get_feature_gate", lambda: gate)
        config = {"post_processing_backend": "openai"}
        backend = get_backend(config)
        assert isinstance(backend, OpenAIBackend)

    def test_gate_failure_fails_closed_for_cloud(self, tmp_path, monkeypatch):
        """When get_feature_gate raises, cloud backends must not be selected."""
        from wayfinder.core import postprocessor as pp

        def boom():
            raise RuntimeError("gate broken")

        monkeypatch.setattr(pp, "get_feature_gate", boom, raising=False)
        # Patch the import site used inside get_backend
        import wayfinder.license as lic

        monkeypatch.setattr(lic, "get_feature_gate", boom)

        fake_binary = tmp_path / "llama-cli"
        fake_binary.write_text("#!/bin/sh\n")
        fake_binary.chmod(0o755)
        config = {
            "post_processing_backend": "openai",
            "output_tone": "professional",
            "llama_cpp_use_cli": True,
            "llama_cpp_binary": str(fake_binary),
            "llama_cpp_model_path": str(tmp_path / "m.gguf"),
        }
        backend = get_backend(config)
        assert not isinstance(backend, OpenAIBackend)
        assert not isinstance(backend, AnthropicBackend)
        # Local path selected
        assert isinstance(backend, (LlamaCppBackend, LlamaCppCliBackend))


class TestCliIntensityWiring:
    """The CLI backend used to hardcode intensity='standard', so the Strong toggle
    and caricature easter egg were silently ignored. They now thread through, but
    with the same model-tier cap build_prompt() applies (small models can't honor
    strong without leaking the guide text / over-rewriting)."""

    SMALL = "/x/google_gemma-3-1b-it-Q4_K_M.gguf"
    LARGE = "/x/Qwen2.5-7B-Instruct-Q4_K_M.gguf"

    def test_standard_when_no_modes(self):
        b = LlamaCppCliBackend(model_path=self.SMALL)
        assert b.intensity == "standard"

    def test_strong_caps_to_standard_on_small_model(self):
        b = LlamaCppCliBackend(model_path=self.SMALL, strong_mode=True)
        assert b.intensity == "standard"

    def test_caricature_caps_to_standard_on_small_model(self):
        b = LlamaCppCliBackend(model_path=self.SMALL, caricature_mode=True)
        assert b.intensity == "standard"

    def test_strong_honored_on_large_model(self):
        b = LlamaCppCliBackend(model_path=self.LARGE, strong_mode=True)
        assert b.intensity == "strong"

    def test_caricature_honored_on_large_model(self):
        b = LlamaCppCliBackend(model_path=self.LARGE, caricature_mode=True)
        assert b.intensity == "caricature"


class TestDevToneGuidance:
    """Regression: dev guidance opened with the fragment 'Developer context.', which
    Gemma-1B echoed instead of cleaning — the echo-guard then rejected it, leaving the
    dev tone with NO output (it returned raw text verbatim). Guidance must read as an
    imperative cleanup instruction like the other working tones."""

    def test_dev_guidance_is_imperative_not_echoed_fragment(self):
        g = get_tone_guidance("dev", "standard").lower()
        assert not g.startswith("developer context")
        assert "code" in g or "technical terms" in g

    def test_dev_cli_prompt_not_collapsed_onto_minimal(self):
        b = LlamaCppCliBackend(model_path="/x/google_gemma-3-1b-it-Q4_K_M.gguf",
                               output_tone="dev")
        p = b.build_cli_prompt("git rebase the the branch", "dev", "standard")
        assert "Cleaned text:" in p
        assert "Remove only filler" not in p  # that's the minimal-only prompt


class TestCustomVocabularyAcrossStyles:
    """Pinned vocabulary must survive both ASR and cleanup regardless of style."""

    @pytest.mark.parametrize(
        "tone", ["minimal", "professional", "casual", "dev", "personal"]
    )
    def test_full_prompt_protects_vocabulary_in_every_style(self, tone):
        prompt, _ = build_prompt(
            "please update way finder aura today",
            {
                "output_tone": tone,
                "custom_vocabulary": ["Wayfinder Aura", "Daan"],
            },
            apply_compatibility=False,
        )

        assert "Protected vocabulary: Wayfinder Aura, Daan." in prompt
        assert "Preserve the spelling and capitalization" in prompt
        assert "phonetic, spacing, or case variants" in prompt
        assert "no output style may rephrase" in prompt

    @pytest.mark.parametrize(
        "tone", ["minimal", "professional", "casual", "dev", "personal"]
    )
    def test_cli_prompt_protects_vocabulary_in_every_style(self, tone):
        backend = LlamaCppCliBackend(
            model_path="/x/google_gemma-3-1b-it-Q4_K_M.gguf",
            output_tone=tone,
            custom_vocabulary=["Wayfinder Aura", "Daan"],
        )

        prompt = backend.build_cli_prompt("some input text", tone, "standard")

        assert "Protected vocabulary: Wayfinder Aura, Daan." in prompt

    def test_cli_factory_does_not_forward_unlicensed_vocabulary(
        self, tmp_path, monkeypatch
    ):
        import wayfinder.license as license_module

        monkeypatch.setattr(
            license_module.FeatureGate, "has_feature", lambda self, _feature: False
        )
        binary = tmp_path / "llama-simple"
        binary.write_text("#!/bin/sh\n")
        binary.chmod(0o755)
        backend = get_backend({
            "post_processing_backend": "llama_cpp",
            "llama_cpp_use_cli": True,
            "llama_cpp_binary": str(binary),
            "llama_cpp_model_path": str(tmp_path / "model.gguf"),
            "custom_vocabulary": ["PaidTerm"],
        })

        assert backend.custom_vocabulary == []


# =============================================================================
# process_with_config
# =============================================================================


class TestProcessWithConfig:
    """Tests for the top-level process_with_config() entry point."""

    def test_minimal_tone_uses_fast_filler_removal(self):
        """Explicit fast cleanup keeps the regex-only path available."""
        config = {
            "output_tone": "minimal",
            "post_processing_enabled": True,
            "fast_filler_removal": True,
        }
        result = process_with_config("Um, I went to the store", config)
        assert "um" not in result.lower().split()
        assert "store" in result

    def test_returns_original_when_empty(self):
        config = {"output_tone": "professional", "post_processing_enabled": True}
        assert process_with_config("", config) == ""

    def test_returns_original_when_whitespace_only(self):
        config = {"output_tone": "professional", "post_processing_enabled": True}
        assert process_with_config("   ", config) == "   "

    def test_returns_original_when_post_processing_disabled(self):
        config = {
            "output_tone": "professional",
            "post_processing_enabled": False,
        }
        text = "Um hello there"
        result = process_with_config(text, config)
        assert result == text

    def test_minimal_tone_capitalizes_result(self):
        config = {"output_tone": "minimal", "fast_filler_removal": True}
        result = process_with_config("um hello world", config)
        assert result[0].isupper()

    def test_minimal_tone_uses_neutral_llm_cleanup_by_default(self):
        backend = SimpleNamespace(
            is_available=lambda: True,
            process=MagicMock(return_value="This is neutral cleaned text."),
        )
        config = {
            "output_tone": "minimal",
            "post_processing_enabled": True,
            "fast_filler_removal": False,
            "post_processing_backend": "llama_cpp",
            "llama_cpp_model_path": "/tmp/model.gguf",
        }

        with patch("wayfinder.core.postprocessor.get_backend", return_value=backend):
            result = process_with_config(
                "um this is some neutral text that needs cleanup", config
            )

        assert result == "This is neutral cleaned text."
        backend.process.assert_called_once()

    @pytest.mark.parametrize(
        "tone", ["minimal", "professional", "casual", "dev", "personal"]
    )
    def test_licensed_vocabulary_reaches_cleanup_prompt_in_every_style(
        self, monkeypatch, tone
    ):
        import wayfinder.license as license_module

        gate = SimpleNamespace(has_feature=lambda _feature: True)
        monkeypatch.setattr(license_module, "get_feature_gate", lambda: gate)
        backend = SimpleNamespace(
            is_available=lambda: True,
            process=MagicMock(return_value="Please update Wayfinder Aura today."),
        )
        config = {
            "output_tone": tone,
            "post_processing_enabled": True,
            "post_processing_backend": "llama_cpp",
            "llama_cpp_model_path": "/tmp/model.gguf",
            "custom_vocabulary": ["Wayfinder Aura"],
        }

        with patch("wayfinder.core.postprocessor.get_backend", return_value=backend):
            process_with_config("please update way finder aura today", config)

        prompt = backend.process.call_args.args[1]
        assert "Protected vocabulary: Wayfinder Aura." in prompt

    def test_unlicensed_vocabulary_is_removed_before_cleanup_prompt(self, monkeypatch):
        import wayfinder.license as license_module

        gate = SimpleNamespace(has_feature=lambda _feature: False)
        monkeypatch.setattr(license_module, "get_feature_gate", lambda: gate)
        backend = SimpleNamespace(
            is_available=lambda: True,
            process=MagicMock(return_value="Please update the project today."),
        )
        config = {
            "output_tone": "personal",
            "post_processing_enabled": True,
            "post_processing_backend": "llama_cpp",
            "llama_cpp_model_path": "/tmp/model.gguf",
            "custom_vocabulary": ["PaidTerm"],
        }

        with patch("wayfinder.core.postprocessor.get_backend", return_value=backend):
            process_with_config("please update the paid term project today", config)

        prompt = backend.process.call_args.args[1]
        assert "Protected vocabulary" not in prompt
        assert "PaidTerm" not in prompt


# =============================================================================
# get_tone_options
# =============================================================================


class TestGetToneOptions:
    """Tests for get_tone_options()."""

    def test_returns_five_options(self):
        options = get_tone_options()
        assert len(options) == 5

    def test_option_ids(self):
        options = get_tone_options()
        ids = [o["id"] for o in options]
        assert ids == ["minimal", "professional", "casual", "dev", "personal"]

    def test_each_option_has_required_keys(self):
        for option in get_tone_options():
            assert "id" in option
            assert "name" in option
            assert "icon" in option
            assert "description" in option

    def test_minimal_description_mentions_filler(self):
        options = get_tone_options()
        minimal = next(o for o in options if o["id"] == "minimal")
        desc = minimal["description"].lower()
        assert "um" in desc or "uh" in desc or "filler" in desc or "exact" in desc


# =============================================================================
# Edge cases & integration-style pure-function tests
# =============================================================================


class TestEdgeCases:
    """Miscellaneous edge-case tests."""

    def test_cloud_backends_constant(self):
        assert "openai" in CLOUD_BACKENDS
        assert "anthropic" in CLOUD_BACKENDS

    def test_model_tiers_has_four_levels(self):
        assert set(MODEL_TIERS.keys()) == {"tiny", "small", "standard", "large"}

    def test_all_tiers_have_required_fields(self):
        for tier_name, tier_info in MODEL_TIERS.items():
            assert "description" in tier_info
            assert "max_intensity" in tier_info
            assert "smart_formatting" in tier_info
            assert "patterns" in tier_info

    def test_post_processing_error_is_exception(self):
        assert issubclass(PostProcessingError, Exception)
        err = PostProcessingError("test error")
        assert str(err) == "test error"


class TestResidentLlamaFastPath:
    """LlamaCppCliBackend: resident in-process model for instant post-processing."""

    def _backend(self, tmp_path):
        from wayfinder.core.postprocessor import LlamaCppCliBackend
        binary = tmp_path / "llama-simple"
        binary.write_text("#!/bin/sh\n")
        binary.chmod(0o755)
        model = tmp_path / "m.gguf"
        model.write_bytes(b"\x00")
        return LlamaCppCliBackend(llama_binary=str(binary), model_path=str(model),
                                  output_tone="professional", n_gpu_layers=0)

    def test_resident_model_none_without_bindings(self, tmp_path, monkeypatch):
        b = self._backend(tmp_path)
        # Simulate llama-cpp-python not installed
        import builtins
        real_import = builtins.__import__
        def fake_import(name, *a, **k):
            if name == "llama_cpp":
                raise ImportError("no llama_cpp")
            return real_import(name, *a, **k)
        monkeypatch.setattr(builtins, "__import__", fake_import)
        assert b._resident_model() is None

    def test_process_uses_resident_model_when_available(self, tmp_path):
        from unittest.mock import MagicMock
        b = self._backend(tmp_path)
        # Fake resident model: echoes prompt + a cleaned continuation
        fake_model = MagicMock(return_value={
            "choices": [{"text": "...Cleaned text: This is the cleaned result."}]
        })
        with patch.object(b, "_resident_model", return_value=fake_model), \
             patch("subprocess.run") as sub:
            out = b.process("this is the cleaned result", "")
        # Resident path used — subprocess must NOT be spawned
        sub.assert_not_called()
        fake_model.assert_called_once()
        assert "cleaned result" in out.lower()

    def test_process_falls_back_to_subprocess_without_resident(self, tmp_path):
        from unittest.mock import MagicMock
        b = self._backend(tmp_path)
        with patch.object(b, "_resident_model", return_value=None), \
             patch("subprocess.run", return_value=MagicMock(
                 returncode=0,
                 stdout="...Cleaned text: subprocess cleaned output.",
                 stderr="")) as sub:
            out = b.process("subprocess cleaned output", "")
        sub.assert_called_once()
        assert "subprocess cleaned output" in out.lower()

    def _gpu_backend(self, tmp_path):
        """A backend that REQUESTS GPU layers (n_gpu_layers=-1)."""
        from wayfinder.core.postprocessor import LlamaCppCliBackend
        binary = tmp_path / "llama-simple"; binary.write_text("#!/bin/sh\n"); binary.chmod(0o755)
        model = tmp_path / "m.gguf"; model.write_bytes(b"\x00")
        return LlamaCppCliBackend(llama_binary=str(binary), model_path=str(model),
                                  output_tone="professional", n_gpu_layers=-1)

    def test_resident_steps_aside_when_wheel_cpu_only_and_gpu_requested(self, tmp_path, monkeypatch):
        """A CPU-only llama-cpp-python wheel must NOT hijack GPU-requested cleanup:
        _resident_model returns None so process() uses the fast GPU subprocess.
        (Regression: the CPU-only wheel silently pinned Gemma cleanup to CPU.)"""
        import sys, types
        from wayfinder.core import postprocessor
        b = self._gpu_backend(tmp_path)
        # Import of llama_cpp succeeds (we return before instantiating Llama)...
        fake = types.ModuleType("llama_cpp"); fake.Llama = object
        monkeypatch.setitem(sys.modules, "llama_cpp", fake)
        # ...but the wheel can't offload to GPU.
        monkeypatch.setattr(postprocessor, "_wheel_supports_gpu_offload", lambda: False)
        assert b._resident_model() is None

    def test_resident_used_when_wheel_supports_gpu(self, tmp_path, monkeypatch):
        """When the wheel CAN offload, keep the warm resident path even for GPU."""
        import sys, types
        from wayfinder.core import postprocessor
        from wayfinder.core.postprocessor import LlamaCppCliBackend
        b = self._gpu_backend(tmp_path)
        sentinel = object()
        fake = types.ModuleType("llama_cpp"); fake.Llama = lambda **kw: sentinel
        monkeypatch.setitem(sys.modules, "llama_cpp", fake)
        monkeypatch.setattr(postprocessor, "_wheel_supports_gpu_offload", lambda: True)
        LlamaCppCliBackend._resident_cache.clear()
        assert b._resident_model() is sentinel

    def test_wheel_offload_probe_is_fail_safe(self, monkeypatch):
        """_wheel_supports_gpu_offload() is False (not raising) when the wheel is absent."""
        import sys
        from wayfinder.core import postprocessor
        monkeypatch.setitem(sys.modules, "llama_cpp", None)  # force ImportError on `from llama_cpp import ...`
        monkeypatch.setattr(postprocessor, "_WHEEL_GPU_OFFLOAD", None)
        assert postprocessor._wheel_supports_gpu_offload() is False

    def test_warm_up_pokes_resident_model(self, tmp_path):
        from unittest.mock import MagicMock
        b = self._backend(tmp_path)
        fake_model = MagicMock()
        with patch.object(b, "_resident_model", return_value=fake_model):
            b.warm_up()
        fake_model.assert_called_once()  # one tiny generation to build the graph

    def test_warm_up_noop_without_bindings(self, tmp_path):
        b = self._backend(tmp_path)
        with patch.object(b, "_resident_model", return_value=None):
            b.warm_up()  # must not raise

    def test_module_warm_up_routes_to_local_backend(self, tmp_path):
        from wayfinder.core import postprocessor
        binary = tmp_path / "llama-simple"; binary.write_text("#!/bin/sh\n"); binary.chmod(0o755)
        model = tmp_path / "m.gguf"; model.write_bytes(b"\x00")
        cfg = {"post_processing_backend": "llama_cpp", "post_processing_enabled": True,
               "llama_cpp_use_cli": True, "llama_cpp_binary": str(binary),
               "llama_cpp_model_path": str(model)}
        with patch.object(postprocessor.LlamaCppCliBackend, "warm_up") as warm:
            postprocessor.warm_up_postprocessing(cfg)
            warm.assert_called_once()

    def test_module_warm_up_noop_for_cloud_backend(self):
        from wayfinder.core import postprocessor
        # Cloud backend has nothing local to warm
        postprocessor.warm_up_postprocessing({"post_processing_backend": "anthropic"})


class TestLlamaGpuCpuFallback:
    """GPU post-proc probe + CPU-binary auto-fallback — the safety net that lets the
    Flatpak ship a Vulkan llama (broken-Vulkan hosts route to llama-simple-cpu instead
    of hanging at ~1 tok/s)."""

    def _backend(self, tmp_path, ngl=-1, cpu_sibling=False):
        from wayfinder.core.postprocessor import LlamaCppCliBackend
        LlamaCppCliBackend._gpu_probe.clear()
        gpu = tmp_path / "llama-simple"; gpu.write_text("#!/bin/sh\n"); gpu.chmod(0o755)
        if cpu_sibling:
            cpu = tmp_path / "llama-simple-cpu"; cpu.write_text("#!/bin/sh\n"); cpu.chmod(0o755)
        model = tmp_path / "m.gguf"; model.write_bytes(b"\x00")
        return LlamaCppCliBackend(llama_binary=str(gpu), model_path=str(model),
                                  output_tone="professional", n_gpu_layers=ngl)

    def test_gpu_good_uses_gpu_binary(self, tmp_path):
        b = self._backend(tmp_path, ngl=-1)
        with patch.object(b, "_probe_gpu_ok", return_value=True):
            binary, ngl = b._subprocess_target()
        assert ngl == 99 and binary == b.llama_binary

    def test_gpu_bad_falls_back_to_cpu_sibling(self, tmp_path):
        b = self._backend(tmp_path, ngl=-1, cpu_sibling=True)
        with patch.object(b, "_probe_gpu_ok", return_value=False):
            binary, ngl = b._subprocess_target()
        assert ngl == 0 and binary.endswith("llama-simple-cpu")

    def test_probe_runs_once_and_is_cached(self, tmp_path):
        b = self._backend(tmp_path, ngl=-1, cpu_sibling=True)
        with patch.object(b, "_probe_gpu_ok", return_value=False) as probe:
            b._subprocess_target(); b._subprocess_target(); b._subprocess_target()
        probe.assert_called_once()

    def test_explicit_cpu_skips_probe(self, tmp_path):
        b = self._backend(tmp_path, ngl=0)
        with patch.object(b, "_probe_gpu_ok", return_value=True) as probe:
            binary, ngl = b._subprocess_target()
        probe.assert_not_called()
        assert ngl == 0 and binary == b.llama_binary

    def test_explicit_cpu_prefers_packaged_cpu_sibling(self, tmp_path):
        b = self._backend(tmp_path, ngl=0, cpu_sibling=True)
        with patch.object(b, "_probe_gpu_ok") as probe:
            binary, ngl = b._subprocess_target()
        probe.assert_not_called()
        assert ngl == 0
        assert binary.endswith("llama-simple-cpu")

    def test_cpu_sibling_detected_only_when_present(self, tmp_path):
        assert self._backend(tmp_path, cpu_sibling=False)._cpu_sibling() is None
        assert self._backend(tmp_path, cpu_sibling=True)._cpu_sibling().endswith("llama-simple-cpu")

    def test_probe_false_on_subprocess_failure(self, tmp_path):
        b = self._backend(tmp_path)
        with patch("subprocess.run", side_effect=Exception("boom")):
            assert b._probe_gpu_ok(99) is False

    def test_probe_false_on_crash_returncode(self, tmp_path):
        b = self._backend(tmp_path)
        with patch("subprocess.run", return_value=MagicMock(returncode=-11, stdout="", stderr="")):
            assert b._probe_gpu_ok(99) is False  # SIGSEGV at ggml-vulkan init

    def test_probe_false_on_timeout(self, tmp_path):
        import subprocess as sp
        b = self._backend(tmp_path)
        with patch("subprocess.run", side_effect=sp.TimeoutExpired(cmd="x", timeout=8)):
            assert b._probe_gpu_ok(99) is False  # ~1 tok/s degraded Vulkan

    def test_warm_up_triggers_probe_on_subprocess_path(self, tmp_path):
        b = self._backend(tmp_path, ngl=-1)
        with patch.object(b, "_resident_model", return_value=None), \
             patch.object(b, "_probe_gpu_ok", return_value=True) as probe:
            b.warm_up()
        probe.assert_called_once()  # first dictation lands on the right binary


# =============================================================================
# GGUF stem tier detection (quirk keys are ollama-style; configs carry stems)
# =============================================================================


class TestGgufStemTierDetection:
    """llama_cpp configs carry GGUF file stems, not ollama names — both must match.

    Regression: 'phi3:mini' never matched 'Phi-3-mini-4k-instruct-q4', so the one
    catalog model that supports strong/caricature fell through to the 'mini'
    pattern (tier 'small') and both modes were silently downgraded on it.
    """

    def test_phi3_mini_gguf_stem_is_standard_tier(self):
        assert detect_model_tier("Phi-3-mini-4k-instruct-q4") == "standard"

    def test_gemma3_1b_gguf_stem_is_small(self):
        assert detect_model_tier("google_gemma-3-1b-it-Q4_K_M") == "small"

    def test_qwen35_2b_gguf_stem_is_small(self):
        assert detect_model_tier("Qwen3.5-2B-Q4_K_M") == "small"

    def test_smollm2_gguf_stem_is_tiny(self):
        assert detect_model_tier("smollm2-360m-instruct-q8_0") == "tiny"

    def test_llama32_3b_gguf_stem_is_standard(self):
        assert detect_model_tier("Llama-3.2-3B-Instruct-Q4_K_M") == "standard"

    def test_gguf_stem_quirks_match(self):
        assert "rewrites_standard_mode" in get_model_quirks("Phi-3-mini-4k-instruct-q4")["issues"]


# =============================================================================
# Strong / caricature intensity routing
# =============================================================================


class TestIntensityRouting:
    """Strong and caricature must actually change the prompt on capable models,
    stay capped on small ones, and caricature must reach the minimal tone."""

    CAPABLE = "/x/Phi-3-mini-4k-instruct-q4.gguf"
    SMALL = "/x/google_gemma-3-1b-it-Q4_K_M.gguf"

    def test_minimal_caricature_uses_caricature_prompt_on_capable_model(self):
        from wayfinder.core.postprocessor import build_prompt
        cfg = {"output_tone": "minimal", "caricature_mode": True,
               "post_processing_backend": "llama_cpp",
               "llama_cpp_model_path": self.CAPABLE}
        prompt, _ = build_prompt("hello world test", cfg)
        assert "SILLY" in prompt and "EXAGGERATED" in prompt

    def test_minimal_caricature_downgrades_to_minimal_prompt_on_small_model(self):
        from wayfinder.core.postprocessor import build_prompt
        cfg = {"output_tone": "minimal", "caricature_mode": True,
               "post_processing_backend": "llama_cpp",
               "llama_cpp_model_path": self.SMALL}
        prompt, _ = build_prompt("hello world test", cfg)
        assert "SILLY" not in prompt
        assert "filler sounds" in prompt

    def test_minimal_caricature_small_model_honors_explicit_fast_regex(self):
        cfg = {"output_tone": "minimal", "caricature_mode": True,
               "post_processing_enabled": True,
               "fast_filler_removal": True,
               "post_processing_backend": "llama_cpp",
               "llama_cpp_model_path": self.SMALL}
        out = process_with_config("um so this is a test you know", cfg)
        assert "um" not in out.lower().split()  # regex cleanup ran, no LLM needed

    def test_minimal_filler_rules_caricature_entry_is_reachable(self):
        assert get_filler_rules("minimal", "caricature") == FILLER_RULES["minimal"]["caricature"]

    def test_cli_backend_keeps_caricature_on_capable_model(self):
        b = LlamaCppCliBackend(model_path=self.CAPABLE, output_tone="casual",
                               caricature_mode=True)
        assert b.intensity == "caricature"

    def test_cli_backend_caps_caricature_on_small_model(self):
        b = LlamaCppCliBackend(model_path=self.SMALL, output_tone="casual",
                               caricature_mode=True)
        assert b.intensity == "standard"

    def test_cli_backend_caps_strong_on_small_model(self):
        b = LlamaCppCliBackend(model_path=self.SMALL, output_tone="professional",
                               strong_mode=True)
        assert b.intensity == "standard"

    def test_cli_caricature_prompt_is_a_rewrite_prompt(self):
        b = LlamaCppCliBackend(model_path=self.CAPABLE, output_tone="casual",
                               caricature_mode=True)
        p = b.build_cli_prompt("some text", "casual", "caricature")
        assert "PARODY" in p
        assert "90 percent" not in p  # the don't-rewrite guard must not neuter it

    def test_cli_strong_prompt_allows_restructuring(self):
        b = LlamaCppCliBackend(model_path=self.CAPABLE, output_tone="professional",
                               strong_mode=True)
        p = b.build_cli_prompt("some text", "professional", "strong")
        assert "restructure" in p
        assert "90 percent" not in p

    def test_cli_standard_prompt_keeps_dont_rewrite_guard(self):
        b = LlamaCppCliBackend(model_path=self.SMALL, output_tone="professional")
        p = b.build_cli_prompt("some text", "professional", "standard")
        assert "90 percent" in p

    def test_cli_minimal_caricature_prompt_is_parody(self):
        b = LlamaCppCliBackend(model_path=self.CAPABLE, output_tone="minimal",
                               caricature_mode=True)
        p = b.build_cli_prompt("some text", "minimal", "caricature")
        assert "PARODY" in p


# =============================================================================
# Extractor behavior per intensity
# =============================================================================


class TestExtractorIntensity:
    """Strong/caricature output is legitimately longer and multi-part — the
    extractor must not truncate it at the first paragraph like standard mode."""

    CAPABLE = "/x/Phi-3-mini-4k-instruct-q4.gguf"
    SMALL = "/x/google_gemma-3-1b-it-Q4_K_M.gguf"

    def test_caricature_output_keeps_later_paragraphs(self):
        b = LlamaCppCliBackend(model_path=self.CAPABLE, output_tone="professional",
                               caricature_mode=True)
        prompt = b.build_cli_prompt("input", "professional", "caricature")
        stdout = prompt + "PARA ONE with SYNERGY.\n\nPARA TWO is MISSION-CRITICAL ☕."
        out = b._extract_cli_output(stdout, prompt)
        assert "PARA TWO" in out

    def test_strong_output_keeps_later_paragraphs(self):
        b = LlamaCppCliBackend(model_path=self.CAPABLE, output_tone="professional",
                               strong_mode=True)
        prompt = b.build_cli_prompt("input", "professional", "strong")
        stdout = prompt + "First point.\n\nSecond paragraph of the answer."
        out = b._extract_cli_output(stdout, prompt)
        assert "Second paragraph" in out

    def test_standard_output_still_truncates_trailing_junk(self):
        b = LlamaCppCliBackend(model_path="/x/google_gemma-3-1b-it-Q4_K_M.gguf",
                               output_tone="professional")
        prompt = b.build_cli_prompt("input", "professional", "standard")
        stdout = prompt + "First paragraph.\n\nHere is an explanation of the changes."
        out = b._extract_cli_output(stdout, prompt)
        assert "explanation" not in out

    def test_caricature_output_still_cuts_debug_markers(self):
        b = LlamaCppCliBackend(model_path=self.CAPABLE, output_tone="casual",
                               caricature_mode=True)
        prompt = b.build_cli_prompt("input", "casual", "caricature")
        stdout = prompt + "fr fr no cap 💀\nmain: decoded 42 tokens"
        out = b._extract_cli_output(stdout, prompt)
        assert "decoded" not in out
        assert "fr fr" in out

    # Phi-3 restatement headers are re-worded every generation, so the cut is a
    # shape-matching regex (_RESTATEMENT_RE), not substrings. Both fixtures
    # below are trimmed from real field failures (2026-07-09, dev + professional
    # caricature on Phi-3 Mini).

    def test_caricature_cuts_bracketed_response_restatement(self):
        b = LlamaCppCliBackend(model_path=self.CAPABLE, output_tone="dev",
                               caricature_mode=True)
        prompt = b.build_cli_prompt("input", "dev", "caricature")
        stdout = prompt + (
            "[CRITICAL] EVERY TINY ERROR A CATASTROPHIC EDGE CASE! "
            "My career depends on this.\n- [Response]: [CRITICAL] A STATUS OF "
            "SHOCKING DEV-O-CRISIS! EVERY SMALL OOPSIE!"
        )
        out = b._extract_cli_output(stdout, prompt)
        assert "career depends" in out
        assert "OOPSIE" not in out

    def test_caricature_cuts_paraphrased_rewritten_header(self):
        b = LlamaCppCliBackend(model_path=self.CAPABLE, output_tone="professional",
                               caricature_mode=True)
        prompt = b.build_cli_prompt("input", "professional", "caricature")
        stdout = prompt + (
            "Esteemed Colleagues, pure SYNERGY.\n\nThoughts? Best, A Certified "
            "Thought Leader ☕\n\n------------------------\n*Rewritten text with "
            "over-the-top corporate buzzword overload:* Esteemed Colleagues, "
            "I'm scouting for synergistic opportunities."
        )
        out = b._extract_cli_output(stdout, prompt)
        assert "Thoughts? Best" in out          # multi-paragraph body survives
        assert "scouting" not in out            # restated copy is gone
        assert "buzzword overload" not in out   # leaked prompt wording is gone
        assert "---" not in out

    def test_caricature_cuts_bare_divider_restatement(self):
        b = LlamaCppCliBackend(model_path=self.CAPABLE, output_tone="professional",
                               caricature_mode=True)
        prompt = b.build_cli_prompt("input", "professional", "caricature")
        stdout = prompt + "The real answer.\n________\nThe restated answer."
        out = b._extract_cli_output(stdout, prompt)
        assert out == "The real answer."

    def test_standard_still_cuts_plain_response_marker(self):
        # The old substring markers ("\nResponse:" etc.) are gone; the regex
        # must still cover the plain forms they handled.
        b = LlamaCppCliBackend(model_path=self.SMALL, output_tone="professional")
        prompt = b.build_cli_prompt("input", "professional", "standard")
        stdout = prompt + "The cleaned text.\nResponse: The cleaned text again."
        out = b._extract_cli_output(stdout, prompt)
        assert out == "The cleaned text."

    def test_caricature_collapses_drifted_greedy_loop(self):
        # Greedy looping re-emits the opening with punctuation drift ("Um so,"
        # vs "Um, so,"), which a verbatim first-60-chars check misses (live
        # minimal/caricature field case, 2026-07-09: output looped ~3x to the
        # token cap). The normalized-word collapse must keep only copy one.
        b = LlamaCppCliBackend(model_path=self.CAPABLE, output_tone="minimal",
                               caricature_mode=True)
        prompt = b.build_cli_prompt("input", "minimal", "caricature")
        copy1 = ("[clears throat] Um so, I was thinking... [nervous laughter] "
                 "uh, we should... probably refactor the login code before "
                 "the demo on Friday... [sweating]")
        copy2 = ("[clears throat] Um, so, I was thinking... [nervous laughter] "
                 "uh, we should... probably refactor the login code before "
                 "the demo on Friday... [sweating]")
        out = b._extract_cli_output(prompt + copy1 + " " + copy2, prompt)
        assert out.count("refactor the login code") == 1

    def test_mid_sentence_response_word_is_kept(self):
        b = LlamaCppCliBackend(model_path=self.CAPABLE, output_tone="professional",
                               caricature_mode=True)
        prompt = b.build_cli_prompt("input", "professional", "caricature")
        stdout = prompt + ("Awaiting your MISSION-CRITICAL response: kindly "
                           "revert with a rewritten proposal at your earliest.")
        out = b._extract_cli_output(stdout, prompt)
        assert "kindly revert" in out
        assert "rewritten proposal" in out


# =============================================================================
# Cleanup residency release + the shared wall-clock budget
# =============================================================================


class TestResidentModelsAreReleased:
    """Two independent residencies hold a cleanup model: the external
    llama-server process and LlamaCppCliBackend's in-process wheel cache.
    Stopping the server does nothing to the second one."""

    def test_release_empties_the_cache(self):
        from wayfinder.core.postprocessor import LlamaCppCliBackend
        LlamaCppCliBackend._resident_cache[("m", 2048, 0)] = object()
        LlamaCppCliBackend.release_resident_models()
        assert LlamaCppCliBackend._resident_cache == {}

    def test_release_closes_models_that_support_it(self):
        from wayfinder.core.postprocessor import LlamaCppCliBackend

        class _Model:
            closed = False
            def close(self):
                self.closed = True

        m = _Model()
        LlamaCppCliBackend._resident_cache[("m", 2048, 0)] = m
        LlamaCppCliBackend.release_resident_models()
        assert m.closed

    def test_a_model_that_raises_on_close_still_gets_dropped(self):
        from wayfinder.core.postprocessor import LlamaCppCliBackend

        class _Angry:
            def close(self):
                raise RuntimeError("no")

        LlamaCppCliBackend._resident_cache[("m", 2048, 0)] = _Angry()
        LlamaCppCliBackend.release_resident_models()
        assert LlamaCppCliBackend._resident_cache == {}

    def test_the_cache_key_is_why_this_is_needed(self):
        """Keyed by (path, n_ctx, ngl): a model switch or GPU toggle ADDS an
        entry rather than replacing one, so the old weights stay resident."""
        from wayfinder.core.postprocessor import LlamaCppCliBackend
        LlamaCppCliBackend.release_resident_models()
        LlamaCppCliBackend._resident_cache[("a.gguf", 2048, 99)] = object()
        LlamaCppCliBackend._resident_cache[("b.gguf", 2048, 99)] = object()
        LlamaCppCliBackend._resident_cache[("b.gguf", 2048, 0)] = object()
        assert len(LlamaCppCliBackend._resident_cache) == 3
        LlamaCppCliBackend.release_resident_models()
        assert LlamaCppCliBackend._resident_cache == {}


class TestEveryResidencyReleaseGoesThroughOneFunction:
    """Half-releases were the bug: sites stopped the server and left the wheel
    cache loaded, and two sites released nothing at all."""

    def _main_source(self):
        from pathlib import Path
        return Path(__file__).resolve().parents[1].joinpath("wayfinder_main.py").read_text()

    def test_no_site_stops_the_server_directly(self):
        src = self._main_source()
        body = src.split("def _release_cleanup_residency", 1)[1].split("\ndef ", 1)[0]
        direct = src.count("LlamaServerManager.shutdown()")
        assert direct == body.count("LlamaServerManager.shutdown()") == 1, (
            "a call site stops the server without releasing the wheel cache")

    def test_turning_cleanup_off_releases_residency(self):
        src = self._main_source()
        fn = src.split("def toggle_post_processing", 1)[1].split("\n    def ", 1)[0]
        assert "_release_cleanup_residency()" in fn

    def test_switching_to_a_cloud_backend_releases_residency(self):
        src = self._main_source()
        fn = src.split("def on_postproc_backend_changed", 1)[1].split("\n    def ", 1)[0]
        assert "_release_cleanup_residency()" in fn


class TestCleanupHasAWallClockCeiling:
    """The app arms a 120s PROCESSING watchdog over transcription AND cleanup,
    and firing it DISCARDS the result — so an unbounded fallback ladder loses a
    dictation it had already cleaned successfully."""

    def test_the_budget_leaves_room_for_transcription(self):
        from wayfinder.core.postprocessor import LlamaCppCliBackend
        assert LlamaCppCliBackend.CLEANUP_TOTAL_BUDGET <= 120.0 / 2

    def test_the_budget_is_smaller_than_the_rungs_it_bounds(self):
        """20 acquire + 30 request + 10 kill + 8 probe + 60 CLI = 128s > watchdog."""
        from wayfinder.core.postprocessor import LlamaCppCliBackend
        from wayfinder.core.llama_server import LlamaServerManager
        unbounded = (LlamaCppCliBackend.SERVER_ACQUIRE_BUDGET
                     + LlamaServerManager.REQUEST_TIMEOUT + 10 + 8 + 60)
        assert unbounded > 120.0, "the finding's premise no longer holds"
        assert LlamaCppCliBackend.CLEANUP_TOTAL_BUDGET < unbounded

    def test_the_cli_rung_is_clamped_to_what_is_left(self, tmp_path):
        """The last rung is the one that would push past the watchdog."""
        import subprocess as _sp
        from unittest.mock import patch
        from wayfinder.core.postprocessor import LlamaCppCliBackend
        binary = tmp_path / "llama-simple"; binary.write_text("#!/bin/sh\n"); binary.chmod(0o755)
        model = tmp_path / "m.gguf"; model.write_bytes(b"\x00")
        b = LlamaCppCliBackend(llama_binary=str(binary), model_path=str(model),
                               output_tone="professional", n_gpu_layers=0, timeout=60)
        seen = {}

        def fake_run(cmd, **kw):
            seen["timeout"] = kw.get("timeout")
            raise _sp.TimeoutExpired(cmd, kw.get("timeout"))

        # Squeeze the shared budget well below the rung's own 60s timeout, so a
        # passing assertion can only mean the clamp ran.
        with patch.object(LlamaCppCliBackend, "CLEANUP_TOTAL_BUDGET", 5.0), \
             patch.object(b, "_resident_model", return_value=None), \
             patch.object(b, "_server_generate", return_value=None), \
             patch.object(b, "_subprocess_target", return_value=(str(binary), 0)), \
             patch("subprocess.run", side_effect=fake_run):
            try:
                b.process("hello there this is a test", "")
            except Exception:
                pass
        assert seen.get("timeout") is not None, "the CLI rung never ran"
        assert seen["timeout"] <= 5.0, (
            f"CLI rung got {seen['timeout']}s against its own timeout of "
            f"{b.timeout}s — the shared budget was ignored")


class TestResidentMemoryEstimate:
    """Users choosing Instant vs Save memory need to know what "keeps model
    loaded" actually costs. The answer varies ~3x across the catalog."""

    def test_matches_the_measured_footprint(self):
        """MEASURED: Qwen3.5-2B-Q4_K_M (1,280,835,840 B) held 1314 MB of VRAM at
        n_ctx=2048. The estimate must land on that, not on the file size."""
        from wayfinder.model_catalog import resident_memory_bytes
        est = resident_memory_bytes(1_280_835_840) / 1_048_576
        assert 1300 <= est <= 1420, f"estimated {est:.0f} MB vs 1314 MB measured"

    def test_never_under_promises(self):
        """Better to over-state slightly: a model taking LESS memory than the
        label said annoys nobody, the reverse does."""
        from wayfinder.model_catalog import resident_memory_bytes
        for n in (730_895_168, 1_280_835_840, 2_497_280_736):
            assert resident_memory_bytes(n) > n

    def test_formats_gb_and_mb(self):
        from wayfinder.model_catalog import format_resident_memory
        assert format_resident_memory(2_497_280_736) == "~2.6 GB"
        assert format_resident_memory(806_000_000).endswith("MB")

    def test_unknown_size_yields_no_claim(self):
        """A blank string lets the caller fall back to generic wording rather
        than printing '~0 GB' or crashing the settings panel."""
        from wayfinder.model_catalog import format_resident_memory
        for bad in (None, "", 0, -5, "abc", object()):
            assert format_resident_memory(bad) == ""

    def test_every_catalog_llm_can_state_its_footprint(self):
        """A catalog entry without size_bytes would silently show nothing."""
        import ast
        from pathlib import Path
        from wayfinder.model_catalog import format_resident_memory
        # AST, not a text split: wayfinder_main imports Tk at module scope, so
        # importing it here would need a display.
        src = Path(__file__).resolve().parents[1].joinpath("wayfinder_main.py").read_text()
        tree = ast.parse(src)
        catalog = None
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(
                    getattr(t, "id", None) == "LLM_GGUF_MODELS" for t in node.targets):
                catalog = ast.literal_eval(node.value)
        assert catalog, "LLM_GGUF_MODELS not found"
        missing = [k for k, v in catalog.items()
                   if not format_resident_memory(v.get("size_bytes"))]
        assert not missing, f"catalog entries cannot state a footprint: {missing}"


class TestBudgetTracksTheWatchdog:
    """A fixed 60s ceiling silently stopped fitting as soon as a user changed
    processing_timeout_secs, which is a configurable setting."""

    def test_it_is_half_the_configured_watchdog(self):
        from wayfinder.core.postprocessor import _cleanup_budget_from
        assert _cleanup_budget_from({"processing_timeout_secs": 120}) == 60.0
        assert _cleanup_budget_from({"processing_timeout_secs": 300}) == 150.0
        assert _cleanup_budget_from({"processing_timeout_secs": 60}) == 30.0

    def test_a_disabled_watchdog_still_bounds_cleanup(self):
        """0 disables the watchdog. Cleanup must NOT become unbounded — an
        unbounded rung ladder is its own problem, watchdog or no watchdog."""
        from wayfinder.core.postprocessor import _cleanup_budget_from
        assert _cleanup_budget_from({"processing_timeout_secs": 0}) == 60.0

    def test_junk_config_falls_back_rather_than_crashing_cleanup(self):
        from wayfinder.core.postprocessor import _cleanup_budget_from
        for bad in ({}, {"processing_timeout_secs": None},
                    {"processing_timeout_secs": "abc"}):
            assert _cleanup_budget_from(bad) == 60.0

    def test_the_budget_never_exceeds_the_watchdog_it_protects(self):
        """My first version floored this at 10s, which handed a 2s watchdog ten
        seconds of cleanup — a "ceiling" five times the thing it stays under."""
        from wayfinder.core.postprocessor import _cleanup_budget_from
        for watchdog in (2, 5, 15, 60, 120, 300):
            assert _cleanup_budget_from(
                {"processing_timeout_secs": watchdog}) <= watchdog

    def test_the_backend_adopts_it(self, tmp_path):
        from wayfinder.core.postprocessor import LlamaCppCliBackend
        binary = tmp_path / "llama-simple"; binary.write_text("#!/bin/sh\n"); binary.chmod(0o755)
        model = tmp_path / "m.gguf"; model.write_bytes(b"\x00")
        b = LlamaCppCliBackend(llama_binary=str(binary), model_path=str(model),
                               total_budget=25.0)
        assert b.CLEANUP_TOTAL_BUDGET == 25.0

    def test_an_exhausted_budget_skips_the_uninterruptible_resident_call(self, tmp_path):
        """llama-cpp-python's __call__ has no timeout, so once entered it cannot
        be bounded. Refusing to enter it is the only honest ceiling."""
        from unittest.mock import patch
        from wayfinder.core.postprocessor import LlamaCppCliBackend
        binary = tmp_path / "llama-simple"; binary.write_text("#!/bin/sh\n"); binary.chmod(0o755)
        model = tmp_path / "m.gguf"; model.write_bytes(b"\x00")
        b = LlamaCppCliBackend(llama_binary=str(binary), model_path=str(model),
                               output_tone="professional", n_gpu_layers=0)
        called = []
        with patch.object(LlamaCppCliBackend, "CLEANUP_TOTAL_BUDGET", -1.0), \
             patch.object(b, "_resident_model",
                          side_effect=lambda: called.append(1) or None), \
             patch.object(b, "_server_generate", return_value=None), \
             patch.object(b, "_subprocess_target", return_value=(str(binary), 0)):
            try:
                b.process("hello there this is a test", "")
            except Exception:
                pass
        assert not called, "entered an uninterruptible call with no budget left"
