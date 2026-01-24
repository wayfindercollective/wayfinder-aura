#!/bin/bash
# Launcher script for Wayfinder Aura
# Simplified - GPU detection is now handled in Python (main.py)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Set FLEXIBLAS backend (fixes numpy on Fedora/Bazzite)
export FLEXIBLAS="${FLEXIBLAS:-OPENBLAS-OPENMP}"

# Auto-detect AMD RDNA 4 GPUs (RX 9000 series) and enable Vulkan for Ollama
# RDNA 4 (gfx12) isn't fully supported by ROCm in Ollama yet, but Vulkan works great
if [ -z "$OLLAMA_VULKAN" ]; then
    if grep -q "gfx_target_version 120" /sys/class/kfd/kfd/topology/nodes/*/properties 2>/dev/null; then
        export OLLAMA_VULKAN=1
        export HSA_OVERRIDE_GFX_VERSION="12.0.0"
        echo "[GPU] Detected RDNA 4 GPU - enabling Vulkan backend for Ollama"
    fi
fi

# Fallback display if not set (helps with autostart)
if [ -z "$DISPLAY" ] && [ -z "$WAYLAND_DISPLAY" ]; then
    export DISPLAY="${DISPLAY:-:0}"
fi

# Use the virtual environment Python if available (has all deps including groq)
# Falls back to system Python if venv not found
if [ -f "$SCRIPT_DIR/venv-gpu/bin/python" ]; then
    PYTHON="$SCRIPT_DIR/venv-gpu/bin/python"
else
    PYTHON="/usr/bin/python3"
fi

# GPU detection is now handled in main.py via setup_gpu_environment()
# No need for bash-based detection anymore - simpler and more reliable!

exec "$PYTHON" "$SCRIPT_DIR/main.py" "$@"
