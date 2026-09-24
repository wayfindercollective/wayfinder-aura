"""Normal style: instant, deterministic filler-sound removal that never
changes meaning (the matrix showed small models rewriting it)."""
from __future__ import annotations

import pytest

from wayfinder.core import postprocessor as P


@pytest.mark.parametrize("text,expected", [
    ("Um, so we should ship it.", "So we should ship it."),
    ("I, uh, think the the plan works.", "I think the plan works."),
    ("Hmm. Let me check.", "Let me check."),
    ("So basically I need to, um, git commit the boolean fix.",
     "So basically I need to git commit the boolean fix."),
    ("We should ship it, um.", "We should ship it."),
    ("The umbrella is uh here.", "The umbrella is here."),
    ("Ah, I see what you mean.", "I see what you mean."),
])
def test_filler_sounds_are_removed(text, expected):
    assert P.normal_filler_removal(text) == expected


@pytest.mark.parametrize("text", [
    "Do you know the right way to turn right?",   # the old regex ate these
    "I think that that is fine.",
    "We had had enough.",
    "We will err on the side of caution.",
    "Mhm, sounds good.",
    "Basically, it actually works, honestly.",
    "I like pizza, you know.",
])
def test_meaningful_words_are_kept(text):
    assert P.normal_filler_removal(text) == text


def test_normal_skips_the_cleanup_model(monkeypatch):
    import wayfinder.license as lic
    from types import SimpleNamespace

    monkeypatch.setattr(lic, "get_feature_gate",
                        lambda *a, **k: SimpleNamespace(is_premium=False, has_feature=lambda f: False))
    monkeypatch.setattr(P, "get_backend", lambda cfg: pytest.fail("Normal must not load a model"))
    cfg = {"post_processing_enabled": True, "output_tone": "minimal"}
    assert P.process_with_config("Um, the the report is ready.", cfg) == "The report is ready."


def test_normal_can_still_opt_into_the_model(monkeypatch):
    import wayfinder.license as lic
    from types import SimpleNamespace

    monkeypatch.setattr(lic, "get_feature_gate",
                        lambda *a, **k: SimpleNamespace(is_premium=False, has_feature=lambda f: False))
    called = []

    class _Backend:
        def is_available(self):
            return True

        def process(self, text, *args, **kwargs):
            called.append(text)
            return text

    monkeypatch.setattr(P, "get_backend", lambda cfg: _Backend())
    cfg = {"post_processing_enabled": True, "output_tone": "minimal", "normal_llm_cleanup": True}
    P.process_with_config("please send the quarterly report to the whole team", cfg)
    assert called


@pytest.mark.parametrize("text,expected", [
    ('"Um we should leave around noon."', '"We should leave around noon."'),
    ("(uh, maybe) tomorrow", "(maybe) tomorrow"),
])
def test_fillers_after_quotes_and_brackets(text, expected):
    assert P.normal_filler_removal(text) == expected
