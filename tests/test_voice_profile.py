"""
Tests for the voice profile learning module.
"""

import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from wayfinder.core.voice_profile import (
    MIN_OCCURRENCES,
    MIN_PHRASE_OCCURRENCES,
    VOCAB_LIMIT,
    VoiceProfile,
    diff_vocab_edit,
    get_voice_profile,
    merge_vocab_view,
    reset_voice_profile,
)
from wayfinder.core.wordlist import STOP_WORDS


# =============================================================================
# VoiceProfile Initialization
# =============================================================================


class TestVoiceProfileInit:
    """Test VoiceProfile creation and defaults."""

    def test_creation_with_defaults(self, voice_profile_dir: Path):
        """VoiceProfile uses sensible defaults when created with a config dir."""
        vp = VoiceProfile(config_dir=voice_profile_dir)

        assert vp.config_dir == voice_profile_dir
        assert vp.history_limit == 100
        assert vp.regen_interval == 20
        assert vp.min_samples_for_profile == 5
        assert vp.profile_file == voice_profile_dir / "voice_profile.json"

    def test_creation_with_custom_params(self, voice_profile_dir: Path):
        """Custom parameters are honoured at init time."""
        vp = VoiceProfile(
            config_dir=voice_profile_dir,
            history_limit=50,
            regen_interval=10,
            min_samples_for_profile=3,
        )

        assert vp.history_limit == 50
        assert vp.regen_interval == 10
        assert vp.min_samples_for_profile == 3

    def test_initial_state_is_empty(self, voice_profile_dir: Path):
        """A fresh profile has no history, summary, or vocabulary."""
        vp = VoiceProfile(config_dir=voice_profile_dir)

        assert vp.get_history_count() == 0
        assert vp.get_summary() == ""
        assert vp.get_vocabulary() == []
        assert not vp.has_profile
        assert not vp.has_vocabulary
        assert not vp.is_regenerating

    def test_config_dir_created_if_missing(self, temp_dir: Path):
        """The config directory is created on first save when it doesn't exist."""
        new_dir = temp_dir / "nonexistent" / "nested" / "config"
        vp = VoiceProfile(config_dir=new_dir)

        # Trigger a save by adding a transcription
        vp.add_transcription("This is a valid test transcription")

        assert new_dir.exists()
        assert (new_dir / "voice_profile.json").exists()


# =============================================================================
# add_transcription
# =============================================================================


class TestAddTranscription:
    """Test adding transcriptions to the profile history."""

    def test_adds_to_history(self, voice_profile_dir: Path):
        """A valid transcription is appended to the history."""
        vp = VoiceProfile(config_dir=voice_profile_dir)
        vp.add_transcription("This is a test transcription with enough words")

        assert vp.get_history_count() == 1

    def test_adds_multiple_transcriptions(self, voice_profile_dir: Path):
        """Multiple transcriptions accumulate in history."""
        vp = VoiceProfile(config_dir=voice_profile_dir)

        for i in range(5):
            vp.add_transcription(f"This is test transcription number {i}")

        assert vp.get_history_count() == 5

    def test_skips_empty_text(self, voice_profile_dir: Path):
        """Empty or whitespace-only strings are ignored."""
        vp = VoiceProfile(config_dir=voice_profile_dir)

        vp.add_transcription("")
        vp.add_transcription("   ")
        vp.add_transcription(None)  # type: ignore[arg-type]

        assert vp.get_history_count() == 0

    def test_skips_short_text(self, voice_profile_dir: Path):
        """Transcriptions with fewer than 3 words are ignored."""
        vp = VoiceProfile(config_dir=voice_profile_dir)

        vp.add_transcription("hi")
        vp.add_transcription("two words")
        vp.add_transcription("ok sure")

        assert vp.get_history_count() == 0

    def test_three_word_minimum_passes(self, voice_profile_dir: Path):
        """Exactly 3 words is the threshold — it should be accepted."""
        vp = VoiceProfile(config_dir=voice_profile_dir)
        vp.add_transcription("three word sentence")

        assert vp.get_history_count() == 1

    def test_strips_whitespace(self, voice_profile_dir: Path):
        """Leading/trailing whitespace is stripped before storing."""
        vp = VoiceProfile(config_dir=voice_profile_dir)
        vp.add_transcription("  hello world today  ")

        stats = vp.get_stats()
        assert stats["history_count"] == 1

    def test_enforces_history_limit(self, voice_profile_dir: Path):
        """History is trimmed to keep only the most recent entries."""
        limit = 5
        vp = VoiceProfile(config_dir=voice_profile_dir, history_limit=limit)

        for i in range(10):
            vp.add_transcription(f"Transcription number {i} with extra words")

        assert vp.get_history_count() == limit

    def test_history_keeps_most_recent(self, voice_profile_dir: Path):
        """When trimmed, the most recent entries survive."""
        limit = 3
        vp = VoiceProfile(config_dir=voice_profile_dir, history_limit=limit)

        for i in range(6):
            vp.add_transcription(f"Sample transcription iteration {i}")

        # The last entry should reference iteration 5
        stats = vp.get_stats()
        assert stats["history_count"] == limit

    def test_records_word_count(self, voice_profile_dir: Path):
        """Each history entry stores a word count."""
        vp = VoiceProfile(config_dir=voice_profile_dir)
        vp.add_transcription("one two three four five")

        stats = vp.get_stats()
        assert stats["total_words"] == 5

    def test_records_timestamp(self, voice_profile_dir: Path):
        """Each history entry receives a timestamp."""
        vp = VoiceProfile(config_dir=voice_profile_dir)
        before = int(time.time())
        vp.add_transcription("This is a timestamped entry")
        after = int(time.time())

        # Read the raw data
        with open(vp.profile_file) as f:
            data = json.load(f)

        ts = data["history"][0]["timestamp"]
        assert before <= ts <= after

    def test_saves_after_each_addition(self, voice_profile_dir: Path):
        """The profile file is written after every add_transcription call."""
        vp = VoiceProfile(config_dir=voice_profile_dir)
        vp.add_transcription("First transcription for save test")

        assert vp.profile_file.exists()

        vp.add_transcription("Second transcription for save test")

        with open(vp.profile_file) as f:
            data = json.load(f)

        assert len(data["history"]) == 2


# =============================================================================
# Vocabulary Extraction
# =============================================================================


