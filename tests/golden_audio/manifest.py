"""Golden-audio corpus manifest for the ASR accuracy test.

The clips are GENERATED deterministically by ``generate.py`` in this directory
(espeak-ng by default, piper if installed) — they are synthetic, license-clean,
and safe to commit to a public repo (no real recorded voice). Regenerate with:

    PYTHONPATH=src python tests/golden_audio/generate.py

Each entry:
  id             stable clip id → ``<id>.wav`` in this directory
  reference_text the exact text that was synthesized (the ground truth)
  key_phrases    phrases that MUST survive transcription (order-robust check)
  category       clean | dev | silence
  max_wer        per-clip Word Error Rate ceiling (see tests/test_golden_asr.py).
                 Calibrated against base.en (free tier) with headroom; silence
                 is special-cased (assert no words, not a WER bound).
"""
from pathlib import Path

GOLDEN_DIR = Path(__file__).parent


CLIPS = [
    {
        "id": "short_clean",
        # NOTE: espeak renders "nine" as the digit "9"; key phrases avoid that token.
        "reference_text": "The weather is clear today and the meeting starts at nine.",
        "key_phrases": ["weather", "clear today", "meeting starts"],
        "category": "clean",
        "max_wer": 0.25,
    },
    {
        "id": "medium_clean",
        "reference_text": (
            "I spent the morning writing notes about the project and reviewing "
            "the plan. The next step is to test everything before we release it "
            "to the team."
        ),
        "key_phrases": ["the project", "the next step", "test everything", "release"],
        "category": "clean",
        "max_wer": 0.30,
    },
    {
        "id": "long_clean",
        "reference_text": (
            "Voice dictation should feel instant and accurate. You press the "
            "button, you speak a few sentences, and the words appear in the "
            "window you were typing in. When the recording runs long, the audio "
            "is split into chunks and stitched back together. The goal is simple. "
            "Never lose a single word that the speaker actually said."
        ),
        "key_phrases": [
            "voice dictation", "press the button", "split into chunks",
            "never lose a single word",
        ],
        "category": "clean",
        "max_wer": 0.20,
    },
    {
        "id": "dev_jargon",
        "reference_text": (
            "Commit the pull request and merge the branch after the build passes "
            "and the tests are green."
        ),
        "key_phrases": ["commit", "merge the branch", "the build", "tests"],
        "category": "dev",
        "max_wer": 0.30,
    },
    {
        "id": "silent",
        "reference_text": "",
        "key_phrases": [],
        "category": "silence",
        "max_wer": 0.0,  # special-cased: assert the output has no dictionary words
    },
]


def clip_path(clip_id: str) -> Path:
    """Absolute path to a clip's WAV (may not exist until generate.py has run)."""
    return GOLDEN_DIR / f"{clip_id}.wav"


def clip_by_id(clip_id: str) -> dict:
    for c in CLIPS:
        if c["id"] == clip_id:
            return c
    raise KeyError(clip_id)
