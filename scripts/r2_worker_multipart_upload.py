#!/usr/bin/env python3
"""
Upload large model files to R2 via the models-cdn Worker multipart admin API.

Uses Cloudflare skill pattern: Worker R2 binding createMultipartUpload / uploadPart /
complete — avoids wrangler 300 MiB and REST 413 limits.

Env:
  MODELS_CDN_BASE          default https://wayfinder-models-cdn.peter-7b5.workers.dev
  ADMIN_UPLOAD_SECRET      must match Worker secret ADMIN_UPLOAD_SECRET
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# Cloudflare Workers free/paid request body limits — stay under ~90 MiB.
PART_SIZE = 90 * 1024 * 1024


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--key", required=True)
    p.add_argument("--file", required=True, type=Path)
    p.add_argument(
        "--base",
        default=os.environ.get(
            "MODELS_CDN_BASE", "https://wayfinder-models-cdn.peter-7b5.workers.dev"
        ).rstrip("/"),
    )
    p.add_argument(
        "--secret",
        default=os.environ.get("ADMIN_UPLOAD_SECRET", ""),
        help="X-Admin-Upload-Secret (or set ADMIN_UPLOAD_SECRET)",
    )
    args = p.parse_args()

    if not args.secret:
        print("Set ADMIN_UPLOAD_SECRET", file=sys.stderr)
        return 2
    if not args.file.is_file():
        print(f"missing file {args.file}", file=sys.stderr)
        return 1

    size = args.file.stat().st_size
    key_q = urllib.parse.quote(args.key, safe="")
    headers = {
        "X-Admin-Upload-Secret": args.secret,
        # workers.dev bot fight can 1010 default Python-urllib UA
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Wayfinder-Aura-Admin/1.0",
        "Accept": "application/json,*/*",
    }

    def call(method: str, path: str, data: bytes | None = None, content_type: str | None = None):
        req = urllib.request.Request(f"{args.base}{path}", data=data, method=method)
        for hk, hv in headers.items():
            req.add_header(hk, hv)
        if content_type:
            req.add_header("Content-Type", content_type)
        if data is not None:
            req.add_header("Content-Length", str(len(data)))
        try:
            with urllib.request.urlopen(req, timeout=600) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            body = e.read().decode()
            raise RuntimeError(f"HTTP {e.code} {path}: {body[:400]}") from e

    print(f"Init multipart {args.key} ({size / 1e6:.1f} MB)…", flush=True)
    init = call("POST", f"/admin/multipart/init?key={key_q}")
    upload_id = init["uploadId"]
    upload_id_q = urllib.parse.quote(upload_id, safe="")
    print(f"  uploadId={upload_id[:16]}…", flush=True)

    parts: list[dict] = []
    part_number = 1
    sent = 0
    try:
        with open(args.file, "rb") as f:
            while True:
                chunk = f.read(PART_SIZE)
                if not chunk:
                    break
                print(
                    f"  part {part_number}: {len(chunk) / 1e6:.1f} MB "
                    f"({sent / 1e6:.0f}/{size / 1e6:.0f})",
                    flush=True,
                )
                res = call(
                    "PUT",
                    f"/admin/multipart/part?key={key_q}&uploadId={upload_id_q}"
                    f"&partNumber={part_number}",
                    data=chunk,
                    content_type="application/octet-stream",
                )
                parts.append(
                    {"partNumber": res["partNumber"], "etag": res["etag"]}
                )
                sent += len(chunk)
                part_number += 1

        print("Completing…", flush=True)
        done = call(
            "POST",
            f"/admin/multipart/complete?key={key_q}&uploadId={upload_id_q}",
            data=json.dumps({"parts": parts}).encode(),
            content_type="application/json",
        )
        print("OK", json.dumps(done), flush=True)
        return 0
    except Exception:
        try:
            call(
                "POST",
                f"/admin/multipart/abort?key={key_q}&uploadId={upload_id_q}",
            )
        except Exception:
            pass
        raise


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as e:
        print(f"FAIL: {e}", file=sys.stderr)
        raise SystemExit(1)
