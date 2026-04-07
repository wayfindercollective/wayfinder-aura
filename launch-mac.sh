#!/bin/bash
# Wayfinder Aura - macOS launcher
# Uses the venv-mac virtual environment

cd "$(dirname "$0")"

# Activate the Mac venv and run
exec venv-mac/bin/python main.py "$@"
