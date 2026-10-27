"""A cleanup model that answers with its own notes or labels types the speaker's
words instead (field report 2026-09-19: Gemma 3 1B typed "Explanation: ...").

One check in process_with_config covers every backend. It never tries to cut
the answer apart: typing what the speaker said is always safe.
"""
from types import SimpleNamespace

import pytest

from wayfinder.core import postprocessor as P

SAID = "please send the quarterly report to the whole team"
CLEAN = "Please send the quarterly report to the whole team."


def _run(monkeypatch, answer, said=SAID):
    import wayfinder.license as lic

    monkeypatch.setattr(lic, "get_feature_gate",
                        lambda *a, **k: SimpleNamespace(is_premium=True, has_feature=lambda f: True))

    class _Backend:
        def is_available(self):
            return True

        def process(self, text, *args, **kwargs):
            return answer

    monkeypatch.setattr(P, "get_backend", lambda cfg: _Backend())
    return P.process_with_config(said, {"post_processing_enabled": True,
                                        "output_tone": "professional"})


@pytest.mark.parametrize("answer", [
    "Explanation: I removed the filler words.",  # the field report: a label at the start
    f"{CLEAN}\n\nExplanation: fixed the punctuation.",
    f"Here is the cleaned text: {CLEAN}",
    f"**Final Answer:** {CLEAN}",
    f"{CLEAN}\nNote: I kept it concise.",
    f"```\n{CLEAN}\n```",
])
def test_an_answer_with_the_models_notes_types_the_speech(monkeypatch, answer):
    assert _run(monkeypatch, answer) == SAID


def test_the_speakers_own_label_does_not_hide_a_later_note(monkeypatch):
    said = f"note {SAID}"
    answer = f"Note: {CLEAN}\nExplanation: I fixed the punctuation."
    assert _run(monkeypatch, answer, said) == said


@pytest.mark.parametrize("said,answer", [
    (SAID, CLEAN),
    ("note buy milk and eggs tonight", "Note: buy milk and eggs tonight."),  # the speaker's label
    ("we ship friday and the answer is yes", "We ship Friday, and the answer: yes."),
])
def test_a_clean_answer_or_the_speakers_own_label_is_kept(monkeypatch, said, answer):
    assert _run(monkeypatch, answer, said) == answer
