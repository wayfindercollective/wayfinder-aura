"""Optional, opt-in LLM-judge for the tone eval harness (advisory only).

Never gates the deterministic metrics — it's a second opinion. Degrades to a
{"skipped": ...} record when no backend/key is available.

Backends:
- anthropic / openai : reuse the app's own cloud backends (needs API key in env).
- local             : grade with a larger local GGUF via process_with_config.
"""

from __future__ import annotations

import json
import os
import re

RUBRIC = {
    "dev": "Keep ~90% of the words and their order. Recognize/keep dev terms "
           "(git, commit, branch, boolean, PR). Do NOT restructure.",
    "casual": "Keep ~90% of words. Lowercase ok, relaxed/less punctuation, calm "
              "texting feel. Do NOT rewrite or summarize.",
    "professional": "Keep ~90% of words. Tighten punctuation/capitalization, "
                    "lightly clean slang as the MAX change. Do NOT rewrite.",
    "minimal": "Remove only um/uh/ah. Change nothing else.",
}

JUDGE_PROMPT = """You are grading a dictation post-processor whose goal is to be a
GENTLE GUIDE: keep ~90% of the speaker's original words, never restructure,
reorder, or summarize.

INTENT for tone "{tone}": {rubric}

ORIGINAL:
{inp}

OUTPUT:
{out}

Return ONLY compact JSON, no prose:
{{"word_fidelity":1-5,"no_restructure":1-5,"tone_fit":1-5,"over_edited":true|false,"verdict":"pass|fail","reason":"<=12 words"}}"""


def _cloud_backend(kind: str):
    from wayfinder.core.postprocessor import AnthropicBackend, OpenAIBackend
    if kind == "anthropic":
        b = AnthropicBackend()
        return b if b.is_available() else None
    if kind == "openai":
        b = OpenAIBackend()
        return b if b.is_available() else None
    return None


def _parse(raw: str) -> dict:
    m = re.search(r"\{.*\}", raw or "", re.S)
    if not m:
        return {"raw": raw}
    try:
        return json.loads(m.group(0))
    except Exception:
        return {"raw": raw}


def judge(inp: str, out: str, tone: str, backend: str = "anthropic",
          local_model: str = "") -> dict:
    rubric = RUBRIC.get(tone, RUBRIC["professional"])
    prompt = JUDGE_PROMPT.format(tone=tone, rubric=rubric, inp=inp, out=out)

    if backend == "local":
        model = local_model or os.path.expanduser(
            "~/.local/share/wayfinder-aura/llm-models/Phi-3-mini-4k-instruct-q4.gguf"
        )
        if not os.path.exists(model):
            return {"skipped": f"local judge model not found: {model}"}
        from wayfinder.core.postprocessor import LlamaCppCliBackend
        be = LlamaCppCliBackend(model_path=model, output_tone="minimal")
        if not be.is_available():
            return {"skipped": "local judge backend unavailable"}
        try:
            # Bypass tone routing: feed the judge prompt straight through.
            return _parse(_run_local(be, prompt))
        except Exception as e:
            return {"error": str(e)}

    be = _cloud_backend(backend)
    if be is None:
        return {"skipped": f"no {backend} backend/key available"}
    try:
        return _parse(be.process(out, prompt))
    except Exception as e:
        return {"error": str(e)}


def _run_local(be, prompt: str) -> str:
    """Run a raw prompt through the CLI backend, skipping tone shaping."""
    import subprocess
    cmd = [be.llama_binary, "-m", be.model_path]
    if be.n_gpu_layers != 0:
        ngl = 99 if be.n_gpu_layers == -1 else be.n_gpu_layers
        cmd += ["-ngl", str(ngl)]
    cmd += ["-n", "120", "-p", prompt]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=be.timeout)
    return r.stdout
