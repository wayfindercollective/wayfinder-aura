"""Headless tests for the exact text boundary logic used by chunked ASR."""

import pytest

from wayfinder.core.chunking import deduplicate_overlap_text, find_text_overlap, join_raw_chunks


def test_exact_overlap_ignores_case_and_punctuation():
    assert find_text_overlap(
        "We finished over the lazy.", "Over the lazy dog and kept running"
    ) == "Over the lazy"


def test_similar_boundary_is_preserved_instead_of_fuzzy_deleted():
    chunks = [
        "Never lose a single word that the speaker.",
        "Word at the speaker actually said.",
    ]
    assert deduplicate_overlap_text(chunks) == " ".join(chunks)


def test_markers_and_blank_chunks_do_not_leak_into_output():
    assert deduplicate_overlap_text(
        ["first", "", "[empty]", "[error]", "second"]
    ) == "first second"


def test_raw_join_removes_only_the_exact_seam_with_original_character_spans():
    # Uneven whitespace must not make character slicing delete a new word.
    assert join_raw_chunks([
        " first over the lazy ", "  OVER\tTHE\nLAZY second  second\n", "[empty]"
    ]) == " first over the lazy  second  second\n"


def test_raw_join_keeps_intentional_repetition_inside_each_chunk():
    assert join_raw_chunks([
        "say go go over the lazy", "over the lazy go go later over the lazy again"
    ]) == "say go go over the lazy go go later over the lazy again"


@pytest.mark.parametrize("chunks", [
    ["finish the configuration", "the documentation works"],
    ["keep all four original words today", "keep all four original words tomorrow"],
    ["I said no", "no is what I said"],
    [". .", ". ."],
])
def test_raw_join_preserves_uncertain_or_non_boundary_matches(chunks):
    assert join_raw_chunks(chunks) == " ".join(chunks)


@pytest.mark.parametrize("gap", ["", " \n ", "[empty]", "[error]"])
def test_raw_join_never_deduplicates_across_a_missing_chunk(gap):
    assert join_raw_chunks([
        "we walked over the lazy", gap, "over the lazy dog"
    ]) == "we walked over the lazy over the lazy dog"


def test_raw_join_uses_only_the_previous_original_chunk_at_each_seam():
    chunks = ["alpha beta", "beta gamma", "alpha beta beta gamma tail"]
    assert join_raw_chunks(chunks) == " ".join(chunks)


def test_raw_join_handles_a_wholly_duplicated_tail_chunk():
    assert join_raw_chunks(["we said two words", " TWO WORDS "]) == "we said two words"


def test_raw_join_does_not_remove_speech_when_audio_did_not_overlap():
    chunks = ["we said two words", "two words are worth repeating"]
    assert join_raw_chunks(chunks, overlap_seconds=0) == " ".join(chunks)


def test_raw_join_preserves_one_shot_text_and_the_alphabet():
    assert join_raw_chunks(["  untouched\n text  "]) == "  untouched\n text  "
    alphabet = "A B C D E F G H I J K L M N O P Q R S T U V W X Y Z"
    assert join_raw_chunks([alphabet, "Y Z and again A B C"]) == (
        alphabet + " and again A B C"
    )


# --- Ragged cut edges (docs/EVAL-2026-09-30-chunking.md) --------------------

from wayfinder.core.chunking import (  # noqa: E402
    PROMPT_TAIL_TRIM_WORDS,
    chunk_prompt_context,
    find_anchored_overlap,
)


def test_invented_word_at_the_cut_is_replaced_by_the_next_rendering():
    # "today" was cut in half; the first chunk ended on an invented "to".
    chunks = [
        "oh that's tight bro nice just commit the boolean fix honestly to",
        "just commit the boolean fix honestly today was kind of a lot",
    ]
    assert deduplicate_overlap_text(chunks) == (
        "oh that's tight bro nice just commit the boolean fix honestly today was kind of a lot"
    )


