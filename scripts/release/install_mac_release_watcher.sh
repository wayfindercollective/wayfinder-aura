#!/usr/bin/env bash
# Install (or update) the Mac release watcher on the Mac that signs Aura.
#
# Copies FIXED versions of mac_release_watcher.py and attach_mac_dmg.sh from
# this checkout into ~/Library/Application Support/wayfinder-aura-release (the
# job never runs code it fetches), then checks hourly with launchd. Each check
# is one GitHub API call; a build (about once a day, after the nightly beta)
# runs at background priority on the efficiency cores, only when Fox Grid's
# Mac admission rule leaves room for it and the Mac CI queue is not held by
# someone else (mac_release_watcher.py).
# Re-run after changing either script.
#
#   scripts/release/install_mac_release_watcher.sh            install / update
#   scripts/release/install_mac_release_watcher.sh --remove   stop and remove
#
# Needs: the Developer ID certificate in the login Keychain, the notarytool
# keychain profile "wayfinder-aura", gh logged in as wayfindercollective, and
# the checkout at ~/wayfinder-aura with venv-mac (AURA_REPO overrides).

set -euo pipefail

LABEL="io.wayfindercollective.aura-mac-release"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
BASE="$HOME/Library/Application Support/wayfinder-aura-release"
LOG="$HOME/Library/Logs/aura-mac-release.log"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AURA_REPO="${AURA_REPO:-$HOME/wayfinder-aura}"
PYTHON="$AURA_REPO/venv-mac/bin/python"

unload() { launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true; }

if [ "${1:-}" = "--remove" ]; then
  unload
  rm -f "$PLIST"
  echo "Removed the launchd job. State left in: $BASE (delete it to reset)."
  exit 0
fi

[ -x "$PYTHON" ] || { echo "No Python at $PYTHON (set AURA_REPO)." >&2; exit 1; }
mkdir -p "$BASE/bin" "$(dirname "$PLIST")" "$(dirname "$LOG")"
install -m 0755 "$SRC/mac_release_watcher.py" "$BASE/bin/mac_release_watcher.py"
install -m 0755 "$SRC/attach_mac_dmg.sh" "$BASE/bin/attach_mac_dmg.sh"
chmod 700 "$BASE"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PYTHON</string>
    <string>$BASE/bin/mac_release_watcher.py</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>AURA_REPO</key><string>$AURA_REPO</string>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
  <key>StartInterval</key><integer>3600</integer>
  <key>RunAtLoad</key><true/>
  <key>ProcessType</key><string>Background</string>
  <key>LowPriorityIO</key><true/>
  <key>Nice</key><integer>10</integer>
  <!-- Time to stop a running build (60 s SIGTERM grace, then SIGKILL) before
       launchd kills the watcher; it releases the Mac CI hold last. -->
  <key>ExitTimeOut</key><integer>120</integer>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict>
</plist>
EOF

unload
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "Installed $LABEL (hourly check). Log: $LOG"
