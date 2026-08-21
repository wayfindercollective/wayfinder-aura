"""Deterministic, stdlib-only metrics for tone post-processing evaluation.

The metrics encode the user's bar: each non-minimal *standard* tone should be a
GENTLE GUIDE — keep ~90% of the speaker's words and their order, never rewrite,
reorder, summarize, or invent content. Tone-specific checks add a little on top.

All thresholds live in PASS_BANDS so they're easy to tune from one place.
"""

from __future__ import annotations

import re
from collections import Counter
from difflib import SequenceMatcher

# Discourse fillers we don't want to credit OR penalize in retention/order.
# Removing these is the model's job, so they're excluded from "content".
FILLER = {
    "um", "uh", "ah", "er", "eh", "hmm", "mm", "uhm", "mhm",
    "like", "basically", "actually", "literally", "honestly",
    "so", "well", "right", "anyway", "yeah", "okay", "ok",
    "i", "you", "know", "mean",  # 'i mean' / 'you know' filler tokens
}

# Slang the professional tone is expected to tidy away (lowercased).
SLANG = {
    "bro", "dude", "man", "tight", "fire", "stoked", "lowkey", "kinda",
    "gonna", "wanna", "dunno", "nah", "sooo", "haha", "lol", "yeah",
    "thats",  # contraction-as-slang spelling
}

PASS_BANDS = {
    "retention_min": 0.85,
    "order_lcs_min": 0.80,
    "len_ratio": (0.75, 1.25),
    "new_words_max": 2,
    "sent_delta_max": {"casual": 2, "_default": 1},
    "prof_caps_min": 0.9,
    # See nonslang_preservation(): MEASURED, good outputs score 0.810-1.000 and
    # cross-sample rewrites top out at 0.800. 0.80 is the boundary of that gap —
    # it rejects every observed rewrite and accepts every observed good output.
    # (An earlier note here claimed 0.75 "sits inside the gap"; it does not, it
    # sits below it, and would have admitted a rewrite scoring 0.79.)
    "nonslang_preservation_min": 0.80,
}

_TOKEN_RE = re.compile(r"[a-z0-9']+")
_SENT_RE = re.compile(r"[.!?]+")


# ----------------------------------------------------------------------------
# Tokenization helpers
# ----------------------------------------------------------------------------
def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall((text or "").lower())


def _content_tokens(text: str) -> list[str]:
    return [t for t in _tokens(text) if t not in FILLER]


def _word_count(text: str) -> int:
    return len((text or "").split())


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENT_RE.split(text or "") if s.strip()]


def _has_word(text: str, word: str) -> bool:
    return re.search(r"\b" + re.escape(word.lower()) + r"\b", (text or "").lower()) is not None


# ----------------------------------------------------------------------------
# Individual metrics
# ----------------------------------------------------------------------------
def word_retention(inp: str, out: str) -> float:
    """Fraction of input *content* words preserved (multiset-capped)."""
    ic = Counter(_content_tokens(inp))
    oc = Counter(_content_tokens(out))
    denom = sum(ic.values())
    if denom == 0:
        return 1.0
    kept = sum(min(c, oc.get(w, 0)) for w, c in ic.items())
    return kept / denom


def order_lcs_ratio(inp: str, out: str) -> float:
    """Order-sensitive similarity of the content-token sequences (0..1).

    High = same words in the same order (a guide). Low = reordered/rewritten.
    """
    a, b = _content_tokens(inp), _content_tokens(out)
    if not a and not b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def length_ratio(inp: str, out: str) -> float:
    iw = _word_count(inp)
    if iw == 0:
        return 1.0
    return _word_count(out) / iw


def new_content_words(inp: str, out: str) -> list[str]:
    """Distinct content words in output that were not in input (invention)."""
    iset = set(_content_tokens(inp))
    return sorted({t for t in _content_tokens(out) if t not in iset})


