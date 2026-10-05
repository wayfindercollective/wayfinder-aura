#!/usr/bin/env python3
"""Give each job a fresh virtualenv while retaining the host's pip cache."""
from __future__ import annotations

import argparse
import os
import platform
import subprocess
import sys
from pathlib import Path


def verify_macos_python() -> None:
    """Check the owner-provisioned native stack without opening a Tk window."""
    if sys.platform != "darwin" or platform.machine() != "arm64":
        raise SystemExit("Aura macOS CI requires a native ARM64 Python interpreter")
    if sys.version_info[:3] != (3, 12, 10):
        raise SystemExit("Aura macOS CI requires owner-provisioned Python 3.12.10")
    try:
        import _tkinter
        import tkinter

        tcl_version = str(tkinter.Tcl().call("info", "patchlevel"))
        parts = tuple(int(part) for part in tcl_version.split("."))
        if _tkinter.TK_VERSION != "8.6" or parts[:2] != (8, 6) or parts < (8, 6, 16):
            raise ValueError(f"Tk binding {_tkinter.TK_VERSION}, Tcl runtime {tcl_version}")
    except Exception as exc:
        raise SystemExit(
            "Aura macOS CI requires Tk 8.6 bindings and Tcl 8.6.16+ in the 8.6 line; "
            f"ask the runner owner to provision the Python.org 3.12.10 framework: {exc}"
        ) from exc
    print(f"Verified native Python 3.12.10, Tk binding 8.6, Tcl {tcl_version}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--macos", action="store_true", help="verify the pinned native Mac stack")
    args = parser.parse_args(argv)
    if args.macos:
        verify_macos_python()
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
