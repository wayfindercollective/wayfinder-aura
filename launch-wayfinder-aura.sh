#!/bin/bash
# Launcher script for Wayfinder Aura
# Mac-native setup: uses venv-mac with Metal/pynput/pyperclip

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Pick the right venv: venv-mac (Apple Silicon) > venv-gpu (Linux/CUDA) > system Python
if [ -f "$SCRIPT_DIR/venv-mac/bin/python" ]; then
    PYTHON="$SCRIPT_DIR/venv-mac/bin/python"
elif [ -f "$SCRIPT_DIR/venv-gpu/bin/python" ]; then
    PYTHON="$SCRIPT_DIR/venv-gpu/bin/python"
else
    PYTHON="/usr/bin/python3"
fi

# GPU detection is handled in main.py via setup_gpu_environment()
exec "$PYTHON" "$SCRIPT_DIR/main.py" "$@"
