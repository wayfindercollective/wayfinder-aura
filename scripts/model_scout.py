#!/usr/bin/env python3
"""Scout for new Whisper.cpp / known GGUF models vs the current catalog.

Does NOT auto-publish. Writes a digest you can act on (Personal OS task, etc.).

Usage:
  python3 scripts/model_scout.py
  python3 scripts/model_scout.py --notify   # run WAYFINDER_SCOUT_NOTIFY_CMD if set
  python3 scripts/model_scout.py --json

Sources (lightweight HEAD/list, not full downloads):
  - Hugging Face ggerganov/whisper.cpp tree for ggml-*.bin
  - A short allowlist of HF GGUF repos (expand in SCOUT_LLM_SOURCES)

Env:
  WAYFINDER_SCOUT_NOTIFY_CMD  shell command; digest path is $1 / env SCOUT_DIGEST
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

UA = "Wayfinder-Aura-Scout/1.0"

# LLM repos we care about (id_prefix, hf_repo, filename_glob-ish exact names optional)
SCOUT_LLM_SOURCES = [
    {
        "label": "Qwen3 instruct GGUF (bartowski)",
        "api": "https://huggingface.co/api/models/bartowski/Qwen_Qwen3-4B-Instruct-2507-GGUF/tree/main",
        "name_re": re.compile(r"Qwen_Qwen3-4B-Instruct.*Q4_K_M\.gguf$", re.I),
    },
    {
        "label": "Gemma 3 1B (bartowski)",
        "api": "https://huggingface.co/api/models/bartowski/google_gemma-3-1b-it-GGUF/tree/main",
        "name_re": re.compile(r"google_gemma-3-1b-it-Q4_K_M\.gguf$", re.I),
    },
    {
        "label": "Qwen3.5 2B (unsloth)",
        "api": "https://huggingface.co/api/models/unsloth/Qwen3.5-2B-GGUF/tree/main",
        "name_re": re.compile(r"Qwen3\.5-2B-Q4_K_M\.gguf$", re.I),
    },
]


def _http_json(url: str, timeout: float = 20.0):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _http_ok(url: str, timeout: float = 15.0) -> bool:
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 400
    except Exception:
        return False


def load_known_filenames() -> set[str]:
    import wayfinder_main as m
    from wayfinder.model_catalog import fetch_remote_catalog, catalog_url_from_config
    from wayfinder.config import load_config

    names: set[str] = set()
    for info in m.WHISPER_CPP_MODELS.values():
        if info.get("filename"):
            names.add(info["filename"])
    for info in m.LLM_GGUF_MODELS.values():
        if info.get("filename"):
            names.add(info["filename"])

    # Also merge remote catalog if live
    try:
        remote = fetch_remote_catalog(load_config(), force=True, timeout=10.0)
        if remote:
            for sec in ("whisper", "llm"):
                for info in (remote.get(sec) or {}).values():
                    if info.get("filename"):
                        names.add(info["filename"])
    except Exception:
        pass
    return names


def scout_whisper(known: set[str]) -> list[dict]:
    """List ggml-*.bin on ggerganov/whisper.cpp not in our catalog."""
    api = "https://huggingface.co/api/models/ggerganov/whisper.cpp/tree/main"
    try:
        tree = _http_json(api)
    except Exception as e:
        return [{"error": f"whisper tree fetch failed: {e}"}]
    hits = []
    for item in tree if isinstance(tree, list) else []:
        path = item.get("path") or item.get("rfilename") or ""
        if not path.startswith("ggml-") or not path.endswith(".bin"):
            continue
        if "for-tests" in path:
            continue
        if path in known:
            continue
        hits.append(
            {
                "kind": "whisper",
                "filename": path,
                "url": f"https://huggingface.co/ggerganov/whisper.cpp/resolve/main/{path}",
                "size": item.get("size"),
            }
        )
    return hits


def scout_llm(known: set[str]) -> list[dict]:
    hits = []
    for src in SCOUT_LLM_SOURCES:
        try:
            tree = _http_json(src["api"])
        except Exception as e:
            hits.append({"error": f"{src['label']}: {e}"})
            continue
        for item in tree if isinstance(tree, list) else []:
            path = item.get("path") or ""
            if not src["name_re"].search(path):
                continue
            if path in known:
                continue
            repo = src["api"].split("/models/")[1].split("/tree")[0]
            hits.append(
                {
                    "kind": "llm",
                    "label": src["label"],
                    "filename": path,
                    "url": f"https://huggingface.co/{repo}/resolve/main/{path}",
                    "size": item.get("size"),
                }
            )
    return hits


def write_digest(path: Path, known: set[str], whisper_new: list, llm_new: list) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        f"# Wayfinder model scout — {now}",
        "",
        f"Known catalog filenames: **{len(known)}**",
        "",
    ]
    if any("error" in x for x in whisper_new + llm_new):
        lines.append("## Errors")
        for x in whisper_new + llm_new:
            if "error" in x:
                lines.append(f"- {x['error']}")
        lines.append("")

    wn = [x for x in whisper_new if "error" not in x]
    ln = [x for x in llm_new if "error" not in x]

    lines.append(f"## New Whisper.cpp weights ({len(wn)})")
    if not wn:
        lines.append("_None vs current catalog._")
    else:
        for x in wn:
            mb = (x.get("size") or 0) / 1e6
            lines.append(f"- `{x['filename']}` (~{mb:.0f} MB) — {x['url']}")
    lines.append("")
    lines.append(f"## Tracked LLM updates ({len(ln)})")
    if not ln:
        lines.append("_None vs current catalog._")
    else:
        for x in ln:
            mb = (x.get("size") or 0) / 1e6
            lines.append(
                f"- **{x.get('label','llm')}**: `{x['filename']}` (~{mb:.0f} MB) — {x['url']}"
            )
    lines.extend(
        [
            "",
            "## Next steps (when you want a model)",
            "1. Decide free vs Ultra (`requires_feature` / PUBLIC_OBJECTS).",
            "2. Upload to R2: `scripts/r2_worker_multipart_upload.py --key … --file …`",
            "3. Add entry to app catalog *or* patch `catalog/v1.json` and run "
            "`scripts/publish_model_catalog.py`.",
            "4. Clients pick it up on next catalog fetch (no app rebuild if only remote catalog).",
            "",
        ]
    )
    text = "\n".join(lines)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return text


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path.home()
        / ".local"
        / "share"
        / "wayfinder-aura"
        / "model-scout-latest.md",
    )
    ap.add_argument("--json", action="store_true", help="Print JSON summary to stdout")
    ap.add_argument(
        "--notify",
        action="store_true",
        help="Run WAYFINDER_SCOUT_NOTIFY_CMD with digest path",
    )
    args = ap.parse_args()

    known = load_known_filenames()
    whisper_new = scout_whisper(known)
    llm_new = scout_llm(known)
    text = write_digest(args.output, known, whisper_new, llm_new)
    print(text)
    print(f"\n(Digest written to {args.output})")

    if args.json:
        print(
            json.dumps(
                {
                    "known": len(known),
                    "whisper_new": [x for x in whisper_new if "error" not in x],
                    "llm_new": [x for x in llm_new if "error" not in x],
                    "digest": str(args.output),
                },
                indent=2,
            )
        )

    if args.notify:
        cmd = (os.environ.get("WAYFINDER_SCOUT_NOTIFY_CMD") or "").strip()
        if cmd:
            env = os.environ.copy()
            env["SCOUT_DIGEST"] = str(args.output)
            # Prefer shell with $1 = digest path
            subprocess.run(
                ["bash", "-lc", cmd + ' "$1"', "scout-notify", str(args.output)],
                check=False,
                env=env,
            )
        else:
            # Soft notify: create a Personal OS / home flag file if present
            flag = Path.home() / ".local" / "share" / "wayfinder-aura" / "SCOUT_NEEDS_REVIEW"
            new_count = len([x for x in whisper_new + llm_new if "error" not in x])
            if new_count:
                flag.write_text(
                    f"{new_count} candidate(s)\nSee {args.output}\n",
                    encoding="utf-8",
                )
                print(f"Flag file: {flag}")
            elif flag.exists():
                flag.unlink()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
