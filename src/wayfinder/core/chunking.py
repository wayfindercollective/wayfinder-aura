"""Pure helpers for joining overlapping ASR chunks.

Kept outside the Tk application so accuracy evaluators and headless package tests
exercise the exact same boundary logic as the UI runtime.
"""

from __future__ import annotations

import re


_TERMINAL_MARKERS = frozenset({"[error]", "[empty]"})

# Words dropped from the end of the previous chunk before it becomes the next
# chunk's Whisper prompt: about the 2 s overlap at dictation pace. The recorder
# cuts mid-word, so those last words are the least reliable (Whisper finishes a
# cut word with an invented one: "today" -> "to", "the budget" -> "the day").
# A prompt ending on them made the next chunk continue from the invented text
# and lose or garble real words. Ending the prompt before the overlap lets the
# next chunk transcribe the overlap afresh, and the join below matches it.
# Measured with the join below (docs/EVAL-2026-09-30-chunking.md): Auto on
# Turbo Q5 8.2% -> 5.7% WER, Base 19.6% -> 14.4%.
PROMPT_TAIL_TRIM_WORDS = 5

# The anchored join: an exact run of at least ANCHOR_MIN_WORDS words that ends
# within ANCHOR_MAX_TAIL words of the previous chunk's end and starts within
# ANCHOR_MAX_LEAD words of the next chunk's start. Only those edge words go.
ANCHOR_MIN_WORDS = 4
ANCHOR_MAX_TAIL = 4
ANCHOR_MAX_LEAD = 3


def chunk_prompt_context(previous_text: str) -> str:
    """The Whisper prompt for the next chunk: the previous text minus its cut edge."""
    words = (previous_text or "").split()
    if len(words) <= PROMPT_TAIL_TRIM_WORDS:
        return ""
    return " ".join(words[:-PROMPT_TAIL_TRIM_WORDS])


# A chunk with speech that comes back with fewer words than this per second was
# cut short: with the previous text as its prompt, Base occasionally returns
# one word ("a") for 17 s of speech. Dictation runs ~2-3 words per second.
MIN_WORDS_PER_SECOND = 0.5
_MIN_RETRY_SECONDS = 5.0


def chunk_text_looks_truncated(text: str, seconds: float | None) -> bool:
    """Whether a prompted chunk's text is too short for its audio to be whole."""
    if not seconds or seconds < _MIN_RETRY_SECONDS:
        return False
    return len((text or "").split()) < MIN_WORDS_PER_SECOND * seconds


def prefer_fuller_chunk_text(prompted: str, unprompted: str) -> str:
    """Keep the unprompted retry only when it is clearly the fuller rendering.

    "Clearly" (twice the words, plus three) keeps a chunk that really was
    mostly pause from trading its few real words for a longer hallucination.
    """
    if len((unprompted or "").split()) >= 2 * len((prompted or "").split()) + 3:
        return unprompted
    return prompted


def _norm(word: str) -> str:
    return word.lower().strip(".,!?;:\"'`")


# A prompted chunk can also come back the other way: whisper-server runs with
# -nf (no temperature fallback), so a decode that starts looping a sentence
# never gets retried internally and just keeps going for the rest of the
# chunk. Dictation runs ~2-3 words/second (fast speech, ~4-5); well beyond
# that for a chunk long enough to judge is almost always a stuck loop rather
# than real speech.
MAX_WORDS_PER_SECOND = 6.0
_MIN_LOOP_SECONDS = 5.0

# The loop shape itself, independent of word rate: a run of LOOP_MIN_RUN or
# more consecutive sentences/phrases, each LOOP_PHRASE_MIN_WORDS to
# LOOP_PHRASE_MAX_WORDS words, that repeat the run's first member word for
# word. Shared with the post-processing collapse in transcriber.py's
# clean_whisper_artifacts so a chunk flagged here is the same chunk that gets
# cleaned up there.
LOOP_PHRASE_MIN_WORDS = 5
LOOP_PHRASE_MAX_WORDS = 40
LOOP_MIN_RUN = 3
# A repeat cut short still counts, from this many words.
LOOP_TRUNCATED_MIN_WORDS = 3


