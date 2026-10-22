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
    assert "chunk_text_looks_looped(raw_prompted_text, get_wav_duration_seconds(chunk_path))" in body
    assert "raw_retry[0] if raw_retry else retry" in body
    # An empty or still-looped retry is discarded rather than trusted whole.
    assert "if retry.strip() and not retry_looped:" in body


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
# can be exactly what tips it into looping a whole sentence.

def test_a_sentence_loop_collapses_to_its_first_sentence():
    from wayfinder.core.chunking import collapse_sentence_loops

    pre = "Let's go over the plan first."
    loop = "If the player is level one they're going to be level one."
    loop_other_case = "if the player is level one, they're going to be level one!"
    loop_truncated = "If the player is level one."  # a late repeat cut short
    post = "That's the rule."

    looped_text = " ".join([pre, loop, loop_other_case, loop, loop_truncated, loop, post])
    assert collapse_sentence_loops(looped_text) == " ".join([pre, loop, post])


def test_two_loops_back_to_back_each_collapse():
    from wayfinder.core.chunking import collapse_sentence_loops

    first = "The escort spawns at the north gate every morning."
    second = "The escort waits by the bridge until the player arrives."
    text = " ".join([first] * 4 + [second] * 5 + ["Just for testing right now."])
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
        assert not chunk_text_looks_looped(text, 8.0)


def test_text_without_a_loop_comes_back_exactly_as_given():
    from wayfinder.core.chunking import chunk_text_looks_looped, collapse_sentence_loops

    # Whisper output often starts with a space; spacing alone is no loop.
    text = " Hello there my friend.  How are you doing today?\nI am fine thanks for asking."
    assert collapse_sentence_loops(text) is text
    assert not chunk_text_looks_looped(text, 6.0)


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


def test_chunk_text_looks_looped_true_for_a_wordy_short_chunk():
    from wayfinder.core.chunking import chunk_text_looks_looped

    # ~170 words of a 15 s chunk: far more than dictation pace can hold.
    assert chunk_text_looks_looped(" ".join(["word"] * 170), 15.0)


def test_chunk_text_looks_looped_true_for_a_sentence_run_regardless_of_duration():
    from wayfinder.core.chunking import chunk_text_looks_looped

    loop = "If the player is level one they're going to be level one."
    text = " ".join([loop, loop, loop])
    assert chunk_text_looks_looped(text, None)


def test_chunk_text_looks_looped_false_for_ordinary_dictation_pace():
    from wayfinder.core.chunking import chunk_text_looks_looped

    # 40 varied, non-repeating words over 15 s — normal, unhurried dictation.
    text = (
        "We spent most of the afternoon going over the budget numbers for next "
        "quarter and the team seemed happy with where things landed after the "
        "review meeting this morning before lunch so we moved on to planning "
        "the product launch"
    )
    assert len(text.split()) == 40
    assert not chunk_text_looks_looped(text, 15.0)


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
    text = " ".join([phrase] * 3) + "."
    assert chunk_text_looks_looped(text, 17.0)
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
    assert not chunk_text_looks_looped(text, 10.0)


def test_a_collapse_exposes_a_smaller_loop_in_one_call_and_is_idempotent():
    from wayfinder.core.chunking import collapse_phrase_window_loops

    # Sol loop r2: collapsing the 6-word loop exposes a 5-word one, which a
    # scan only upward from 6 never saw.
    p = "we need to go home"
    q = "home we need to go home"
    text = " ".join(["we need to go", q, q, q, p])
    once = collapse_phrase_window_loops(text)
    assert once == p
    assert collapse_phrase_window_loops(once) == once
