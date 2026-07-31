"""Pure helpers for joining overlapping ASR chunks.

Kept outside the Tk application so accuracy evaluators and headless package tests
exercise the exact same boundary logic as the UI runtime.
"""

from __future__ import annotations

import re


_TERMINAL_MARKERS = frozenset({"[error]", "[empty]"})


def find_text_overlap(
    text1: str,
    text2: str,
    min_words: int = 2,
    max_words: int = 15,
) -> str:
    """Return the exact word-sequence shared by ``text1``'s end and ``text2``'s start.

    Matching ignores case and common punctuation but is deliberately not fuzzy.
    Deleting a merely similar boundary is worse than retaining a duplicate because
    the deleted word may be unique speech that ASR rendered differently.
    """

    words1 = text1.split()
    words2 = text2.split()
    if len(words1) < min_words or len(words2) < min_words:
        return ""

    def _norm(word: str) -> str:
        return word.lower().strip(".,!?;:\"'`")

    for overlap_len in range(
        min(max_words, len(words1), len(words2)), min_words - 1, -1
    ):
        end_norm = [_norm(word) for word in words1[-overlap_len:]]
        start_norm = [_norm(word) for word in words2[:overlap_len]]
        if end_norm == start_norm and all(end_norm):
            return " ".join(words2[:overlap_len])
    return ""


def deduplicate_overlap_text(transcriptions: list[str]) -> str:
    """Join chunk transcripts, removing only confident exact boundary repeats."""

    valid = [
        text.strip()
        for text in transcriptions
        if text and text.strip() and text.strip() not in _TERMINAL_MARKERS
    ]
    if not valid:
        return ""
    if len(valid) == 1:
        return valid[0]

    combined = valid[0]
    for next_chunk in valid[1:]:
        overlap = find_text_overlap(combined, next_chunk)
        if overlap:
            remainder = next_chunk[len(overlap) :].lstrip()
            if not remainder:
                continue
            # Whisper may end the earlier rendering with sentence punctuation even
            # when the overlapped second rendering continues the same clause.
            if remainder[:1].islower() and combined.rstrip()[-1:] in ".!?":
                combined = combined.rstrip().rstrip(".!?")
            combined += " " + remainder
        else:
            combined += " " + next_chunk

    return re.sub(r"\s+", " ", combined).strip()
