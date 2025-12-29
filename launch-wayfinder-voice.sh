#!/bin/bash
# Launcher script for Wayfinder Voice
# This handles various runtime configurations and provides a clean entry point

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Set FLEXIBLAS backend (fixes numpy on Fedora/Bazzite)
export FLEXIBLAS="${FLEXIBLAS:-OPENBLAS-OPENMP}"

# Wait for display to be ready (helps with autostart timing)
# This prevents Tk scaling issues when display isn't fully initialized
if [ -z "$DISPLAY" ] && [ -z "$WAYLAND_DISPLAY" ]; then
    # No display yet - wait a bit for desktop to initialize
    sleep 3
fi

# Ensure Tk can find a valid scaling value
# This prevents "-NaN" scaling errors on autostart
export TK_SCALING="${TK_SCALING:-1.0}"

# Fallback: if still no display after waiting, try common values
if [ -z "$DISPLAY" ]; then
    export DISPLAY="${DISPLAY:-:0}"
fi

# Use system Python explicitly (has tkinter and correct deps)
PYTHON="/usr/bin/python3"

# Run with Python (most reliable on Fedora-based systems)
exec "$PYTHON" "$SCRIPT_DIR/main.py" "$@"
