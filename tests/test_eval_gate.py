"""The tone-eval gate: metrics applicability and baseline comparison.

Pure — no model, no GPU. These cover the parts of the harness that decide
pass/fail, because a gate that is silently inert (or that rejects a correct fix)
is worse than no gate.
"""
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import eval_tones as E  # noqa: E402
from tone_eval import metrics as M  # noqa: E402
from tone_eval.corpus import CORPUS, expected_removals  # noqa: E402

SAMPLE = {"id": "x", "stresses": [], "expected_removals": ["um", "uh"]}


class TestFillerCanary:
    def test_word_retention_is_blind_to_filler_removal(self):
        """Why this metric exists at all: _content_tokens() excludes the whole
        FILLER set, so retention cannot see whether fillers were removed."""
        inp = "um so basically i went uh to the store"
        assert M.word_retention(inp, inp) == 1.0          # nothing removed
        assert M.word_retention(inp, "i went to the store") == 1.0  # all removed

    def test_counts_occurrences_not_types(self):
        """A sound present twice must be removed twice."""
        assert M.required_filler_removal("um a um b", "um a b", ["um", "um"]) == 0.5
        assert M.required_filler_removal("um a um b", "a b", ["um", "um"]) == 1.0

    def test_returns_none_when_unannotated(self):
        """None, not 1.0 — an unannotated sample must not look like a pass."""
        assert M.required_filler_removal("hello", "hello", []) is None

    def test_echoed_output_fails_the_gate(self):
        """The Gemma-under-template failure: model returns the input verbatim."""
        inp = "um so basically i went uh to the store"
        m = M.compute_all({**SAMPLE, "expected_removals": expected_removals(inp)},
                          "minimal", inp, inp, "standard")
        assert m["required_filler_removal"] == 0.0
        assert m["passes"]["required_filler_removal"] is False

    def test_corpus_annotations_are_filler_sounds_only(self):
        """Discourse words like "like"/"you know" are NOT removable targets —
        minimal only promises um/uh/ah/er."""
        for s in CORPUS:
            for w in s["expected_removals"]:
                assert w.rstrip("m h r") in ("u", "a", "e", "")


class TestMetricApplicability:
    @pytest.mark.parametrize("tone,intensity,applies", [
        ("minimal", "standard", True),
        ("dev", "standard", True),
        ("personal", "standard", True),
        ("professional", "standard", False),   # legitimately replaces slang
        ("casual", "strong", False),           # transformative
        ("casual", "caricature", False),       # transformative
    ])
    def test_retention_applicability(self, tone, intensity, applies):
        assert M.retention_applies(tone, intensity) is applies

    def test_professional_retention_would_reject_a_correct_fix(self):
        """An echoed baseline retains every token (1.0); the CORRECT professional
        output for this sample scores 0.4. A relative gate would reject the fix,
        which is why retention is N/A for professional."""
        s = next(x for x in CORPUS if x["id"] == "short_01_slang")
        echoed = M.word_retention(s["text"], s["text"])
        correct = M.word_retention(s["text"], "Oh, cool, nice.")
        assert echoed == 1.0 and correct < 0.5
        assert M.compute_all(s, "professional", s["text"], "Oh, cool, nice.",
                             "standard")["word_retention"] is None

    def test_transformative_omits_the_whole_guide_family(self):
        """Not just retention — order/length/new-word/sentence gates would all
        penalize exactly what strong/caricature exist to produce."""
        m = M.compute_all(SAMPLE, "casual", "um hello there", "YO FAM 💀", "caricature")
        for key in ("retention", "order_lcs", "len_ratio", "new_words", "sentence_delta"):
            assert key not in m["passes"]
        assert m["guide_score"] is None
        assert m["word_retention"] is None

    def test_caricature_has_no_automated_quality_gate(self):
        m = M.compute_all(SAMPLE, "casual", "um hello there", "YO FAM 💀", "caricature")
        assert m["passes"] == {}

    def test_standard_keeps_the_guide_family(self):
        m = M.compute_all(SAMPLE, "minimal", "um hello there friend", "hello there friend")
        assert "retention" in m["passes"] and m["guide_score"] is not None


class TestSummarizeHandlesNA:
    def test_does_not_raise_on_none_metrics(self):
        recs = [
            {"tone": "casual", "intensity": "caricature", "latency_s": 1.0,
             "metrics": M.compute_all(SAMPLE, "casual", "um hi there", "YO 💀", "caricature")},
            {"tone": "casual", "intensity": "standard", "latency_s": 1.0,
             "metrics": M.compute_all(SAMPLE, "casual", "um hi there mate", "hi there mate")},
        ]
        s = M.summarize(recs)["casual"]
        assert s["n"] == 2
        # Averaged over the ONE row where it applied, and says so.
        assert s["word_retention"]["n"] == 1
        assert s["guide_score"]["n"] == 1


def _payload(fingerprint=None, rows=None):
    return {
        "fingerprint": {"corpus_version": 2, "model_key": "k", "chat_template": "off",
                        **(fingerprint or {})},
        "results": rows if rows is not None else [_row()],
    }


def _row(**kw):
    row = {
        "sample_id": "x", "tone": "minimal", "intensity": "standard",
        "latency_s": 1.0, "error": None, "fell_back": False, "fallback_markers": [],
        "echoed_input": False, "termination": {"passed": True},
        "metrics": {"passes": {}, "word_retention": 0.9, "dev_term_preservation": 1.0},
    }
    row.update(kw)
    return row