def lowercase_ratio(text: str) -> float:
    alpha = [c for c in (text or "") if c.isalpha()]
    if not alpha:
        return 1.0
    return sum(1 for c in alpha if c.islower()) / len(alpha)


def terminal_punct_density(text: str) -> float:
    sents = _sentences(text) or [""]
    return len(_SENT_RE.findall(text or "")) / len(sents)


def sentence_start_caps_ratio(text: str) -> float:
    sents = _sentences(text)
    if not sents:
        return 1.0
    caps = 0
    for s in sents:
        m = re.search(r"[A-Za-z]", s)
        if m and s[m.start()].isupper():
            caps += 1
    return caps / len(sents)


def wer(reference: str, hypothesis: str) -> float:
    """Word Error Rate = (substitutions + deletions + insertions) / |reference words|.

    Token-level Levenshtein over lowercased alphanumeric tokens (the same
    normalization intent as the app's inject path). 0.0 == perfect; can exceed 1.0
    when the hypothesis is much longer than the reference. Used by the golden-audio
    ASR accuracy test to assert transcription quality against known speech.
    """
    ref = _tokens(reference)
    hyp = _tokens(hypothesis)
    if not ref:
        return 0.0 if not hyp else 1.0
    prev = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        cur = [i]
        for j, h in enumerate(hyp, 1):
            cost = 0 if r == h else 1
            cur.append(min(prev[j] + 1,        # deletion
                           cur[j - 1] + 1,     # insertion
                           prev[j - 1] + cost))  # match / substitution
        prev = cur
    return prev[-1] / len(ref)


def phrase_hits(text: str, phrases: list[str]) -> list[str]:
    """Key phrases present in ``text`` (normalized whole-token substring match).

    Robust to trailing-word ASR errors — the real 'did it understand the sentence'
    check that complements WER in the golden-audio test.
    """
    norm = " ".join(_tokens(text))
    hits = []
    for p in phrases or []:
        pn = " ".join(_tokens(p))
        if pn and pn in norm:
            hits.append(p)
    return hits


def dev_term_preservation(out: str, dev_terms: list[str]) -> float:
    if not dev_terms:
        return 1.0
    return sum(1 for t in dev_terms if _has_word(out, t)) / len(dev_terms)


def slang_remaining(inp: str, out: str, slang: list[str] | None) -> list[str]:
    """Slang tokens present in input that still appear in output."""
    candidates = set(s.lower() for s in (slang or []))
    candidates |= {t for t in _tokens(inp) if t in SLANG}
    return sorted(t for t in candidates if _has_word(inp, t) and _has_word(out, t))


def nonslang_preservation(inp: str, out: str, slang: list[str] | None) -> float | None:
    """Fraction of NON-SLANG input content tokens surviving, in order (0..1).

    This is the gate that keeps "professional" honest. That tone is licensed to
    replace slang, so order_lcs and new_words are N/A for it — but without a
    replacement, an output could rewrite every non-slang word and still pass on
    length and capitalization alone. Masking the slang and measuring the rest
    tests precisely the part the tone is NOT licensed to change.

    Uses LCS length over the masked input rather than SequenceMatcher's ratio,
    so the substituted words the model ADDS in place of slang do not count
    against it — only dropped or reordered non-slang content does.
    """
    drop = set(x.lower() for x in (slang or [])) | SLANG
    src = [t for t in _content_tokens(inp) if t not in drop]
    if not src:
        return None  # nothing this tone had to preserve
    dst = _content_tokens(out)
    # LCS length
    prev = [0] * (len(dst) + 1)
    for a in src:
        cur = [0]
        for j, b in enumerate(dst):
            cur.append(prev[j] + 1 if a == b else max(cur[j], prev[j + 1]))
        prev = cur
    return prev[len(dst)] / len(src)