def test_a_garbled_lead_in_the_next_chunk_is_dropped_at_the_anchor():
    chunks = [
        "so I'm low key gonna head out in like ten",
        "Logi gonna head out in like ten minutes you wanna grab food",
    ]
    assert deduplicate_overlap_text(chunks) == (
        "so I'm low key gonna head out in like ten minutes you wanna grab food"
    )


def test_three_shared_words_are_not_enough_to_cut():
    chunks = ["we walked down by the river", "down by the sea we sat"]
    assert deduplicate_overlap_text(chunks) == " ".join(chunks)


def test_a_repeat_away_from_the_cut_edges_is_never_an_anchor():
    # The shared run sits mid-chunk in both texts: real repeated speech.
    first = "I said check the endpoint config and then we talked about lunch plans for a while today"
    second = "then again check the endpoint config before shipping it tonight"
    assert find_anchored_overlap(first, second) is None
    assert deduplicate_overlap_text([first, second]) == f"{first} {second}"


def test_continuation_without_overlap_is_kept_whole():
    # With the prompt, Whisper usually continues right after the previous text.
    chunks = ["something nothing crazy haha yeah that meeting was", "so long I almost fell asleep"]
    assert deduplicate_overlap_text(chunks) == " ".join(chunks)


def test_prompt_context_drops_the_cut_edge():
    text = "we decided to take a long walk by the river and it was"
    assert chunk_prompt_context(text) == "we decided to take a long walk by"
    assert len(text.split()) - len(chunk_prompt_context(text).split()) == PROMPT_TAIL_TRIM_WORDS
    assert chunk_prompt_context("too short to help") == ""
    assert chunk_prompt_context("") == ""


def test_the_app_prompts_each_chunk_with_the_trimmed_context():
    from pathlib import Path

    source = (Path(__file__).resolve().parent.parent / "wayfinder_main.py").read_text(encoding="utf-8")
    body = source[source.index("def _transcribe_chunk"):]
    body = body[: body.index("def _update_recording_duration")]
    assert "context = chunk_prompt_context(prev_text)" in body
    assert "context = prev_text" not in body
    # A collapsed prompted chunk is heard again without the prompt.
    assert "chunk_text_looks_truncated(text, get_wav_duration_seconds(chunk_path))" in body
    assert "prefer_fuller_chunk_text(text, retry)" in body
    # A chunk that loops with its prompt is heard again without one too. The
    # decision is made on the backend's RAW text (raw_out), not the cleaned
    # `text`/`retry` — the default config's own cleanup would otherwise
    # collapse the loop away before either check ever saw it (Sol loop r1).
    assert "raw_prompted_text = raw_prompted[0] if raw_prompted else text" in body
    assert "chunk_text_looks_looped(raw_prompted_text)" in body
    assert "raw_retry[0] if raw_retry else retry" in body
    # The dictation's retirement is rechecked right after the retry returns,
    # AND before the prompted text is salvaged on a retry failure (Sol loop
    # r3, minor 4; Sol loop r4, finding 5) — both sides of the retry check.
    assert body.count("WhisperServerBackend._check_not_abandoned(request_token)") == 2
    # One bar, nothing else (Sol loop r4): a non-empty, non-looped retry
    # always replaces the prompted text, with no word-count comparison.
    assert "accepted = bool(retry.strip() and not retry_looped)" in body


def test_a_collapsed_prompted_chunk_is_heard_again():
    from wayfinder.core.chunking import chunk_text_looks_truncated, prefer_fuller_chunk_text

    # Base once returned "a" for 17 s of speech when prompted.
    assert chunk_text_looks_truncated("a", 17.0)
    assert not chunk_text_looks_truncated(" ".join(["word"] * 40), 17.0)
    # Short tails and unreadable files are never retried.
    assert not chunk_text_looks_truncated("a", 3.0)
    assert not chunk_text_looks_truncated("a", None)
    retry = "the budget because we still haven't heard back and the deadline is close"
    assert prefer_fuller_chunk_text("a", retry) == retry


