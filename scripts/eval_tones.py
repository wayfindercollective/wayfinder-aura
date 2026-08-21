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
import contextlib
import re
import copy
import hashlib
import io
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

from wayfinder.core.postprocessor import (                            # noqa: E402
    chat_template_key, get_backend, process_with_config, strip_echoed_input,
)
from tone_eval.corpus import CORPUS, CORPUS_VERSION                   # noqa: E402
from tone_eval import metrics as M                                    # noqa: E402
from tone_eval import report as R                                     # noqa: E402

# CLAUDE.md rule 5 names FIVE tones; "personal" was missing, so it was never
# exercised by this harness.
ALL_TONES = ["minimal", "dev", "casual", "professional", "personal"]

# The matrix is ENUMERATED, not a cross-product: a tone x intensity product would
# produce caricature for every tone, which is not what rule 5 asks for. This is
# rule 5's five tones at both intensities, plus the reported caricature case.
DEFAULT_CELLS = (
    [(t, "standard") for t in ALL_TONES]
    + [(t, "strong") for t in ALL_TONES]
    + [("casual", "caricature")]
)

# Failure markers process_with_config() prints instead of raising. It catches
# PostProcessingError AND bare Exception and returns the INPUT, so a failed
# cleanup otherwise scores as a perfect pass.
# Printed by the server path when stop_type == "limit". This is the EXACT
# termination signal the CLI path can only infer by generating twice.
CAP_MARKER = "\u26a0 generation hit the token cap"

# Metrics that a no-op output satisfies by construction. Used to suppress
# meaningless gate-flip comparisons against a baseline that changed nothing.
# NOT prof_caps/prof_slang_removal/required_filler_removal: a no-op FAILS those
# (the slang and fillers are still there), so a flip on them is a real signal.
_PRESERVATION_GATES = frozenset({
    "retention", "order_lcs", "dev_term_preservation",
    "len_ratio", "sentence_delta", "new_words",
})

FAILURE_MARKERS = (
    "[Post-processing] \u2717",
    "\u26a0 No output from llama",
    "\u26a0 Model hallucinated",
    "Resident model load failed",
)

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


_BINARY_OVERRIDE = {"path": None}
_PATH = {"use_gpu": True, "server": False}


def make_config(tone: str, model_path: str, intensity: str = "standard",
                chat_template: str = "auto") -> dict:
    cfg = copy.deepcopy(BASE_CONFIG)
    if _BINARY_OVERRIDE["path"]:
        cfg["llama_cpp_binary"] = _BINARY_OVERRIDE["path"]
    cfg["output_tone"] = tone
    cfg["llama_cpp_model_path"] = os.path.expanduser(model_path)
    cfg["strong_mode"] = intensity == "strong"
    cfg["caricature_mode"] = intensity == "caricature"
    cfg["llama_cpp_chat_template"] = chat_template
    # force_subprocess keeps the factory off the resident sub-branch; use_gpu
    # selects CLI-GPU vs CLI-CPU. BOTH are greedy — llama-simple exposes no
    # sampling flags — so both are deterministic, which is what the byte-comparison
    # termination gate needs. GPU is the default because it is ~4x faster here
    # (2.4s vs 10.4s per generation on Qwen3-4B, verified byte-identical across
    # three consecutive runs) and is the path users actually run.
    cfg["use_gpu"] = _PATH["use_gpu"]
    # server-* paths must NOT force the subprocess: force_subprocess is the flag
    # that pins the deterministic one-shot path, and it also disables the server.
    cfg["post_processing_force_subprocess"] = not _PATH["server"]
    cfg["llama_cpp_residency"] = "instant" if _PATH["server"] else "save_memory"
    return cfg