class TestVocabularyExtraction:
    """Test the _update_vocabulary method (distinctiveness model)."""

    def test_extracts_frequent_uncommon_words(self, voice_profile_dir: Path):
        """Distinctive words appearing 2+ times are extracted."""
        vp = VoiceProfile(config_dir=voice_profile_dir)

        # Add the same domain term multiple times
        for _ in range(3):
            vp.add_transcription("The kubernetes cluster needs refactoring soon")

        vocab = vp.get_vocabulary()
        assert "kubernetes" in vocab
        assert "cluster" in vocab
        assert "refactoring" in vocab

    def test_excludes_stop_words(self, voice_profile_dir: Path):
        """Ordinary English (STOP_WORDS) is never included in vocabulary."""
        vp = VoiceProfile(config_dir=voice_profile_dir)

        for _ in range(5):
            vp.add_transcription("The quick brown fox jumps over something")

        vocab = vp.get_vocabulary()
        for word in vocab:
            assert word.casefold() not in STOP_WORDS, f"{word!r} should be excluded"

    def test_ignores_short_words(self, voice_profile_dir: Path):
        """Words shorter than 3 characters are excluded by the regex."""
        vp = VoiceProfile(config_dir=voice_profile_dir)

        # "kubernetes" recurs so the learned list is non-empty; the 2-letter
        # AI/ML/DB tokens must never sneak in.
        for _ in range(5):
            vp.add_transcription("An AI ML DB kubernetes is not enough today")

        vocab = vp.get_vocabulary()
        assert "kubernetes" in vocab
        for word in vocab:
            assert len(word) >= 3

    def test_single_occurrence_excluded(self, voice_profile_dir: Path):
        """Words appearing only once are not included in vocabulary."""
        vp = VoiceProfile(config_dir=voice_profile_dir)

        vp.add_transcription("unique xylophone architecture testing words here")
        vp.add_transcription("completely different sentence about gardening tips today")

        vocab = vp.get_vocabulary()
        # Each uncommon word appears only once, so vocabulary should be empty
        assert "xylophone" not in vocab
        assert "architecture" not in vocab

    def test_surfaces_distinctive_over_generic(self, voice_profile_dir: Path):
        """THE USER STORY: generic English is dropped; names/jargon/products
        surface WITH their capitalization.

        Regression for the real complaint — the learned list used to be pure
        junk ("everything, something, looks, stuff, fix, ...") because it ranked
        by raw frequency."""
        vp = VoiceProfile(config_dir=voice_profile_dir)

        # Realistic generic speech, repeated (this is what a person says most).
        generic = [
            "everything looks cool, fix the checkout page later",
            "add something to the main window",
            "everything looks great, fix the order page later",
            "add something cool to the window",
        ]
        # Distinctive lines — names, product terms, jargon.
        distinctive = [
            "Daan pushed the Flatpak build",
            "the Wayfinder overlay needs the dictation fix",
            "Daan tested the Flatpak on Bazzite",
            "Wayfinder dictation through the overlay again",
        ]
        for line in generic + distinctive:
            vp.add_transcription(line)

        vocab = vp.get_vocabulary()

        # None of the generic junk from the real complaint survives.
        junk = {
            "everything", "something", "looks", "stuff", "fix", "anything",
            "add", "off", "check", "mode", "running", "main", "window", "page",
            "cool", "later", "any", "order", "match", "style", "models",
            "november", "cart", "discount", "orders", "checkout", "great",
        }
        for word in vocab:
            assert word.casefold() not in junk, f"junk word {word!r} leaked in"

        # Distinctive terms appear WITH their capitalization preserved.
        assert "Daan" in vocab
        assert "Flatpak" in vocab
        assert "Wayfinder" in vocab

    def test_distinctive_outranks_mid_at_equal_count(self, voice_profile_dir: Path):
        """A distinctive word said 2x outranks a mid-band word said 2x."""
        vp = VoiceProfile(config_dir=voice_profile_dir)

        # "kubernetes" = distinctive tail; "endpoint" = mid-band. Equal counts.
        for _ in range(2):
            vp.add_transcription("the kubernetes endpoint was migrated cleanly")

        vocab = vp.get_vocabulary()
        assert "kubernetes" in vocab
        assert "endpoint" in vocab
        assert vocab.index("kubernetes") < vocab.index("endpoint")

    def test_mid_word_surfaces_when_repeated_far_more(self, voice_profile_dir: Path):
        """A mid-band word said >4x as often DOES outrank a distinctive one.

        DISTINCTIVE_WEIGHT is 4x MID_WEIGHT, so a mid word needs more than a 4:1
        repetition ratio to win."""
        vp = VoiceProfile(config_dir=voice_profile_dir)

        # "endpoint" (mid) said 10x vs "kubernetes" (distinctive) said 2x.
        for _ in range(10):
            vp.add_transcription("the endpoint endpoint responded to the endpoint")
        for _ in range(2):
            vp.add_transcription("the kubernetes migration finished today")

        vocab = vp.get_vocabulary()
        assert "endpoint" in vocab
        assert "kubernetes" in vocab
        # endpoint score = 30*1 = 30; kubernetes score = 2*4 = 8 -> endpoint wins
        assert vocab.index("endpoint") < vocab.index("kubernetes")

    def test_min_occurrences_excludes_single_distinctive(self, voice_profile_dir: Path):
        """A distinctive word said only ONCE does not appear (MIN_OCCURRENCES)."""
        assert MIN_OCCURRENCES == 2
        vp = VoiceProfile(config_dir=voice_profile_dir)

        vp.add_transcription("Daan reviewed the pull request this morning")
        vp.add_transcription("the deploy finished without any trouble tonight")

        assert "Daan" not in vp.get_vocabulary()

    def test_vocab_limit_respected(self, voice_profile_dir: Path):
        """The learned list is capped at VOCAB_LIMIT entries."""
        vp = VoiceProfile(config_dir=voice_profile_dir)

        # 40 distinct distinctive words (purely alphabetic — the tokenizer only
        # matches letter runs), each said twice -> 40 candidates.
        words = [f"zzq{chr(97 + i // 26)}{chr(97 + i % 26)}" for i in range(40)]
        for w in words:
            vp.add_transcription(f"the {w} {w} appeared in the transcript")

        vocab = vp.get_vocabulary()
        assert len(vocab) == VOCAB_LIMIT

    def test_surface_form_capitalized_word_keeps_capital(self, voice_profile_dir: Path):
        """A word usually said capitalized is stored with its capital."""
        vp = VoiceProfile(config_dir=voice_profile_dir)

        # "Daan" capitalized 3x, lowercase once -> most-common surface is "Daan".
        vp.add_transcription("Daan opened the merge request")
        vp.add_transcription("Daan approved the change")
        vp.add_transcription("Daan shipped it, said daan happily")

        vocab = vp.get_vocabulary()
        assert "Daan" in vocab
        assert "daan" not in vocab

    def test_surface_form_lowercase_word_stays_lowercase(self, voice_profile_dir: Path):
        """A word usually said lowercase is stored lowercase."""
        vp = VoiceProfile(config_dir=voice_profile_dir)

        # "kubernetes" lowercase 3x, capitalized once -> surface stays lowercase.
        vp.add_transcription("the kubernetes rollout went fine")
        vp.add_transcription("another kubernetes upgrade landed")
        vp.add_transcription("kubernetes again, plus one Kubernetes typo")

        vocab = vp.get_vocabulary()
        assert "kubernetes" in vocab
        assert "Kubernetes" not in vocab

    def test_contraction_fragments_never_learned(self, voice_profile_dir: Path):
        """"doesn't" tokenizes to "doesn" — fragments land in the distinctive
        tier of the written-web corpus and must be stopped by the curated
        EXTRA_STOP_WORDS supplement."""
        vp = VoiceProfile(config_dir=voice_profile_dir)

        for _ in range(3):
            vp.add_transcription("it doesn't work and it wasn't the Flatpak build")

        vocab = vp.get_vocabulary()
        assert "doesn" not in [w.lower() for w in vocab]
        assert "wasn" not in [w.lower() for w in vocab]
        assert "Flatpak" in vocab

    def test_spoken_fillers_never_learned(self, voice_profile_dir: Path):
        """Universal speech fillers (okay/dude/gonna) are not personal signal,
        even though the written-web corpus rates them mid-band or rarer."""
        vp = VoiceProfile(config_dir=voice_profile_dir)

        for _ in range(4):
            vp.add_transcription("Okay dude, gonna fix the Flatpak build now, alright")

        vocab_lower = [w.lower() for w in vp.get_vocabulary()]
        for filler in ("okay", "dude", "gonna", "alright"):
            assert filler not in vocab_lower
        assert "flatpak" in vocab_lower

    def test_ignoring_lowercase_removes_cased_learned_word(self, voice_profile_dir: Path):
        """Ignoring 'daan' (lowercase) removes the learned 'Daan' (cased)."""
        vp = VoiceProfile(config_dir=voice_profile_dir)

        for _ in range(3):
            vp.add_transcription("Daan pushed the Flatpak build again")

        assert "Daan" in vp.get_vocabulary()

        vp.set_ignored_words(["daan"])  # lowercase ignore

        vocab = vp.get_vocabulary()
        assert "Daan" not in vocab
        assert "Flatpak" in vocab

    def test_self_heal_recomputes_junk_without_disk_write(self, voice_profile_dir: Path):
        """A stale junk vocabulary is cleaned in memory on load — no disk write.

        Older profiles were built by the frequency-only extractor. Loading one
        must recompute a clean list immediately (from history) but must NOT
        persist until the next real save, so the on-disk file is untouched."""
        profile_file = voice_profile_dir / "voice_profile.json"
        stale = {
            "history": [
                {"text": "Daan pushed the Flatpak build today", "timestamp": 1, "word_count": 6},
                {"text": "Daan tested the Flatpak on Bazzite", "timestamp": 2, "word_count": 6},
                {"text": "the Wayfinder overlay handles dictation", "timestamp": 3, "word_count": 5},
                {"text": "Wayfinder dictation with the overlay again", "timestamp": 4, "word_count": 6},
            ],
            "profile": {
                "summary": "prior summary",
                # Pure junk from the old extractor.
                "vocabulary": ["everything", "something", "looks", "stuff", "fix"],
                "vocabulary_ignore": [],
                "generated_at": 5,
                "samples_used": 4,
            },
            "transcriptions_since_regen": 0,
        }
        raw_before = json.dumps(stale, indent=2)
        profile_file.write_text(raw_before)

        vp = VoiceProfile(config_dir=voice_profile_dir)

        # In-memory vocabulary is recomputed clean.
        vocab = vp.get_vocabulary()
        assert "everything" not in vocab
        assert "something" not in vocab
        assert "Daan" in vocab
        assert "Flatpak" in vocab

        # Disk is UNCHANGED — self-heal did not save.
        assert profile_file.read_text() == raw_before

    # -------------------------------------------------------------------------
    # Two-word phrase extraction (bigrams share the unigram scored pool)
    # -------------------------------------------------------------------------

    def test_learns_distinctive_phrase_with_capitalization(self, voice_profile_dir: Path):
        """A distinctive two-word phrase said 3+ times is learned WITH its
        capitalization, stored as the exact space-joined surface form."""
        vp = VoiceProfile(config_dir=voice_profile_dir)

        for _ in range(3):
            vp.add_transcription("Wayfinder Aura handles the dictation nicely")

        vocab = vp.get_vocabulary()
        assert "Wayfinder Aura" in vocab

    def test_phrase_needs_min_phrase_occurrences(self, voice_profile_dir: Path):
        """A phrase said only twice is NOT learned (MIN_PHRASE_OCCURRENCES=3),
        even though a unigram said twice still is (MIN_OCCURRENCES=2)."""
        assert MIN_PHRASE_OCCURRENCES == 3
        vp = VoiceProfile(config_dir=voice_profile_dir)

        for _ in range(2):
            vp.add_transcription("the kubernetes cluster deploy went fine")

        vocab = vp.get_vocabulary()
        # Unigram passes the 2x bar; the exact pair does not clear the 3x bar.
        assert "kubernetes" in vocab
        assert "kubernetes cluster" not in vocab

    def test_no_cross_entry_phrase(self, voice_profile_dir: Path):
        """Adjacency is measured per history entry — the last word of one entry
        never pairs with the first word of the next."""
        vp = VoiceProfile(config_dir=voice_profile_dir)

        for _ in range(3):
            vp.add_transcription("we just fixed the taskbar")      # ends "taskbar"
        for _ in range(3):
            vp.add_transcription("Wayfinder needs another quick fix")  # starts "Wayfinder"

        vocab_lower = [w.casefold() for w in vp.get_vocabulary()]
        assert "taskbar wayfinder" not in vocab_lower

    def test_no_cross_punctuation_phrase(self, voice_profile_dir: Path):
        """A sentence break inside one entry breaks adjacency too — a pair may
        not span a punctuation segment boundary."""
        vp = VoiceProfile(config_dir=voice_profile_dir)

        for _ in range(4):
            vp.add_transcription("fixed the taskbar. Wayfinder needs it too")

        vocab_lower = [w.casefold() for w in vp.get_vocabulary()]
        assert "taskbar wayfinder" not in vocab_lower

    def test_short_word_breaks_phrase_adjacency(self, voice_profile_dir: Path):
        """A short word occupies an adjacency slot, so it prevents a false pair:
        "point of sale" must never yield "point sale"."""
        vp = VoiceProfile(config_dir=voice_profile_dir)

        for _ in range(4):
            vp.add_transcription("the point of sale system crashed again")

        vocab_lower = [w.casefold() for w in vp.get_vocabulary()]
        assert "point sale" not in vocab_lower

    def test_both_stop_phrase_dropped(self, voice_profile_dir: Path):
        """A pair of two ordinary-English words is never learned as a phrase."""
        vp = VoiceProfile(config_dir=voice_profile_dir)

        for _ in range(4):
            vp.add_transcription("the main window again please now")

        vocab_lower = [w.casefold() for w in vp.get_vocabulary()]
        assert "main window" not in vocab_lower

    def test_extra_stop_phrase_never_learned(self, voice_profile_dir: Path):
        """A pair touching a spoken filler (EXTRA_STOP_WORDS) is dropped even if
        the other word is distinctive — "gonna fix" is never a term."""
        vp = VoiceProfile(config_dir=voice_profile_dir)

        for _ in range(4):
            vp.add_transcription("gonna fix the Flatpak build right now")

        vocab = vp.get_vocabulary()
        vocab_lower = [w.casefold() for w in vocab]
        assert "gonna fix" not in vocab_lower
        # sanity: the distinctive unigram still surfaces
        assert "Flatpak" in vocab

    def test_one_stop_plus_distinctive_phrase_learnable(self, voice_profile_dir: Path):
        """RULE-3 CHOICE (base spec, kept): a phrase with ONE stop word — even
        as the leading word — is learnable, inheriting its distinctive word's
        weight. "new Flatpak" ("new" is a stop word) is learned given enough
        repetition. Real-data smoke showed no leading-stop pollution, so the
        first word is NOT required to be non-stop."""
        vp = VoiceProfile(config_dir=voice_profile_dir)

        for _ in range(3):
            vp.add_transcription("shipped the new Flatpak build today")

        assert "new Flatpak" in vp.get_vocabulary()

    def test_phrase_ignore_round_trip(self, voice_profile_dir: Path):
        """Ignoring the casefolded phrase drops it and keeps it gone across new
        transcriptions (a learned phrase round-trips through the editor)."""
        vp = VoiceProfile(config_dir=voice_profile_dir)

        for _ in range(3):
            vp.add_transcription("Wayfinder Aura handles the dictation nicely")
        assert "Wayfinder Aura" in vp.get_vocabulary()

        vp.set_ignored_words(["wayfinder aura"])  # casefolded phrase
        assert "Wayfinder Aura" not in vp.get_vocabulary()

        for _ in range(3):
            vp.add_transcription("Wayfinder Aura handles the dictation nicely")
        assert "Wayfinder Aura" not in vp.get_vocabulary()

    def test_ignoring_a_phrase_keeps_its_words(self, voice_profile_dir: Path):
        """Ignoring the phrase itself does NOT ignore its component words —
        only the exact phrase is filtered."""
        vp = VoiceProfile(config_dir=voice_profile_dir)

        for _ in range(3):
            vp.add_transcription("Wayfinder Aura handles the dictation nicely")

        vp.set_ignored_words(["wayfinder aura"])
        vocab = vp.get_vocabulary()
        assert "Wayfinder Aura" not in vocab
        # The distinctive word "Wayfinder" is unaffected as a unigram.
        assert "Wayfinder" in vocab

    def test_combined_pool_capped_with_phrases(self, voice_profile_dir: Path):
        """Words and phrases share ONE pool capped at VOCAB_LIMIT — the total
        stays within the cap, and a high-scoring phrase competes into it."""
        vp = VoiceProfile(config_dir=voice_profile_dir)

        # One strong distinctive phrase (score 10*4=40) — outranks the filler
        # unigrams below, so it earns a slot in the shared, capped pool.
        for _ in range(10):
            vp.add_transcription("the kubernetes cluster keeps growing")

        # 40 distinct distinctive unigrams (score 2*4=8 each) overflow the cap.
        for i in range(40):
            tok = f"zzq{chr(97 + i // 26)}{chr(97 + i % 26)}"
            for _ in range(2):
                vp.add_transcription(f"we mentioned {tok} in passing")

        vocab = vp.get_vocabulary()
        assert len(vocab) == VOCAB_LIMIT
        assert "kubernetes cluster" in vocab