def test_a_sparse_chunk_keeps_its_words_over_a_longer_guess():
    from wayfinder.core.chunking import prefer_fuller_chunk_text

    # A mostly-pause chunk: 5 real words vs a 9-word unprompted rendering.
    prompted = "okay let me think here"
    guess = "okay let me think here thank you for watching"
    assert prefer_fuller_chunk_text(prompted, guess) == prompted


# --- Sentence-length repetition loops ---------------------------------------
# whisper-server runs with -nf (no temperature fallback), so a stuck decode
# never retries a looping decode itself; the prompt from the previous chunk
# can be exactly what tips it into looping a whole sentence. One bar
# (LOOP_MIN_RUN FULL copies) both suspects and collapses a loop (Sol loop r4).

def test_a_sentence_loop_collapses_to_its_first_sentence():
    from wayfinder.core.chunking import collapse_sentence_loops

    pre = "Let's go over the plan first."
    loop = "If the player is level one they're going to be level one."
    loop_other_case = "if the player is level one, they're going to be level one!"
    loop_truncated = "If the player is level one."  # a late repeat cut short
    post = "That's the rule."

    # 5 full copies (loop_other_case counts as full: case/punctuation aside)
    # before the trailing truncated repeat, which only rides along once the
    # run has already earned the bar on full copies alone.
    looped_text = " ".join(
        [pre, loop, loop_other_case, loop, loop, loop, loop_truncated, post]
    )
    assert collapse_sentence_loops(looped_text) == " ".join([pre, loop, post])


def test_two_loops_back_to_back_each_collapse():
    from wayfinder.core.chunking import collapse_sentence_loops

    first = "The escort spawns at the north gate every morning."
    second = "The escort waits by the bridge until the player arrives."
    text = " ".join([first] * 5 + [second] * 5 + ["Just for testing right now."])
    assert collapse_sentence_loops(text) == f"{first} {second} Just for testing right now."


def test_list_sentences_differing_in_one_word_all_survive():
    from wayfinder.core.chunking import chunk_text_looks_looped, collapse_sentence_loops

    # Similar is not a loop: a stuck decode repeats itself word for word.
    for text in (
        "Add eggs to the list. Add milk to the list. Add bread to the list.",
        "I need eggs from the store. I need milk from the store. "
        "I need bread from the store.",
        "If the player is level one they stay level one. If the player is "
        "level five they stay level five. If the player is level nine they "
        "stay level nine.",
    ):
        assert collapse_sentence_loops(text) == text
        assert not chunk_text_looks_looped(text)


def test_text_without_a_loop_comes_back_exactly_as_given():
    from wayfinder.core.chunking import chunk_text_looks_looped, collapse_sentence_loops

    # Whisper output often starts with a space; spacing alone is no loop.
    text = " Hello there my friend.  How are you doing today?\nI am fine thanks for asking."
    assert collapse_sentence_loops(text) is text
    assert not chunk_text_looks_looped(text)


def test_distinct_complete_sentences_do_not_collapse_via_prefix_alone():
    from wayfinder.core.chunking import collapse_sentence_loops

    # Each sentence is a strict prefix of the one before it, but these are
    # three distinct, shrinking thoughts — not a loop. A prefix match alone
    # (with no full copies behind it) must never build a run.
    text = "I want to go home after the meeting. I want to go home. I want to go."
    assert collapse_sentence_loops(text) == text


def test_a_short_fragment_does_not_extend_a_run():
    from wayfinder.core.chunking import collapse_sentence_loops

    # "So." is a prefix of the sentence, but too short to be its cut-short repeat.
    text = "So I went to the store today. So. So. So I went to the store today."
    assert collapse_sentence_loops(text) == text


def test_two_repeats_of_a_sentence_survive():
    from wayfinder.core.chunking import collapse_sentence_loops

    # People do say a sentence twice; only 3+ in a row is a loop.
    text = "Ship the full build to the team today. Ship the full build to the team today."
    assert collapse_sentence_loops(text) == text


