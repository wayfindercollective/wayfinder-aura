#!/usr/bin/env python3
"""Full speech x style evaluation matrix on macOS, through the app's own code.

Stages (each resumable: rows are appended to JSONL and skipped on re-run):

  asr       every whisper model x {cpu, gpu} over the golden clips plus the
            tone corpus spoken by several macOS voices -> WER, silence
            hallucination, latency. Uses transcribe_with_config() and the
            resident whisper-server exactly as the app does.
  tone      every cleanup model x tone x intensity over the tone corpus text
            (the clean reference) -> tone_eval metrics + latency, via
            process_with_config() on the resident llama-server path.
  pipeline  every whisper model's transcripts (one voice) -> every cleanup
            model x tone x intensity: what users actually get end to end.

Models come from a separate eval cache (default
~/Library/Caches/wayfinder-aura-eval/models/{whisper,llm}), binaries from the
app's native build (build/macos-native/bin). The app's own model folders,
config and licence are never read or written.

EVAL-ONLY LICENCE SIMULATION: this process replaces get_feature_gate() with a
stub that grants every Ultra feature (except voice_profiles, so the user's real
voice history is never read) so every tone/model combination can be graded
before a licence is activated on the test machine. It lives only in this
developer script, which the app never imports or ships.

  PYTHONPATH=src python3 scripts/eval_matrix.py --stages asr,tone,pipeline
  PYTHONPATH=src python3 scripts/eval_matrix.py --stages tone --llm gemma --quick
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import re
import statistics
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for p in (str(ROOT / "src"), str(ROOT / "scripts"), str(ROOT / "tests" / "golden_audio")):
    if p not in sys.path:
        sys.path.insert(0, p)


# ---------------------------------------------------------------- licence stub
class _EvalUltraGate:
    """EVAL ONLY (see module docstring). Never used by the app."""

    is_premium = True
    tier = "ultra"

    def has_feature(self, feature_id: str) -> bool:
        return feature_id != "voice_profiles"


def _install_eval_gate() -> None:
    import wayfinder.license as lic

    gate = _EvalUltraGate()
    lic.get_feature_gate = lambda *a, **k: gate  # noqa: E731 - eval-only stub


# ------------------------------------------------------------------- helpers
MODELS_DIR = Path.home() / "Library/Caches/wayfinder-aura-eval/models"
BIN_DIR = ROOT / "build/macos-native/bin"
WHISPER_PORT = 8391  # the running app owns 8178
TONES = ["minimal", "professional", "casual", "dev", "personal"]
INTENSITIES = ["standard", "strong"]
FAILURE_MARKERS = (
    "[Post-processing] ✗", "⚠ No output from llama", "⚠ Model hallucinated",
    "Resident model load failed", "⚠ generation hit the token cap",
)
LEAK_RE = re.compile(r"\[(here'?s|cleaned|corrected|output|note)[^\]]*\]|^(here is|here's) the",
                     re.IGNORECASE | re.MULTILINE)


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", file=sys.stderr, flush=True)


def norm_words(text: str) -> str:
    text = (text or "").lower().replace("’", "'")
    text = re.sub(r"[^a-z0-9' ]+", " ", text)
    return " ".join(text.split())


def done_keys(path: Path) -> set:
    keys = set()
    if path.exists():
        for line in path.read_text().splitlines():
            try:
                keys.add(json.loads(line)["key"])
            except Exception:
                pass
    return keys


def append(path: Path, row: dict) -> None:
    with path.open("a") as fh:
        fh.write(json.dumps(row) + "\n")


def whisper_models(filter_: str | None) -> list[Path]:
    models = sorted((MODELS_DIR / "whisper").glob("ggml-*.bin"),
                    key=lambda p: p.stat().st_size)
    return [m for m in models if not filter_ or any(f in m.name for f in filter_.split(","))]


def llm_models(filter_: str | None) -> list[Path]:
    models = sorted((MODELS_DIR / "llm").glob("*.gguf"), key=lambda p: p.stat().st_size)
    return [m for m in models
            if not filter_ or any(f.lower() in m.name.lower() for f in filter_.split(","))]


def voices_available() -> set:
    out = subprocess.run(["say", "-v", "?"], capture_output=True, text=True).stdout
    # "Samantha (English (US)) en_US    # Hello!" - the name ends at the locale.
    names = set()
    for line in out.splitlines():
        m = re.match(r"^(.*?)\s+[a-z]{2,3}_[A-Z]{2,3}\s+#", line)
        if m:
            names.add(m.group(1).strip())
    return names


# -------------------------------------------------------------- audio corpus
def build_audio_corpus(audio_dir: Path, voices: list[str]) -> list[dict]:
    """Golden clips + the tone corpus spoken by each voice (16 kHz mono WAV)."""
    from manifest import CLIPS, clip_path
    from tone_eval.corpus import CORPUS

    audio_dir.mkdir(parents=True, exist_ok=True)
    clips = []
    for clip in CLIPS:
        clips.append({"id": f"golden/{clip['id']}", "wav": str(clip_path(clip["id"])),
                      "reference": clip.get("reference_text", ""),
                      "category": clip.get("category", "clean"), "sample_id": None,
                      "voice": "golden"})
    have = voices_available()
    for voice in voices:
        if voice not in have:
            log(f"voice {voice!r} not installed - skipped")
            continue
        for sample in CORPUS:
            wav = audio_dir / f"{sample['id']}__{voice.split()[0]}.wav"
            if not wav.exists():
                subprocess.run(["say", "-v", voice, "--file-format=WAVE",
                                "--data-format=LEI16@16000", "-o", str(wav), sample["text"]],
                               check=True, capture_output=True)
            clips.append({"id": f"{sample['id']}/{voice.split()[0]}", "wav": str(wav),
                          "reference": sample["text"], "category": "dictation",
                          "sample_id": sample["id"], "voice": voice.split()[0]})
    return clips


# ------------------------------------------------------------------ ASR stage
def asr_config(model: Path, gpu: bool) -> dict:
    from wayfinder.config import DEFAULT_CONFIG

    cfg = dict(DEFAULT_CONFIG)
    cfg.update({
        "transcription_backend": "whisper_cpp",
        "whisper_binary": str(BIN_DIR / "whisper-cli"),
        "whisper_server_mode": True,
        "whisper_server_port": WHISPER_PORT,
        "model_path": str(model),
        "use_gpu": gpu,
        "output_tone": "minimal",
        "post_processing_enabled": False,
        "custom_vocabulary": [],
    })
    return cfg


def run_asr(clips: list[dict], models: list[Path], modes: list[str], out: Path) -> None:
    from tone_eval import metrics as M
    from wayfinder.core.transcriber import WhisperServerBackend, transcribe_with_config

    seen = done_keys(out)
    for model in models:
        for mode in modes:
            gpu = mode == "gpu"
            todo = [c for c in clips if f"{model.name}|{mode}|{c['id']}" not in seen]
            if not todo:
                continue
            cfg = asr_config(model, gpu)
            log(f"ASR {model.name} [{mode}] - {len(todo)} clips")
            try:  # warm-up (server spawn + model load) is reported, not scored
                t0 = time.perf_counter()
                with contextlib.redirect_stdout(io.StringIO()):
                    transcribe_with_config(clips[0]["wav"], cfg, skip_post_processing=True)
                append(out, {"key": f"{model.name}|{mode}|__warmup__", "stage": "asr",
                             "model": model.name, "mode": mode, "warmup_s": round(time.perf_counter() - t0, 3)})
            except Exception as exc:
                log(f"  warm-up failed: {exc}")
            for clip in todo:
                buf = io.StringIO()
                t0 = time.perf_counter()
                try:
                    with contextlib.redirect_stdout(buf):
                        text = transcribe_with_config(clip["wav"], cfg, skip_post_processing=True)
                    err = None
                except Exception as exc:
                    text, err = "", f"{type(exc).__name__}: {exc}"
                dt = time.perf_counter() - t0
                ref = clip["reference"]
                row = {
                    "key": f"{model.name}|{mode}|{clip['id']}", "stage": "asr",
                    "model": model.name, "mode": mode, "clip": clip["id"],
                    "voice": clip["voice"], "category": clip["category"],
                    "sample_id": clip["sample_id"], "text": text, "seconds": round(dt, 3),
                    "error": err,
                }
                if clip["category"] == "silence":
                    row["silence_words"] = len(norm_words(text).split())
                else:
                    row["wer"] = round(M.wer(norm_words(ref), norm_words(text)), 4)
                append(out, row)
            try:
                WhisperServerBackend.shutdown()
            except Exception:
                pass


# ----------------------------------------------------------------- tone stage
def cleanup_config(model: Path, tone: str, intensity: str, gpu: bool) -> dict:
    from wayfinder.config import DEFAULT_CONFIG

    cfg = dict(DEFAULT_CONFIG)
    cfg.update({
        "post_processing_enabled": True,
        "post_processing_backend": "llama_cpp",
        "llama_cpp_use_cli": True,
        "llama_cpp_binary": str(BIN_DIR / "llama-simple"),
        "llama_cpp_model_path": str(model),
        "llama_cpp_residency": "instant",
        "post_processing_force_subprocess": False,
        "llama_cpp_chat_template": "auto",
        "use_gpu": gpu,
        "output_tone": tone,
        "strong_mode": intensity == "strong",
        "caricature_mode": False,
        "custom_vocabulary": [],
    })
    return cfg


def clean_once(text: str, cfg: dict) -> tuple[str, float, list[str]]:
    from wayfinder.core.postprocessor import process_with_config

    buf = io.StringIO()
    t0 = time.perf_counter()
    with contextlib.redirect_stdout(buf):
        out = process_with_config(text, cfg)
    dt = time.perf_counter() - t0
    log_text = buf.getvalue()
    return out, dt, [m for m in FAILURE_MARKERS if m in log_text]


def score(sample: dict, tone: str, intensity: str, inp: str, out: str) -> dict:
    from tone_eval import metrics as M

    m = M.compute_all(sample, tone, inp, out, intensity)
    failed = sorted(k for k, v in (m.get("passes") or {}).items() if v is False)
    return {"guide_score": m.get("guide_score"), "failed_gates": failed,
            "all_pass": not failed, "leak": bool(LEAK_RE.search(out or "")),
            "unchanged": norm_words(out) == norm_words(inp)}


def run_tone(models: list[Path], samples: list[dict], out: Path, gpu: bool) -> None:
    from wayfinder.core.llama_server import LlamaServerManager

    seen = done_keys(out)
    for model in models:
        for tone in TONES:
            for intensity in INTENSITIES:
                todo = [s for s in samples
                        if f"{model.name}|{tone}|{intensity}|{s['id']}" not in seen]
                if not todo:
                    continue
                log(f"TONE {model.name} {tone}/{intensity} - {len(todo)} samples")
                cfg = cleanup_config(model, tone, intensity, gpu)
                for sample in todo:
                    try:
                        text_out, dt, markers = clean_once(sample["text"], cfg)
                        err = None
                    except Exception as exc:
                        text_out, dt, markers, err = "", 0.0, [], f"{type(exc).__name__}: {exc}"
                    row = {"key": f"{model.name}|{tone}|{intensity}|{sample['id']}",
                           "stage": "tone", "llm": model.name, "tone": tone,
                           "intensity": intensity, "sample": sample["id"],
                           "input": sample["text"], "output": text_out,
                           "seconds": round(dt, 3), "markers": markers, "error": err}
                    row.update(score(sample, tone, intensity, sample["text"], text_out))
                    append(out, row)
        try:
            LlamaServerManager.shutdown()
        except Exception:
            pass


def run_pipeline(asr_rows: Path, models: list[Path], samples: dict, voice: str,
                 mode: str, out: Path, gpu: bool) -> None:
    from wayfinder.core.llama_server import LlamaServerManager

    transcripts = {}
    for line in asr_rows.read_text().splitlines():
        r = json.loads(line)
        if (r.get("stage") == "asr" and r.get("mode") == mode and r.get("voice") == voice
                and r.get("sample_id") in samples and r.get("text")):
            transcripts[(r["model"], r["sample_id"])] = r["text"]
    seen = done_keys(out)
    for model in models:
        for tone in TONES:
            for intensity in INTENSITIES:
                cfg = cleanup_config(model, tone, intensity, gpu)
                todo = [(wm, sid, t) for (wm, sid), t in sorted(transcripts.items())
                        if f"{wm}|{model.name}|{tone}|{intensity}|{sid}" not in seen]
                if not todo:
                    continue
                log(f"PIPE {model.name} {tone}/{intensity} - {len(todo)} transcripts")
                for wm, sid, asr_text in todo:
                    sample = samples[sid]
                    try:
                        text_out, dt, markers = clean_once(asr_text, cfg)
                        err = None
                    except Exception as exc:
                        text_out, dt, markers, err = "", 0.0, [], f"{type(exc).__name__}: {exc}"
                    from tone_eval import metrics as M
                    row = {"key": f"{wm}|{model.name}|{tone}|{intensity}|{sid}",
                           "stage": "pipeline", "whisper": wm, "llm": model.name,
                           "tone": tone, "intensity": intensity, "sample": sid,
                           "asr": asr_text, "output": text_out, "seconds": round(dt, 3),
                           "markers": markers, "error": err,
                           # End to end: how much of what was SAID survives.
                           "said_retention": round(M.word_retention(sample["text"], text_out), 3)}
                    row.update(score(sample, tone, intensity, asr_text, text_out))
                    append(out, row)
        try:
            LlamaServerManager.shutdown()
        except Exception:
            pass


# -------------------------------------------------------------------- report
def _mean(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.mean(xs), 3) if xs else None


def _p90(xs):
    xs = sorted(x for x in xs if x is not None)
    return round(xs[min(len(xs) - 1, int(0.9 * len(xs)))], 3) if xs else None


def report(out_dir: Path) -> Path:
    lines = [f"# Wayfinder Aura - speech x style matrix ({datetime.now():%Y-%m-%d %H:%M})", ""]
    asr = out_dir / "asr.jsonl"
    if asr.exists():
        rows = [json.loads(l) for l in asr.read_text().splitlines()]
        lines += ["## Speech models", "",
                  "| Model | Mode | WER mean | WER p90 | Silence words | Latency mean s | p90 s | Warm-up s |",
                  "|---|---|---|---|---|---|---|---|"]
        combos = sorted({(r["model"], r["mode"]) for r in rows},
                        key=lambda c: (Path(MODELS_DIR / "whisper" / c[0]).stat().st_size
                                       if (MODELS_DIR / "whisper" / c[0]).exists() else 0, c[1]))
        for model, mode in combos:
            rs = [r for r in rows if r["model"] == model and r["mode"] == mode]
            scored = [r for r in rs if "wer" in r]
            sil = [r.get("silence_words") for r in rs if "silence_words" in r]
            warm = [r.get("warmup_s") for r in rs if "warmup_s" in r]
            lines.append(f"| {model} | {mode} | {_mean([r['wer'] for r in scored])} | "
                         f"{_p90([r['wer'] for r in scored])} | {sum(x or 0 for x in sil)} | "
                         f"{_mean([r['seconds'] for r in scored])} | {_p90([r['seconds'] for r in scored])} | "
                         f"{_mean(warm)} |")
        lines.append("")
    for stage, title, group in (("tone", "Styles on clean text", ("llm", "tone", "intensity")),
                                ("pipeline", "Styles end to end (speech model output)",
                                 ("llm", "tone", "intensity"))):
        path = out_dir / f"{stage}.jsonl"
        if not path.exists():
            continue
        rows = [json.loads(l) for l in path.read_text().splitlines()]
        lines += [f"## {title}", "",
                  "| Cleanup model | Tone | Strength | Pass rate | Guide score | Leaks | Unchanged | Errors | Latency mean s | p90 s | Top failing gates |",
                  "|---|---|---|---|---|---|---|---|---|---|---|"]
        for key in sorted({tuple(r[g] for g in group) for r in rows}):
            rs = [r for r in rows if tuple(r[g] for g in group) == key]
            gates = {}
            for r in rs:
                for g in r.get("failed_gates") or []:
                    gates[g] = gates.get(g, 0) + 1
            top = ", ".join(f"{g} x{n}" for g, n in sorted(gates.items(), key=lambda kv: -kv[1])[:3])
            n = len(rs)
            lines.append(
                f"| {key[0]} | {key[1]} | {key[2]} | {sum(r['all_pass'] for r in rs)}/{n} | "
                f"{_mean([r.get('guide_score') for r in rs])} | {sum(r['leak'] for r in rs)} | "
                f"{sum(r['unchanged'] for r in rs)} | {sum(bool(r['error'] or r['markers']) for r in rs)} | "
                f"{_mean([r['seconds'] for r in rs])} | {_p90([r['seconds'] for r in rs])} | {top} |")
        if stage == "pipeline":
            lines += ["", "| Speech model | Cleanup model | Pass rate | Said-word retention |",
                      "|---|---|---|---|"]
            for wm, llm in sorted({(r["whisper"], r["llm"]) for r in rows}):
                rs = [r for r in rows if r["whisper"] == wm and r["llm"] == llm]
                lines.append(f"| {wm} | {llm} | {sum(r['all_pass'] for r in rs)}/{len(rs)} | "
                             f"{_mean([r['said_retention'] for r in rs])} |")
        lines.append("")
    path = out_dir / "REPORT.md"
    path.write_text("\n".join(lines) + "\n")
    return path


# ---------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stages", default="asr,tone,pipeline")
    ap.add_argument("--out", default=str(ROOT / "scripts/tone_eval/results/matrix"))
    ap.add_argument("--whisper", help="comma filters on model file names")
    ap.add_argument("--llm", help="comma filters on model file names")
    ap.add_argument("--modes", default="cpu,gpu")
    ap.add_argument("--voices", default="Samantha (English (US)),Daniel (English (UK)),Rishi (English (India))")
    ap.add_argument("--pipeline-voice", default="Samantha")
    ap.add_argument("--pipeline-mode", default="gpu")
    ap.add_argument("--cleanup-gpu", action="store_true", default=True)
    ap.add_argument("--cleanup-cpu", action="store_true", help="cleanup on CPU (Free path)")
    ap.add_argument("--quick", action="store_true", help="6 samples, one voice")
    ap.add_argument("--report-only", action="store_true")
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    if args.report_only:
        print(report(out_dir))
        return 0

    _install_eval_gate()
    os.environ.setdefault("WAYFINDER_DISABLE_KEYCHAIN", "1")
    from tone_eval.corpus import CORPUS

    samples = CORPUS[:6] if args.quick else CORPUS
    stages = set(args.stages.split(","))
    gpu_cleanup = not args.cleanup_cpu
    if "asr" in stages:
        voices = [v.strip() for v in args.voices.split(",")][: 1 if args.quick else None]
        clips = build_audio_corpus(out_dir / "audio", voices)
        if args.quick:
            clips = [c for c in clips if c["sample_id"] is None or c["sample_id"] in {s["id"] for s in samples}]
        run_asr(clips, whisper_models(args.whisper), args.modes.split(","), out_dir / "asr.jsonl")
        print(report(out_dir))
    if "tone" in stages:
        run_tone(llm_models(args.llm), samples, out_dir / "tone.jsonl", gpu_cleanup)
        print(report(out_dir))
    if "pipeline" in stages and (out_dir / "asr.jsonl").exists():
        run_pipeline(out_dir / "asr.jsonl", llm_models(args.llm), {s["id"]: s for s in samples},
                     args.pipeline_voice, args.pipeline_mode, out_dir / "pipeline.jsonl", gpu_cleanup)
    print(report(out_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
