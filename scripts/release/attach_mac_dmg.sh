#!/usr/bin/env bash
# Build, notarize and attach the Mac DMG for a release tag, on this Mac.
#
#   scripts/release/attach_mac_dmg.sh v1.2.0-beta.1
#
# On the Mac that holds the Developer ID certificate and the "wayfinder-aura"
# notarytool profile, mac_release_watcher.py runs this for every new release
# (docs/RELEASING.md); it also works by hand. The Release workflow does it
# instead if the six Mac signing secrets are ever set.
#
# It builds in a throwaway worktree at the tag, reuses this checkout's native
# whisper/llama binaries (build/macos-native/bin) when present, pauses the Mac
# CI queue while it runs (docs/CI.md) and uploads with --clobber, so a re-run
# replaces the asset. GH_TOKEN must belong to an account that can edit releases.
set -euo pipefail

TAG="${1:?usage: $0 vX.Y.Z[-beta.N]}"
REPO="wayfindercollective/wayfinder-aura"
IDENTITY="${MACOS_CODESIGN_IDENTITY:-Developer ID Application: Wayfinder Collective LLC (5JJQ8L5HHD)}"
PROFILE="${MACOS_NOTARY_PROFILE:-wayfinder-aura}"
# AURA_REPO: the checkout to build from (the installed watcher sets it).
ROOT="${AURA_REPO:-$(git -C "$(dirname "$0")" rev-parse --show-toplevel)}"
PYTHON="${AURA_MAC_PYTHON:-$ROOT/venv-mac/bin/python}"
[ -x "$PYTHON" ] || PYTHON="$HOME/wayfinder-aura/venv-mac/bin/python"
HOLD="$HOME/.cache/foxgrid/aura-mac-ci.hold"

gh release view "$TAG" --repo "$REPO" >/dev/null   # the release must exist first
git -C "$ROOT" fetch -q origin "refs/tags/$TAG:refs/tags/$TAG"

WORK="$(mktemp -d -t aura-dmg)"
OWN_HOLD=0  # never lift a hold someone else (Infra Mac, a DMG build) put there
cleanup() {
  if [ "$OWN_HOLD" = 1 ]; then rm -f "$HOLD"; fi
  git -C "$ROOT" worktree remove --force "$WORK/src" >/dev/null 2>&1 || true
  rm -rf "$WORK"
}
trap cleanup EXIT
trap 'exit 143' TERM INT HUP   # a timeout's SIGTERM still runs cleanup
mkdir -p "$(dirname "$HOLD")"
# The watcher takes (and always lifts) the hold itself and says so.
if [ -z "${AURA_HOLD_HELD:-}" ] && [ ! -e "$HOLD" ]; then touch "$HOLD"; OWN_HOLD=1; fi

git -C "$ROOT" worktree add -q --detach "$WORK/src" "$TAG"
cd "$WORK/src"
NATIVE_ARGS=()
for candidate in "$ROOT/build/macos-native/bin" "$HOME/wayfinder-aura/build/macos-native/bin"; do
  if [ -x "$candidate/whisper-server" ]; then
    mkdir -p build/macos-native && cp -Rp "$candidate" build/macos-native/bin
    NATIVE_ARGS=(--skip-native-build)
    break
  fi
done

MACOS_CODESIGN_IDENTITY="$IDENTITY" PYTHONPATH="$PWD/src:$PWD" \
  "$PYTHON" packaging/macos/build.py --notarize-profile "$PROFILE" "${NATIVE_ARGS[@]}"

DMG="dist/Wayfinder_Aura-${TAG#v}-macOS-$(uname -m).dmg"
[ -f "$DMG" ] || { echo "expected $DMG" >&2; ls dist >&2; exit 1; }
spctl --assess --type open --context context:primary-signature -v "$DMG"
xcrun stapler validate "$DMG"
gh release upload "$TAG" "$DMG" --repo "$REPO" --clobber
echo "Attached $(basename "$DMG") to $TAG"
