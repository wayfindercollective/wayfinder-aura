#!/usr/bin/env python3
"""Reject hosted runners and unreviewable dynamic routing in Aura workflows."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ALLOWED = {
    frozenset(("self-hosted", "Linux", "X64", "aura-linux")),
    frozenset(("self-hosted", "Linux", "X64", "aura-build")),
    frozenset(("self-hosted", "Windows", "X64", "aura-windows")),
    frozenset(("self-hosted", "macOS", "ARM64", "aura-macos")),
}


def check_workflow(source: str) -> list[str]:
    errors = []
    for number, line in enumerate(source.splitlines(), 1):
        match = re.match(r"\s*runs-on:\s*(.*?)\s*(?:#.*)?$", line)
        if match:
            value = match[1]
            if not value.startswith("[") or not value.endswith("]"):
                errors.append(f"line {number}: runs-on must be an explicit Aura self-hosted label list")
                continue
            labels = frozenset(part.strip().strip("\"'") for part in value[1:-1].split(","))
            if labels not in ALLOWED:
                errors.append(f"line {number}: unapproved runner labels: {value}")
        # External reusable workflows can hide a hosted runs-on declaration.
        if re.match(r"^    uses:\s*", line) and not re.match(r"^    uses:\s*\./\.github/workflows/", line):
            errors.append(f"line {number}: reusable jobs must use a checked-in workflow")
    return errors


def main() -> int:
    errors = []
    for path in sorted((ROOT / ".github/workflows").glob("*.y*ml")):
        errors.extend(f"{path.name}: {error}" for error in check_workflow(path.read_text()))
    if errors:
        print("\n".join(errors))
        return 1
    print("RUNNER_POLICY_OK: every job explicitly uses Aura's own hardware")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
