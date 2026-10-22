"""Pure helpers for joining overlapping ASR chunks.

Kept outside the Tk application so accuracy evaluators and headless package tests
exercise the exact same boundary logic as the UI runtime.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


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

# A run of LOOP_MIN_RUN only SUSPECTS a loop: in wayfinder_main it triggers a
# non-destructive unprompted re-hear, nothing is thrown away. Collapsing text
# outright — the unprompted cleanup in transcriber.py's clean_whisper_artifacts,
# which has no chunk timing to go on — needs more evidence than that: three
# ordinary-paced, intentionally repeated sentences ("We need to go home." x3)
# must survive. collapse_sentence_loops and collapse_phrase_window_loops take
# the run length to collapse at as a min_run parameter; cleanup passes this
# higher bar instead of LOOP_MIN_RUN, so 3-4 copies of a sentence or
# unpunctuated phrase come through untouched (Sol loop r3).
LOOP_CLEANUP_MIN_RUN = 5


def _loop_words(phrase: str) -> list[str]:
    """Normalised words for loop matching (see _norm) with empties dropped."""
    return [w for w in (_norm(word) for word in phrase.split()) if w]


def _is_full_loop_repeat(anchor_words: list[str], candidate_words: list[str]) -> bool:
    """Whether ``candidate_words`` is the same looped phrase, word for word
    (case and punctuation aside)."""
    return candidate_words == anchor_words


def _is_truncated_loop_repeat(anchor_words: list[str], candidate_words: list[str]) -> bool:
    """Whether ``candidate_words`` is the anchor cut short — a late repeat in
    a loop sometimes ends early, e.g. "if the player is level one." for the
    full sentence."""
    return (
        LOOP_TRUNCATED_MIN_WORDS <= len(candidate_words) < len(anchor_words)
        and candidate_words == anchor_words[: len(candidate_words)]
    )


def _is_loop_repeat(anchor_words: list[str], candidate_words: list[str]) -> bool:
    """Whether ``candidate_words`` is the same looped phrase as ``anchor_words``:
    full or cut short. Never merely similar: parallel sentences differing in
    one word ("Add eggs to the list. Add milk to the list. Add bread to the
    list.") are real dictation and must all survive.
    """
    return _is_full_loop_repeat(anchor_words, candidate_words) or _is_truncated_loop_repeat(
        anchor_words, candidate_words
    )


# The sentence boundary shared by collapse_sentence_loops and
# collapse_phrase_window_loops: a sentence ends at [.!?], optionally followed
# by a closing quote or bracket, then whitespace. A closing quote right
# after the terminator ("He said "stop." Then left.") used to defeat a
# lookbehind that only accepted [.!?] itself before the split point, which
# left the quoted run unsplit and let a word-window match straddle it,
# deleting real words at the far sentence's end (Sol loop r3). Python's re
# can't lookbehind a variable width, so this scans forward with finditer and
# splits on the whitespace group instead, keeping the separators themselves
# as parts so joining with "" reproduces the original text exactly.
_SENTENCE_END = re.compile(r'[.!?]["\'”’)\]]*(\s+)')


def _split_sentences_with_seps(text: str) -> list[str]:
    """``[sentence, sep, sentence, sep, ..., sentence]``; ``"".join(...) == text``."""
    parts = []
    start = 0
    for m in _SENTENCE_END.finditer(text):
        sep_start, sep_end = m.span(1)
        parts.append(text[start:sep_start])
        parts.append(text[sep_start:sep_end])
        start = sep_end
    parts.append(text[start:])
    return parts


def collapse_sentence_loops(text: str, *, min_run: int = LOOP_MIN_RUN) -> str:
    """Collapse a run of ``min_run``+ consecutive repeats of one sentence to
    the first.

    Targets a stuck decode that loops a whole SENTENCE — _collapse_whisper_
    repetitions (transcriber.py) only tries 2-4 word windows, so a loop built
    from 5-40 word sentences passes it untouched. Fewer than ``min_run``
    repeats are left alone (people do say a sentence twice, or even three
    times on purpose — see LOOP_CLEANUP_MIN_RUN), and only word-for-word
    repeats (or one cut short) join a run, so list-like sentences differing
    in a word all survive. Text with no run comes back exactly as given.

    A cut-short repeat may only extend a run that already has LOOP_MIN_RUN
    FULL copies behind it (the anchor plus 2 exact repeats), regardless of
    ``min_run``: a prefix match alone is too cheap a coincidence to trust.
    Without that gate, "I want to go home after the meeting. I want to go
    home. I want to go." — three distinct sentences, each a prefix of the one
    before — collapsed to the first sentence alone. The run as a whole must
    still reach ``min_run`` members (full or trailing truncated) to collapse.
    """
    if not any(mark in text for mark in ".!?"):
        return text  # nothing to split on; collapse_phrase_window_loops covers this

    sentences = [s for s in _split_sentences_with_seps(text.strip())[0::2] if s.strip()]
    if len(sentences) < min_run:
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
        full_copies = 1  # the anchor itself
        while j < len(sentences):
            candidate_words = _loop_words(sentences[j])
            if _is_full_loop_repeat(anchor_words, candidate_words):
                full_copies += 1
                j += 1
                continue
            if full_copies >= LOOP_MIN_RUN and _is_truncated_loop_repeat(anchor_words, candidate_words):
                j += 1
                continue
            break

        kept.append(sentences[i])  # first member only, whether or not a run formed
        if j - i >= min_run:
            changed = True
            i = j
        else:
            i += 1

    return " ".join(kept) if changed else text


def _window_scan_one_span(text: str, *, min_run: int = LOOP_MIN_RUN) -> str:
    """The window scan itself, run over one span (a sentence, or the whole
    text when it has none) by collapse_phrase_window_loops below.

    Tries the SMALLEST window first and keeps trying larger ones after a
    collapse, rather than returning on the first window that changes
    anything: a window that is a multiple of the true period matches too
    (every block of N repeats looks identical to the next), so starting big
    can collapse only part of the run under a composite period and return
    before the true, smaller period is ever tried. An 8-word phrase repeated
    13 times, tried largest-window-first, used to leave 5 copies (a 32-word
    "period", 4x the real one); repeated 6 times it left 2 (a 16-word
    "period", 2x the real one). Smallest-first finds the true period and
    collapses the whole run to one copy.

    After a collapse it starts again from the smallest window: removing a
    run can expose a shorter loop at a size already scanned (Sol loop r2).
    Every collapse shortens the text, so this ends. Returns the identical
    object when nothing changed. ``min_run`` is the run length (in window
    repeats) required to collapse — see LOOP_CLEANUP_MIN_RUN (Sol loop r3).
    """
    words = text.split()
    if len(words) < min_run * LOOP_PHRASE_MIN_WORDS:
        return text

    changed_any = False
    window = LOOP_PHRASE_MIN_WORDS
    while True:
        max_window = min(LOOP_PHRASE_MAX_WORDS, len(words) // min_run)
        if window > max_window:
            break

        # Normalised once per window size, not re-derived for every slice
        # compared — keeps a long, loop-free chunk (the common case) from
        # doing quadratic work across the ~35 window sizes tried.
        norm = [_norm(w) for w in words]
        kept = []
        i = 0
        changed = False
        while i < len(words):
            if i + window > len(words):
                kept.extend(words[i:])
                break
            anchor = norm[i:i + window]
            j = i + window
            while j + window <= len(words) and norm[j:j + window] == anchor:
                j += window
            if (j - i) // window >= min_run:
                kept.extend(words[i:i + window])  # first occurrence only
                i = j
                changed = True
            else:
                kept.append(words[i])
                i += 1
        if changed:
            words = kept
            changed_any = True
            window = LOOP_PHRASE_MIN_WORDS  # a smaller loop may now be exposed
            continue
        window += 1

    return " ".join(words) if changed_any else text


def collapse_phrase_window_loops(text: str, *, min_run: int = LOOP_MIN_RUN) -> str:
    """Same loop shape as collapse_sentence_loops, scanning fixed-size word
    windows instead of sentences — the only way to see a loop stuck inside a
    single sentence, or in speech with no sentence punctuation at all.

    Each sentence is scanned on its own: across sentences a window matched
    "we need to go home" over "We need to go home. We need to go home. We
    need to go home before dinner." and deleted "before dinner" (Sol loop
    r2). Repeats across sentences are collapse_sentence_loops' job. Text
    with no run comes back as the same object. ``min_run`` is forwarded to
    _window_scan_one_span (see LOOP_CLEANUP_MIN_RUN, Sol loop r3).
    """
    if not any(mark in text for mark in ".!?"):
        return _window_scan_one_span(text, min_run=min_run)

    parts = _split_sentences_with_seps(text)  # [sentence, sep, sentence, sep, ...]
    changed_any = False
    for i in range(0, len(parts), 2):
        collapsed = _window_scan_one_span(parts[i], min_run=min_run)
        if collapsed is not parts[i]:
            changed_any = True
            parts[i] = collapsed
    return "".join(parts) if changed_any else text


@dataclass(frozen=True)
class LoopEvidence:
    """What chunk_loop_evidence found in a prompted chunk's own raw text.

    ``strong`` lowers the bar a retry must clear to replace the prompted
    text: any non-empty, non-looped retry does. Otherwise the retry needs at
    least ``kept_words`` words — what collapsing at LOOP_MIN_RUN left of the
    prompted text — to be trusted over ordinary-paced, intentionally
    repeated speech (Sol loop r3).
    """

    strong: bool
    kept_words: int


def chunk_loop_evidence(text: str, seconds: float | None = None) -> LoopEvidence | None:
    """Whether a prompted chunk's RAW text shows a Whisper repetition loop.

    None unless collapsing at LOOP_MIN_RUN actually changes the text: word
    rate alone is never a loop on its own — a fast, 31-word, non-repeating
    5 s chunk used to trip the old rate check with no repetition at all
    (Sol loop r3). Once a run of LOOP_MIN_RUN+ copies is found, ``strong`` is
    set when collapsing at the higher LOOP_CLEANUP_MIN_RUN bar still changes
    it (a long enough run to collapse outright), or the rate is implausible
    for speech; either makes any non-empty, non-looped retry trustworthy.
    Otherwise the retry must match or beat ``kept_words``.
    """
    if not text:
        return None

    collapsed = collapse_phrase_window_loops(
        collapse_sentence_loops(text, min_run=LOOP_MIN_RUN), min_run=LOOP_MIN_RUN
    )
    if collapsed == text:
        return None

    collapsed_for_cleanup = collapse_phrase_window_loops(
        collapse_sentence_loops(text, min_run=LOOP_CLEANUP_MIN_RUN), min_run=LOOP_CLEANUP_MIN_RUN
    )
    words = text.split()
    strong = collapsed_for_cleanup != text or (
        bool(seconds) and seconds >= _MIN_LOOP_SECONDS and len(words) > MAX_WORDS_PER_SECOND * seconds
    )
    return LoopEvidence(strong=strong, kept_words=len(collapsed.split()))


def chunk_text_looks_looped(text: str, seconds: float | None = None) -> bool:
    """Whether a prompted chunk's text is a Whisper repetition loop, not real speech."""
    return chunk_loop_evidence(text, seconds) is not None


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
