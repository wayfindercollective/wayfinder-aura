#!/usr/bin/env bash
# Install (or update) the local Slack poller for Wayfinder Aura on this Mac.
#
# Copies FIXED versions of the notify script, prompts and poller from this
# checkout into ~/Library/Application Support/wayfinder-aura-slack (the poller
# never runs code it fetches), then schedules it every 2 minutes with launchd.
# Re-run after changing .github/scripts/slack-notify.sh or the prompts.
#
#   scripts/install_slack_local_poller.sh            install / update
#   scripts/install_slack_local_poller.sh --remove   stop and remove the job
#
# The Slack webhook URLs live in the login Keychain (you paste them; they are
# never shown):
#   security add-generic-password -U -a wayfinder-aura-slack -s SLACK_WEBHOOK_DEV -w
#   security add-generic-password -U -a wayfinder-aura-slack -s SLACK_WEBHOOK_PROD -w
# Then make the GitHub workflow stand down so posts are not doubled:
#   gh variable set SLACK_POSTER --body local --repo wayfindercollective/wayfinder-aura

set -euo pipefail

LABEL="io.wayfindercollective.aura-slack-poller"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
BASE="$HOME/Library/Application Support/wayfinder-aura-slack"
LOG="$HOME/Library/Logs/aura-slack-poller.log"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

unload() { launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true; }

if [ "${1:-}" = "--remove" ]; then
  unload
  rm -f "$PLIST"
  echo "Removed the launchd job. Data left in: $BASE (delete it to reset)."
  exit 0
fi

mkdir -p "$BASE/bin" "$BASE/prompts" "$(dirname "$PLIST")" "$(dirname "$LOG")"
install -m 0755 "$SRC/.github/scripts/slack-notify.sh" "$BASE/bin/slack-notify.sh"
install -m 0644 "$SRC/.github/scripts/extract-llm-summary.cjs" "$BASE/bin/extract-llm-summary.cjs"
install -m 0644 "$SRC/.github/prompts/"*.txt "$BASE/prompts/"
install -m 0755 "$SRC/scripts/slack_local_poller.py" "$BASE/bin/slack_local_poller.py"
chmod 700 "$BASE"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>$BASE/bin/slack_local_poller.py</string>
  </array>
  <key>StartInterval</key><integer>120</integer>
  <key>RunAtLoad</key><true/>
  <key>ProcessType</key><string>Background</string>
  <key>LowPriorityIO</key><true/>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict>
</plist>
EOF

unload
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "Installed $LABEL (every 2 minutes). Log: $LOG"
security find-generic-password -a wayfinder-aura-slack -s SLACK_WEBHOOK_DEV >/dev/null 2>&1 \
  && security find-generic-password -a wayfinder-aura-slack -s SLACK_WEBHOOK_PROD >/dev/null 2>&1 \
  && echo "Slack webhooks found in the Keychain." \
  || echo "Next: add the two Slack webhook URLs to the Keychain (see the top of this script)."
