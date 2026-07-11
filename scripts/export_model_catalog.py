#!/usr/bin/env python3
"""Export built-in app catalogs to catalog/v1.json for CDN publish.

Usage:
  python3 scripts/export_model_catalog.py
  python3 scripts/export_model_catalog.py -o /tmp/catalog/v1.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "-o",
        "--output",
        type=Path,
        default=ROOT / "catalog" / "v1.json",
        help="Output path (default: catalog/v1.json in repo)",
    )
    args = ap.parse_args()

    import wayfinder_main as m
    from wayfinder.model_catalog import build_export_document

    doc = build_export_document(m.WHISPER_CPP_MODELS, m.LLM_GGUF_MODELS)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        f"Wrote {args.output} "
        f"(whisper={len(doc['whisper'])} llm={len(doc['llm'])} updated_at={doc['updated_at']})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
