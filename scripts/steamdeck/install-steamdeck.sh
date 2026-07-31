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
#   ~/.local/bin/wayfinder-aura-show-or-start         (executable)
#   ~/.local/share/applications/wayfinder-aura.desktop
#   ~/.config/systemd/user/wayfinder-trigger.service
#   ~/.config/systemd/user/wayfinder-mode-supervisor.service
#   ~/.config/systemd/user/wayfinder-aura.service
#   ~/.config/systemd/user/wayfinder-aura-failed.service
#
# then reloads systemd and enables --now the three active services:
#   wayfinder-aura.service, wayfinder-trigger.service, wayfinder-mode-supervisor.service
# It also disables the superseded r4-f3-bridge.service if present (the README
# says never run both).
#
# The Flatpak is installed rootlessly from the latest GitHub release when it is
# not already present. No pacman, read-only-root toggle, compiler, Python
# package, ydotool, or input-group mutation is required.

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SYSTEMD_SRC="${SCRIPT_DIR}/systemd"

BIN_DIR="${HOME}/.local/bin"
APPLICATION_DIR="${HOME}/.local/share/applications"
UNIT_DIR="${HOME}/.config/systemd/user"
DROPIN_DIR="${UNIT_DIR}/wayfinder-aura.service.d"
ICON_DIR="${HOME}/.local/share/icons/hicolor/256x256/apps"
APP_ID="io.wayfindercollective.WayfinderAura"
MIN_FLATPAK_VERSION="1.1.6"
LATEST_FLATPAK_URL="https://github.com/wayfindercollective/wayfinder-aura/releases/latest/download/${APP_ID}.flatpak"
FLATPAK_BUNDLE=""
SKIP_FLATPAK=0
REFRESH_FLATPAK=0

usage() {
  cat <<EOF
Usage: $0 [--flatpak-bundle PATH | --refresh-flatpak | --skip-flatpak]

  --flatpak-bundle PATH  Install/update from an already-downloaded bundle
  --refresh-flatpak      Download and install the latest GitHub release
  --skip-flatpak         Install host helpers only (app must already be installed)
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --flatpak-bundle)
      [[ $# -ge 2 ]] || { echo "ERROR: --flatpak-bundle needs a path" >&2; exit 2; }
      FLATPAK_BUNDLE="$2"
      shift
      ;;
    --refresh-flatpak) REFRESH_FLATPAK=1 ;;
    --skip-flatpak) SKIP_FLATPAK=1 ;;
    --help|-h) usage; exit 0 ;;
    *) echo "ERROR: unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

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
require "${SCRIPT_DIR}/wayfinder-aura-show-or-start.py"
require "${SCRIPT_DIR}/wayfinder-aura.desktop"
require "${SYSTEMD_SRC}/wayfinder-trigger.service"
require "${SYSTEMD_SRC}/wayfinder-mode-supervisor.service"
require "${SYSTEMD_SRC}/wayfinder-aura.service"
require "${SYSTEMD_SRC}/wayfinder-aura-failed.service"
require "${SCRIPT_DIR}/../../assets/icon.png"

# --- 0. install the release Flatpak without touching SteamOS's rootfs -------
if ! command -v flatpak >/dev/null 2>&1; then
  echo "ERROR: flatpak is unavailable; stock SteamOS includes it" >&2
  exit 1
fi

flatpak_installed=0
if flatpak info --user "${APP_ID}" >/dev/null 2>&1; then
  flatpak_installed=1
fi

DOWNLOAD_DIR=""
cleanup() {
  if [[ -n "${DOWNLOAD_DIR}" && -d "${DOWNLOAD_DIR}" ]]; then
    rm -rf -- "${DOWNLOAD_DIR}"
  fi
}
trap cleanup EXIT

if [[ "${SKIP_FLATPAK}" -eq 0 ]] \
   && [[ -n "${FLATPAK_BUNDLE}" || "${REFRESH_FLATPAK}" -eq 1 || "${flatpak_installed}" -eq 0 ]]; then
  if [[ -z "${FLATPAK_BUNDLE}" ]]; then
    DOWNLOAD_DIR="$(mktemp -d -t wayfinder-aura-steamdeck.XXXXXX)"
    FLATPAK_BUNDLE="${DOWNLOAD_DIR}/${APP_ID}.flatpak"
    say "Downloading the latest Wayfinder Aura Flatpak from GitHub"
    if command -v curl >/dev/null 2>&1; then
      curl --fail --location --retry 3 --output "${FLATPAK_BUNDLE}" "${LATEST_FLATPAK_URL}"
    elif command -v wget >/dev/null 2>&1; then
      wget --tries=3 --output-document="${FLATPAK_BUNDLE}" "${LATEST_FLATPAK_URL}"
    else
      echo "ERROR: curl or wget is required to download the release Flatpak" >&2
      exit 1
    fi
  elif [[ ! -f "${FLATPAK_BUNDLE}" ]]; then
    echo "ERROR: Flatpak bundle not found: ${FLATPAK_BUNDLE}" >&2
    exit 1
  fi

  say "Ensuring the Flathub runtime source is configured"
  flatpak remote-add --user --if-not-exists flathub \
    https://flathub.org/repo/flathub.flatpakrepo
  say "Installing Wayfinder Aura for this user"
  flatpak install --user --noninteractive -y --or-update "${FLATPAK_BUNDLE}"