def preflight(model_path: str, chat_template: str) -> dict:
    """Assert the EFFECTIVE runtime path before any rows run, or abort.

    force_subprocess only applies after the factory has already chosen
    LlamaCppCliBackend; the factory can fall back to the bindings backend, and
    entitlement can silently downgrade both the tone and the model. Without this
    the matrix can run to completion against something other than what it claims.
    """
    from wayfinder.core.postprocessor import LlamaCppCliBackend, get_backend
    from wayfinder.license import get_feature_gate

    problems = []
    cfg = make_config("casual", model_path, "strong", chat_template)
    backend = get_backend(cfg)

    if not isinstance(backend, LlamaCppCliBackend):
        problems.append(f"backend is {type(backend).__name__}, not LlamaCppCliBackend")
    else:
        if _PATH["server"]:
            if backend.force_subprocess:
                problems.append("force_subprocess set on a server path (server disabled)")
            if not backend._server_enabled():
                problems.append(f"server path requested but residency={backend.residency!r} "
                                "disables it")
            from wayfinder.core.llama_server import resolve_server_binary
            sbin = resolve_server_binary(backend.llama_binary, use_gpu=_PATH["use_gpu"])
            if not sbin:
                problems.append(f"no llama-server next to {backend.llama_binary}")
        elif not backend.force_subprocess:
            problems.append("force_subprocess is not set")
        want_gpu = _PATH["use_gpu"]
        if want_gpu and backend.n_gpu_layers == 0:
            problems.append("requested cli-gpu but effective n_gpu_layers=0 "
                            "(entitlement or use_gpu resolution dropped it)")
        if not want_gpu and backend.n_gpu_layers != 0:
            problems.append(f"requested cli-cpu but n_gpu_layers={backend.n_gpu_layers}")
        if os.path.expanduser(model_path) != backend.model_path:
            problems.append(f"model swapped: {backend.model_path}")
        want = _chat_template_expectation(chat_template, model_path)
        got = "chatml" if backend.template else None
        if got != want:
            problems.append(f"effective template {got!r}, expected {want!r}")

    gate = get_feature_gate()
    for feature, why in (("tone_system", "styled tones silently become minimal"),
                         ("large_cleanup_models", "Qwen3-4B is swapped for a Free model")):
        if not gate.has_feature(feature):
            problems.append(f"licence lacks {feature!r} — {why}")

    # strong/caricature are capped by model tier; a capped model cannot exercise
    # the modes this change fixes.
    if backend.intensity != "strong":
        problems.append(f"intensity capped to {backend.intensity!r} for this model")

    server_binary = None
    if _PATH["server"]:
        from wayfinder.core.llama_server import resolve_server_binary
        server_binary = resolve_server_binary(
            getattr(backend, "llama_binary", ""), use_gpu=_PATH["use_gpu"])
    return {"ok": not problems, "problems": problems,
            "binary": getattr(backend, "llama_binary", None),
            "server_binary": server_binary}


def _chat_template_expectation(setting: str, model_path: str):
    from wayfinder.core.postprocessor import _chat_template_for
    return "chatml" if _chat_template_for(model_path, setting) else None


def _prompt_fingerprint(model_path: str, cells: list) -> str:
    """Hash of the ACTUAL prompt text for every cell in the matrix.

    Without this, a tone-guidance edit is invisible to the gate: none of the
    other fingerprint fields (model, binary, corpus, cells) change when a prompt
    string does, so two runs of different prompts would compare as though only
    the execution path differed.
    """
    h = hashlib.sha256()
    for tone, intensity in sorted(cells):
        b = get_backend(make_config(tone, model_path, intensity))
        h.update(b.build_cli_prompt("probe text for hashing", tone,
                                    b.intensity, b.template).encode("utf-8"))
    return h.hexdigest()[:16]


def _guard_fingerprint(model_path: str, cells: list) -> dict:
    """The hallucination-guard floor per intensity, read from the backend."""
    out = {}
    for tone, intensity in cells:
        if intensity not in out:
            out[intensity] = get_backend(
                make_config(tone, model_path, intensity))._guard_threshold()
    return out