class TestBaselineGate:
    def test_clean_candidate_passes(self):
        base = _payload()
        cand = _payload({"chat_template": "auto"})
        assert E.compare_to_baseline(base, cand) == []

    def test_fingerprint_mismatch_refuses_to_compare(self):
        """A different model/binary/corpus makes the whole comparison meaningless."""
        v = E.compare_to_baseline(_payload(), _payload({"model_key": "other"}))
        assert any("FINGERPRINT model_key" in x for x in v)
        assert any("refusing to compare" in x for x in v)

    def test_chat_template_is_the_exempt_field(self):
        assert E.compare_to_baseline(_payload(), _payload({"chat_template": "auto"})) == []

    def test_missing_row_in_either_direction_fails(self):
        """A silently shrunken matrix must not look like a pass."""
        assert any("MISSING in candidate" in x
                   for x in E.compare_to_baseline(_payload(), _payload(rows=[])))
        assert any("MISSING in baseline" in x
                   for x in E.compare_to_baseline(_payload(rows=[]), _payload()))

    @pytest.mark.parametrize("kw,marker", [
        ({"echoed_input": True}, "ECHO"),
        ({"termination": {"passed": False, "reason": "hit the cap"}}, "TERMINATION"),
    ])
    def test_defect_gates_are_absolute(self, kw, marker):
        """The baseline IS the bug, so these cannot be relative to it."""
        v = E.compare_to_baseline(_payload(), _payload({"chat_template": "auto"},
                                                       rows=[_row(**kw)]))
        assert any(x.startswith(marker) for x in v)

    @pytest.mark.parametrize("kw,marker", [
        ({"error": "boom"}, "ERROR"),
        ({"fell_back": True, "fallback_markers": ["x"]}, "FALLBACK"),
    ])
    def test_health_gates_fail_only_when_new(self, kw, marker):
        """The hallucination guard fires on 5 baseline rows; requiring the
        candidate to fix pre-existing model behaviour would block a correct
        template change for an unrelated reason."""
        cand = _payload({"chat_template": "auto"}, rows=[_row(**kw)])
        assert any(x.startswith(marker)
                   for x in E.compare_to_baseline(_payload(), cand))          # new -> fails
        assert E.compare_to_baseline(_payload(rows=[_row(**kw)]), cand) == []  # carried -> passes

    def test_gate_flip_is_caught(self):
        """A gate the baseline passed and the candidate fails."""
        bad = {"passes": {"required_filler_removal": False}, "word_retention": 0.9}
        good = {"passes": {"required_filler_removal": True}, "word_retention": 0.9}
        cand = _payload({"chat_template": "auto"}, rows=[_row(metrics=bad)])
        assert any("GATE" in x and "required_filler_removal" in x
                   for x in E.compare_to_baseline(_payload(rows=[_row(metrics=good)]), cand))
        # Already failing in the baseline -> carried, not a regression.
        assert E.compare_to_baseline(_payload(rows=[_row(metrics=bad)]), cand) == []

    def test_raw_metric_drops_are_not_regressions(self):
        """MEASURED: an untouched output scores word_retention 1.0, so a candidate
        that correctly removes "lowkey" (0.944) or the whisper stutter "i think i
        think" (0.826) would look like a regression under a magnitude comparison
        while being strictly better. Only gate FLIPS count."""
        passing = {"passes": {"retention": True}, "word_retention": 1.0}
        cleaned = {"passes": {"retention": True}, "word_retention": 0.944}
        assert E.compare_to_baseline(
            _payload(rows=[_row(metrics=passing)]),
            _payload({"chat_template": "auto"}, rows=[_row(metrics=cleaned)]),
        ) == []

    def test_gate_absent_from_candidate_is_skipped(self):
        """Applicability is inherited: a gate the candidate row does not carry
        (transformative intensity, non-dev tone) is never compared."""
        base = _payload(rows=[_row(metrics={"passes": {"dev_term_preservation": True}})])
        cand = _payload({"chat_template": "auto"}, rows=[_row(metrics={"passes": {}})])
        assert E.compare_to_baseline(base, cand) == []

    def test_wall_clock_is_advisory_not_a_gate(self):
        """MEASURED: two full candidate runs on identical committed code gave 0 of
        198 differing outputs but a 1.30x median latency swing, purely from
        background load. A wall-clock band reports the machine, not the change."""
        base = _payload(rows=[_row(latency_s=1.0)])
        cand = _payload({"chat_template": "auto"}, rows=[_row(latency_s=3.0)])
        adv = []
        assert E.compare_to_baseline(base, cand, adv) == []
        assert any("latency" in a and "3.00x" in a for a in adv)

    def test_generated_bytes_are_gated_instead(self):
        """Deterministic proxy for 'does the change make the model do more work'."""
        base = _payload(rows=[_row(termination={"passed": True, "bytes1": 100})])
        worse = _payload({"chat_template": "auto"},
                         rows=[_row(termination={"passed": True, "bytes1": 200})])
        better = _payload({"chat_template": "auto"},
                          rows=[_row(termination={"passed": True, "bytes1": 50})])
        assert any(x.startswith("GENERATED") for x in E.compare_to_baseline(base, worse))
        assert E.compare_to_baseline(base, better) == []


class TestMatrixShape:
    def test_default_cells_are_enumerated_not_a_cross_product(self):
        """A cross product would give caricature for every tone; rule 5 asks for
        five tones at standard+strong, plus the reported caricature case."""
        assert len(E.DEFAULT_CELLS) == 11
        assert sum(1 for _, i in E.DEFAULT_CELLS if i == "caricature") == 1
        assert ("casual", "caricature") in E.DEFAULT_CELLS

    def test_all_five_rule5_tones_are_present(self):
        assert set(E.ALL_TONES) == {"minimal", "professional", "casual", "dev", "personal"}
        for tone in E.ALL_TONES:
            assert (tone, "standard") in E.DEFAULT_CELLS
            assert (tone, "strong") in E.DEFAULT_CELLS
