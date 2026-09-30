#!/usr/bin/env python3
"""Installed OUTSIDE the checkout as a runner pre-job hook; reject fork code.

Updating this source does not update the installed hook. Install through the
host's administrator, never from a repository workflow.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

REPOSITORY = "wayfindercollective/wayfinder-aura"


def trusted_event(name: str, payload: dict) -> bool:
    if payload.get("repository", {}).get("full_name") != REPOSITORY:
        return False
    if name == "pull_request":
        return payload.get("pull_request", {}).get("head", {}).get("repo", {}).get("full_name") == REPOSITORY
    if name == "workflow_run":
        run = payload.get("workflow_run", {})
        return run.get("head_repository", {}).get("full_name") == REPOSITORY and run.get("event") in {
            "push", "pull_request", "workflow_dispatch", "schedule"
        }
    return name in {"push", "workflow_dispatch", "schedule"}


def main() -> int:
    try:
        payload = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
        allowed = os.environ.get("GITHUB_REPOSITORY") == REPOSITORY and trusted_event(
            os.environ.get("GITHUB_EVENT_NAME", ""), payload
        )
    except (KeyError, ValueError, OSError, AttributeError):
        allowed = False
    if not allowed:
        print("::error::This Aura runner accepts only trusted events from its own repository; fork code is refused before checkout.")
        return 1
    if sys.platform == "darwin":
        # This module is installed next to the hook, outside any job checkout.
        import importlib.util
        spec = importlib.util.spec_from_file_location("mac_capacity", Path(__file__).with_name("mac-runner-supervisor.py"))
        capacity = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(capacity)
        if capacity.headroom() < 40 * 1024**3:
            print("::error::Mac CI needs 40 GiB headroom before accepting a job.")
            return 1
    minimum = float(os.environ.get("AURA_MIN_FREE_GB", "2"))
    disk = Path(os.environ.get("RUNNER_TEMP", "."))
    if shutil.disk_usage(disk).free < minimum * 1024**3:
        print(f"::error::Runner needs at least {minimum:g} GiB free; no existing data was deleted.")
        return 1
    print("AURA_TRUSTED_RUNNER_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
