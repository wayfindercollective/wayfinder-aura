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
#
# Crash-restart loop: PortAudio can hard-abort the whole process (SIGABRT — an
# uncatchable C assertion, PaUnixMutex_Terminate) when the selected mic's PipeWire
# node vanishes mid-recording. That left the app dead AND the overlay subprocess
# orphaned and hung. Relaunch on any non-zero exit so an audio glitch becomes a
# ~1s blip instead of a dead app. A clean quit (exit 0) ends the loop; a tight
# crash-loop (5 crashes in <60s) bails out so we don't spin on a broken install.
crash_count=0
window_start=$SECONDS
while true; do
    "$PYTHON" "$SCRIPT_DIR/main.py" "$@"
    code=$?
    [ "$code" -eq 0 ] && break   # clean quit — stop relaunching

    # Crashed: reap any orphaned overlay so the relaunch starts from a clean slate.
    pkill -9 -f "overlay.py" 2>/dev/null

    # Reset the crash window after 60 quiet seconds.
    if [ $((SECONDS - window_start)) -gt 60 ]; then
        crash_count=0
        window_start=$SECONDS
    fi
    crash_count=$((crash_count + 1))
    if [ "$crash_count" -ge 5 ]; then
        echo "[launcher] Wayfinder crashed $crash_count times in <60s (last exit $code) — not restarting." >&2
        exit "$code"
    fi
    echo "[launcher] Wayfinder exited with code $code — restarting (#$crash_count)…" >&2
    sleep 1
done
