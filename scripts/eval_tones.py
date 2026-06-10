#!/usr/bin/env python3
"""Tone post-processing evaluation harness (STANDALONE, host-only).

Runs the REAL local llama backend over a corpus x tone matrix, captures
input/output/latency, computes deterministic "guide, not rewrite" metrics, and
writes a side-by-side Markdown report + machine-readable JSON.

Run on the FLATPAK HOST (the GPU + llama-simple only exist there):

  flatpak-spawn --host sh -c 'cd /var/home/bazzite/Dev/wayfinder-aura; \
      PYTHONPATH=src python3 scripts/eval_tones.py'

A/B a different model:

  ... python3 scripts/eval_tones.py \
      --model ~/.local/share/wayfinder-aura/llm-models/LFM2.5-1.2B-Instruct-Q4_K_M.gguf

Subset / faster iteration:

  ... python3 scripts/eval_tones.py --tones dev,professional --samples dev_01_git_flow
"""
import argparse
import copy
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

# --- make src/ and the harness package importable ---
ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
SCRIPTS = Path(__file__).resolve().parent
for p in (str(SRC), str(SCRIPTS)):
    if p not in sys.path:
        sys.path.insert(0, p)

from wayfinder.core.postprocessor import process_with_config          # noqa: E402
from tone_eval.corpus import CORPUS                                   # noqa: E402
from tone_eval import metrics as M                                    # noqa: E402
from tone_eval import report as R                                     # noqa: E402

ALL_TONES = ["minimal", "dev", "casual", "professional"]

# The user's REAL config, reproduced faithfully. Only output_tone and
# llama_cpp_model_path are varied per run.
BASE_CONFIG = {
    "output_tone": "professional",
    "post_processing_enabled": True,
    "post_processing_backend": "llama_cpp",
    "llama_cpp_use_cli": True,
    "llama_cpp_binary": "~/llama.cpp/build/bin/llama-cli",   # ctor swaps to llama-simple
    "llama_cpp_model_path": str(Path.home() / ".local/share/wayfinder-aura"
                                / "llm-models/Qwen3.5-2B-Q4_K_M.gguf"),
    "llama_cpp_n_ctx": 2048,
    "llama_cpp_n_threads": 4,
    "llama_cpp_n_gpu_layers": -1,
    "post_processing_temperature": 0.1,
    "post_processing_max_tokens": 1024,
    "strong_mode": False,
    "caricature_mode": False,
}


def make_config(tone: str, model_path: str) -> dict:
    cfg = copy.deepcopy(BASE_CONFIG)
    cfg["output_tone"] = tone
    cfg["llama_cpp_model_path"] = os.path.expanduser(model_path)
    return cfg


def run_one(text: str, tone: str, model_path: str) -> dict:
    cfg = make_config(tone, model_path)
    t0 = time.perf_counter()
    try:
        out = process_with_config(text, cfg)
        err = None
    except Exception as e:  # the harness must never crash mid-matrix
        out, err = text, f"{type(e).__name__}: {e}"
    return {"output": out, "latency_s": round(time.perf_counter() - t0, 3), "error": err}


def main(argv=None):
    ap = argparse.ArgumentParser(description="Tone post-processing eval harness")
    ap.add_argument("--model", default=BASE_CONFIG["llama_cpp_model_path"],
                    help="GGUF model path (A/B different models)")
    ap.add_argument("--tones", default=",".join(ALL_TONES),
                    help="comma list subset of: " + ",".join(ALL_TONES))
    ap.add_argument("--samples", default="",
                    help="comma list of sample ids (default: all)")
    ap.add_argument("--out-dir", default=str(ROOT / "scripts" / "tone_eval" / "results"))
    ap.add_argument("--judge", action="store_true",
                    help="opt-in LLM-judge pass (advisory only)")
    ap.add_argument("--judge-backend", default="anthropic",
                    choices=["anthropic", "openai", "local"])
    args = ap.parse_args(argv)

    tones = [t.strip() for t in args.tones.split(",") if t.strip()]
    want_ids = {s.strip() for s in args.samples.split(",") if s.strip()}
    samples = [s for s in CORPUS if (not want_ids or s["id"] in want_ids)]

    model_path = os.path.expanduser(args.model)
    if not Path(model_path).exists():
        print(f"[eval] WARNING model not found: {model_path}", file=sys.stderr)

    results = []
    for s in samples:
        for tone in tones:
            print(f"[eval] {s['id']:<24} tone={tone:<12} ...", flush=True)
            r = run_one(s["text"], tone, model_path)
            rec = {
                "sample_id": s["id"],
                "stresses": s["stresses"],
                "tone": tone,
                "input": s["text"],
                "output": r["output"],
                "latency_s": r["latency_s"],
                "error": r["error"],
                "metrics": M.compute_all(s, tone, s["text"], r["output"]),
            }
            if args.judge:
                from tone_eval import judge as J
                rec["judge"] = J.judge(s["text"], r["output"], tone, backend=args.judge_backend)
            results.append(rec)

    Path(args.out_dir).mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = f"tone-eval_{Path(model_path).stem}_{stamp}"
    json_path = Path(args.out_dir) / f"{base}.json"
    md_path = Path(args.out_dir) / f"{base}.md"

    payload = {
        "generated": stamp,
        "model": model_path,
        "tones": tones,
        "config": {k: BASE_CONFIG[k] for k in (
            "post_processing_temperature", "post_processing_max_tokens",
            "llama_cpp_n_ctx", "llama_cpp_n_gpu_layers")},
        "results": results,
        "summary": M.summarize(results),
    }
    json_path.write_text(json.dumps(payload, indent=2))
    md_path.write_text(R.render_markdown(payload))
    print(f"\n[eval] JSON -> {json_path}\n[eval] MD   -> {md_path}")
    R.print_console_summary(payload)


if __name__ == "__main__":
    main()
