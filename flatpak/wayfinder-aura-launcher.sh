#!/bin/bash
# Flatpak launcher for Wayfinder Aura
# This script sets up the environment and launches the application

set -e

# Mark as running in Flatpak
export WAYFINDER_FLATPAK=1

# Set up Python path for package imports
export PYTHONPATH="/app/lib/wayfinder-aura:/app/lib/wayfinder-aura/src:${PYTHONPATH}"

# Use the bundled Xft-enabled Tcl/Tk 8.6.14 instead of the runtime's Tk (which is built
# WITHOUT Xft and can only render the X core "fixed" bitmap font — symbols show as \uXXXX).
# /app/lib must precede the runtime libs so python's _tkinter loads our libtcl8.6/libtk8.6,
# and TCL/TK_LIBRARY point Tcl/Tk at the matching bundled script libraries.
export LD_LIBRARY_PATH="/app/lib:${LD_LIBRARY_PATH}"
export TCL_LIBRARY="/app/lib/tcl8.6"
export TK_LIBRARY="/app/lib/tk8.6"

# Whisper models - prefer user's models, fallback to bundled
if [ -d "${HOME}/.local/share/whisper.cpp" ]; then
    export WHISPER_MODELS_DIR="${HOME}/.local/share/whisper.cpp"
elif [ -d "${HOME}/whisper.cpp/models" ]; then
    export WHISPER_MODELS_DIR="${HOME}/whisper.cpp/models"
else
    export WHISPER_MODELS_DIR="/app/share/whisper-models"
fi

# LLM models for post-processing - prefer user's models, fallback to bundled
if [ -d "${HOME}/.local/share/wayfinder-aura/llm-models" ]; then
    export LLM_MODELS_DIR="${HOME}/.local/share/wayfinder-aura/llm-models"
else
    export LLM_MODELS_DIR="/app/share/llm-models"
fi

# Whisper binary
export WHISPER_BIN="/app/bin/whisper-cli"

# XDG directories for config and cache
export XDG_CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
export XDG_CACHE_HOME="${XDG_CACHE_HOME:-$HOME/.cache}"

# Ensure config directory exists
mkdir -p "${XDG_CONFIG_HOME}/wayfinder-aura"
mkdir -p "${XDG_CACHE_HOME}/wayfinder-aura"

# GPU environment for Vulkan: do NOT pin VK_ICD_FILENAMES. Pinning it to only the
# radeon+intel ICDs hid the NVIDIA ICD and silently disabled GPU accel on NVIDIA
# hosts. The freedesktop GL extension (finish-args --device=dri) supplies every
# host ICD, so let the Vulkan loader auto-discover them (AMD/Deck path unaffected).

# Launch the application
exec python3 /app/lib/wayfinder-aura/main.py "$@"