def test_a_numbered_list_survives():
    from wayfinder.core.chunking import collapse_sentence_loops

    # Shares "Step ... is to ..." structure across all three, which can push
    # a naive similarity check too high — but not within LOOP_SIMILARITY_RATIO.
    text = (
        "Step one is to open the file. Step two is to edit the text. "
        "Step three is to save your changes."
    )
    assert collapse_sentence_loops(text) == text


def test_sentences_sharing_a_prefix_but_differing_in_meaning_survive():
    from wayfinder.core.chunking import collapse_sentence_loops

    # Twice, not three-in-a-row, and the second extends the first's meaning.
    text = (
        "I need to call the dentist tomorrow morning. "
        "I need to call the dentist tomorrow morning and reschedule my cleaning."
    )
    assert collapse_sentence_loops(text) == text


def test_an_unpunctuated_repeated_phrase_collapses():
    from wayfinder.core.chunking import collapse_phrase_window_loops

    phrase = "we need to update the database before lunch"
    assert collapse_phrase_window_loops(" ".join([phrase] * 5)) == phrase


def test_chunk_text_looks_looped_true_for_heavy_single_word_repetition():
    from wayfinder.core.chunking import chunk_text_looks_looped

    # The same word, well past LOOP_MIN_RUN consecutive copies: a genuine
    # repetition loop, not a rate judgment (there is no rate check anymore).
    assert chunk_text_looks_looped(" ".join(["word"] * 40))


def test_chunk_text_looks_looped_true_for_a_sentence_run():
    from wayfinder.core.chunking import chunk_text_looks_looped

    loop = "If the player is level one they're going to be level one."
    text = " ".join([loop] * 5)
    assert chunk_text_looks_looped(text)


def test_chunk_text_looks_looped_false_for_ordinary_dictation_pace():
    from wayfinder.core.chunking import chunk_text_looks_looped

    # 40 varied, non-repeating words — normal, unhurried dictation.
    text = (
        "We spent most of the afternoon going over the budget numbers for next "
        "quarter and the team seemed happy with where things landed after the "
        "review meeting this morning before lunch so we moved on to planning "
        "the product launch"
    )
    assert len(text.split()) == 40
    assert not chunk_text_looks_looped(text)


def test_chunk_text_looks_looped_takes_no_seconds_argument():
    from wayfinder.core.chunking import chunk_text_looks_looped

    # Sol loop r4: word rate is no longer part of loop detection at all;
    # chunk_text_looks_looped is a one-argument function now.
    import inspect

    params = list(inspect.signature(chunk_text_looks_looped).parameters)
    assert params == ["text"]


def test_three_and_four_copies_of_a_short_sentence_are_not_looped():
    from wayfinder.core.chunking import chunk_text_looks_looped, collapse_sentence_loops

    # Below LOOP_MIN_RUN (5): nothing happens, no retry and no collapse,
    # however many times someone says a short sentence on purpose.
    sentence = "We need to go home."
    for copies in (3, 4):
        text = " ".join([sentence] * copies)
        assert not chunk_text_looks_looped(text)
        assert collapse_sentence_loops(text) == text


def test_five_copies_of_a_short_sentence_are_looped_and_collapse():
    from wayfinder.core.chunking import chunk_text_looks_looped, collapse_sentence_loops

    sentence = "We need to go home."
    text = " ".join([sentence] * 5)
    assert chunk_text_looks_looped(text)
    assert collapse_sentence_loops(text) == sentence


def test_three_and_four_copies_of_an_unpunctuated_phrase_are_not_looped():
    from wayfinder.core.chunking import chunk_text_looks_looped, collapse_phrase_window_loops

    phrase = "we need to go home"
    for copies in (3, 4):
        text = " ".join([phrase] * copies)
        assert not chunk_text_looks_looped(text)
        assert collapse_phrase_window_loops(text) == text