def _sampling_fingerprint(model_path: str, cells: list) -> dict:
    """The sampling profile actually used, per intensity present in the matrix.

    Only meaningful on the server path; the CLI path exposes no sampling flags.
    """
    if not _PATH["server"]:
        return {}
    from wayfinder.core.postprocessor import get_backend
    out = {}
    for tone, intensity in cells:
        if intensity in out:
            continue
        b = get_backend(make_config(tone, model_path, intensity))
        out[intensity] = b._sampling_profile()
    return out


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
    except OSError:
        return "?"
    return h.hexdigest()[:16]


def run_one(text: str, tone: str, model_path: str, intensity: str = "standard",
            chat_template: str = "auto") -> dict:
    cfg = make_config(tone, model_path, intensity, chat_template)
    buf = io.StringIO()
    t0 = time.perf_counter()
    try:
        # process_with_config swallows failures and returns the input, so the only
        # signal that a row failed is what it printed.
        with contextlib.redirect_stdout(buf):
            out = process_with_config(text, cfg)
        err = None
    except Exception as e:  # the harness must never crash mid-matrix
        out, err = text, f"{type(e).__name__}: {e}"
    logged = buf.getvalue()
    fell_back = [m for m in FAILURE_MARKERS if m in logged]
    # Which path ACTUALLY ran. Without this the matrix can silently measure the
    # CLI while claiming the server: _server_generate() swallows every failure
    # and falls through by design, so a broken server looks like a clean run.
    m = re.search(r"llama\.cpp (\S+) completed", logged)
    return {
        "output": out,
        "latency_s": round(time.perf_counter() - t0, 3),
        "error": err,
        "fell_back": bool(fell_back),
        "fallback_markers": fell_back,
        "mode": m.group(1) if m else None,
        "hit_cap": CAP_MARKER in logged,
        "stdout": logged.strip()[-500:],
    }


def termination_check(text: str, tone: str, model_path: str, intensity: str,
                      chat_template: str, row_hit_cap: bool = False) -> dict:
    """Prove generation stopped on end-of-turn rather than at the token cap.

    Cannot use process_with_config: it returns only the EXTRACTED text, and the
    extractor truncates at cut markers, paragraph breaks and terminal punctuation,
    so an N-token and a 2N-token generation can normalize to identical final text
    while the raw generations differ. Uses the eval-only _probe_raw, compares raw
    BYTES (production decodes with errors="replace", which is lossy for a capped
    generation ending mid-codepoint).
    """
    from wayfinder.core.postprocessor import get_backend

    backend = get_backend(make_config(tone, model_path, intensity, chat_template))

    # On the server path the answer is already known exactly: llama-server reports
    # stop_type ("eos" | "limit" | "word") for the REAL production request, which
    # run_one() already made. No second generation, no byte comparison, no
    # heuristic — and half the runtime.
    if _PATH["server"]:
        return {"passed": not row_hit_cap, "signal": "stop_type",
                "reason": None if not row_hit_cap else
                          "llama-server reported stop_type=limit (hit the cap)"}

    n1 = backend._token_budget(text)
    try:
        raw1, eff1, cmd1 = backend._probe_raw(text, n1)
        raw2, eff2, cmd2 = backend._probe_raw(text, n1 * 2)
    except Exception as e:
        return {"passed": False, "reason": f"probe failed: {type(e).__name__}: {e}"}

    # Preconditions: recording these is not gating. Identical output would pass
    # vacuously if both calls used the same budget or a different executable.
    if eff2 != eff1 * 2:
        return {"passed": False, "reason": f"budgets not distinct: {eff1} vs {eff2}"}
    shape1, shape2 = (cmd1[0], cmd1[2], cmd1[4]), (cmd2[0], cmd2[2], cmd2[4])
    if shape1 != shape2:
        return {"passed": False, "reason": f"command differs beyond -n: {shape1} vs {shape2}"}

    return {
        "passed": raw1 == raw2,
        "n1": eff1, "n2": eff2,
        "bytes1": len(raw1), "bytes2": len(raw2),
        "reason": None if raw1 == raw2 else "raw continuation grew with the budget (hit the cap)",
    }


