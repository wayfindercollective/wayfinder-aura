#!/usr/bin/env python3
"""Standalone ASR accuracy report over the golden-audio corpus.

Transcribes each generated golden clip with the real whisper-cli and prints a
WER + key-phrase table — the human-readable companion to tests/test_golden_asr.py.
Runs the free tier (base.en / CPU) and, when available, the premium tier
(large-v3-turbo / GPU) side by side.

Usage:
    PYTHONPATH=src:scripts python scripts/eval_asr.py [--json OUT]

Regenerate the corpus first if missing:
    PYTHONPATH=src python tests/golden_audio/generate.py
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
for _p in (REPO / "src", REPO / "scripts", REPO / "tests" / "golden_audio"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from tone_eval.metrics import wer, phrase_hits  # noqa: E402
from manifest import CLIPS, clip_path  # noqa: E402


def _find(paths):
    for p in paths:
        if Path(p).exists():
            return str(p)
    return None


def _whisper_cli():
    return _find([Path.home() / "whisper.cpp/build/bin/whisper-cli",
                  "/usr/bin/whisper-cli", "/app/bin/whisper-cli"])


def _model(names):
    dirs = [Path.home() / "whisper.cpp/models",
            Path.home() / ".local/share/wayfinder-aura/whisper-models",
            Path("/app/share/whisper-models")]
    return _find([d / n for d in dirs for n in names])


def _gpu_present():
    try:
        import wayfinder_main
        return wayfinder_main.get_gpu_info().has_gpu
    except Exception:
        return False


def _asr(cli, model, wav, use_gpu):
    from wayfinder.core.transcriber import (
        WhisperCppBackend, clean_whisper_artifacts, normalize_whisper_caps,
    )
    b = WhisperCppBackend(whisper_binary=cli, model_path=model, language="en",
                          use_gpu=use_gpu, beam_size=5, best_of=5)
    return normalize_whisper_caps(clean_whisper_artifacts(b.transcribe(str(wav))))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", default="", help="also write a machine-readable report")
    args = ap.parse_args()

    cli = _whisper_cli()
    base = _model(["ggml-base.en.bin", "ggml-base.bin"])
    turbo = _model(["ggml-large-v3-turbo.bin", "ggml-large-v3-turbo-q5_0.bin"])
    if not (cli and base):
        print("ERROR: need whisper-cli + a base.en model. Build whisper.cpp first.",
              file=sys.stderr)
        return 2
    if not all(clip_path(c["id"]).exists() for c in CLIPS):
        print("ERROR: golden corpus missing. Run tests/golden_audio/generate.py first.",
              file=sys.stderr)
        return 2

    do_premium = bool(turbo and _gpu_present())
    tiers = [("free/base.en/CPU", base, False)]
    if do_premium:
        tiers.append(("premium/turbo/GPU", turbo, True))

    report = {"tiers": {}}
    for label, model, use_gpu in tiers:
        print(f"\n=== {label} ===")
        print(f"{'clip':14s} {'WER':>6s}  {'phrases':>8s}  text")
        wers = []
        rows = []
        for c in CLIPS:
            wav = clip_path(c["id"])
            out = _asr(cli, model, wav, use_gpu)
            if c["category"] == "silence":
                words = [t for t in out.lower().split() if t.isalpha()]
                ok = "OK" if not words else f"HALLUCINATED({len(words)})"
                print(f"{c['id']:14s} {'--':>6s}  {ok:>8s}  {out!r}")
                rows.append({"id": c["id"], "silence_words": len(words)})
                continue
            w = wer(c["reference_text"], out)
            hits = phrase_hits(out, c["key_phrases"])
            flag = "" if w <= c["max_wer"] else "  <-- OVER max_wer"
            print(f"{c['id']:14s} {w:6.3f}  {len(hits)}/{len(c['key_phrases']):<6d}  {out!r}{flag}")
            wers.append(w)
            rows.append({"id": c["id"], "wer": round(w, 3),
                         "phrase_hits": len(hits), "phrase_total": len(c["key_phrases"])})
        mean = sum(wers) / len(wers) if wers else 0.0
        print(f"{'mean WER':14s} {mean:6.3f}")
        report["tiers"][label] = {"mean_wer": round(mean, 3), "clips": rows}

    if not do_premium:
        print("\n(premium tier skipped: no turbo model or no GPU on this machine)")

    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2))
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