# =============================================================================
# Persistence (save / load round-trip)
# =============================================================================


class TestPersistence:
    """Test save and load round-trip behaviour."""

    def test_save_then_load_round_trip(self, voice_profile_dir: Path):
        """Data persists across VoiceProfile instances."""
        vp1 = VoiceProfile(config_dir=voice_profile_dir)

        for _ in range(3):
            vp1.add_transcription("kubernetes deployment pipeline orchestration test")

        count_before = vp1.get_history_count()
        vocab_before = vp1.get_vocabulary()

        # Create a new instance pointing at the same directory
        vp2 = VoiceProfile(config_dir=voice_profile_dir)

        assert vp2.get_history_count() == count_before
        assert vp2.get_vocabulary() == vocab_before

    def test_load_with_missing_file(self, voice_profile_dir: Path):
        """Loading when no profile file exists results in empty state."""
        profile_file = voice_profile_dir / "voice_profile.json"
        assert not profile_file.exists()

        vp = VoiceProfile(config_dir=voice_profile_dir)

        assert vp.get_history_count() == 0
        assert vp.get_summary() == ""
        assert vp.get_vocabulary() == []

    def test_load_with_corrupt_json(self, voice_profile_dir: Path):
        """Corrupt JSON is handled gracefully without crashing."""
        profile_file = voice_profile_dir / "voice_profile.json"
        profile_file.write_text("{invalid json content!!!")

        vp = VoiceProfile(config_dir=voice_profile_dir)

        assert vp.get_history_count() == 0
        assert vp.get_summary() == ""

    def test_load_with_partial_data(self, voice_profile_dir: Path):
        """Partial/missing keys in the JSON are filled with defaults."""
        profile_file = voice_profile_dir / "voice_profile.json"
        profile_file.write_text(json.dumps({"history": []}))

        vp = VoiceProfile(config_dir=voice_profile_dir)

        assert vp.get_history_count() == 0
        assert vp.get_summary() == ""
        assert vp.get_vocabulary() == []

    def test_load_with_pre_existing_data(
        self, voice_profile_dir: Path, sample_voice_profile_data: dict
    ):
        """Pre-existing profile data is loaded correctly."""
        profile_file = voice_profile_dir / "voice_profile.json"
        profile_file.write_text(json.dumps(sample_voice_profile_data))

        vp = VoiceProfile(config_dir=voice_profile_dir)

        assert vp.get_history_count() == 5
        assert vp.has_profile
        assert vp.has_vocabulary
        assert "frontend" in vp.get_vocabulary()
        assert "software development" in vp.get_summary()