def _loop_words(phrase: str) -> list[str]:
    """Normalised words for loop matching (see _norm) with empties dropped."""
    return [w for w in (_norm(word) for word in phrase.split()) if w]


def _is_loop_repeat(anchor_words: list[str], candidate_words: list[str]) -> bool:
    """Whether ``candidate_words`` is the same looped phrase as ``anchor_words``.

    The same words (case and punctuation aside), or the anchor cut short — a
    late repeat in a loop sometimes ends early, e.g. "if the player is level
    one." for the full sentence. Never merely similar: parallel sentences
    differing in one word ("Add eggs to the list. Add milk to the list. Add
    bread to the list.") are real dictation and must all survive.
    """
    if candidate_words == anchor_words:
        return True
    return (
        LOOP_TRUNCATED_MIN_WORDS <= len(candidate_words) < len(anchor_words)
        and candidate_words == anchor_words[: len(candidate_words)]
    )


def collapse_sentence_loops(text: str) -> str:
    """Collapse a run of 3+ consecutive repeats of one sentence to the first.

    Targets a stuck decode that loops a whole SENTENCE — _collapse_whisper_
    repetitions (transcriber.py) only tries 2-4 word windows, so a loop built
    from 5-40 word sentences passes it untouched. Two repeats are left alone
    (people do say a sentence twice), and only word-for-word repeats (or one
    cut short) join a run, so list-like sentences differing in a word all
    survive. Text with no run comes back exactly as given.
    """
    if not any(mark in text for mark in ".!?"):
        return text  # nothing to split on; collapse_phrase_window_loops covers this

    sentences = [s for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s.strip()]
    if len(sentences) < LOOP_MIN_RUN:
        return text

    kept = []
    changed = False
    i = 0
    while i < len(sentences):
        anchor_words = _loop_words(sentences[i])
        if not (LOOP_PHRASE_MIN_WORDS <= len(anchor_words) <= LOOP_PHRASE_MAX_WORDS):
            kept.append(sentences[i])
            i += 1
            continue

        j = i + 1
        while j < len(sentences) and _is_loop_repeat(anchor_words, _loop_words(sentences[j])):
            j += 1

        kept.append(sentences[i])  # first member only, whether or not a run formed
        if j - i >= LOOP_MIN_RUN:
            changed = True
            i = j
        else:
            i += 1

    return " ".join(kept) if changed else text


