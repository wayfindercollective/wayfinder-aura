#!/usr/bin/env python3
"""Give each job a fresh virtualenv while retaining the host's pip cache."""
from __future__ import annotations

import os
import platform
import subprocess
import sys
from pathlib import Path


def main() -> None:
    if sys.version_info[:2] != (3, 12):
        raise SystemExit("Aura CI requires Python 3.12")
    root = Path(os.environ["RUNNER_TEMP"]) / "aura-venv"
    subprocess.run([sys.executable, "-m", "venv", str(root), "--clear"], check=True)
    scripts = root / ("Scripts" if os.name == "nt" else "bin")
    with open(os.environ["GITHUB_PATH"], "a", encoding="utf-8") as stream:
        stream.write(str(scripts) + "\n")
    with open(os.environ["GITHUB_ENV"], "a", encoding="utf-8") as stream:
        stream.write(f"VIRTUAL_ENV={root}\n")
    message = f"Aura CI: runner={os.environ.get('RUNNER_NAME')} host={platform.node()} platform={platform.system()} {platform.machine()} revision={os.environ.get('GITHUB_SHA')} Python={platform.python_version()}"
    print(message)
    with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as stream:
        stream.write(message + "\n")


if __name__ == "__main__":
    main()