# =============================================================================
# get_prompt_context
# =============================================================================


class TestGetPromptContext:
    """Test prompt context generation."""

    def test_returns_empty_when_no_profile(self, voice_profile_dir: Path):
        """Empty string when there is no summary or vocabulary."""
        vp = VoiceProfile(config_dir=voice_profile_dir)

        assert vp.get_prompt_context() == ""

    def test_returns_summary_when_populated(
        self, voice_profile_dir: Path, sample_voice_profile_data: dict
    ):
        """Summary text is included when present."""
        profile_file = voice_profile_dir / "voice_profile.json"
        profile_file.write_text(json.dumps(sample_voice_profile_data))

        vp = VoiceProfile(config_dir=voice_profile_dir)
        context = vp.get_prompt_context()

        assert sample_voice_profile_data["profile"]["summary"] in context

    def test_returns_vocabulary_when_populated(
        self, voice_profile_dir: Path, sample_voice_profile_data: dict
    ):
        """Vocabulary terms appear in the context string."""
        profile_file = voice_profile_dir / "voice_profile.json"
        profile_file.write_text(json.dumps(sample_voice_profile_data))

        vp = VoiceProfile(config_dir=voice_profile_dir)
        context = vp.get_prompt_context()

        assert "Common terms:" in context
        assert "frontend" in context

    def test_vocabulary_only_context(self, voice_profile_dir: Path):
        """Context works with vocabulary but no summary."""
        data = {
            "history": [],
            "profile": {
                "summary": "",
                "vocabulary": ["kubernetes", "terraform"],
                "generated_at": 0,
                "samples_used": 0,
            },
            "transcriptions_since_regen": 0,
        }
        profile_file = voice_profile_dir / "voice_profile.json"
        profile_file.write_text(json.dumps(data))

        vp = VoiceProfile(config_dir=voice_profile_dir)
        context = vp.get_prompt_context()

        assert "Common terms:" in context
        assert "kubernetes" in context
        # With no summary, the context is just the vocabulary line
        assert context.startswith("Common terms:")


