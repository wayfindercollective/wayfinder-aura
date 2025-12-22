#!/bin/bash
# Flatpak launcher for Wayfinder Voice

# Set up environment for Flatpak
export WAYFINDER_FLATPAK=1
export WHISPER_MODELS_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/whisper.cpp"

# Fallback to bundled model if user doesn't have one
if [ ! -d "$WHISPER_MODELS_DIR" ]; then
    export WHISPER_MODELS_DIR="/app/share/whisper-models"
fi

# Launch the application
exec python3 /app/lib/wayfinder-voice/main.py "$@"



