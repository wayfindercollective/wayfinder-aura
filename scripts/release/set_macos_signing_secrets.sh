#!/usr/bin/env bash
# Store the six Mac signing secrets so every release (nightly betas included)
# gets a signed, notarized DMG without anyone building it by hand.
#
#   scripts/release/set_macos_signing_secrets.sh ~/Desktop/DeveloperID.p12 ~/Downloads/AuthKey_ABC123XYZ.p8
#
# Run it yourself, in a terminal: it asks for the .p12 password, the API Key ID
# and the Issuer ID, and pipes every value straight into `gh secret set`, so
# nothing is printed, logged or written anywhere else. How to export the .p12
# and create the App Store Connect API key: packaging/macos/README.md
# ("Releasing a signed DMG"). Delete both files afterwards.
set -euo pipefail

P12="${1:?usage: $0 DeveloperID.p12 AuthKey_XXXXXXXXXX.p8}"
P8="${2:?usage: $0 DeveloperID.p12 AuthKey_XXXXXXXXXX.p8}"
REPO="wayfindercollective/wayfinder-aura"
[ -f "$P12" ] || { echo "no such file: $P12" >&2; exit 1; }
[ -f "$P8" ] || { echo "no such file: $P8" >&2; exit 1; }

# Releases belong to the work account (git and gh may default to another one).
export GH_TOKEN="${GH_TOKEN:-$(gh auth token --user wayfindercollective)}"

read -r -s -p "Password you gave the .p12 on export: " P12_PASSWORD; echo
openssl pkcs12 -in "$P12" -nokeys -passin "pass:$P12_PASSWORD" >/dev/null 2>&1 \
  || openssl pkcs12 -legacy -in "$P12" -nokeys -passin "pass:$P12_PASSWORD" >/dev/null 2>&1 \
  || { echo "That password does not open $P12." >&2; exit 1; }
default_id="$(basename "$P8" .p8)"; default_id="${default_id#AuthKey_}"
read -r -p "API Key ID [$default_id]: " KEY_ID; KEY_ID="${KEY_ID:-$default_id}"
read -r -p "Issuer ID (UUID above the key list): " ISSUER_ID
[[ "$KEY_ID" =~ ^[A-Z0-9]{10}$ ]] || { echo "Key ID should be 10 letters/digits" >&2; exit 1; }
[[ "$ISSUER_ID" =~ ^[0-9a-fA-F-]{36}$ ]] || { echo "Issuer ID should be a UUID" >&2; exit 1; }

set_secret() { gh secret set "$1" --repo "$REPO" >/dev/null; echo "  set $1"; }
base64 -i "$P12" | tr -d '\n' | set_secret MACOS_CERTIFICATE_P12_BASE64
printf '%s' "$P12_PASSWORD" | set_secret MACOS_CERTIFICATE_PASSWORD
openssl rand -base64 32 | tr -d '\n' | set_secret MACOS_KEYCHAIN_PASSWORD
base64 -i "$P8" | tr -d '\n' | set_secret APPLE_API_KEY_P8_BASE64
printf '%s' "$KEY_ID" | set_secret APPLE_API_KEY_ID
printf '%s' "$ISSUER_ID" | set_secret APPLE_API_ISSUER_ID
unset P12_PASSWORD

echo "Done. The next release tag builds, notarizes and attaches the Mac DMG itself."
echo "Now delete $P12 and $P8 (or keep them only in your password manager)."