# =============================================================================
# Getters and Properties
# =============================================================================


class TestGettersAndProperties:
    """Test accessor methods and properties."""

    def test_get_vocabulary_returns_copy(self, voice_profile_dir: Path):
        """get_vocabulary returns a copy, not a direct reference."""
        vp = VoiceProfile(config_dir=voice_profile_dir)

        for _ in range(3):
            vp.add_transcription("kubernetes deployment pipeline setup test")

        vocab1 = vp.get_vocabulary()
        vocab2 = vp.get_vocabulary()

        assert vocab1 == vocab2
        assert vocab1 is not vocab2  # distinct list objects

    def test_get_summary_returns_empty_initially(self, voice_profile_dir: Path):
        """Summary is an empty string for a fresh profile."""
        vp = VoiceProfile(config_dir=voice_profile_dir)
        assert vp.get_summary() == ""

    def test_has_profile_false_initially(self, voice_profile_dir: Path):
        """has_profile is False when there is no summary."""
        vp = VoiceProfile(config_dir=voice_profile_dir)
        assert not vp.has_profile

    def test_has_profile_true_with_summary(
        self, voice_profile_dir: Path, sample_voice_profile_data: dict
    ):
        """has_profile is True when a summary exists."""
        profile_file = voice_profile_dir / "voice_profile.json"
        profile_file.write_text(json.dumps(sample_voice_profile_data))

        vp = VoiceProfile(config_dir=voice_profile_dir)
        assert vp.has_profile

    def test_has_vocabulary_false_initially(self, voice_profile_dir: Path):
        """has_vocabulary is False for a fresh profile."""
        vp = VoiceProfile(config_dir=voice_profile_dir)
        assert not vp.has_vocabulary

    def test_has_vocabulary_true_after_repeated_terms(self, voice_profile_dir: Path):
        """has_vocabulary becomes True once vocabulary is populated."""
        vp = VoiceProfile(config_dir=voice_profile_dir)

        for _ in range(3):
            vp.add_transcription("terraform ansible infrastructure automation test")

        assert vp.has_vocabulary

    def test_get_history_count_increments(self, voice_profile_dir: Path):
        """get_history_count reflects the number of added transcriptions."""
        vp = VoiceProfile(config_dir=voice_profile_dir)

        assert vp.get_history_count() == 0
        vp.add_transcription("first valid transcription here")
        assert vp.get_history_count() == 1
        vp.add_transcription("second valid transcription here")
        assert vp.get_history_count() == 2

    def test_is_regenerating_false_by_default(self, voice_profile_dir: Path):
        """is_regenerating is False when no regen is in progress."""
        vp = VoiceProfile(config_dir=voice_profile_dir)
        assert not vp.is_regenerating


# =============================================================================
# get_stats
# =============================================================================


