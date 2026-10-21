#!/usr/bin/env python3
"""Which speech engine is fastest on this Mac, at what accuracy?

Benchmarks the built app's own whisper-server binaries on one GGML model:
CPU (-ng), Metal, and the Core ML encoder twin (Apple Neural Engine encoder +
Metal decoder), each at greedy and beam-3 decoding. The servers are spawned
with the app's own command line (WhisperServerBackend._server_cmd_attempts) and
receive the app's own request fields, so the numbers are what dictation sees.

All engines stay resident and every clip is sent to each of them in a shuffled
order, round after round, so a fanless Mac heating up penalises no engine more
than another. The Core ML encoder is converted with the app's own pipeline, and
its first load (macOS compiles it for the Neural Engine) runs with no deadline
so its real duration is measured, then once more warm.

    HOME=~/aura-qa/home python scripts/bench_mac_engines.py \\
        --app "dist/Wayfinder Aura.app" --model ~/models/ggml-large-v3-turbo-q5_0.bin \\
        --out ~/aura-qa/bench

Run with HOME pointing at a scratch directory: nothing is written to the
user's real Aura settings or caches. Requires macOS on Apple Silicon.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import random
import re
import shutil
import socket
import statistics
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
for p in (str(ROOT / "src"), str(ROOT / "scripts"), str(ROOT / "tests" / "golden_audio")):
    if p not in sys.path:
        sys.path.insert(0, p)

import eval_matrix as EM  # noqa: E402  corpus, WER and the eval-only licence stub

LONG_TEXTS = 4  # tone-corpus texts joined into each multi-window clip


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wav_seconds(path: str) -> float:
    import wave

    with wave.open(path) as w:
        return w.getnframes() / float(w.getframerate())


def thermal() -> str:
    out = subprocess.run(["pmset", "-g", "therm"], capture_output=True, text=True).stdout
    found = re.findall(r"(CPU_Speed_Limit|CPU_Scheduler_Limit|Thermal_Level)\s*=\s*(\d+)", out)
    return ", ".join(f"{k}={v}" for k, v in found) or "no thermal warning recorded"


def rss_mb(pid: int) -> float:
    out = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True).stdout
    return round(int(out.strip() or 0) / 1024, 1)


def cpu_seconds(pid: int) -> float:
    out = subprocess.run(["ps", "-o", "time=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    total = 0.0
    for part in out.split(":"):
        total = total * 60 + float(part or 0)
    return total


# ------------------------------------------------------------------- corpus
def long_clips(audio_dir: Path, voice: str) -> list[dict]:
    """Two multi-window clips (Whisper encodes 30 s at a time)."""
    from tone_eval.corpus import CORPUS

    clips = []
    for n, start in enumerate((0, LONG_TEXTS)):
        text = " ".join(s["text"] for s in CORPUS[start:start + LONG_TEXTS])
        wav = audio_dir / f"long{n}__{voice.split()[0]}.wav"
        if not wav.exists():
            subprocess.run(["say", "-v", voice, "--file-format=WAVE", "--data-format=LEI16@16000",
                            "-o", str(wav), text], check=True, capture_output=True)
        clips.append({"id": f"long{n}/{voice.split()[0]}", "wav": str(wav), "reference": text,
                      "category": "long", "voice": voice.split()[0]})
    return clips


# ------------------------------------------------------------------ engines
class Engine:
    """One resident whisper-server, spawned exactly as the app spawns it."""

    def __init__(self, name: str, backend, binary: str, model: str, gpu: bool, coreml: bool):
        self.name, self.backend, self.gpu, self.coreml = name, backend, gpu, coreml
        self.binary, self.model = binary, model
        self.proc = None
        self.port = 0
        self.tail: deque = deque(maxlen=400)
        self.load_seconds: list[float] = []

    def command(self) -> tuple[list, dict]:
        from wayfinder.utils.hostexec import bundle_binary_env
        from wayfinder.utils.macos_ggml_env import whisper_metal_env

        backend = self.backend
        backend.whisper_server_binary = self.binary
        backend.model_path = self.model
        backend.use_gpu = self.gpu
        cmd = backend._server_cmd_attempts(self.port)[0] + ["--host", "127.0.0.1"]
        return cmd, bundle_binary_env(whisper_metal_env(gpu=self.gpu))

    def start(self, deadline_s: float) -> float:
        # The Core ML engine starts twice (cold, during the contention test,
        # then warm): replacing the handle without stopping it orphaned the
        # cold server for the rest of the run, and after it.
        self.stop()
        self.port = free_port()
        cmd, env = self.command()
        self.tail.clear()
        began = time.perf_counter()
        self.proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     text=True, errors="replace")
        threading.Thread(target=self._drain, daemon=True).start()
        while time.perf_counter() - began < deadline_s:
            if self.proc.poll() is not None:
                raise RuntimeError(f"{self.name} exited {self.proc.returncode}: {list(self.tail)[-5:]}")
            try:
                with urlopen(f"http://127.0.0.1:{self.port}/health", timeout=1) as r:
                    ready = r.status == 200
            except OSError:
                ready = False
            if ready and (not self.coreml or any("Core ML model loaded" in l for l in list(self.tail))):
                took = time.perf_counter() - began
                self.load_seconds.append(round(took, 2))
                return took
            time.sleep(0.25)
        self.stop()
        raise TimeoutError(f"{self.name} not ready after {deadline_s:.0f} s")

    def _drain(self) -> None:
        for line in self.proc.stdout:
            self.tail.append(line.rstrip())

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)

    def infer(self, wav: str, beam: int, timeout: float) -> tuple[str, float]:
        best_of = 1 if beam == 1 else min(beam, 5)  # transcriber.whisper_decoding
        boundary = "AuraEngineBench"
        body = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="clip.wav"\r\n'
                f'Content-Type: audio/wav\r\n\r\n').encode() + Path(wav).read_bytes() + b"\r\n"
        for name, value in (("response_format", "json"), ("no_timestamps", "true"),
                            ("beam_size", beam), ("best_of", best_of), ("temperature_inc", "0.0"),
                            ("prompt", self.backend._build_prompt())):
            body += f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
        body += f"--{boundary}--\r\n".encode()
        request = Request(f"http://127.0.0.1:{self.port}/inference", data=body,
                          headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        began = time.perf_counter()
        with urlopen(request, timeout=timeout) as r:
            text = json.loads(r.read()).get("text", "")
        return text, time.perf_counter() - began


def make_backend(config: dict):
    from wayfinder.core.transcriber import get_backend

    backend = get_backend(config)
    if type(backend).__name__ != "WhisperServerBackend":
        raise SystemExit(f"expected the resident whisper-server backend, got {type(backend).__name__}")
    return backend


def prepare_coreml(app_bin: Path, model: Path, work: Path) -> tuple[Path, dict]:
    """The app's conversion (macos_coreml.prepare_encoder), timed, without its deadline."""
    from wayfinder.utils.macos_coreml import encoder_path
    from wayfinder.utils.macos_coreml_convert import populate_package
    from wayfinder.utils.hostexec import bundle_binary_env

    target = work / "coreml-model" / model.name
    asset = encoder_path(str(target))
    timings: dict = {}
    if asset.is_dir():
        timings["reused"] = True
        return target, timings
    stage = work / "coreml-stage"
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True)
    began = time.perf_counter()
    populate_package(model, stage / "encoder.mlpackage")
    timings["populate_s"] = round(time.perf_counter() - began, 1)
    began = time.perf_counter()
    subprocess.run([str(app_bin / "coreml-compile"), str(stage / "encoder.mlpackage"), str(stage / asset.name)],
                   env=bundle_binary_env(), check=True, capture_output=True)
    timings["compile_s"] = round(time.perf_counter() - began, 1)
    target.parent.mkdir(parents=True, exist_ok=True)
    (stage / asset.name).rename(asset)
    if not target.exists():
        target.symlink_to(model)
    shutil.rmtree(stage, ignore_errors=True)
    return target, timings


