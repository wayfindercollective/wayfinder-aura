"""Tone post-processing evaluation harness (standalone, host-run).

Modules:
- corpus:  realistic dictation samples (>bypass length) for each tone.
- metrics: stdlib-only deterministic "guide, not rewrite" metrics.
- report:  Markdown + console summary writers.
- judge:   optional, opt-in LLM-judge (advisory only).

The runner lives at scripts/eval_tones.py.
"""