class TestGetStats:
    """Test the get_stats method."""

    def test_stats_structure(self, voice_profile_dir: Path):
        """get_stats returns all expected keys."""
        vp = VoiceProfile(config_dir=voice_profile_dir)
        stats = vp.get_stats()

        expected_keys = {
            "history_count",
            "total_words",
            "vocabulary_count",
            "has_summary",
            "summary",
            "vocabulary",
            "generated_at",
            "samples_used",
            "transcriptions_since_regen",
        }
        assert set(stats.keys()) == expected_keys

    def test_stats_empty_profile(self, voice_profile_dir: Path):
        """Stats for a fresh profile have zeroed-out values."""
        vp = VoiceProfile(config_dir=voice_profile_dir)
        stats = vp.get_stats()

        assert stats["history_count"] == 0
        assert stats["total_words"] == 0
        assert stats["vocabulary_count"] == 0
        assert stats["has_summary"] is False
        assert stats["summary"] == ""
        assert stats["vocabulary"] == []
        assert stats["generated_at"] == 0
        assert stats["samples_used"] == 0
        assert stats["transcriptions_since_regen"] == 0

    def test_stats_after_transcriptions(self, voice_profile_dir: Path):
        """Stats are accurate after adding transcriptions."""
        vp = VoiceProfile(config_dir=voice_profile_dir)

        vp.add_transcription("hello world this is five")  # 5 words
        vp.add_transcription("another test with four")  # 4 words

        stats = vp.get_stats()
        assert stats["history_count"] == 2
        assert stats["total_words"] == 9
        assert stats["transcriptions_since_regen"] == 2

    def test_stats_with_loaded_profile(
        self, voice_profile_dir: Path, sample_voice_profile_data: dict
    ):
        """Stats reflect pre-loaded profile data."""
        profile_file = voice_profile_dir / "voice_profile.json"
        profile_file.write_text(json.dumps(sample_voice_profile_data))

        vp = VoiceProfile(config_dir=voice_profile_dir)
        stats = vp.get_stats()

        assert stats["history_count"] == 5
        assert stats["has_summary"] is True
        assert stats["samples_used"] == 5
        assert len(stats["vocabulary"]) > 0


# =============================================================================
# set_summary
# =============================================================================


class TestSetSummary:
    """Test manual summary editing."""

    def test_set_summary_updates_value(self, voice_profile_dir: Path):
        """set_summary stores the new summary."""
        vp = VoiceProfile(config_dir=voice_profile_dir)
        vp.set_summary("Custom summary text")

        assert vp.get_summary() == "Custom summary text"
        assert vp.has_profile

    def test_set_summary_strips_whitespace(self, voice_profile_dir: Path):
        """Leading/trailing whitespace is stripped from summaries."""
        vp = VoiceProfile(config_dir=voice_profile_dir)
        vp.set_summary("  padded summary  ")

        assert vp.get_summary() == "padded summary"

    def test_set_summary_persists(self, voice_profile_dir: Path):
        """set_summary writes to disk immediately."""
        vp = VoiceProfile(config_dir=voice_profile_dir)
        vp.set_summary("Persisted summary")

        vp2 = VoiceProfile(config_dir=voice_profile_dir)
        assert vp2.get_summary() == "Persisted summary"

    def test_set_summary_updates_generated_at(self, voice_profile_dir: Path):
        """set_summary updates the generated_at timestamp."""
        vp = VoiceProfile(config_dir=voice_profile_dir)
        before = int(time.time())
        vp.set_summary("Timestamped summary")
        after = int(time.time())

        with open(vp.profile_file) as f:
            data = json.load(f)

        assert before <= data["profile"]["generated_at"] <= after


# =============================================================================
# clear
# =============================================================================


class TestClear:
    """Test clearing all profile data."""

    def test_clear_wipes_history(self, voice_profile_dir: Path):
        """clear removes all transcription history."""
        vp = VoiceProfile(config_dir=voice_profile_dir)
        vp.add_transcription("Some transcription for clear test")
        assert vp.get_history_count() > 0

        vp.clear()

        assert vp.get_history_count() == 0

    def test_clear_wipes_summary(self, voice_profile_dir: Path):
        """clear removes the profile summary."""
        vp = VoiceProfile(config_dir=voice_profile_dir)
        vp.set_summary("A summary to be cleared")
        assert vp.has_profile

        vp.clear()

        assert not vp.has_profile
        assert vp.get_summary() == ""

    def test_clear_wipes_vocabulary(self, voice_profile_dir: Path):
        """clear removes learned vocabulary."""
        vp = VoiceProfile(config_dir=voice_profile_dir)
        for _ in range(3):
            vp.add_transcription("kubernetes terraform ansible orchestration test")
        assert vp.has_vocabulary

        vp.clear()

        assert not vp.has_vocabulary
        assert vp.get_vocabulary() == []

    def test_clear_persists(self, voice_profile_dir: Path):
        """clear writes the empty state to disk."""
        vp = VoiceProfile(config_dir=voice_profile_dir)
        vp.add_transcription("Data that will be cleared")
        vp.clear()

        vp2 = VoiceProfile(config_dir=voice_profile_dir)
        assert vp2.get_history_count() == 0
        assert vp2.get_summary() == ""

    def test_clear_resets_transcriptions_since_regen(self, voice_profile_dir: Path):
        """clear resets the regeneration counter."""
        vp = VoiceProfile(config_dir=voice_profile_dir)
        vp.add_transcription("Increment the regen counter here")
        stats_before = vp.get_stats()
        assert stats_before["transcriptions_since_regen"] > 0

        vp.clear()

        stats_after = vp.get_stats()
        assert stats_after["transcriptions_since_regen"] == 0


# =============================================================================
# regenerate_profile
# =============================================================================


