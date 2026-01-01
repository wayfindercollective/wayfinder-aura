#!/bin/bash
# Launcher script for Wayfinder Voice
# This handles various runtime configurations and provides a clean entry point

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Set FLEXIBLAS backend (fixes numpy on Fedora/Bazzite)
export FLEXIBLAS="${FLEXIBLAS:-OPENBLAS-OPENMP}"

# Force Vulkan to use discrete GPU if available (ggml_vulkan device selection)
# On systems with iGPU + dGPU, device 0 is often the slower iGPU
# Set GGML_VK_VISIBLE_DEVICES=1 to force the discrete GPU
# Users can override this by setting the variable before running
export GGML_VK_VISIBLE_DEVICES="${GGML_VK_VISIBLE_DEVICES:-1}"

# Fallback display if not set (helps with autostart)
if [ -z "$DISPLAY" ] && [ -z "$WAYLAND_DISPLAY" ]; then
    export DISPLAY="${DISPLAY:-:0}"
fi

# Use system Python explicitly (has tkinter and correct deps)
PYTHON="/usr/bin/python3"

# Run with Python (most reliable on Fedora-based systems)
# Note: Scaling handling is done in Python with cached values
exec "$PYTHON" "$SCRIPT_DIR/main.py" "$@"