# --- Composite periods and punctuation-blind phrase loops -------------------
# Trying windows largest-first finds a multiple of the true period (which
# matches too, since every N-repeat block looks identical to the next) and
# returns on the first window that changes anything, before the true,
# smaller period is ever tried.

def test_a_composite_period_x6_collapses_fully_not_to_two_copies():
    from wayfinder.core.chunking import collapse_phrase_window_loops

    phrase = "we need to update the database before lunch"
    assert collapse_phrase_window_loops(" ".join([phrase] * 6)) == phrase


def test_a_composite_period_x13_collapses_fully_not_to_five_copies():
    from wayfinder.core.chunking import collapse_phrase_window_loops

    phrase = "we need to update the database before lunch"
    assert collapse_phrase_window_loops(" ".join([phrase] * 13)) == phrase


def test_a_phrase_loop_inside_one_sentence_is_caught_despite_terminal_punctuation():
    from wayfinder.core.chunking import (
        chunk_text_looks_looped,
        collapse_phrase_window_loops,
        collapse_sentence_loops,
    )

    # One sentence, by punctuation — collapse_sentence_loops can't see
    # repetition inside it. The old punctuation gate on
    # collapse_phrase_window_loops left this loop uncaught entirely.
    phrase = "we need to update the database before lunch"
    text = " ".join([phrase] * 5) + "."
    assert chunk_text_looks_looped(text)
    collapsed = collapse_phrase_window_loops(collapse_sentence_loops(text))
    assert collapsed != text
    assert collapsed.rstrip(".") == phrase


def test_word_window_scan_stays_fast_on_a_long_non_looping_chunk():
    import time

    from wayfinder.core.chunking import collapse_phrase_window_loops

    # 300 distinct words, no loop: the common case. Scanning ~35 window
    # sizes over this must stay comfortably sub-second, not quadratic.
    text = " ".join(f"word{i}" for i in range(300))
    start = time.perf_counter()
    result = collapse_phrase_window_loops(text)
    elapsed = time.perf_counter() - start
    assert result == text
    assert elapsed < 2.0


def test_phrase_windows_do_not_erase_a_sentence_boundary():
    from wayfinder.core.chunking import chunk_text_looks_looped, collapse_phrase_window_loops

    # Sol loop r2: a window across the periods deleted "before dinner".
    text = (
        "We need to go home. We need to go home. "
        "We need to go home before dinner."
    )
    assert collapse_phrase_window_loops(text) is text
    assert not chunk_text_looks_looped(text)


def test_a_collapse_exposes_a_smaller_loop_in_one_call_and_is_idempotent():
    from wayfinder.core.chunking import collapse_phrase_window_loops

    # Sol loop r2: collapsing the 6-word loop exposes a 5-word one, which a
    # scan only upward from 6 never saw. Scaled to clear LOOP_MIN_RUN (5) at
    # both the outer (5 copies of q) and the exposed inner level (2 copies
    # from the prefix+q collapse, plus 3 more copies of p) (Sol loop r4).
    p = "we need to go home"
    q = "home we need to go home"
    text = " ".join(["we need to go"] + [q] * 5 + [p] * 3)
    once = collapse_phrase_window_loops(text)
    assert once == p
    assert collapse_phrase_window_loops(once) == once


# --- Closing quotes/brackets at a sentence boundary (Sol loop r3) -----------

def test_a_closing_quote_after_the_period_still_splits_the_sentence():
    from wayfinder.core.chunking import chunk_text_looks_looped, collapse_sentence_loops

    # A closing quote right after [.!?] used to defeat the lookbehind split,
    # merging all three quoted sentences into one long "sentence" that a
    # window scan then matched across, deleting "before dinner.""
    text = (
        '"We need to go home." "We need to go home." '
        '"We need to go home before dinner."'
    )
    assert collapse_sentence_loops(text) is text
    assert not chunk_text_looks_looped(text)