class TestRegenerateProfile:
    """Test the regenerate_profile method."""

    def test_returns_false_when_already_in_progress(self, voice_profile_dir: Path):
        """regenerate_profile returns False if a regen is already running."""
        vp = VoiceProfile(config_dir=voice_profile_dir, min_samples_for_profile=1)

        # Add enough samples
        for i in range(5):
            vp.add_transcription(f"Sample transcription number {i} with words")

        # Simulate in-progress regeneration
        vp._regen_in_progress = True

        result = vp.regenerate_profile(lambda prompt: "test summary")
        assert result is False

    def test_returns_false_with_too_few_samples(self, voice_profile_dir: Path):
        """regenerate_profile returns False when below min_samples_for_profile."""
        vp = VoiceProfile(config_dir=voice_profile_dir, min_samples_for_profile=10)

        # Only add 3 samples (below the threshold of 10)
        for i in range(3):
            vp.add_transcription(f"Sample transcription number {i} with words")

        result = vp.regenerate_profile(lambda prompt: "test summary")
        assert result is False

    def test_returns_true_and_starts_regen(self, voice_profile_dir: Path):
        """regenerate_profile returns True and kicks off background regen."""
        vp = VoiceProfile(config_dir=voice_profile_dir, min_samples_for_profile=3)

        for i in range(5):
            vp.add_transcription(f"Sample transcription number {i} with words")

        mock_callback = MagicMock(return_value="Generated profile summary text")
        result = vp.regenerate_profile(mock_callback)

        assert result is True

    def test_regen_updates_summary_via_callback(self, voice_profile_dir: Path):
        """After regen completes, the summary is updated from the LLM callback."""
        vp = VoiceProfile(config_dir=voice_profile_dir, min_samples_for_profile=3)

        for i in range(5):
            vp.add_transcription(f"Sample transcription number {i} with words")

        summary_text = "Technical speaker discussing software engineering topics."
        mock_callback = MagicMock(return_value=summary_text)

        vp.regenerate_profile(mock_callback)

        # Wait for the background thread to complete
        import threading
        for t in threading.enumerate():
            if t.daemon and t is not threading.current_thread():
                t.join(timeout=5)

        assert vp.get_summary() == summary_text
        assert vp.has_profile


# =============================================================================
# Global Singleton
# =============================================================================


class TestGlobalSingleton:
    """Test the module-level get_voice_profile / reset_voice_profile functions."""

    def test_get_voice_profile_returns_singleton(self, voice_profile_dir: Path):
        """get_voice_profile returns the same instance on repeated calls."""
        reset_voice_profile()

        vp1 = get_voice_profile(config_dir=voice_profile_dir)
        vp2 = get_voice_profile(config_dir=voice_profile_dir)

        assert vp1 is vp2

    def test_reset_voice_profile_clears_singleton(self, voice_profile_dir: Path):
        """reset_voice_profile causes the next get to create a fresh instance."""
        vp1 = get_voice_profile(config_dir=voice_profile_dir)
        reset_voice_profile()
        vp2 = get_voice_profile(config_dir=voice_profile_dir)

        assert vp1 is not vp2

    def test_get_voice_profile_passes_params(self, voice_profile_dir: Path):
        """Parameters are forwarded to the VoiceProfile constructor."""
        reset_voice_profile()

        vp = get_voice_profile(
            config_dir=voice_profile_dir,
            history_limit=42,
            regen_interval=7,
        )

        assert vp.history_limit == 42
        assert vp.regen_interval == 7

    def test_get_voice_profile_ignores_params_on_subsequent_calls(
        self, voice_profile_dir: Path
    ):
        """Once the singleton exists, new parameters are ignored."""
        reset_voice_profile()

        vp1 = get_voice_profile(config_dir=voice_profile_dir, history_limit=42)
        vp2 = get_voice_profile(config_dir=voice_profile_dir, history_limit=99)

        # Still the same instance with the original limit
        assert vp2 is vp1
        assert vp2.history_limit == 42


# =============================================================================
# Editable vocabulary — ignore list (persistence + effect)
# =============================================================================


class TestVocabularyIgnore:
    """Test the persistent vocabulary_ignore list on VoiceProfile."""

    def test_ignore_empty_by_default(self, voice_profile_dir: Path):
        """A fresh profile has an empty ignore list."""
        vp = VoiceProfile(config_dir=voice_profile_dir)
        assert vp.get_ignored_words() == []

    def test_set_ignored_words_persists_across_reload(self, voice_profile_dir: Path):
        """Ignored words survive a save/load round-trip."""
        vp = VoiceProfile(config_dir=voice_profile_dir)
        vp.set_ignored_words(["don", "foo"])

        vp2 = VoiceProfile(config_dir=voice_profile_dir)
        assert vp2.get_ignored_words() == ["don", "foo"]

    def test_set_ignored_words_dedups_and_strips(self, voice_profile_dir: Path):
        """Ignore list is deduped case-insensitively; blanks are dropped."""
        vp = VoiceProfile(config_dir=voice_profile_dir)
        vp.set_ignored_words(["  don  ", "Don", "", "   ", "foo"])

        assert vp.get_ignored_words() == ["don", "foo"]

    def test_get_ignored_words_returns_copy(self, voice_profile_dir: Path):
        """get_ignored_words returns a distinct list object."""
        vp = VoiceProfile(config_dir=voice_profile_dir)
        vp.set_ignored_words(["don"])

        a = vp.get_ignored_words()
        b = vp.get_ignored_words()
        assert a == b
        assert a is not b

    def test_clear_resets_ignore_list(self, voice_profile_dir: Path):
        """clear() wipes the ignore list."""
        vp = VoiceProfile(config_dir=voice_profile_dir)
        vp.set_ignored_words(["don"])
        assert vp.get_ignored_words() == ["don"]

        vp.clear()

        assert vp.get_ignored_words() == []

    def test_load_old_file_defaults_ignore_empty(self, voice_profile_dir: Path):
        """A profile file predating the feature loads with an empty ignore list."""
        old_data = {
            "history": [],
            "profile": {
                "summary": "old",
                "vocabulary": ["kubernetes"],
                "generated_at": 0,
                "samples_used": 0,
            },
            "transcriptions_since_regen": 0,
        }
        profile_file = voice_profile_dir / "voice_profile.json"
        profile_file.write_text(json.dumps(old_data))

        vp = VoiceProfile(config_dir=voice_profile_dir)
        assert vp.get_ignored_words() == []

    def test_update_vocabulary_excludes_ignored_after_add(self, voice_profile_dir: Path):
        """A learned word, once ignored, is filtered from future extraction."""
        vp = VoiceProfile(config_dir=voice_profile_dir)

        for _ in range(3):
            vp.add_transcription("the standup with Daan covered kubernetes work")

        # "Daan" is learned (distinctive name, appears 3x)
        assert "Daan" in vp.get_vocabulary()

        vp.set_ignored_words(["Daan"])
        # Immediately reflected — set_ignored_words re-runs extraction
        assert "Daan" not in vp.get_vocabulary()
        assert "kubernetes" in vp.get_vocabulary()

        # And it stays gone as new transcriptions arrive
        for _ in range(2):
            vp.add_transcription("another standup where Daan talked about kubernetes")
        assert "Daan" not in vp.get_vocabulary()

    def test_set_ignored_words_takes_effect_immediately(self, voice_profile_dir: Path):
        """get_vocabulary reflects a new ignore list without any re-add."""
        vp = VoiceProfile(config_dir=voice_profile_dir)
        for _ in range(3):
            vp.add_transcription("kubernetes terraform ansible orchestration test")

        assert "terraform" in vp.get_vocabulary()
        vp.set_ignored_words(["terraform"])
        assert "terraform" not in vp.get_vocabulary()


