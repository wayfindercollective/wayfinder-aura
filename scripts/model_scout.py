#!/usr/bin/env python3
"""Scout for new Whisper.cpp / GGUF post-processing models vs the current catalog.

**Reports only.** This script NEVER replaces a model, edits a catalog, uploads
weights, or opens a PR. Promotion is a reviewed manual step — see
``docs/MODEL-REFRESH-PLAYBOOK.md``. Anything that changes production must go
through that playbook, not through this scout or the workflow that runs it.

Usage:
  python3 scripts/model_scout.py
  python3 scripts/model_scout.py --notify   # run WAYFINDER_SCOUT_NOTIFY_CMD if set
  python3 scripts/model_scout.py --json     # JSON *only* on stdout (CI-parseable)

Sources (lightweight tree listings, not full downloads):
  - Hugging Face ggerganov/whisper.cpp tree for ggml-*.bin
  - ``PIN_WATCH``: the exact repos/files we ship, to catch a re-release — both a
    new matching filename and a same-filename re-upload (LFS oid vs our pin)
  - ``DISCOVERY``: a small curated list of eligible candidate repos to check for
    *new* Q4_K_M releases. Curated on purpose — an open-ended HF search returns
    unranked noise and needs relevance logic nobody would maintain.

This module must stay **headless**: it reads the app catalogs as AST literals
rather than importing ``wayfinder_main`` (which pulls in tkinter and the whole
UI stack), so it can run in CI and on a server with no display.

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

# ---------------------------------------------------------------------------
# LLM sources, in two clearly separated groups.
#
# PIN_WATCH  — the repos/files we actually ship. A hit means our revision pin
#              may be stale, in either of the two ways that can happen:
#                a) upstream published a NEW matching filename, or
#                b) upstream re-uploaded the SAME filename with different bytes
#                   — detected by comparing HuggingFace's LFS oid on `main`
#                   against our pinned sha256 (an LFS oid *is* the sha256).
#              (b) matters because it is invisible to every other check we run:
#              `model-pin-drift.yml` reads our *pinned revision*, so it still
#              sees our bytes and correctly passes. It answers "is our pin still
#              intact?"; this answers "has upstream moved past our pin?".
#              `tests/test_catalog_ratchet.py` enforces a 1:1 mapping between
#              this list and LLM_GGUF_MODELS.
# DISCOVERY  — candidate repos we do not ship, checked for new Q4_K_M weights.
#              Purely informational; a hit is a suggestion to evaluate, never a
#              reason to change anything automatically. NOT ratcheted against
#              the catalog (these are deliberately not shipped).
# ---------------------------------------------------------------------------
PIN_WATCH = [
    {
        "group": "pin_watch",
        "label": "Qwen3 4B Instruct 2507 (bartowski)",
        "api": "https://huggingface.co/api/models/bartowski/Qwen_Qwen3-4B-Instruct-2507-GGUF/tree/main",
        "name_re": re.compile(r"Qwen_Qwen3-4B-Instruct.*Q4_K_M\.gguf$", re.I),
    },
    {
        "group": "pin_watch",
        "label": "Gemma 3 1B (bartowski)",
        "api": "https://huggingface.co/api/models/bartowski/google_gemma-3-1b-it-GGUF/tree/main",
        "name_re": re.compile(r"google_gemma-3-1b-it-Q4_K_M\.gguf$", re.I),
    },
    {
        "group": "pin_watch",
        "label": "Qwen3.5 2B (unsloth)",
        "api": "https://huggingface.co/api/models/unsloth/Qwen3.5-2B-GGUF/tree/main",
        "name_re": re.compile(r"Qwen3\.5-2B-Q4_K_M\.gguf$", re.I),
    },
]

DISCOVERY = [
    {
        "group": "discovery",
        "label": "Qwen3.5 4B (bartowski) — thinking VLM, candidate #1",
        "api": "https://huggingface.co/api/models/bartowski/Qwen_Qwen3.5-4B-GGUF/tree/main",
        "name_re": re.compile(r"Qwen_Qwen3\.5-4B.*Q4_K_M\.gguf$", re.I),
        "note": "Apache-2.0. Thinking model — verify non-thinking latency before adopting.",
    },
    {
        "group": "discovery",
        "label": "Qwen3.5 2B instruct variants (bartowski)",
        "api": "https://huggingface.co/api/models/bartowski/Qwen_Qwen3.5-2B-GGUF/tree/main",
        "name_re": re.compile(r"Qwen_Qwen3\.5-2B.*Q4_K_M\.gguf$", re.I),
        "note": "Apache-2.0. Alternate quantiser for the shipped medium tier.",
    },
    {
        "group": "discovery",
        "label": "Phi-4 mini instruct (bartowski)",
        "api": "https://huggingface.co/api/models/bartowski/microsoft_Phi-4-mini-instruct-GGUF/tree/main",
        "name_re": re.compile(r"Phi-4-mini-instruct.*Q4_K_M\.gguf$", re.I),
        "note": "MIT. 3.8B — heavy-tier alternative to Qwen3 4B Instruct 2507.",
    },
    {
        "group": "discovery",
        # HuggingFaceTB/SmolLM3-3B-GGUF returns HTTP 401 to anonymous API
        # callers (checked 2026-09); bartowski's mirror is public.
        "label": "SmolLM3 3B (bartowski mirror)",
        "api": "https://huggingface.co/api/models/bartowski/HuggingFaceTB_SmolLM3-3B-GGUF/tree/main",
        "name_re": re.compile(r"SmolLM3-3B.*Q4_K_M\.gguf$", re.I),
        "note": "Apache-2.0. Successor to the retired SmolLM2 360M.",
    },
    {
        "group": "discovery",
        "label": "Gemma 3 4B instruct (bartowski)",
        "api": "https://huggingface.co/api/models/bartowski/google_gemma-3-4b-it-GGUF/tree/main",
        "name_re": re.compile(r"google_gemma-3-4b-it.*Q4_K_M\.gguf$", re.I),
        "note": "Gemma ToS — a NEW Gemma pick needs a fresh redistribution review.",
    },
    {
        "group": "discovery",
        "label": "Llama 3.3 / 3.2 instruct GGUF (bartowski)",
        "api": "https://huggingface.co/api/models/bartowski/Llama-3.2-3B-Instruct-GGUF/tree/main",
        "name_re": re.compile(r"Llama-3\.[23]-3B-Instruct.*Q4_K_M\.gguf$", re.I),
        "note": "Llama Community License — acceptable-use terms follow redistribution.",
    },
]

# Back-compat alias: everything the scout checks, in one list.
SCOUT_LLM_SOURCES = PIN_WATCH + DISCOVERY


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


def read_app_catalogs() -> dict[str, dict]:
    """Read WHISPER_CPP_MODELS / LLM_GGUF_MODELS without importing wayfinder_main.

    Importing that module pulls in customtkinter and the whole UI stack, which
    makes the scout unrunnable on a CI runner or a headless box — the exact
    failure this function exists to avoid. Same approach as
    ``scripts/verify-model-digests.py::_catalogs``.
    """
    import ast

    tree = ast.parse((ROOT / "wayfinder_main.py").read_text(encoding="utf-8"))
    wanted = {"WHISPER_CPP_MODELS", "LLM_GGUF_MODELS"}
    found: dict[str, dict] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in wanted:
                    found[target.id] = ast.literal_eval(node.value)
    missing = wanted - set(found)
    if missing:
        raise SystemExit(f"could not find catalog(s) in wayfinder_main.py: {sorted(missing)}")
    return found


def load_known_filenames() -> set[str]:
    """Every filename we already ship or already publish."""
    names: set[str] = set()
    catalogs = read_app_catalogs()
    for section in ("WHISPER_CPP_MODELS", "LLM_GGUF_MODELS"):
        for info in catalogs[section].values():
            if info.get("filename"):
                names.add(info["filename"])

    # Also merge the published catalog checked into the repo, so a model that
    # only exists remotely is not re-reported as "new" every fortnight. Read
    # from disk, not over the network: the scout must work offline-ish and a
    # network read here would import the config/UI stack.
    published = ROOT / "catalog" / "v1.json"
    try:
        doc = json.loads(published.read_text(encoding="utf-8"))
        for sec in ("whisper", "llm"):
            for info in (doc.get(sec) or {}).values():
                if isinstance(info, dict) and info.get("filename"):
                    names.add(info["filename"])
    except Exception:
        pass
    return names


def load_pinned_digests() -> dict[str, str]:
    """Map every shipped filename to the sha256 we pin it at.

    A HuggingFace LFS oid is the file's sha256, so this is directly comparable
    to `item["lfs"]["oid"]` from a tree listing — no download required.
    """
    pins: dict[str, str] = {}
    catalogs = read_app_catalogs()
    for section in ("WHISPER_CPP_MODELS", "LLM_GGUF_MODELS"):
        for info in catalogs[section].values():
            filename, digest = info.get("filename"), info.get("sha256")
            if filename and digest:
                pins[filename] = digest
    return pins


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


def scout_llm(
    known: set[str],
    sources=None,
    pins: dict[str, str] | None = None,
    pin_stats: dict | None = None,
) -> list[dict]:
    """Check every configured source for weights we do not already know about.

    `pins` maps filename -> pinned sha256 (see `load_pinned_digests`). It is what
    makes a *same-filename* re-release visible: without it, filtering on `known`
    alone silently swallows the most common form of pin staleness.

    `pin_stats`, if given, is filled in with which shipped pins were actually
    *compared* against upstream and which could not be. This matters because
    "no stale pins found" and "we never managed to look" are indistinguishable
    from the hit list alone — every non-comparison path here ends in a bare
    `continue`, so silence is ambiguous. Reporting it as success was the bug.
    """
    hits = []
    pins = pins or {}
    verified: list[str] = []
    unverifiable: list[dict] = []

    def _expected(src) -> list[str]:
        """Shipped filenames this source is supposed to be watching.

        Computed offline from `known` + the source regex, so an unreachable
        source still knows how many pins it failed to check.
        """
        return sorted(f for f in known if src["name_re"].search(f))

    for src in (SCOUT_LLM_SOURCES if sources is None else sources):
        group = src.get("group", "discovery")
        watching = _expected(src) if group == "pin_watch" else []
        try:
            tree = _http_json(src["api"])
        except Exception as e:
            hits.append({"error": f"{src['label']}: {e}", "group": group})
            # An unreachable source verified nothing, so every pin it watches
            # is unverifiable — not "matching".
            for filename in watching:
                unverifiable.append(
                    {"filename": filename, "reason": f"source unreachable: {e}"}
                )
            continue
        seen: set[str] = set()
        for item in tree if isinstance(tree, list) else []:
            path = item.get("path") or ""
            if not src["name_re"].search(path):
                continue
            upstream_oid = (item.get("lfs") or {}).get("oid") or ""
            pinned_oid = pins.get(path, "")
            stale_pin = False
            if path in known:
                # A file we already ship. For a DISCOVERY repo that is simply
                # not news. For a PIN_WATCH repo it is only "not news" while
                # the bytes on `main` still match the bytes we pin.
                if group != "pin_watch":
                    continue
                seen.add(path)
                if not upstream_oid or not pinned_oid:
                    # Nothing to compare (not LFS, or we pin no digest). Stay
                    # quiet rather than emit an unactionable fortnightly alert —
                    # but record that this pin went unchecked.
                    unverifiable.append(
                        {
                            "filename": path,
                            "reason": "no upstream LFS oid"
                            if not upstream_oid
                            else "no pinned sha256",
                        }
                    )
                    continue
                verified.append(path)
                if upstream_oid == pinned_oid:
                    continue
                stale_pin = True
            repo = src["api"].split("/models/")[1].split("/tree")[0]
            hits.append(
                {
                    "kind": "llm",
                    "group": group,
                    "label": src["label"],
                    "note": src.get("note", ""),
                    "repo": repo,
                    "filename": path,
                    "url": f"https://huggingface.co/{repo}/resolve/main/{path}",
                    "size": item.get("size"),
                    "stale_pin": stale_pin,
                    "pinned_sha256": pinned_oid if stale_pin else "",
                    "upstream_oid": upstream_oid if stale_pin else "",
                }
            )
        # A pin we watch that never appeared in the tree (empty tree, renamed
        # upstream, repo restructured) was also not compared.
        for filename in watching:
            if filename not in seen:
                unverifiable.append(
                    {"filename": filename, "reason": "not present in the upstream tree"}
                )

    if pin_stats is not None:
        pin_stats["verified"] = verified
        pin_stats["unverifiable"] = unverifiable
    return hits


def write_digest(
    path: Path,
    known: set[str],
    whisper_new: list,
    llm_new: list,
    pin_stats: dict | None = None,
) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        f"# Wayfinder model scout — {now}",
        "",
        "_Report only. Nothing here changes production._ Promotion is manual and",
        "reviewed — see `docs/MODEL-REFRESH-PLAYBOOK.md`.",
        "",
        f"Known catalog filenames: **{len(known)}**",
        "",
    ]
    if any("error" in x for x in whisper_new + llm_new):
        lines.append("## Errors (source unreachable — not a candidate)")
        for x in whisper_new + llm_new:
            if "error" in x:
                lines.append(f"- {x['error']}")
        lines.append("")

    wn = [x for x in whisper_new if "error" not in x]
    ln = [x for x in llm_new if "error" not in x]
    pinned = [x for x in ln if x.get("group") == "pin_watch"]
    discovered = [x for x in ln if x.get("group") != "pin_watch"]

    stats = pin_stats or {}
    verified = list(stats.get("verified") or [])
    unverifiable = list(stats.get("unverifiable") or [])
    checked = len(verified) + len(unverifiable)

    lines.append(f"## Pin watch — shipped models re-released upstream ({len(pinned)})")
    if not pinned:
        # "No stale pins" and "we never managed to compare anything" look
        # identical from the hit list. Claiming the former when the latter is
        # true is how a silently-broken watcher reads as a clean bill of health.
        if pin_stats is None:
            lines.append("_No stale pins reported. Verification coverage not recorded._")
        elif unverifiable:
            lines.append(
                f"_No stale pins among the pins we could check — but coverage is "
                f"incomplete: **{len(verified)} of {checked}** shipped pins were "
                f"verified against upstream; **{len(unverifiable)}** could not be "
                f"checked._"
            )
        else:
            lines.append(
                f"_None. All **{len(verified)}** shipped pins were verified against "
                "upstream and every one still matches the file we ship._"
            )
    else:
        lines.append("Our revision pin may be stale. Re-pin only after re-verifying the digest:")
        for x in pinned:
            mb = (x.get("size") or 0) / 1e6
            lines.append(
                f"- **{x.get('label', 'llm')}**: `{x['filename']}` (~{mb:.0f} MB) — {x['url']}"
            )
            if x.get("stale_pin"):
                lines.append(
                    "  - **Same filename, different bytes.** Upstream `main` oid "
                    f"`{x.get('upstream_oid', '')[:16]}…` != our pinned sha256 "
                    f"`{x.get('pinned_sha256', '')[:16]}…`. Our pinned *revision* is "
                    "untouched (so `model-pin-drift.yml` still passes and downloads "
                    "still work) — this is a freshness signal, not an outage."
                )
    if unverifiable:
        lines.append("")
        lines.append(
            f"**Could not verify {len(unverifiable)} shipped pin(s)** — absence of an "
            "alert above does NOT mean these still match:"
        )
        for entry in unverifiable:
            lines.append(f"- `{entry['filename']}` — {entry['reason']}")
    lines.append("")

    lines.append(f"## Discovery — candidate models to evaluate ({len(discovered)})")
    if not discovered:
        lines.append("_No new eligible candidates in the curated source list._")
    else:
        for x in discovered:
            mb = (x.get("size") or 0) / 1e6
            note = f" — {x['note']}" if x.get("note") else ""
            lines.append(
                f"- **{x.get('label', 'llm')}**: `{x['filename']}` (~{mb:.0f} MB){note}"
            )
            lines.append(f"  - {x['url']}")
    lines.append("")

    lines.append(f"## New Whisper.cpp weights ({len(wn)})")
    if not wn:
        lines.append("_None vs current catalog._")
    else:
        for x in wn:
            mb = (x.get("size") or 0) / 1e6
            lines.append(f"- `{x['filename']}` (~{mb:.0f} MB) — {x['url']}")

    lines.extend(
        [
            "",
            "## What happens next (all of it manual)",
            "A candidate is a *suggestion*. Before anything ships it must, per",
            "`docs/MODEL-REFRESH-PLAYBOOK.md`:",
            "1. pass a license check for CDN redistribution;",
            "2. beat the incumbent on the 11-cell `scripts/eval_tones.py` matrix",
            "   (5 tones x standard, 5 tones x strong, casual x caricature) on",
            "   quality *and* latency at Strong/Caricature;",
            "3. get pinned (revision + sha256 + exact byte count) at all catalog",
            "   sync points, reviewed by a human;",
            "4. have its R2 object uploaded **before** the catalog is republished.",
            "",
            "This scout will never do any of those steps for you.",
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
    ap.add_argument(
        "--json",
        action="store_true",
        help="Print ONLY JSON on stdout (digest goes to stderr) so CI can parse it",
    )
    ap.add_argument(
        "--notify",
        action="store_true",
        help="Run WAYFINDER_SCOUT_NOTIFY_CMD with digest path",
    )
    args = ap.parse_args()

    known = load_known_filenames()
    pins = load_pinned_digests()
    whisper_new = scout_whisper(known)
    pin_stats: dict = {}
    llm_new = scout_llm(known, pins=pins, pin_stats=pin_stats)
    text = write_digest(args.output, known, whisper_new, llm_new, pin_stats=pin_stats)

    # With --json, stdout is reserved for machine-readable output: a CI step
    # pipes it straight into `jq`, and a markdown digest mixed in would make it
    # unparseable. The human digest still goes somewhere (stderr + the file).
    digest_stream = sys.stderr if args.json else sys.stdout
    print(text, file=digest_stream)
    print(f"\n(Digest written to {args.output})", file=digest_stream)

    if args.json:
        errors = [x["error"] for x in whisper_new + llm_new if "error" in x]
        whisper_hits = [x for x in whisper_new if "error" not in x]
        llm_hits = [x for x in llm_new if "error" not in x]
        payload = {
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "known": len(known),
            # `candidates` is what a CI step branches on, so it counts only
            # *actionable* hits: a shipped pin that moved, or a new candidate
            # model to evaluate. It deliberately EXCLUDES `whisper_new` —
            # that is a near-constant ~22-entry list of whisper.cpp quantised
            # variants we choose not to ship, so counting it would make every
            # biweekly run notify and train the reader to ignore the issue.
            "candidates": len(llm_hits),
            "whisper_new_count": len(whisper_hits),
            "pin_watch_hits": len([x for x in llm_hits if x.get("group") == "pin_watch"]),
            "stale_pin_hits": len([x for x in llm_hits if x.get("stale_pin")]),
            # Additive coverage counters. NOT part of `candidates`: an
            # unverifiable pin is not actionable news, it is a gap in what this
            # run could see, and inflating `candidates` would open a GitHub
            # issue every time HuggingFace hiccups.
            "pins_verified": len(pin_stats.get("verified") or []),
            "pins_unverifiable": len(pin_stats.get("unverifiable") or []),
            "discovery_hits": len([x for x in llm_hits if x.get("group") != "pin_watch"]),
            "whisper_new": whisper_hits,
            "llm_new": llm_hits,
            "errors": errors,
            "digest": str(args.output),
        }
        json.dump(payload, sys.stdout, indent=2)
        sys.stdout.write("\n")

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
                print(f"Flag file: {flag}", file=digest_stream)
            elif flag.exists():
                flag.unlink()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
