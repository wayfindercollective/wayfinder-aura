#!/bin/bash
# Launcher script for Wayfinder Aura
# This handles various runtime configurations and provides a clean entry point

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

# Use system Python explicitly (has tkinter and correct deps)
PYTHON="/usr/bin/python3"

# Auto-detect optimal Vulkan device if not already set
# This handles systems with both integrated and discrete GPUs
if [ -z "$GGML_VK_VISIBLE_DEVICES" ]; then
    # Use Python to detect the optimal device
    OPTIMAL_DEVICE=$("$PYTHON" -c "
import sys
sys.path.insert(0, '$SCRIPT_DIR/src')
try:
    from wayfinder.utils.gpu import get_optimal_vulkan_device
    print(get_optimal_vulkan_device())
except:
    print('0')  # Default to device 0 if detection fails
" 2>/dev/null)
    
    if [ -n "$OPTIMAL_DEVICE" ] && [ "$OPTIMAL_DEVICE" != "0" ]; then
        export GGML_VK_VISIBLE_DEVICES="$OPTIMAL_DEVICE"
        echo "[GPU] Auto-selected Vulkan device $OPTIMAL_DEVICE (discrete GPU detected)"
    fi
fi

# Run with Python (most reliable on Fedora-based systems)
# Note: Scaling handling is done in Python with cached values
exec "$PYTHON" "$SCRIPT_DIR/main.py" "$@"
