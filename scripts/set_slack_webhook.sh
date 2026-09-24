#!/usr/bin/env bash
# Save a Slack webhook URL for the local Slack poller, straight from the
# clipboard, without ever showing it.
#
#   scripts/set_slack_webhook.sh dev     then copy the dev channel's URL in Slack
#   scripts/set_slack_webhook.sh prod    then copy the prod channel's URL
#
# Order doesn't matter: if the clipboard doesn't hold a webhook yet, it waits
# (up to 2 minutes) for you to copy one. Only https://hooks.slack.com/... is
# accepted; the clipboard is cleared afterwards.

set -euo pipefail

case "${1:-}" in
  dev)  SERVICE="SLACK_WEBHOOK_DEV" ;;
  prod) SERVICE="SLACK_WEBHOOK_PROD" ;;
  *) echo "usage: $0 dev|prod" >&2; exit 2 ;;
esac

is_webhook() { [[ "$1" =~ ^https://hooks\.slack\.com/services/[A-Za-z0-9/]{20,}$ ]]; }

url="$(pbpaste | tr -d '[:space:]')"
if ! is_webhook "$url"; then
  echo "Now copy the ${1} channel's webhook URL in Slack (Incoming Webhooks > Copy)."
  echo "Waiting for it on the clipboard..."
  for _ in $(seq 1 240); do
    sleep 0.5
    url="$(pbpaste | tr -d '[:space:]')"
    is_webhook "$url" && break
  done
fi
if ! is_webhook "$url"; then
  echo "No Slack webhook URL showed up on the clipboard; nothing saved." >&2
  exit 1
fi

security add-generic-password -U -a wayfinder-aura-slack -s "$SERVICE" -w "$url"
unset url
pbcopy < /dev/null
echo "Saved the ${1} webhook to the Keychain (clipboard cleared)."
