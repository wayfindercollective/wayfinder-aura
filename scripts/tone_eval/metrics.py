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


def dev_term_preservation(out: str, dev_terms: list[str]) -> float:
    if not dev_terms:
        return 1.0
    return sum(1 for t in dev_terms if _has_word(out, t)) / len(dev_terms)


def slang_remaining(inp: str, out: str, slang: list[str] | None) -> list[str]:
    """Slang tokens present in input that still appear in output."""
    candidates = set(s.lower() for s in (slang or []))
    candidates |= {t for t in _tokens(inp) if t in SLANG}
    return sorted(t for t in candidates if _has_word(inp, t) and _has_word(out, t))


# ----------------------------------------------------------------------------
# Aggregation
# ----------------------------------------------------------------------------
def compute_all(sample: dict, tone: str, inp: str, out: str) -> dict:
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
    passes: dict[str, bool] = {
        "retention": retention >= PASS_BANDS["retention_min"],
        "order_lcs": order >= PASS_BANDS["order_lcs_min"],
        "len_ratio": lo <= lratio <= hi,
        "new_words": len(new_words) <= PASS_BANDS["new_words_max"],
        "sentence_delta": sent_delta <= sent_delta_max,
    }

    # Tone-specific gates (only added when applicable to the sample/tone).
    if tone == "dev":
        dterms = sample.get("dev_terms", [])
        if dterms:
            passes["dev_term_preservation"] = dev_term_preservation(out, dterms) == 1.0
    if tone == "professional":
        passes["prof_caps"] = sentence_start_caps_ratio(out) >= PASS_BANDS["prof_caps_min"]
        passes["prof_slang_removal"] = len(slang_remaining(inp, out, sample.get("slang"))) == 0

    guide_score = sum(1 for v in passes.values() if v) / len(passes) if passes else 1.0

    return {
        "word_retention": round(retention, 3),
        "order_lcs_ratio": round(order, 3),
        "length_ratio": round(lratio, 3),
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
        "guide_score": round(guide_score, 3),
    }


def summarize(results: list[dict]) -> dict:
    """Roll up per-tone means + gate pass-rates across all records."""
    by_tone: dict[str, list[dict]] = {}
    for r in results:
        by_tone.setdefault(r["tone"], []).append(r)

    summary = {}
    for tone, recs in by_tone.items():
        n = len(recs)
        ms = [r["metrics"] for r in recs]
        gate_keys = sorted({k for m in ms for k in m["passes"]})
        gate_rates = {
            k: round(
                sum(1 for m in ms if m["passes"].get(k)) / n, 3
            ) for k in gate_keys
        }
        summary[tone] = {
            "n": n,
            "mean_word_retention": round(sum(m["word_retention"] for m in ms) / n, 3),
            "mean_order_lcs": round(sum(m["order_lcs_ratio"] for m in ms) / n, 3),
            "mean_length_ratio": round(sum(m["length_ratio"] for m in ms) / n, 3),
            "mean_guide_score": round(sum(m["guide_score"] for m in ms) / n, 3),
            "mean_latency_s": round(sum(r.get("latency_s", 0) for r in recs) / n, 3),
            "gate_pass_rates": gate_rates,
        }
    return summary
