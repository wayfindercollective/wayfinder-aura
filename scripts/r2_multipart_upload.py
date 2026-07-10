#!/usr/bin/env python3
"""
Multipart upload to Cloudflare R2 via S3 API (bypasses wrangler 300 MiB limit).

Required env:
  R2_ACCESS_KEY_ID
  R2_SECRET_ACCESS_KEY
  R2_ENDPOINT   # https://<ACCOUNT_ID>.r2.cloudflarestorage.com
  R2_BUCKET     # default: wayfinder-aura-models

Usage:
  python3 scripts/r2_multipart_upload.py \\
    --key whisper/ggml-large-v3-turbo-q5_0.bin \\
    --file ~/.cache/wayfinder-aura-model-uploads/whisper/ggml-large-v3-turbo-q5_0.bin
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def main() -> int:
    try:
        import boto3
        from botocore.config import Config
    except ImportError:
        print("Install boto3: pip install boto3", file=sys.stderr)
        return 1

    p = argparse.ArgumentParser()
    p.add_argument("--key", required=True, help="Object key in the bucket")
    p.add_argument("--file", required=True, type=Path, help="Local file path")
    p.add_argument("--bucket", default=os.environ.get("R2_BUCKET", "wayfinder-aura-models"))
    p.add_argument(
        "--endpoint",
        default=os.environ.get(
            "R2_ENDPOINT",
            "https://<ACCOUNT_ID>.r2.cloudflarestorage.com",
        ),
    )
    args = p.parse_args()

    access = os.environ.get("R2_ACCESS_KEY_ID") or os.environ.get("AWS_ACCESS_KEY_ID")
    secret = os.environ.get("R2_SECRET_ACCESS_KEY") or os.environ.get("AWS_SECRET_ACCESS_KEY")
    if not access or not secret:
        print(
            "Missing R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY.\n"
            "Create at: Cloudflare Dashboard → R2 → Manage R2 API Tokens\n"
            "  Permission: Object Read & Write on bucket wayfinder-aura-models\n"
            "Then:\n"
            "  export R2_ACCESS_KEY_ID=...\n"
            "  export R2_SECRET_ACCESS_KEY=...\n"
            "  export R2_ENDPOINT=https://<ACCOUNT_ID>.r2.cloudflarestorage.com",
            file=sys.stderr,
        )
        return 2

    if not args.file.is_file():
        print(f"File not found: {args.file}", file=sys.stderr)
        return 1

    size = args.file.stat().st_size
    print(f"Uploading {args.file} ({size / 1e6:.1f} MB) → s3://{args.bucket}/{args.key}")

    client = boto3.client(
        "s3",
        endpoint_url=args.endpoint.rstrip("/"),
        aws_access_key_id=access,
        aws_secret_access_key=secret,
        region_name="auto",
        config=Config(signature_version="s3v4", retries={"max_attempts": 5}),
    )

    # Transfer manager handles multipart for large files
    from boto3.s3.transfer import TransferConfig, S3Transfer

    config = TransferConfig(
        multipart_threshold=64 * 1024 * 1024,
        multipart_chunksize=64 * 1024 * 1024,
        max_concurrency=4,
        use_threads=True,
    )
    transfer = S3Transfer(client, config)

    def progress(bytes_amount, _total=size, _acc=[0]):
        _acc[0] += bytes_amount
        if _acc[0] == bytes_amount or _acc[0] % (32 * 1024 * 1024) < bytes_amount:
            pct = 100.0 * _acc[0] / _total if _total else 0
            print(f"  … {_acc[0] / 1e6:.0f} / {_total / 1e6:.0f} MB ({pct:.0f}%)", flush=True)

    transfer.upload_file(
        str(args.file),
        args.bucket,
        args.key,
        callback=progress,
        extra_args={"ContentType": "application/octet-stream"},
    )
    print("OK", args.key)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