_NEGATIONS = frozenset({
    "not", "no", "never", "none", "cannot", "cant", "dont", "doesnt", "didnt",
    "wont", "wouldnt", "shouldnt", "couldnt", "isnt", "arent", "wasnt",
    "werent", "hasnt", "havent", "hadnt", "nor", "neither", "without",
    # Colloquial forms MUST be here or the metric fires on correct cleanups:
    # MEASURED, "or nah" -> "or not" and "dunno" -> "don't know" are faithful
    # expansions that looked like added negations until the input side could
    # see the originals.
    "nah", "nope", "dunno", "aint", "nuh",
})


def negation_delta(inp: str, out: str) -> int:
    """Net negation markers ADDED (+) or DROPPED (-) by the cleanup.

    Cleanup must never flip the meaning of a dictation, and the cheapest way to
    do exactly that is to add or drop a negation: "send the payment" ->
    "do not send the payment" is a one-token edit that scores perfectly on every
    other metric here. It survives nonslang_preservation (input-token recall,
    where insertions are free) and cannot be caught by an insertion budget —
    MEASURED, good professional rows insert up to 4 content words beyond what
    slang replacement accounts for, while that inversion inserts only 2, so the
    two populations overlap and no budget separates them.

    Deliberately narrow: it catches the inversion class, not paraphrase. n't is
    normalized because "don't" tokenizes apart from "dont".
    """
    def count(text: str) -> int:
        t = re.sub(r"n[\u2019']t\b", " not", (text or "").lower())
        return sum(1 for w in _content_tokens(t) if w in _NEGATIONS)
    return count(out) - count(inp)


# ----------------------------------------------------------------------------
# Aggregation
# ----------------------------------------------------------------------------
# Absolute gate for filler removal. 0.80 rather than 1.0 because a small model
# occasionally keeps one sound in a long utterance; a model that keeps MOST of
# them (the Gemma-under-template failure) lands far below this.
REQUIRED_FILLER_REMOVAL_MIN = 0.80

# Intensities that intentionally transform the text. Every guide-preservation
# metric is meaningless for them: an echoed (buggy) output scores word_retention
# 1.0 because it contains the whole input, while a CORRECT caricature scores
# 0.286 — so a relative gate would reject the fix rather than the bug.
_TRANSFORMATIVE = ("strong", "caricature")

# Tones that promise word preservation. "professional" is excluded at every
# intensity because it legitimately replaces slang ("oh thats tight bro nice" ->
# "Oh, cool, nice." retains 0.4 and is correct).
_PRESERVING_TONES = ("minimal", "casual", "dev", "personal")


def required_filler_removal(inp: str, out: str, expected: list[str] | None) -> float | None:
    """Fraction of annotated filler occurrences absent from the output.

    Occurrence/multiset based: a sound present twice in the input must be gone
    twice. Returns None when the sample has no annotated fillers (not 1.0 — an
    unannotated sample must not look like a pass).
    """
    if not expected:
        return None
    out_counts = Counter(_tokens(out))
    exp_counts = Counter(w.lower() for w in expected)
    removed = sum(max(0, c - out_counts.get(w, 0)) for w, c in exp_counts.items())
    return round(removed / sum(exp_counts.values()), 3)


def retention_applies(tone: str, intensity: str) -> bool:
    """Whether word_retention is a meaningful gate for this cell."""
    return intensity not in _TRANSFORMATIVE and tone in _PRESERVING_TONES


def substitution_applies(tone: str, intensity: str) -> bool:
    """Whether the substitution-sensitive gates (order_lcs, new_words) mean
    anything for this cell.

    Same rule as retention: a tone licensed to replace words cannot be scored by
    metrics that treat replacement as damage.
    """
    return intensity not in _TRANSFORMATIVE and tone in _PRESERVING_TONES


