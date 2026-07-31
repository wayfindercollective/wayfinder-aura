"""Headless tests for the exact text boundary logic used by chunked ASR."""

from wayfinder.core.chunking import deduplicate_overlap_text, find_text_overlap


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