# =============================================================================
# merge_vocab_view (pure function)
# =============================================================================


class TestMergeVocabView:
    """Test the merge_vocab_view helper."""

    def test_custom_terms_come_first(self):
        """Custom (pinned) terms are listed before learned words."""
        result = merge_vocab_view(["Daan", "Wayfinder"], ["standup", "kubernetes"], [])
        assert result[:2] == ["Daan", "Wayfinder"]
        assert "standup" in result
        assert "kubernetes" in result

    def test_case_insensitive_dedup_custom_suppresses_learned(self):
        """A custom term case-insensitively suppresses the learned duplicate."""
        result = merge_vocab_view(["Daan"], ["daan", "standup"], [])
        # "daan" learned is dropped in favour of the pinned "Daan"
        assert result == ["Daan", "standup"]

    def test_ignored_words_excluded(self):
        """Ignored learned words never appear in the view."""
        result = merge_vocab_view([], ["don", "standup"], ["don"])
        assert result == ["standup"]

    def test_case_preserved_and_order_preserved(self):
        """Custom case and order are preserved verbatim."""
        result = merge_vocab_view(["ZeroTier", "aBc"], [], [])
        assert result == ["ZeroTier", "aBc"]

    def test_empty_inputs(self):
        """All-empty inputs yield an empty list."""
        assert merge_vocab_view([], [], []) == []

    def test_no_case_insensitive_duplicates(self):
        """The result contains no case-insensitive duplicates."""
        result = merge_vocab_view(["Foo"], ["foo", "FOO", "bar"], [])
        lowered = [w.lower() for w in result]
        assert len(lowered) == len(set(lowered))
        assert result == ["Foo", "bar"]


# =============================================================================
# diff_vocab_edit (pure function)
# =============================================================================


class TestDiffVocabEdit:
    """Test the diff_vocab_edit helper."""

    def test_the_daan_story(self):
        """The motivating case: fix a mis-learned name.

        Whisper heard "Daan" as "don"; the user deletes "don" and types "Daan".
        Result: "Daan" is pinned (custom), "don" is ignored.
        """
        custom, ignored = diff_vocab_edit(["Daan", "standup"], ["don", "standup"])
        assert custom == ["Daan"]
        assert ignored == ["don"]

    def test_pure_addition_ignores_nothing(self):
        """Adding a new term ignores no learned words."""
        custom, ignored = diff_vocab_edit(["standup", "Daan"], ["standup"])
        assert custom == ["Daan"]
        assert ignored == []

    def test_pure_deletion_pins_nothing(self):
        """Deleting a learned word pins no custom terms."""
        custom, ignored = diff_vocab_edit(["standup"], ["don", "standup"])
        assert custom == []
        assert ignored == ["don"]

    def test_whitespace_and_blank_lines_dropped(self):
        """Blank and whitespace-only lines are ignored; terms are stripped."""
        custom, ignored = diff_vocab_edit(
            ["", "  Daan  ", "   ", "standup"], ["standup"]
        )
        assert custom == ["Daan"]
        assert ignored == []

    def test_case_insensitive_learned_match_kept(self):
        """A learned word re-typed with different case is kept, not ignored."""
        custom, ignored = diff_vocab_edit(["Standup"], ["standup"])
        assert custom == []
        assert ignored == []

    def test_custom_deduped_case_insensitively(self):
        """Duplicate custom lines collapse case-insensitively (first wins)."""
        custom, ignored = diff_vocab_edit(["Daan", "daan", "DAAN"], [])
        assert custom == ["Daan"]
        assert ignored == []

    def test_empty_edit_ignores_all_learned(self):
        """An emptied editor ignores every learned word."""
        custom, ignored = diff_vocab_edit([], ["don", "standup"])
        assert custom == []
        assert ignored == ["don", "standup"]

    def test_prior_ignores_survive_a_second_save(self):
        """REGRESSION: 'don' was ignored on save 1, so it's absent from
        `learned` by save 2 — without prev_ignored it would drop off the
        ignore list and the extractor would resurrect it from history."""
        custom, ignored = diff_vocab_edit(
            ["Daan", "standup"], ["standup"], prev_ignored=["don"]
        )
        assert custom == ["Daan"]
        assert ignored == ["don"]

    def test_retyping_an_ignored_word_unignores_it(self):
        """Typing a previously deleted word back in revives it (as pinned)."""
        custom, ignored = diff_vocab_edit(
            ["don", "standup"], ["standup"], prev_ignored=["don"]
        )
        assert ignored == []
        assert custom == ["don"]

    def test_prev_ignored_not_duplicated_into_ignores(self):
        """A word both freshly deleted AND previously ignored appears once."""
        custom, ignored = diff_vocab_edit(
            [], ["don"], prev_ignored=["don", "Don"]
        )
        assert custom == []
        assert ignored == ["don"]


# =============================================================================
# custom_vocabulary config round-trip
# =============================================================================


class TestCustomVocabularyConfigRoundTrip:
    """The pinned vocabulary must survive save_config/load_config."""

    def test_custom_vocabulary_survives_round_trip(self, temp_config_dir: Path):
        """A custom_vocabulary list persists through save/load."""
        from wayfinder.config import load_config, save_config

        config = load_config()
        config["custom_vocabulary"] = ["Daan", "Wayfinder", "ZeroTier"]
        save_config(config)

        reloaded = load_config()
        assert reloaded["custom_vocabulary"] == ["Daan", "Wayfinder", "ZeroTier"]