def compute_all(sample: dict, tone: str, inp: str, out: str,
                intensity: str = "standard") -> dict:
    """Return every raw metric + a `passes` dict of booleans + guide_score."""
    retention = word_retention(inp, out)
    order = order_lcs_ratio(inp, out)
    lratio = length_ratio(inp, out)
    new_words = new_content_words(inp, out)
    sent_in, sent_out = len(_sentences(inp)), len(_sentences(out))
    sent_delta = abs(sent_out - sent_in)
    sent_delta_max = PASS_BANDS["sent_delta_max"].get(
        tone, PASS_BANDS["sent_delta_max"]["_default"]
    )

    lo, hi = PASS_BANDS["len_ratio"]
    transformative = intensity in _TRANSFORMATIVE
    passes: dict[str, bool] = {}
    if not transformative:
        # The whole guide-preservation family is omitted for strong/caricature —
        # not just retention. Keeping order/length/new-word/sentence gates active
        # would penalize exactly the transformation those modes exist to produce,
        # and contradicts "caricature has no automated quality metric".
        passes.update({
            "len_ratio": lo <= lratio <= hi,
            "sentence_delta": sent_delta <= sent_delta_max,
        })
        # order_lcs and new_words are omitted for "professional" for the SAME
        # reason retention already is — the tone is defined to replace slang, and
        # both metrics score substitution as if it were a failure:
        #   * new_words counts substitutions directly ("nah" -> "not" is a new
        #     content word), so it is definitionally in conflict.
        #   * order_lcs is SequenceMatcher over content TOKENS, so a substituted
        #     token leaves the match exactly as a reordered one does. It cannot
        #     distinguish the two. MEASURED: "you wanna grab food after or nah"
        #     -> "Do you want to grab food afterward or not" preserves every
        #     clause in order and still scores as reordering.
        # On the preserving tones there are few substitutions, so both stay
        # meaningful there. prof_caps, prof_slang_removal, len_ratio,
        # sentence_delta and required_filler_removal remain gated for
        # professional and are what actually holds the tone to its contract.
        if substitution_applies(tone, intensity):
            passes["order_lcs"] = order >= PASS_BANDS["order_lcs_min"]
            passes["new_words"] = len(new_words) <= PASS_BANDS["new_words_max"]
        if retention_applies(tone, intensity):
            passes["retention"] = retention >= PASS_BANDS["retention_min"]

    # Meaning-inversion gate. Adding or dropping a negation flips what the user
    # said, which is the worst thing a cleanup can do and the cheapest to do by
    # accident: "send the payment" -> "do not send the payment" scores perfectly
    # on every other metric here, including nonslang_preservation (input-token
    # recall, where insertions are free).
    #
    # Standard only — MEASURED across the 198-row matrix: at standard nothing
    # flags, while strong legitimately drops whole clauses (6 rows) and
    # caricature adds parody negations (15 rows). Same applicability rule the
    # preservation family already uses.
    if intensity == "standard":
        passes["negation_preserved"] = negation_delta(inp, out) == 0

    # Absolute filler-removal gate. Caricature is specified to KEEP and ADD
    # fillers, so it is N/A there.
    filler_removed = None
    if intensity != "caricature":
        filler_removed = required_filler_removal(inp, out, sample.get("expected_removals"))
        if filler_removed is not None:
            passes["required_filler_removal"] = filler_removed >= REQUIRED_FILLER_REMOVAL_MIN

    # Tone-specific gates (only added when applicable to the sample/tone).
    if tone == "dev":
        dterms = sample.get("dev_terms", [])
        if dterms:
            passes["dev_term_preservation"] = dev_term_preservation(out, dterms) == 1.0
    if tone == "professional":
        passes["prof_caps"] = sentence_start_caps_ratio(out) >= PASS_BANDS["prof_caps_min"]
        passes["prof_slang_removal"] = len(slang_remaining(inp, out, sample.get("slang"))) == 0
        # Replaces the omitted order_lcs/new_words with a gate that survives
        # substitution. Without it, an output could rewrite every NON-slang word
        # and still pass on length + capitalization alone.
        # Threshold MEASURED, not chosen: over the 18 professional/standard rows
        # the good outputs score 0.810-1.000 while cross-sample rewrites top out
        # at 0.800 — a clean gap with no overlap, and 0.80 is its boundary.
        #
        # This is RECALL of the input's non-slang content, so output INSERTIONS
        # are free and it cannot see a meaning inversion — "send the payment" ->
        # "do not send the payment" scores 1.0 here. negation_preserved covers
        # that specific class; an insertion budget was tried and cannot, because
        # good rows insert up to 4 content words beyond what slang replacement
        # explains while that inversion inserts only 2.
        nonslang = nonslang_preservation(inp, out, sample.get("slang"))
        if nonslang is not None:
            passes["nonslang_preservation"] = nonslang >= PASS_BANDS["nonslang_preservation_min"]

    # None rather than a number for transformative rows: a guide score computed
    # from a partial gate set would look comparable to a standard row and is not.
    guide_score = (
        None if transformative
        else (sum(1 for v in passes.values() if v) / len(passes) if passes else 1.0)
    )

    return {
        # N/A metrics are None AND their key is absent from `passes` — never False,
        # which would silently penalize every transformative row via guide_score.
        "word_retention": round(retention, 3) if retention_applies(tone, intensity) else None,
        "required_filler_removal": filler_removed,
        "order_lcs_ratio": round(order, 3) if not transformative else None,
        "length_ratio": round(lratio, 3) if not transformative else None,
        "new_content_words": new_words,
        "sentence_count_in": sent_in,
        "sentence_count_out": sent_out,
        "sentence_delta": sent_delta,
        # informational (non-gating) signals
        "lowercase_ratio": round(lowercase_ratio(out), 3),
        "terminal_punct_density": round(terminal_punct_density(out), 3),
        "sentence_start_caps_ratio": round(sentence_start_caps_ratio(out), 3),
        "dev_term_preservation": round(dev_term_preservation(out, sample.get("dev_terms", [])), 3),
        "slang_remaining": slang_remaining(inp, out, sample.get("slang")),
        "passes": passes,
        "guide_score": round(guide_score, 3) if guide_score is not None else None,
    }


