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


# The loop shape: a run of LOOP_MIN_RUN or more consecutive sentences/phrases,
# each LOOP_PHRASE_MIN_WORDS to LOOP_PHRASE_MAX_WORDS words, that repeat the
# run's first member word for word. One bar, used both to suspect a loop (and
# trigger the unprompted re-hear in wayfinder_main) and to collapse it in
# cleanup (transcriber.py's clean_whisper_artifacts): a chunk flagged by
# chunk_text_looks_looped is exactly the chunk collapse_sentence_loops and
# collapse_phrase_window_loops would collapse. Below it nothing happens —
# three or four intentional copies of a short sentence ("We need to go
# home." x3) survive both the re-hear and the cleanup untouched (Sol loop
# r4: a second, lower bar that only suspected a loop let truncated repeats
# inflate a weak run past a destructive threshold it never earned on full
# copies alone — see _is_truncated_loop_repeat below).
LOOP_PHRASE_MIN_WORDS = 5
LOOP_PHRASE_MAX_WORDS = 40
LOOP_MIN_RUN = 5
# A repeat cut short still counts, from this many words.
LOOP_TRUNCATED_MIN_WORDS = 3


def _loop_norm(word: str) -> str:
    """Normalise one word for loop matching only — never find_text_overlap's
    _norm, which this leaves untouched.

    Strips common punctuation and curly quotes from both ends, but a period
    only from the right (with any quote around it: "home." and home". alike): stripping it from the left too made ".5" and "5"
    compare equal, erasing a decimal point's meaning and collapsing distinct
    numeric sentences into one (Sol loop r4, finding 4).
    """
    edge = ",!?;:\"'`“”‘’"
    return word.lstrip(edge).rstrip(edge + ".").lower()


def _window_norm(word: str) -> str:
    """_loop_norm for the phrase-window scan, which keeps quote marks,
    brackets and a trailing ,;: — everything that can close a list item.
    A word-window run can cross list items, and with those stripped the
    prefix of a longer item ("we need to go home or stay here") matched a
    full one ("we need to go home") and counted as a copy, collapsing a
    four-item list (Sol loop r6). Kept, `"home"`, `home,` and `home` differ,
    so a window only matches an item that ends the same way. A real loop
    repeats its quotes, commas and possessives verbatim, so it still
    matches; splitting spans at quotes instead hid such loops wherever a
    quote or apostrophe was not a list boundary (Sol loop r5, finding 2).
    """
    return word.lstrip(",!?;:").rstrip(".!?").lower()


def _same_copy_at_span_end(candidate: list[str], anchor: list[str]) -> bool:
    """Whether the span's LAST copy matches the anchor but for the ,;: its
    final word may lack: a loop's last copy ends the span with a period or
    nothing where every earlier copy had the "," that led into the next."""
    return (candidate[:-1] == anchor[:-1]
            and candidate[-1].rstrip(",;:") == anchor[-1].rstrip(",;:"))


def _loop_words(phrase: str) -> list[str]:
    """Normalised words for loop matching (see _loop_norm) with empties dropped."""
    return [w for w in (_loop_norm(word) for word in phrase.split()) if w]


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


# The sentence boundary shared by collapse_sentence_loops and
# collapse_phrase_window_loops: a sentence ends at [.!?], optionally followed
# by a closing quote or bracket, then optionally one of ,;: (a quoted list
# item: "We need to go home.", said next), then whitespace. A closing quote
# right after the terminator ("He said "stop." Then left.") used to defeat a
# lookbehind that only accepted [.!?] itself before the split point, which
# left the quoted run unsplit and let a word-window match straddle it,
# deleting real words at the far sentence's end (Sol loop r3); the trailing
# list punctuation closes the same gap one token further out (Sol loop r4,
# finding 3). Python's re can't lookbehind a variable width, so this scans
# forward with finditer and splits on the whitespace group instead, keeping
# the separators themselves as parts so joining with "" reproduces the
# original text exactly.
_CLOSERS = r'["\'`“”‘’)\]]'  # every quote _loop_norm strips, plus brackets
_SENTENCE_END = re.compile(rf'[.!?]{_CLOSERS}*[,;:]?(\s+)')

def _split_on(text: str, pattern: re.Pattern) -> list[str]:
    """``[span, sep, span, sep, ..., span]`` by ``pattern``; ``"".join(...) == text``."""
    parts = []
    start = 0
    for m in pattern.finditer(text):
        sep_start, sep_end = m.span(1)
        parts.append(text[start:sep_start])
        parts.append(text[sep_start:sep_end])
        start = sep_end
    parts.append(text[start:])
    return parts