elif [[ "${flatpak_installed}" -eq 1 ]]; then
  say "Wayfinder Aura Flatpak is already installed"
elif [[ "${SKIP_FLATPAK}" -eq 1 ]]; then
  echo "ERROR: --skip-flatpak was used, but ${APP_ID} is not installed for this user" >&2
  exit 1
fi

flatpak info --user "${APP_ID}" >/dev/null
installed_version="$(flatpak info --user --show-version "${APP_ID}")"
oldest="$(printf '%s\n%s\n' "${MIN_FLATPAK_VERSION}" "${installed_version}" | sort -V | head -n 1)"
if [[ "${oldest}" != "${MIN_FLATPAK_VERSION}" ]]; then
  echo "ERROR: installed Wayfinder Aura ${installed_version} is older than the SteamOS-safe ${MIN_FLATPAK_VERSION}." >&2
  echo "       Publish/install the v${MIN_FLATPAK_VERSION} GitHub release before enabling Deck services." >&2
  exit 1
fi
say "Wayfinder Aura Flatpak ${installed_version} is SteamOS-ready"

# --- 1. daemon scripts -> ~/.local/bin (executable) -----------------------
say "Installing daemon scripts to ${BIN_DIR}"
install -Dm755 "${SCRIPT_DIR}/wayfinder-trigger-daemon.py"  "${BIN_DIR}/wayfinder-trigger-daemon.py"
install -Dm755 "${SCRIPT_DIR}/wayfinder-mode-supervisor.py" "${BIN_DIR}/wayfinder-mode-supervisor.py"
install -Dm755 "${SCRIPT_DIR}/wayfinder-aura-show-or-start.py" "${BIN_DIR}/wayfinder-aura-show-or-start"

say "Host trigger input backend: $("${BIN_DIR}/wayfinder-trigger-daemon.py" --print-input-backend)"

# Restore a hidden live window through the existing app socket. The old desktop entry only
# called `systemctl start`, which is a no-op while the service is already active.
say "Installing responsive taskbar entry to ${APPLICATION_DIR}"
desktop_tmp="$(mktemp)"
sed "s|@WAYFINDER_BIN_DIR@|${BIN_DIR}|g" \
  "${SCRIPT_DIR}/wayfinder-aura.desktop" > "${desktop_tmp}"
install -Dm644 "${desktop_tmp}" "${APPLICATION_DIR}/wayfinder-aura.desktop"
rm -f -- "${desktop_tmp}"
install -Dm644 "${SCRIPT_DIR}/../../assets/icon.png" \
  "${ICON_DIR}/${APP_ID}.png"

# --- 2. systemd user units -> ~/.config/systemd/user ----------------------
say "Installing systemd user units to ${UNIT_DIR}"
install -Dm644 "${SYSTEMD_SRC}/wayfinder-trigger.service"         "${UNIT_DIR}/wayfinder-trigger.service"
install -Dm644 "${SYSTEMD_SRC}/wayfinder-mode-supervisor.service" "${UNIT_DIR}/wayfinder-mode-supervisor.service"
install -Dm644 "${SYSTEMD_SRC}/wayfinder-aura.service"            "${UNIT_DIR}/wayfinder-aura.service"
install -Dm644 "${SYSTEMD_SRC}/wayfinder-aura-failed.service"     "${UNIT_DIR}/wayfinder-aura-failed.service"

# v1.1.4 and earlier used a drop-in over a source-build service. The service
# now launches Flatpak directly; remove only that obsolete installer-owned file.
rm -f -- "${DROPIN_DIR}/flatpak.conf"
rmdir --ignore-fail-on-non-empty "${DROPIN_DIR}" 2>/dev/null || true

# --- 3. reload systemd ----------------------------------------------------
say "Reloading systemd user manager"
systemctl --user daemon-reload
systemctl --user reset-failed "${ACTIVE_SERVICES[@]}" 2>/dev/null || true

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
  * The release Flatpak is installed. Launch/check it with:
      flatpak run io.wayfindercollective.WayfinderAura

  * Check service status:
      systemctl --user status wayfinder-aura.service wayfinder-trigger.service wayfinder-mode-supervisor.service

  * Watch the host-side logs:
      tail -f /tmp/wayfinder-trigger.log
      tail -f /tmp/wayfinder-mode-supervisor.log

  * Trigger dictation: bind R4 (or another back/custom button) to Right Joystick
    Click in Steam's Desktop Layout. In Game Mode, repeat it in each game's
    Controller Settings or apply a reusable layout template. The game may also
    receive its normal right-stick-click action.
    See scripts/steamdeck/README.md for the full trigger story.

  * Game Mode: dictation is stopped in Game Mode by default to free RAM. To keep
    it running in Game Mode:
      mkdir -p ~/.var/app/io.wayfindercollective.WayfinderAura/config/wayfinder-aura
      echo 1 > ~/.var/app/io.wayfindercollective.WayfinderAura/config/wayfinder-aura/game-mode-dictation

To remove everything: scripts/steamdeck/uninstall-steamdeck.sh
EOF
