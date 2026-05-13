#!/bin/bash
# Wayfinder Aura launcher — Steam Deck (SteamOS, system Python + systemd --user)
#
# This is the launcher invoked by the systemd user unit on SteamOS. It is the
# single source of truth for the Deck dev-path. Symlinked from
# ~/.local/bin/wayfinder-aura-launcher (which the systemd unit's ExecStart points at).
#
# Two distinct deploy targets exist for this app:
#   - flatpak/wayfinder-aura-launcher.sh  (v2 target — Flatpak runtime, isolated)
#   - scripts/wayfinder-aura-launcher-steamdeck.sh  (this file — dev-path, system Python)
#
# Background and history of the issues this script works around live in
# STEAMDECK-INSTALL-LOG.md at the repo root.

set -u

cd /home/deck/dev/wayfinder-aura
[ -z "${DISPLAY:-}" ] && export DISPLAY=:0

# SteamOS lacks AppIndicator3/AyatanaAppIndicator3 typelibs by default (and a SteamOS
# major update can wipe the pacman package that provides them). The gtk backend uses
# Gtk.StatusIcon via GdkPixbuf and renders correctly in Plasma without those typelibs.
export PYSTRAY_BACKEND=gtk

# PortAudio's JACK host API chokes inside a systemd user service on PipeWire/SteamOS
# ("PaErrorCode -9999 JACK error -1"). Tell libjack not to autostart a server so PortAudio
# falls through to ALSA/Pulse cleanly instead of failing the whole stream open.
export JACK_NO_START_SERVER=1
export JACK_NO_AUDIO_RESERVATION=1

# --- Venv health check ---------------------------------------------------------
# SteamOS atomic image updates can swap /usr/bin/python3 to a new minor version
# (e.g. 3.13 -> 3.14), which leaves .cpython-313-*.so files in site-packages
# unloadable. Even within the same Python minor, a host package rev can break ABI
# for venv packages that link against it (we run with --system-site-packages, so
# the host's site-packages are part of our import surface).
#
# Strategy: smoke-test critical imports. If they fail, capture pre-rebuild state
# to /tmp/wfa-prerebuild-<ts>.log (so we have evidence of *what* broke for future
# diagnosis), then rebuild from a SteamOS-safe filtered requirements file.

SMOKE='import customtkinter, pystray, PIL, numpy'
SMOKE_OUT=$(mktemp /tmp/wfa-smoke-XXXXXX.log)

capture_prerebuild_state() {
    # Dumps everything we'd want to inspect post-mortem before rm -rf'ing the venv.
    # Cheap (<100ms typical); never fails the launcher even if a subcommand errors.
    local out="$1"
    local smoke_log="$2"
    {
        echo "=== Wayfinder Aura pre-rebuild state — $(date -Is) ==="
        echo
        echo "--- Smoke-test stderr (the actual failure) ---"
        cat "$smoke_log" 2>/dev/null || echo "(no smoke log)"
        echo
        echo "--- System python ---"
        /usr/bin/python3 --version 2>&1
        readlink -f /usr/bin/python3 2>&1
        echo
        echo "--- Venv python ---"
        ./.venv/bin/python --version 2>&1
        readlink -f ./.venv/bin/python 2>&1
        echo
        echo "--- pyvenv.cfg ---"
        cat ./.venv/pyvenv.cfg 2>&1
        echo
        echo "--- Venv sys.path ---"
        ./.venv/bin/python -c 'import sys; print("\n".join(sys.path))' 2>&1
        echo
        echo "--- .so files in venv (recent first) ---"
        find ./.venv -name '*.so' -printf '%T@ %TY-%Tm-%Td %p\n' 2>/dev/null \
            | sort -rn | head -30 | cut -d' ' -f2-
        echo
        echo "--- Pacman packages we depend on ---"
        pacman -Q libayatana-appindicator ydotool xdotool python-evdev 2>&1
        echo
        echo "--- OS / kernel ---"
        grep -E '^VERSION|^BUILD' /etc/os-release 2>&1
        uname -r
        echo
        echo "--- Boot history (last 5) ---"
        last -x reboot 2>&1 | head -5
    } > "$out" 2>&1
}

if ! ./.venv/bin/python -c "$SMOKE" >"$SMOKE_OUT" 2>&1; then
    STATE_LOG="/tmp/wfa-prerebuild-$(date +%Y%m%d-%H%M%S).log"
    capture_prerebuild_state "$STATE_LOG" "$SMOKE_OUT"

    /usr/bin/notify-send -u normal -i view-refresh "Wayfinder Aura" \
        "Rebuilding venv (smoke imports failed). Pre-rebuild state captured to $STATE_LOG. This will take ~1 minute..."

    rm -rf .venv
    /usr/bin/python3 -m venv --system-site-packages .venv

    # SteamOS rootfs has no gcc/kernel-headers, so source-build-only deps must be
    # skipped here. This filter is belt-and-suspenders against requirements.txt
    # regressions; the canonical-safe install set lives in requirements.txt itself
    # (with the previously-toxic deps moved to requirements-optional.txt).
    #   evdev: satisfied by pacman's python-evdev via --system-site-packages.
    #   llama-cpp-python: unused on the Deck (whisper.cpp handles transcription).
    SKIP_RE='^(evdev|llama-cpp-python)'
    grep -vE "$SKIP_RE" requirements.txt > /tmp/wfa-req.txt

    if ! ./.venv/bin/pip install --quiet -r /tmp/wfa-req.txt; then
        /usr/bin/notify-send -u critical -i dialog-error "Wayfinder Aura" \
            "Venv rebuild failed — see /tmp/wfa-stderr.log and $STATE_LOG. Run: cd ~/dev/wayfinder-aura && rm -rf .venv && python3 -m venv --system-site-packages .venv && grep -vE '$SKIP_RE' requirements.txt > /tmp/wfa-req.txt && .venv/bin/pip install -r /tmp/wfa-req.txt"
        exit 1
    fi
    /usr/bin/notify-send -u normal -i emblem-default "Wayfinder Aura" "Venv rebuilt successfully."
fi
rm -f "$SMOKE_OUT"

# --- Injection-tool health check ----------------------------------------------
# SteamOS updates can wipe pacman-installed ydotool, and we can't rule out xdotool
# getting dropped from a future base image either. If neither tool is reachable
# the app will record + transcribe fine, but produced text will only show up in
# the UI panel — not paste into the focused field. Warn loudly instead of
# failing silently.
if ! command -v xdotool >/dev/null 2>&1 && ! command -v ydotool >/dev/null 2>&1; then
    /usr/bin/notify-send -u critical -i dialog-error "Wayfinder Aura" \
        "No text injection tool found (xdotool/ydotool missing). Transcriptions will show in the UI but won't paste. Install: sudo pacman -S xdotool"
fi

exec ./.venv/bin/python main.py
