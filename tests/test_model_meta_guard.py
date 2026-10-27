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
    f"{CLEAN} **Explanation:** I fixed the punctuation.",  # a backend joined the lines
    f"Here is the cleaned text: {CLEAN}",
    f"**Final Answer:** {CLEAN}",
    f"{CLEAN}\nNote: I kept it concise.",
    f"```\n{CLEAN}\n```",
])
def test_an_answer_with_the_models_notes_types_the_speech(monkeypatch, answer):
    assert _run(monkeypatch, answer) == SAID


@pytest.mark.parametrize("said,answer", [
    # Saying the word doesn't excuse the model's label; typing the speech is safe.
    ("please send an explanation to the client about the delayed delivery",
     "Explanation: Please send an explanation to the client about the delayed delivery."),
    (f"note {SAID}", f"Note: {CLEAN}\nExplanation: I fixed the punctuation."),
    ("note buy milk and eggs tonight", "Note: buy milk and eggs tonight."),
])
def test_a_label_the_speaker_also_said_still_types_the_speech(monkeypatch, said, answer):
    assert _run(monkeypatch, answer, said) == said


@pytest.mark.parametrize("said,answer", [
    (SAID, CLEAN),
    ("here's the plan ship it friday", "Here's the plan: ship it Friday."),
])
def test_a_clean_answer_is_kept(monkeypatch, said, answer):
    assert _run(monkeypatch, answer, said) == answer


def test_a_note_the_cli_backend_joined_onto_the_answer_is_caught():
    said = "um so send the quarterly report to uh the whole team"
    prompt = "Text: " + said + "\n\nCleaned text:"
    stdout = prompt + " Send the quarterly report to the whole team.\n**Explanation:** I removed the filler words."
    answer = P.LlamaCppCliBackend()._extract_cli_output(stdout, prompt, said)
    assert "\n" not in answer and P.model_added_meta(answer)