def compare_to_baseline(baseline: dict, candidate: dict,
                        advisories: list | None = None,
                        ignore_fingerprint: set | None = None) -> list[str]:
    """Gate the candidate against a baseline run. Returns a list of violations.

    The fingerprint must match on everything except the one variable under test,
    or the comparison is meaningless (a Qwen3 baseline vs a different model,
    binary or probe budget would compare happily otherwise).
    """
    violations = []
    advisories = [] if advisories is None else advisories
    bf, cf = baseline.get("fingerprint", {}), candidate.get("fingerprint", {})
    # Everything not named here must match. The caller must DECLARE the variable
    # under test, so a run that differs in some other way cannot quietly compare.
    varying = {"chat_template"} | set(ignore_fingerprint or ())
    for key in sorted(set(bf) | set(cf)):
        if key in varying:
            continue
        if bf.get(key) != cf.get(key):
            violations.append(
                f"FINGERPRINT {key}: baseline={bf.get(key)!r} candidate={cf.get(key)!r}"
            )
    if violations:
        return violations + ["refusing to compare rows across incompatible runs"]

    def index(payload):
        return {(r["sample_id"], r["tone"], r["intensity"]): r for r in payload["results"]}

    b_rows, c_rows = index(baseline), index(candidate)
    for key in sorted(set(b_rows) - set(c_rows)):
        violations.append(f"MISSING in candidate: {key}")
    for key in sorted(set(c_rows) - set(b_rows)):
        violations.append(f"MISSING in baseline: {key}")

    for key in sorted(set(b_rows) & set(c_rows)):
        b, c = b_rows[key], c_rows[key]
        tag = "/".join(key)

        # --- Gates on the defect under repair: ABSOLUTE. The candidate must be
        # clean regardless of the baseline, because the baseline IS the bug.
        if c.get("echoed_input"):
            violations.append(f"ECHO {tag}: output opens with a verbatim copy of the input")
        # The candidate must have run on the path its fingerprint claims. The
        # server path degrades to the CLI silently by design, so without this a
        # server run that never reached the server would compare clean.
        want_mode = "server" if cf.get("path", "").startswith("server-") else "CLI"
        got_mode = c.get("mode")
        if got_mode is not None and not got_mode.startswith(want_mode):
            violations.append(f"PATH {tag}: claimed {want_mode}, actually ran {got_mode}")
        term = c.get("termination")
        if term and not term.get("passed"):
            violations.append(f"TERMINATION {tag}: {term.get('reason')}")

        # --- Health gates: fail only when NEW. These are pre-existing conditions
        # of the model/prompt (the hallucination guard firing, an empty
        # generation), not of this change. Failing on a baseline condition would
        # block a correct fix for an unrelated reason; ignoring a NEW one would
        # let this change break a row silently. Both are reported.
        if c["error"] and not b["error"]:
            violations.append(f"ERROR {tag}: {c['error']}")
        if c["fell_back"] and not b["fell_back"]:
            violations.append(f"FALLBACK {tag}: {c['fallback_markers']}")
        # --- Quality gates: compare PASS/FAIL, never raw metric deltas.
        #
        # Comparing magnitudes is wrong for this change and measured so: an
        # untouched output scores word_retention 1.0, so a candidate that
        # correctly removes "lowkey" (0.944) or a whisper stutter "i think i
        # think" (0.826) looks like a regression while being strictly better. A
        # gate flip — baseline inside the band, candidate outside — is the signal
        # that survives a change which makes the model do MORE cleanup.
        #
        # Only keys present in BOTH rows' `passes` are compared, which inherits
        # every applicability rule for free: retention is absent for
        # transformative/professional rows, dev_term_preservation for non-dev
        # tones, and so on.
        # A baseline row whose output IS its input passes every preservation
        # metric trivially — it preserved everything by doing nothing. MEASURED:
        # on short_01_slang the CLI baseline returned "oh thats tight bro nice"
        # untouched (retention 1.0) while the server correctly wrote "oh that's
        # tight bro nice" (retention 0.8), and the gate called the fix a
        # regression. Comparing against a no-op is not a bar worth clearing.
        b_noop = b["output"].strip() == b["input"].strip()
        # Gates the BASELINE never had. The flip comparison below is blind to
        # these — a metric added since the baseline has no prior value to flip
        # FROM, so a newly added gate could sit red on every row and the run
        # would still report clean. MEASURED: that is exactly what happened when
        # nonslang_preservation was added and wrongly applied to professional
        # /strong, failing 17 of 18 rows invisibly.
        for gate, c_ok in c["metrics"]["passes"].items():
            if gate not in b["metrics"]["passes"] and c_ok is False:
                violations.append(
                    f"NEW GATE {tag} {gate}: fails (absent from the baseline, so "
                    "no flip to compare — check the gate's applicability)")
        for gate, b_ok in b["metrics"]["passes"].items():
            c_ok = c["metrics"]["passes"].get(gate)
            if c_ok is None:
                continue  # not applicable to the candidate row
            if b_noop and gate in _PRESERVATION_GATES:
                advisories.append(f"skipped {tag} {gate}: baseline was a no-op")
                continue
            if b_ok and not c_ok:
                violations.append(f"GATE {tag} {gate}: passed in baseline, fails in candidate")
    # --- Work done, per (tone, intensity). GATED, because it is deterministic.
    #
    # Wall-clock is NOT gated: MEASURED, two full candidate runs on identical
    # committed code produced 0 of 198 differing outputs but a 1.30x median
    # latency swing, purely from background load on the workstation. A wall-clock
    # band therefore reports the machine, not the change. Generated bytes from the
    # termination probe measure the thing that actually matters — whether the
    # change makes the model generate MORE — and are reproducible byte-for-byte.
    def medians(payload, pick):
        buckets = {}
        for r in payload["results"]:
            v = pick(r)
            if v is not None:
                buckets.setdefault((r["tone"], r["intensity"]), []).append(v)
        return {k: sorted(v)[len(v) // 2] for k, v in buckets.items()}

    _bytes = lambda r: (r.get("termination") or {}).get("bytes1")
    b_gen, c_gen = medians(baseline, _bytes), medians(candidate, _bytes)
    for key, bv in b_gen.items():
        cv = c_gen.get(key)
        if cv is not None and bv > 0 and cv > bv * 1.15:
            violations.append(
                f"GENERATED {'/'.join(key)}: {bv} -> {cv} bytes (>1.15x more work)")

    # Advisory only — printed, never a violation.
    b_lat, c_lat = medians(baseline, lambda r: r.get("latency_s")), \
        medians(candidate, lambda r: r.get("latency_s"))
    for key, bv in sorted(b_lat.items()):
        cv = c_lat.get(key)
        if cv is not None and bv > 0:
            advisories.append(f"latency {'/'.join(key)}: {bv}s -> {cv}s ({cv / bv:.2f}x)")
    return violations


def main(argv=None):
    ap = argparse.ArgumentParser(description="Tone post-processing eval harness")
    ap.add_argument("--model", default=BASE_CONFIG["llama_cpp_model_path"],
                    help="GGUF model path (A/B different models)")
    ap.add_argument("--cells", default="",
                    help="comma list of tone:intensity cells (default: rule-5 matrix)")
    ap.add_argument("--samples", default="",
                    help="comma list of sample ids (default: all)")
    ap.add_argument("--path", default="cli-gpu",
                    choices=["cli-gpu", "cli-cpu", "server-gpu", "server-cpu"],
                    help="execution path to pin and assert. cli-* are greedy one-shot "
                         "(deterministic); server-* use the resident llama-server "
                         "(sampled, and report stop_type exactly)")
    ap.add_argument("--binary", default=BASE_CONFIG["llama_cpp_binary"],
                    help="llama.cpp binary dir/path (the ctor resolves llama-simple*)")
    ap.add_argument("--chat-template", default="auto", choices=["auto", "off"],
                    help="baseline runs with 'off', candidate with 'auto'")
    ap.add_argument("--baseline", default="",
                    help="baseline results JSON; turns this run into a pass/fail gate")
    ap.add_argument("--gate-ignore", default="",
                    help="comma list of fingerprint keys allowed to differ from the "
                         "baseline (the variable under test). chat_template is always "
                         "allowed. e.g. cli-gpu -> server-gpu needs "
                         "path,force_subprocess,sampling,server_binary_sha")
    ap.add_argument("--skip-termination", action="store_true",
                    help="skip the N/2N raw-continuation probe (doubles runtime)")
    ap.add_argument("--out-dir", default=str(ROOT / "scripts" / "tone_eval" / "results"))
    ap.add_argument("--judge", action="store_true",
                    help="opt-in LLM-judge pass (advisory only)")
    ap.add_argument("--judge-backend", default="anthropic",
                    choices=["anthropic", "openai", "local"])
    args = ap.parse_args(argv)

    if args.cells:
        cells = [tuple(c.split(":", 1)) for c in args.cells.split(",") if c.strip()]
    else:
        cells = list(DEFAULT_CELLS)
    want_ids = {s.strip() for s in args.samples.split(",") if s.strip()}
    samples = [s for s in CORPUS if (not want_ids or s["id"] in want_ids)]

    _PATH["use_gpu"] = args.path.endswith("-gpu")
    _PATH["server"] = args.path.startswith("server-")
    _BINARY_OVERRIDE["path"] = os.path.expanduser(args.binary)
    model_path = os.path.expanduser(args.model)
    if not Path(model_path).exists():
        print(f"[eval] FATAL model not found: {model_path}", file=sys.stderr)
        return 2

    pre = preflight(model_path, args.chat_template)
    if not pre["ok"]:
        print("[eval] FATAL preflight failed — the matrix would not measure what it claims:",
              file=sys.stderr)
        for p in pre["problems"]:
            print(f"         - {p}", file=sys.stderr)
        return 2
    print(f"[eval] preflight OK — {args.path}, template={args.chat_template}, "
          f"binary={pre['server_binary'] or pre['binary']}")

    results = []
    total = len(samples) * len(cells)
    i = 0
    for s in samples:
        for tone, intensity in cells:
            i += 1
            print(f"[eval] {i:>3}/{total} {s['id']:<26} {tone}/{intensity} ...", flush=True)
            r = run_one(s["text"], tone, model_path, intensity, args.chat_template)
            metrics = M.compute_all(s, tone, s["text"], r["output"], intensity)
            rec = {
                "sample_id": s["id"], "stresses": s["stresses"],
                "tone": tone, "intensity": intensity,
                "input": s["text"], "output": r["output"],
                "latency_s": r["latency_s"], "error": r["error"],
                "fell_back": r["fell_back"], "fallback_markers": r["fallback_markers"],
                "mode": r.get("mode"), "hit_cap": r.get("hit_cap"),
                "stdout": r["stdout"],
                # Absolute echo check: did the output open with a verbatim copy of
                # the input and then continue? That is the reported defect.
                "echoed_input": strip_echoed_input(r["output"], s["text"]) != r["output"],
                "metrics": metrics,
            }
            if not args.skip_termination:
                rec["termination"] = termination_check(
                    s["text"], tone, model_path, intensity, args.chat_template,
                    row_hit_cap=bool(r.get("hit_cap")))
            if args.judge:
                from tone_eval import judge as J
                rec["judge"] = J.judge(s["text"], r["output"], tone, backend=args.judge_backend)
            results.append(rec)

    Path(args.out_dir).mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = f"tone-eval_{Path(model_path).stem}_{args.path}_{args.chat_template}_{stamp}"
    json_path = Path(args.out_dir) / f"{base}.json"
    md_path = Path(args.out_dir) / f"{base}.md"

    payload = {
        "generated": stamp,
        "model": model_path,
        "tones": sorted({t for t, _ in cells}),
        "fingerprint": {
            # Everything that can change the output. A resolved path is not binary
            # identity — the AppImage mount point changes between runs.
            "corpus_version": CORPUS_VERSION,
            "schema": 2,
            "model_key": chat_template_key(model_path),
            "model_sha": _sha256(model_path),
            "binary_sha": _sha256(pre["binary"] or ""),
            "server_binary_sha": _sha256(pre["server_binary"] or "") if _PATH["server"] else None,
            "use_gpu": _PATH["use_gpu"],
            "path": args.path,
            "force_subprocess": not _PATH["server"],
            # Sampling is part of the output's identity on the server path
            # (the CLI path has none). Comparing a repeat_penalty=1.1 run to a
            # 1.15 run as if only the template changed would be meaningless.
            "sampling": _sampling_fingerprint(model_path, cells),
            "prompt_profile": _prompt_fingerprint(model_path, cells),
            # The hallucination-guard floor: it decides whether a rewrite reaches
            # the user at all, so a change to it must not be invisible to the
            # gate. Read from the backend, never restated here.
            "guard_thresholds": _guard_fingerprint(model_path, cells),
            "chat_template": args.chat_template,
            "cells": sorted(f"{t}:{i}" for t, i in cells),
            "sample_ids": sorted(s["id"] for s in samples),
            "max_tokens": BASE_CONFIG["post_processing_max_tokens"],
            "temperature": BASE_CONFIG["post_processing_temperature"],
            "n_ctx": BASE_CONFIG["llama_cpp_n_ctx"],
        },
        "results": results,
        "summary": M.summarize(results),
    }
    json_path.write_text(json.dumps(payload, indent=2))
    md_path.write_text(R.render_markdown(payload))
    print(f"\n[eval] JSON -> {json_path}\n[eval] MD   -> {md_path}")
    R.print_console_summary(payload)

    if args.baseline:
        baseline = json.loads(Path(args.baseline).read_text())
        advisories: list[str] = []
        violations = compare_to_baseline(
            baseline, payload, advisories,
            ignore_fingerprint={k.strip() for k in args.gate_ignore.split(",") if k.strip()})
        print(f"\n[eval] === GATE vs {Path(args.baseline).name} ===")
        # Pre-existing health conditions are surfaced but do not fail the gate.
        carried = []
        b_rows = {(r["sample_id"], r["tone"], r["intensity"]): r
                  for r in baseline["results"]}
        for r in payload["results"]:
            b = b_rows.get((r["sample_id"], r["tone"], r["intensity"]))
            if b and (r["fell_back"] or r["error"]) and (b["fell_back"] or b["error"]):
                carried.append(f"{r['sample_id']}/{r['tone']}/{r['intensity']} "
                               f"{r['fallback_markers'] or r['error']}")
        for a in advisories:
            print(f"  [advisory] {a}")
        if carried:
            print(f"  [carried from baseline, not a regression] {len(carried)} row(s):")
            for cx in carried:
                print(f"      {cx}")
        if violations:
            for v in violations:
                print(f"  FAIL {v}")
            print(f"[eval] {len(violations)} violation(s)")
            return 1
        print("[eval] PASS — no violations")
    return 0


if __name__ == "__main__":
    sys.exit(main())
