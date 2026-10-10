"""Headless tests for the exact text boundary logic used by chunked ASR."""

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


def test_raw_join_keeps_boundary_repetitions_and_model_whitespace():
    assert join_raw_chunks([" first over the lazy ", "over the lazy second\n", "[empty]"]) == (
        " first over the lazy  over the lazy second\n"
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