# --------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--app", required=True, help="the built Wayfinder Aura.app")
    ap.add_argument("--model", required=True, help="GGML model (Turbo for the Core ML twin)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--engines", default="cpu,metal,coreml")
    ap.add_argument("--beams", default="1,3")
    ap.add_argument("--repeats", type=int, default=3, help="rounds for the GPU engines")
    ap.add_argument("--cpu-repeats", type=int, default=1)
    ap.add_argument("--voices", default="Samantha (English (US)),Daniel (English (UK)),Rishi (English (India))")
    ap.add_argument("--quick", action="store_true", help="golden clips + 6 samples, one voice")
    ap.add_argument("--soak-minutes", type=float, default=10.0, help="sustained run on the fastest engine")
    ap.add_argument("--coreml-load-deadline", type=float, default=2400.0)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    if sys.platform != "darwin":
        raise SystemExit("macOS only")
    if str(Path.home()) == os.path.expanduser("~" + os.environ.get("USER", "")) and not os.environ.get("AURA_BENCH_REAL_HOME"):
        raise SystemExit("run with HOME set to a scratch directory (or AURA_BENCH_REAL_HOME=1)")
    EM._install_eval_gate()
    from wayfinder.config import DEFAULT_CONFIG

    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    app_bin = Path(args.app).expanduser() / "Contents/Frameworks/bin"
    model = Path(args.model).expanduser().resolve()
    rng = random.Random(args.seed)
    beams = [int(b) for b in args.beams.split(",")]
    meta: dict = {"started": time.strftime("%Y-%m-%d %H:%M:%S"), "model": model.name,
                  "machine": subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"],
                                            capture_output=True, text=True).stdout.strip(),
                  "memory_gb": int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True,
                                                  text=True).stdout) // 2**30,
                  "macos": platform.mac_ver()[0], "thermal_start": thermal(), "args": vars(args)}

    voices = args.voices.split(",")[:1] if args.quick else args.voices.split(",")
    clips = EM.build_audio_corpus(out / "audio", voices)
    if args.quick:
        clips = [c for c in clips if c["voice"] == "golden"] + [c for c in clips if c["voice"] != "golden"][:6]
    clips += long_clips(out / "audio", voices[0])
    for clip in clips:
        clip["seconds"] = round(wav_seconds(clip["wav"]), 2)
    log(f"{len(clips)} clips, {sum(c['seconds'] for c in clips) / 60:.1f} min of audio")

    config = dict(DEFAULT_CONFIG)
    config.update({"transcription_backend": "whisper_cpp", "whisper_binary": str(app_bin / "whisper-cli"),
                   "whisper_server_mode": True, "model_path": str(model), "use_gpu": True,
                   "whisper_coreml_encoder": False, "custom_vocabulary": [], "post_processing_enabled": False})
    engines: list[Engine] = []
    wanted = args.engines.split(",")
    if "cpu" in wanted:
        engines.append(Engine("cpu", make_backend({**config, "use_gpu": False}),
                              str(app_bin / "whisper-server"), str(model), gpu=False, coreml=False))
    if "metal" in wanted:
        engines.append(Engine("metal", make_backend(config), str(app_bin / "whisper-server"),
                              str(model), gpu=True, coreml=False))
    if "coreml" in wanted:
        log("Converting the Core ML encoder with the app's pipeline")
        coreml_model, meta["coreml_conversion"] = prepare_coreml(app_bin, model, out)
        log(f"  conversion: {meta['coreml_conversion']}")
        engines.append(Engine("coreml", make_backend(config), str(app_bin / "whisper-coreml/whisper-server"),
                              str(coreml_model), gpu=True, coreml=True))
    meta["threads"] = engines[0].backend.threads

    try:
        metal = next((e for e in engines if e.name == "metal"), None)
        coreml_engine = next((e for e in engines if e.coreml), None)
        if metal and coreml_engine and not meta.get("coreml_conversion", {}).get("reused"):
            # What a user meets on the first launches: the app's background
            # comparison compiles the encoder while they dictate on Metal.
            metal.start(600)
            metal.infer(clips[0]["wav"], 1, 600)
            meta["contention"] = contention(metal, coreml_engine, clips, args.coreml_load_deadline)
            metal.stop()
        for engine in engines:
            if engine.coreml and not engine.load_seconds:
                log("Core ML first load: macOS compiles the encoder for the Neural Engine (no deadline)")
                cold = engine.start(args.coreml_load_deadline)
                log(f"  cold load {cold:.1f} s; restarting for the warm load")
                engine.stop()
            took = engine.start(600)
            log(f"{engine.name}: ready in {took:.1f} s, {rss_mb(engine.proc.pid)} MB")

        meta["load_seconds"] = {e.name: e.load_seconds for e in engines}
        meta["rss_after_load_mb"] = {e.name: rss_mb(e.proc.pid) for e in engines}
        for engine in engines:  # first request warms each server's graph
            engine.infer(clips[0]["wav"], 1, 600)
        cpu_before = {e.name: cpu_seconds(e.proc.pid) for e in engines}
        rows_path = out / "rows.jsonl"
        rows_path.write_text("")
        requests = {e.name: 0 for e in engines}
        rounds = max(args.repeats, args.cpu_repeats)
        for rnd in range(rounds):
            order = clips[:]
            rng.shuffle(order)
            for i, clip in enumerate(order):
                jobs = [(e, b) for e in engines for b in beams
                        if not (e.name == "cpu" and (b != 1 or rnd >= args.cpu_repeats))
                        and not (e.name != "cpu" and rnd >= args.repeats)]
                rng.shuffle(jobs)
                for engine, beam in jobs:
                    text, seconds = engine.infer(clip["wav"], beam, 900)
                    requests[engine.name] += 1
                    row = {"round": rnd, "engine": engine.name, "beam": beam, "clip": clip["id"],
                           "category": clip["category"], "audio_s": clip["seconds"],
                           "seconds": round(seconds, 4), "text": text,
                           "wer": (round(EM.wer_no_fillers(clip["reference"], text), 4)
                                   if clip["reference"] else None)}
                    with rows_path.open("a") as f:
                        f.write(json.dumps(row) + "\n")
                if i % 10 == 0:
                    log(f"round {rnd + 1}/{rounds}: clip {i + 1}/{len(order)}")
        meta["cpu_seconds_per_request"] = {
            e.name: round((cpu_seconds(e.proc.pid) - cpu_before[e.name]) / max(requests[e.name], 1), 3)
            for e in engines}
        meta["rss_after_run_mb"] = {e.name: rss_mb(e.proc.pid) for e in engines}
        meta["thermal_after_matrix"] = thermal()

        rows = [json.loads(l) for l in rows_path.read_text().splitlines()]
        summary = summarise(rows)
        meta["summary"] = summary
        fastest = min((k for k in summary if k.endswith("/b1") and not k.startswith("cpu")),
                      key=lambda k: summary[k]["median_s"], default=None)
        if fastest and args.soak_minutes > 0:
            meta["soak"] = soak(next(e for e in engines if e.name == fastest.split("/")[0]),
                                [c for c in clips if c["category"] == "dictation"] or clips,
                                args.soak_minutes)
    finally:
        for engine in engines:
            engine.stop()
    meta["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
    (out / "summary.json").write_text(json.dumps(meta, indent=2))
    (out / "report.md").write_text(report(meta))
    print(report(meta))
    return 0


def summarise(rows: list[dict]) -> dict:
    by: dict = {}
    for row in rows:
        by.setdefault(f"{row['engine']}/b{row['beam']}", []).append(row)
    texts: dict = {}
    for row in rows:
        texts.setdefault((row["engine"], row["beam"], row["clip"]), set()).add(row["text"].strip())
    out = {}
    for key, rs in sorted(by.items()):
        engine, beam = key.split("/")
        per_clip: dict = {}
        for r in rs:
            per_clip.setdefault(r["clip"], []).append(r["seconds"])
        medians = [statistics.median(v) for v in per_clip.values()]
        wers = [r["wer"] for r in rs if r["wer"] is not None]
        rtf = [r["seconds"] / r["audio_s"] for r in rs if r["audio_s"]]
        silent = [r for r in rs if r["category"] == "silent"]
        varying = sum(1 for (e, b, _c), t in texts.items() if e == engine and f"b{b}" == beam and len(t) > 1)
        out[key] = {
            "requests": len(rs), "clips": len(per_clip),
            "median_s": round(statistics.median(medians), 3),
            "mean_s": round(statistics.mean(medians), 3),
            "p90_s": EM._p90(medians),
            "median_rtf": round(statistics.median(rtf), 3) if rtf else None,
            "short_median_s": round(statistics.median(
                [statistics.median(v) for c, v in per_clip.items() if not c.startswith("long")]), 3),
            "long_median_s": (round(statistics.median(
                [statistics.median(v) for c, v in per_clip.items() if c.startswith("long")]), 3)
                if any(c.startswith("long") for c in per_clip) else None),
            "mean_wer": round(statistics.mean(wers), 4) if wers else None,
            "exact_clips": sum(1 for w in wers if w == 0.0), "scored": len(wers),
            "silent_hallucinations": sum(1 for r in silent if r["text"].strip()),
            "clips_with_varying_text": varying,
        }
    return out


def contention(metal: Engine, coreml: Engine, clips: list[dict], deadline: float) -> dict:
    """Metal dictation latency alone, then while the Core ML encoder first loads."""
    sample = [c for c in clips if c["category"] in ("clean", "dictation")][:8]
    quiet = [metal.infer(c["wav"], 1, 600)[1] for c in sample]
    log("Contention: Metal dictation while the Core ML encoder compiles for the Neural Engine")
    loaded: dict = {}

    def load():
        try:
            loaded["seconds"] = coreml.start(deadline)
        except Exception as exc:
            loaded["error"] = repr(exc)

    loader = threading.Thread(target=load, daemon=True)
    loader.start()
    busy, i = [], 0
    while loader.is_alive():
        clip = sample[i % len(sample)]
        _text, seconds = metal.infer(clip["wav"], 1, 600)
        busy.append({"at_s": round(sum(b["seconds"] for b in busy) + seconds, 1),
                     "clip": clip["id"], "seconds": round(seconds, 3),
                     "quiet_seconds": round(quiet[i % len(sample)], 3)})
        i += 1
    loader.join()
    ratios = [b["seconds"] / b["quiet_seconds"] for b in busy if b["quiet_seconds"]]
    return {"quiet_median_s": round(statistics.median(quiet), 3),
            "during_compile_median_s": round(statistics.median(b["seconds"] for b in busy), 3) if busy else None,
            "during_compile_max_s": max((b["seconds"] for b in busy), default=None),
            "median_slowdown_x": round(statistics.median(ratios), 2) if ratios else None,
            "requests_during_compile": len(busy), "coreml_cold_load": loaded}


def soak(engine: Engine, clips: list[dict], minutes: float) -> dict:
    """Back-to-back dictation-sized requests: does a fanless Mac slow down?"""
    log(f"Soak: {minutes:.0f} min on {engine.name}")
    began = time.perf_counter()
    samples = []
    i = 0
    while time.perf_counter() - began < minutes * 60:
        clip = clips[i % len(clips)]
        _text, seconds = engine.infer(clip["wav"], 1, 600)
        samples.append((time.perf_counter() - began, seconds / clip["seconds"]))
        i += 1
    window = max(1, len(samples) // 10)
    first = statistics.median(s for _t, s in samples[:window])
    last = statistics.median(s for _t, s in samples[-window:])
    return {"engine": engine.name, "requests": len(samples), "first_tenth_rtf": round(first, 3),
            "last_tenth_rtf": round(last, 3), "slowdown_pct": round((last / first - 1) * 100, 1),
            "thermal_end": thermal()}


def report(meta: dict) -> str:
    s = meta.get("summary", {})
    lines = [f"# Speech engine benchmark: {meta['machine']}, {meta['memory_gb']} GB, macOS {meta['macos']}",
             "", f"Model {meta['model']}, {meta.get('threads')} threads, {meta['started']} to {meta.get('finished')}.",
             "", "| Engine | Requests | Median s | Short clips s | Long clips s | p90 s | Median RTF | Mean WER | "
             "Exact | Varying text | Silent halluc. |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    for key, v in s.items():
        lines.append(f"| {key} | {v['requests']} | {v['median_s']} | {v['short_median_s']} | {v['long_median_s']} | "
                     f"{v['p90_s']} | {v['median_rtf']} | {v['mean_wer']} | {v['exact_clips']}/{v['scored']} | "
                     f"{v['clips_with_varying_text']} | {v['silent_hallucinations']} |")
    lines += ["", f"Load seconds (Core ML: cold, then warm): {meta.get('load_seconds')}",
              f"Core ML conversion: {meta.get('coreml_conversion')}",
              f"Server memory after load MB: {meta.get('rss_after_load_mb')}; after the run: {meta.get('rss_after_run_mb')}",
              f"CPU seconds per request: {meta.get('cpu_seconds_per_request')}",
              f"Thermal: start {meta['thermal_start']}; after matrix {meta.get('thermal_after_matrix')}",
              f"Metal dictation while the Core ML encoder first compiles: {meta.get('contention')}",
              f"Soak: {meta.get('soak')}"]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    sys.exit(main())