def test_a_closing_quote_survives_the_full_default_cleanup_unchanged():
    from wayfinder.core.chunking import collapse_phrase_window_loops, collapse_sentence_loops

    text = (
        '"We need to go home." "We need to go home." '
        '"We need to go home before dinner."'
    )
    cleaned = collapse_phrase_window_loops(collapse_sentence_loops(text))
    assert cleaned == text


@pytest.mark.parametrize("closer", ['"', "'", "”", "’", ")", "]"])
def test_other_closers_also_split_the_sentence(closer):
    from wayfinder.core.chunking import collapse_sentence_loops

    sentence = f"We need to go home.{closer}"
    text = " ".join([sentence, sentence, "We need to go home before dinner." + closer])
    assert collapse_sentence_loops(text) is text


# --- Sol loop r4, finding 2: truncated repeats never count toward the bar --

def test_two_distinct_prefix_sentences_do_not_inflate_three_full_copies():
    from wayfinder.core.chunking import collapse_sentence_loops

    # Three full copies, then two shorter, DISTINCT sentences that happen to
    # be prefixes of the anchor. Round-4 finding 2: the old cleanup bar let
    # truncated repeats join the run's length even though full_copies alone
    # (3) never reached it, inflating 3 full + 2 prefixes past the bar and
    # collapsing all five to the first. Full copies alone decide now.
    anchor = "We need to leave this building right now."
    shorter = "We need to leave this building."
    shortest = "We need to leave."
    text = " ".join([anchor] * 3 + [shorter, shortest])
    assert collapse_sentence_loops(text) == text


def test_a_truncated_repeat_only_rides_a_run_that_already_has_five_full():
    from wayfinder.core.chunking import collapse_sentence_loops

    # The mirror case: once full_copies reaches the bar on its own, a
    # trailing truncated repeat is absorbed into the collapse rather than
    # surviving as a leftover fragment.
    anchor = "We need to leave this building right now."
    shorter = "We need to leave this building."
    text = " ".join([anchor] * 5 + [shorter])
    assert collapse_sentence_loops(text) == anchor


# --- Sol loop r4, finding 3: quoted list items are never crossed -----------

def test_a_quoted_list_with_periods_survives_full_default_cleanup():
    from wayfinder.core.chunking import (
        chunk_text_looks_looped,
        collapse_phrase_window_loops,
        collapse_sentence_loops,
    )

    item = '"We need to go home.",'
    text = " ".join([item] * 4 + ['"We need to go home before dinner."'])
    assert not chunk_text_looks_looped(text)
    assert collapse_phrase_window_loops(collapse_sentence_loops(text)) == text


def test_a_quoted_list_with_no_periods_survives_full_default_cleanup():
    from wayfinder.core.chunking import (
        chunk_text_looks_looped,
        collapse_phrase_window_loops,
        collapse_sentence_loops,
    )

    # Same list, no terminal periods at all: _SENTENCE_END can't split this
    # (it needs [.!?]), so only the phrase-window span boundary — a closing
    # quote/bracket alone, even without one — keeps the scan off the item.
    item = '"we need to go home",'
    text = " ".join([item] * 4 + ['"we need to go home before dinner"'])
    assert not chunk_text_looks_looped(text)
    assert collapse_phrase_window_loops(collapse_sentence_loops(text)) == text


# --- Sol loop r5: lone closers end list items, never possessives ---------

@pytest.mark.parametrize(
    "item, last",
    [
        # finding 1: _loop_norm strips backticks, so the boundaries must too
        ("`we need to update the database before lunch.`,",
         "`we need to update the database before lunch tomorrow.`"),
        ("`we need to update the database before lunch`,",
         "`we need to update the database before lunch tomorrow`"),
        # a possessive inside a quoted item must not end the item early
        ("'the players' equipment is ready before lunch',",
         "'the players' equipment is ready before lunch today'"),
    ],
)
def test_quoted_lists_of_four_survive_full_default_cleanup(item, last):
    from wayfinder.core.chunking import (
        chunk_text_looks_looped,
        collapse_phrase_window_loops,
        collapse_sentence_loops,
    )

    text = " ".join([item] * 4 + [last])
    assert not chunk_text_looks_looped(text)
    assert collapse_phrase_window_loops(collapse_sentence_loops(text)) == text


