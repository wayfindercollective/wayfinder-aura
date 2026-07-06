#!/usr/bin/env bash
#
# install-steamdeck.sh — install the Wayfinder Aura host-side infrastructure on
# a Steam Deck (systemd --user services + the evdev trigger daemon).
#
# This automates the manual copy documented in scripts/steamdeck/README.md
# ("Install map" + the "Enable with:" block). It installs, for the ACTIVE
# setup:
#
#   ~/.local/bin/wayfinder-trigger-daemon.py         (executable)
#   ~/.local/bin/wayfinder-mode-supervisor.py        (executable)
#   ~/.config/systemd/user/wayfinder-trigger.service
#   ~/.config/systemd/user/wayfinder-mode-supervisor.service
#   ~/.config/systemd/user/wayfinder-aura.service
#   ~/.config/systemd/user/wayfinder-aura.service.d/flatpak.conf
#   ~/.config/systemd/user/wayfinder-aura-failed.service
#
# then reloads systemd and enables --now the three active services:
#   wayfinder-aura.service, wayfinder-trigger.service, wayfinder-mode-supervisor.service
# It also disables the superseded r4-f3-bridge.service if present (the README
# says never run both).
#
# Notes:
#   * The .service units are copied VERBATIM. They hardcode /home/deck/... paths
#     (this is a Steam Deck installer; the Deck user is `deck`).
#   * wayfinder-aura.service's own ExecStart points at a launcher, but the
#     flatpak drop-in (flatpak.conf) overrides it to `flatpak run ...`, which is
#     what actually starts the app — so the flatpak build must be installed.
#   * Idempotent: safe to re-run; it overwrites the copied files in place.

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SYSTEMD_SRC="${SCRIPT_DIR}/systemd"

BIN_DIR="${HOME}/.local/bin"
UNIT_DIR="${HOME}/.config/systemd/user"
DROPIN_DIR="${UNIT_DIR}/wayfinder-aura.service.d"

ACTIVE_SERVICES=(
  wayfinder-aura.service
  wayfinder-trigger.service
  wayfinder-mode-supervisor.service
)

say() { printf '==> %s\n' "$*"; }

say "Wayfinder Aura — Steam Deck host-side install"
say "Source: ${SCRIPT_DIR}"

# --- sanity: make sure the source files are all present -------------------
require() {
  if [[ ! -f "$1" ]]; then
    echo "ERROR: expected source file not found: $1" >&2
    exit 1
  fi
}
require "${SCRIPT_DIR}/wayfinder-trigger-daemon.py"
require "${SCRIPT_DIR}/wayfinder-mode-supervisor.py"
require "${SYSTEMD_SRC}/wayfinder-trigger.service"
require "${SYSTEMD_SRC}/wayfinder-mode-supervisor.service"
require "${SYSTEMD_SRC}/wayfinder-aura.service"
require "${SYSTEMD_SRC}/wayfinder-aura.service.d-flatpak.conf"
require "${SYSTEMD_SRC}/wayfinder-aura-failed.service"

# --- 1. daemon scripts -> ~/.local/bin (executable) -----------------------
say "Installing daemon scripts to ${BIN_DIR}"
install -Dm755 "${SCRIPT_DIR}/wayfinder-trigger-daemon.py"  "${BIN_DIR}/wayfinder-trigger-daemon.py"
install -Dm755 "${SCRIPT_DIR}/wayfinder-mode-supervisor.py" "${BIN_DIR}/wayfinder-mode-supervisor.py"

# --- 2. systemd user units -> ~/.config/systemd/user ----------------------
say "Installing systemd user units to ${UNIT_DIR}"
install -Dm644 "${SYSTEMD_SRC}/wayfinder-trigger.service"         "${UNIT_DIR}/wayfinder-trigger.service"
install -Dm644 "${SYSTEMD_SRC}/wayfinder-mode-supervisor.service" "${UNIT_DIR}/wayfinder-mode-supervisor.service"
install -Dm644 "${SYSTEMD_SRC}/wayfinder-aura.service"            "${UNIT_DIR}/wayfinder-aura.service"
install -Dm644 "${SYSTEMD_SRC}/wayfinder-aura-failed.service"     "${UNIT_DIR}/wayfinder-aura-failed.service"

# The flatpak drop-in overrides wayfinder-aura.service's ExecStart to run the flatpak.
say "Installing flatpak drop-in to ${DROPIN_DIR}/flatpak.conf"
install -Dm644 "${SYSTEMD_SRC}/wayfinder-aura.service.d-flatpak.conf" "${DROPIN_DIR}/flatpak.conf"

# --- 3. reload systemd ----------------------------------------------------
say "Reloading systemd user manager"
systemctl --user daemon-reload

# --- 4. disable the superseded bridge (never run both) --------------------
say "Disabling superseded r4-f3-bridge.service (if present)"
systemctl --user disable --now r4-f3-bridge.service 2>/dev/null || true

# --- 5. enable + start the active services --------------------------------
say "Enabling and starting: ${ACTIVE_SERVICES[*]}"
systemctl --user enable --now "${ACTIVE_SERVICES[@]}"

# --- done -----------------------------------------------------------------
cat <<EOF

Done. Installed and enabled the Wayfinder Aura Steam Deck services.

Next steps:
  * Make sure the Flatpak app is installed:
      flatpak run io.github.wayfindercollective.WayfinderAura
    (the wayfinder-aura.service drop-in launches it via 'flatpak run').

  * Check service status:
      systemctl --user status wayfinder-aura.service wayfinder-trigger.service wayfinder-mode-supervisor.service

  * Watch the host-side logs:
      tail -f /tmp/wayfinder-trigger.log
      tail -f /tmp/wayfinder-mode-supervisor.log

  * Trigger dictation: bind a back button (R4) to Right Joystick Click in the
    Steam Desktop Layout, or use the programmed Corsair Scimitar side grid.
    See scripts/steamdeck/README.md for the full trigger story.

  * Game Mode: dictation is stopped in Game Mode by default to free RAM. To keep
    it running in Game Mode:
      echo 1 > ~/.config/wayfinder-aura/game-mode-dictation

To remove everything: scripts/steamdeck/uninstall-steamdeck.sh
EOF
