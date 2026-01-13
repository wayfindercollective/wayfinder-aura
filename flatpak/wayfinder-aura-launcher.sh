#!/bin/bash
# Flatpak launcher for Wayfinder Aura
# This script sets up the environment and launches the application

set -e

# Mark as running in Flatpak
export WAYFINDER_FLATPAK=1

# Set up Python path for package imports
export PYTHONPATH="/app/lib/wayfinder-aura:/app/lib/wayfinder-aura/src:${PYTHONPATH}"

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

# GPU environment hints for Vulkan
export VK_ICD_FILENAMES="${VK_ICD_FILENAMES:-/usr/share/vulkan/icd.d/radeon_icd.x86_64.json:/usr/share/vulkan/icd.d/intel_icd.x86_64.json}"

# Launch the application
exec python3 /app/lib/wayfinder-aura/main.py "$@"