@pytest.mark.parametrize(
    "text",
    [
        # a lead-in before the first item
        "Items: " + " ".join(['"we need to go home",'] * 4 + ['"we need to go home before dinner"']),
        # items joined by a word or two, not just punctuation
        " ".join(['"we need to go home",'] + ['or "we need to go home",'] * 3
                 + ['or "we need to go home before dinner"']),
        " ".join(['"we need to go home",'] + ['and then "we need to go home",'] * 3
                 + ['and then "we need to go home before dinner"']),
    ],
)
def test_quoted_lists_with_words_between_items_survive_full_default_cleanup(text):
    from wayfinder.core.chunking import (
        chunk_text_looks_looped,
        collapse_phrase_window_loops,
        collapse_sentence_loops,
    )

    assert not chunk_text_looks_looped(text)
    assert collapse_phrase_window_loops(collapse_sentence_loops(text)) == text


@pytest.mark.parametrize(
    "copy",
    [
        # finding 2: a possessive apostrophe used to end every span
        "we changed the players' equipment before lunch",
        "we changed the players’ equipment before lunch",
        "we fixed the players', coaches' gear today",
        # interior quoted terms, wherever they sit in the copy
        'we turned on the "fast" mode before lunch',
        '"Fast" mode is on before lunch',
        'we turned on the "fast", then the slow mode',
    ],
)
def test_five_copies_with_apostrophes_or_interior_quotes_still_collapse(copy):
    from wayfinder.core.chunking import (
        chunk_text_looks_looped,
        collapse_phrase_window_loops,
        collapse_sentence_loops,
    )

    text = " ".join([copy] * 5) + "."
    assert chunk_text_looks_looped(text)
    assert collapse_phrase_window_loops(collapse_sentence_loops(text)) == copy


# --- Sol loop r4, finding 4: loop normalisation keeps numeric meaning ------

def test_alternating_decimal_and_whole_number_sentences_all_survive():
    from wayfinder.core.chunking import collapse_sentence_loops

    # _norm (used by find_text_overlap) strips a leading "." too, which
    # would wrongly equate ".5" and "5"; _loop_norm only strips trailing.
    decimal = "Please set the scale to .5."
    whole = "Please set the scale to 5."
    text = " ".join([decimal, whole, decimal, whole, decimal])
    assert collapse_sentence_loops(text) == text


def test_five_identical_decimal_sentences_still_collapse():
    from wayfinder.core.chunking import collapse_sentence_loops

    decimal = "Please set the scale to .5."
    text = " ".join([decimal] * 5)
    assert collapse_sentence_loops(text) == decimal


# --- The user's real case (Sol loop r4) -------------------------------------

def test_the_reported_shape_collapses_fully_except_the_intentional_copies():
    from wayfinder.core.chunking import chunk_text_looks_looped, collapse_sentence_loops

    # 3 intentional copies of a "level 5" sentence, then the stuck decode:
    # 8 full copies of a "level 1" sentence, one cut short, then one more
    # full copy. The level-5 sentences are a different anchor (only 3 full
    # copies of THAT sentence) and must survive; the level-1 run clears the
    # bar on its 9 full copies alone and the cut-short rides along.
    level5 = "If the player is level 5 they're going to be level 1."
    level1 = "If the player is level 1 they're going to be level 1."
    level1_cut = "If the player is level 1."
    text = " ".join([level5] * 3 + [level1] * 8 + [level1_cut, level1])
    assert chunk_text_looks_looped(text)
    assert collapse_sentence_loops(text) == " ".join([level5] * 3 + [level1])