def collapse_phrase_window_loops(text: str) -> str:
    """Same loop shape as collapse_sentence_loops, for speech with no sentence
    punctuation to split on — scans fixed-size word windows instead."""
    if any(mark in text for mark in ".!?"):
        return text  # collapse_sentence_loops above already covers this text

    words = text.split()
    if len(words) < LOOP_MIN_RUN * LOOP_PHRASE_MIN_WORDS:
        return text

    max_window = min(LOOP_PHRASE_MAX_WORDS, len(words) // LOOP_MIN_RUN)
    for window in range(max_window, LOOP_PHRASE_MIN_WORDS - 1, -1):
        kept = []
        i = 0
        changed = False
        while i < len(words):
            if i + window > len(words):
                kept.extend(words[i:])
                break
            anchor_words = [_norm(w) for w in words[i:i + window]]
            j = i + window
            while j + window <= len(words) and _is_loop_repeat(
                anchor_words, [_norm(w) for w in words[j:j + window]]
            ):
                j += window
            if (j - i) // window >= LOOP_MIN_RUN:
                kept.extend(words[i:i + window])  # first occurrence only
                i = j
                changed = True
            else:
                kept.append(words[i])
                i += 1
        if changed:
            return " ".join(kept)
    return text


def chunk_text_looks_looped(text: str, seconds: float | None) -> bool:
    """Whether a prompted chunk's text is a Whisper repetition loop, not real speech."""
    if not text:
        return False
    words = text.split()
    if seconds and seconds >= _MIN_LOOP_SECONDS and len(words) > MAX_WORDS_PER_SECOND * seconds:
        return True
    return (
        collapse_phrase_window_loops(collapse_sentence_loops(text)) != text
    )


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

    for overlap_len in range(
        min(max_words, len(words1), len(words2)), min_words - 1, -1
    ):
        end_norm = [_norm(word) for word in words1[-overlap_len:]]
        start_norm = [_norm(word) for word in words2[:overlap_len]]
        if end_norm == start_norm and all(end_norm):
            return " ".join(words2[:overlap_len])
    return ""


def find_anchored_overlap(text1: str, text2: str) -> tuple[int, int] | None:
    """Where to splice two chunks that repeat the overlap with ragged edges.

    Returns ``(keep1, skip2)``: keep ``text1``'s first ``keep1`` words and
    continue from ``text2``'s word ``skip2``, or None. The two texts must share
    an exact run of at least ANCHOR_MIN_WORDS words (case and punctuation
    ignored) that ends within ANCHOR_MAX_TAIL words of ``text1``'s end and
    starts within ANCHOR_MAX_LEAD words of ``text2``'s start: that run is the
    overlap, heard twice. Only the words beyond it at the two cut edges are
    dropped (``text1``'s tail and ``text2``'s lead), and both chunks cover that
    audio, so the other rendering of it stays. The longest run wins.
    """
    words1 = text1.split()
    words2 = text2.split()
    norm1 = [_norm(word) for word in words1]
    norm2 = [_norm(word) for word in words2]
    longest = min(15, len(words1), len(words2))
    for run in range(longest, ANCHOR_MIN_WORDS - 1, -1):
        for tail in range(ANCHOR_MAX_TAIL + 1):
            start1 = len(words1) - tail - run
            if start1 < 0:
                break
            anchor = norm1[start1 : start1 + run]
            if not all(anchor):
                continue
            for lead in range(min(ANCHOR_MAX_LEAD, len(words2) - run) + 1):
                if norm2[lead : lead + run] == anchor:
                    return start1, lead
    return None


def _splice(combined: str, remainder: str) -> str:
    # Whisper may end the earlier rendering with sentence punctuation even
    # when the overlapped second rendering continues the same clause.
    if remainder[:1].islower() and combined.rstrip()[-1:] in ".!?":
        combined = combined.rstrip().rstrip(".!?")
    return combined + " " + remainder


def deduplicate_overlap_text(transcriptions: list[str]) -> str:
    """Join chunk transcripts, removing only confident boundary repeats.

    An exact repeat (end of one chunk == start of the next) is removed first.
    Otherwise an exact run of 4+ words at the two cut edges anchors the splice
    (find_anchored_overlap). With neither, both texts are kept whole.
    """

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
            combined = _splice(combined, remainder)
            continue
        anchored = find_anchored_overlap(combined, next_chunk)
        if anchored is not None:
            keep, skip = anchored
            kept = " ".join(combined.split()[:keep])
            remainder = " ".join(next_chunk.split()[skip:])
            combined = _splice(kept, remainder) if kept else remainder
            continue
        combined += " " + next_chunk

    return re.sub(r"\s+", " ", combined).strip()


def join_raw_chunks(
    transcriptions: list[str], *, overlap_seconds: float = 2.0
) -> str:
    """Join raw outputs, removing exact repeats only at adjacent audio seams.

    Match the previous chunk's suffix against the next chunk's prefix, ignoring
    case and common punctuation. Require at least two matching words and use
    the original character spans to remove that prefix; all other text remains
    untouched. There is no fuzzy splice or search inside a chunk.

    A missing/failed chunk breaks adjacency. Recordings with no audio overlap
    have no duplicated seam to remove. Neither is a user cleanup preference.
    """
    pieces = []
    previous = None
    for text in transcriptions:
        if not text or not text.strip() or text.strip() in _TERMINAL_MARKERS:
            previous = None
            continue

        remainder = text
        if overlap_seconds > 0 and previous is not None:
            overlap = find_text_overlap(previous, text)
            if overlap:
                skip = len(overlap.split())
                spans = list(re.finditer(r"\S+", text))
                remainder = text[spans[skip].start():] if skip < len(spans) else ""

        if remainder:
            pieces.append(remainder)
        # Use this original chunk at the next seam, not the accumulated output.
        previous = text

    return " ".join(pieces)
