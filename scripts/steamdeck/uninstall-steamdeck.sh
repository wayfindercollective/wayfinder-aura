#!/usr/bin/env bash
#
# uninstall-steamdeck.sh — reverse install-steamdeck.sh.
#
# Stops and disables the Wayfinder Aura host-side systemd --user services and
# removes the files that install-steamdeck.sh copied. It does NOT touch your
# user data (config, cache, downloaded models) — see the reminder at the end.
#
# Idempotent: safe to re-run; missing units/files are ignored.

set -euo pipefail

BIN_DIR="${HOME}/.local/bin"
UNIT_DIR="${HOME}/.config/systemd/user"
DROPIN_DIR="${UNIT_DIR}/wayfinder-aura.service.d"

ACTIVE_SERVICES=(
  wayfinder-aura.service
  wayfinder-trigger.service
  wayfinder-mode-supervisor.service
)

# Files this installer creates, so we only remove what we put there.
INSTALLED_FILES=(
  "${BIN_DIR}/wayfinder-trigger-daemon.py"
  "${BIN_DIR}/wayfinder-mode-supervisor.py"
  "${UNIT_DIR}/wayfinder-trigger.service"
  "${UNIT_DIR}/wayfinder-mode-supervisor.service"
  "${UNIT_DIR}/wayfinder-aura.service"
  "${UNIT_DIR}/wayfinder-aura-failed.service"
  "${DROPIN_DIR}/flatpak.conf"
)

say() { printf '==> %s\n' "$*"; }

say "Wayfinder Aura — Steam Deck host-side uninstall"

# --- 1. stop + disable the services ---------------------------------------
say "Stopping and disabling: ${ACTIVE_SERVICES[*]}"
systemctl --user disable --now "${ACTIVE_SERVICES[@]}" 2>/dev/null || true

# Also clear the superseded bridge, in case it was left enabled.
systemctl --user disable --now r4-f3-bridge.service 2>/dev/null || true

# --- 2. remove copied files -----------------------------------------------
say "Removing installed files"
for f in "${INSTALLED_FILES[@]}"; do
  if [[ -e "$f" ]]; then
    rm -f "$f"
    printf '    removed %s\n' "$f"
  fi
done

# Remove the drop-in dir if it's now empty.
if [[ -d "${DROPIN_DIR}" ]]; then
  rmdir --ignore-fail-on-non-empty "${DROPIN_DIR}" 2>/dev/null || true
fi

# --- 3. reload systemd ----------------------------------------------------
say "Reloading systemd user manager"
systemctl --user daemon-reload
systemctl --user reset-failed 2>/dev/null || true

# --- done -----------------------------------------------------------------
cat <<EOF

Done. The Wayfinder Aura Steam Deck services are stopped, disabled, and removed.

Your user data was LEFT IN PLACE (settings, license, cache, downloaded models):
  ~/.config/wayfinder-aura        (config.json, license.json)
  ~/.cache/wayfinder-aura         (activity.log and other caches)
  ~/.local/share/wayfinder-aura   (downloaded LLM models, etc.)

To remove those too:
  rm -rf ~/.config/wayfinder-aura ~/.cache/wayfinder-aura ~/.local/share/wayfinder-aura

The Flatpak app itself (if installed) is separate. To remove it:
  flatpak uninstall io.github.wayfindercollective.WayfinderAura
EOF