def collapse_sentence_loops(text: str) -> str:
    """Collapse a run of LOOP_MIN_RUN+ consecutive repeats of one sentence to
    the first.

    Targets a stuck decode that loops a whole SENTENCE — _collapse_whisper_
    repetitions (transcriber.py) only tries 2-4 word windows, so a loop built
    from 5-40 word sentences passes it untouched. Fewer than LOOP_MIN_RUN
    full copies are left alone (people do say a sentence twice, or even three
    or four times on purpose), and only word-for-word repeats (or one cut
    short) join a run, so list-like sentences differing in a word all
    survive. Text with no run comes back exactly as given.

    A cut-short repeat never counts toward LOOP_MIN_RUN itself; it may only
    extend a run that already has LOOP_MIN_RUN FULL copies, and is then
    absorbed into the collapse (Sol loop r4, finding 2). Without that gate,
    three full copies of "We need to leave this building right now."
    followed by the shorter, distinct sentences "We need to leave this
    building." and "We need to leave." used to inflate a 3-copy run past the
    bar on the strength of two prefixes alone and collapse all five to the
    first. Whether the run collapses depends only on the FULL copy count;
    any trailing truncated copies ride along once it does.
    """
    sentences = [s for s in _split_on(text.strip(), _SENTENCE_END)[0::2] if s.strip()]
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
        full_copies = 1  # the anchor itself
        while j < len(sentences):
            candidate_words = _loop_words(sentences[j])
            if _is_full_loop_repeat(anchor_words, candidate_words):
                full_copies += 1
                j += 1
                continue
            if full_copies >= LOOP_MIN_RUN and _is_truncated_loop_repeat(anchor_words, candidate_words):
                j += 1  # absorbed, never counted toward the bar itself
                continue
            break

        kept.append(sentences[i])  # first member only, whether or not a run formed
        if full_copies >= LOOP_MIN_RUN:
            changed = True
            i = j
        else:
            i += 1

    return " ".join(kept) if changed else text


def _window_scan_one_span(text: str) -> str:
    """The window scan itself, run over one span (between the boundaries
    _SENTENCE_END finds, or the whole text when it finds none) by
    collapse_phrase_window_loops below.

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
    object when nothing changed.
    """
    words = text.split()
    if len(words) < LOOP_MIN_RUN * LOOP_PHRASE_MIN_WORDS:
        return text

    changed_any = False
    window = LOOP_PHRASE_MIN_WORDS
    while True:
        max_window = min(LOOP_PHRASE_MAX_WORDS, len(words) // LOOP_MIN_RUN)
        if window > max_window:
            break

        # Normalised once per window size, not re-derived for every slice
        # compared — keeps a long, loop-free chunk (the common case) from
        # doing quadratic work across the ~35 window sizes tried.
        norm = [_window_norm(w) for w in words]
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
            if j + window == len(words) and _same_copy_at_span_end(norm[j:], anchor):
                j += window
            if (j - i) // window >= LOOP_MIN_RUN:
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


def collapse_phrase_window_loops(text: str) -> str:
    """Same loop shape as collapse_sentence_loops, scanning fixed-size word
    windows instead of sentences — the only way to see a loop stuck inside a
    single sentence, or in speech with no sentence punctuation at all.

    Each span between _SENTENCE_END boundaries is scanned on its own:
    across sentences a window matched "we need to go home" over "We need to
    go home. We need to go home. We need to go home before dinner." and
    deleted "before dinner" (Sol loop r2). Quoted list items with no
    terminal period have no boundary to split on: _window_norm keeps their
    quotes instead, so a longer item's prefix never counts as a copy of a
    shorter one (Sol loop r6). Repeats across spans are
    collapse_sentence_loops' job. Text with no boundary at all is scanned
    whole. Text with no run comes back as the same object.
    """
    parts = _split_on(text, _SENTENCE_END)  # [span, sep, span, sep, ...]
    changed_any = False
    for i in range(0, len(parts), 2):
        collapsed = _window_scan_one_span(parts[i])
        if collapsed is not parts[i]:
            changed_any = True
            parts[i] = collapsed
    return "".join(parts) if changed_any else text


def chunk_text_looks_looped(text: str) -> bool:
    """Whether collapsing ``text`` at the one loop bar (LOOP_MIN_RUN) would
    change it — the same check wayfinder_main uses to suspect a prompted
    chunk's raw text and transcriber.py's clean_whisper_artifacts uses to
    collapse it, so a chunk flagged here is the same chunk cleanup collapses.
    """
    if not text:
        return False
    return collapse_phrase_window_loops(collapse_sentence_loops(text)) != text


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
