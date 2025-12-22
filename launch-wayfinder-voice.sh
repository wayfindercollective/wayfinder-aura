#!/bin/bash
# Launcher script for Wayfinder Voice
# This handles various runtime configurations and provides a clean entry point

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Set FLEXIBLAS backend (fixes numpy on Fedora/Bazzite)
export FLEXIBLAS="${FLEXIBLAS:-OPENBLAS-OPENMP}"

# Use system Python explicitly (has tkinter and correct deps)
PYTHON="/usr/bin/python3"

# Run with Python (most reliable on Fedora-based systems)
exec "$PYTHON" "$SCRIPT_DIR/main.py" "$@"