def summarize(results: list[dict]) -> dict:
    """Roll up per-tone means + gate pass-rates across all records."""
    by_tone: dict[str, list[dict]] = {}
    for r in results:
        by_tone.setdefault(r["tone"], []).append(r)

    def _mean(ms: list[dict], key: str):
        """Mean over rows where the metric APPLIES.

        N/A metrics are None (transformative intensities omit the whole
        guide-preservation family), so a bare sum() would raise. Returns the count
        it averaged over so a mean drawn from 2 rows is not read as one from 18.
        """
        vals = [m[key] for m in ms if m.get(key) is not None]
        return {"mean": round(sum(vals) / len(vals), 3) if vals else None, "n": len(vals)}

    summary = {}
    for tone, recs in by_tone.items():
        n = len(recs)
        ms = [r["metrics"] for r in recs]
        # A gate absent from a row is N/A there, not a failure — divide by the
        # rows where it actually applied.
        gate_keys = sorted({k for m in ms for k in m["passes"]})
        gate_rates = {}
        for k in gate_keys:
            applicable = [m for m in ms if k in m["passes"]]
            gate_rates[k] = {
                "rate": round(sum(1 for m in applicable if m["passes"][k]) / len(applicable), 3),
                "n": len(applicable),
            }
        summary[tone] = {
            "n": n,
            "word_retention": _mean(ms, "word_retention"),
            "required_filler_removal": _mean(ms, "required_filler_removal"),
            "order_lcs": _mean(ms, "order_lcs_ratio"),
            "length_ratio": _mean(ms, "length_ratio"),
            "guide_score": _mean(ms, "guide_score"),
            "mean_latency_s": round(sum(r.get("latency_s", 0) for r in recs) / n, 3),
            "gate_pass_rates": gate_rates,
        }
    return summary
