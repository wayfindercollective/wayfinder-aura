#!/usr/bin/env python3
"""Export + upload catalog/v1.json to the Models CDN (R2 via Worker multipart).

Requires ADMIN_UPLOAD_SECRET and MODELS_CDN_BASE (same as model uploads).

Usage:
  export ADMIN_UPLOAD_SECRET=...
  export MODELS_CDN_BASE=https://wayfinder-models-cdn.peter-7b5.workers.dev
  python3 scripts/publish_model_catalog.py
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--file",
        type=Path,
        default=None,
        help="Existing catalog JSON (default: export from app then publish)",
    )
    ap.add_argument(
        "--skip-export",
        action="store_true",
        help="Do not re-export; use --file or catalog/v1.json",
    )
    args = ap.parse_args()

    out = args.file or (ROOT / "catalog" / "v1.json")
    if not args.skip_export or not out.exists():
        r = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "export_model_catalog.py"), "-o", str(out)],
            check=False,
        )
        if r.returncode != 0:
            return r.returncode

    if not out.exists():
        print(f"missing catalog file: {out}", file=sys.stderr)
        return 1

    secret = os.environ.get("ADMIN_UPLOAD_SECRET", "").strip()
    if not secret:
        sec_path = ROOT / "infra" / "models-cdn" / ".secrets" / "admin_upload_secret"
        if sec_path.exists():
            secret = sec_path.read_text().strip()
            os.environ["ADMIN_UPLOAD_SECRET"] = secret
    if not secret:
        print("ADMIN_UPLOAD_SECRET required", file=sys.stderr)
        return 1

    os.environ.setdefault(
        "MODELS_CDN_BASE",
        "https://wayfinder-models-cdn.peter-7b5.workers.dev",
    )
    print(f"↑ catalog/v1.json from {out}")
    subprocess.check_call(
        [
            sys.executable,
            str(ROOT / "scripts" / "r2_worker_multipart_upload.py"),
            "--key",
            "catalog/v1.json",
            "--file",
            str(out),
        ],
        env=os.environ.copy(),
    )
    base = os.environ["MODELS_CDN_BASE"].rstrip("/")
    print(f"Published. Clients fetch: {base}/v1/catalog")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
