#!/usr/bin/env bash
# Build and smoke-test a release-grade Flatpak from the current checkout.

set -Eeuo pipefail

APP_ID="io.wayfindercollective.WayfinderAura"
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd -- "$SCRIPT_DIR/../.." && pwd)
OUTPUT="$REPO_ROOT/flatpak/$APP_ID.flatpak"
STATE_DIR="$REPO_ROOT/.flatpak-builder"
TAG=""

usage() {
  echo "Usage: $0 [--tag vX.Y.Z] [--output PATH] [--state-dir PATH]" >&2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --tag)
      [[ $# -ge 2 ]] || { usage; exit 2; }
      TAG=$2
      shift 2
      ;;
    --output)
      [[ $# -ge 2 ]] || { usage; exit 2; }
      OUTPUT=$2
      shift 2
      ;;
    --state-dir)
      [[ $# -ge 2 ]] || { usage; exit 2; }
      STATE_DIR=$2
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 2
      ;;
  esac
done

if [[ -n "$TAG" && ! "$TAG" =~ ^v[0-9][0-9A-Za-z._-]*$ ]]; then
  echo "Invalid release tag: $TAG" >&2
  exit 2
fi

for tool in flatpak flatpak-builder pulseaudio pactl xvfb-run python3; do
  command -v "$tool" >/dev/null || {
    echo "Missing required build tool: $tool" >&2
    exit 1
  }
done

mkdir -p -- "$(dirname -- "$OUTPUT")" "$STATE_DIR"
OUTPUT=$(realpath -m -- "$OUTPUT")
STATE_DIR=$(realpath -m -- "$STATE_DIR")

flatpak remote-add --user --if-not-exists \
  flathub https://flathub.org/repo/flathub.flatpakrepo
flatpak install --user --noninteractive -y flathub \
  org.kde.Platform//6.11 \
  org.kde.Sdk//6.11 \
  com.riverbankcomputing.PyQt.BaseApp//6.11

manifest="io.wayfindercollective.WayfinderAura.yml"
if [[ -n "$TAG" ]]; then
  python3 "$REPO_ROOT/flatpak/prepare-release-manifest.py" \
    --tag "$TAG" \
    --output "$REPO_ROOT/flatpak/release/io.wayfindercollective.WayfinderAura.yml"
  manifest="release/io.wayfindercollective.WayfinderAura.yml"
fi

(
  cd "$REPO_ROOT/flatpak"
  flatpak-builder --user --force-clean --ccache --jobs=2 \
    --state-dir="$STATE_DIR" --repo=repo build-dir "$manifest"
)

sink_module=""
source_module=""
test_logs=""

cleanup() {
  [[ -z "$source_module" ]] || pactl unload-module "$source_module" >/dev/null 2>&1 || true
  [[ -z "$sink_module" ]] || pactl unload-module "$sink_module" >/dev/null 2>&1 || true
  [[ -z "$test_logs" ]] || rm -rf -- "$test_logs"
}
trap cleanup EXIT

pulseaudio --check || pulseaudio --daemonize=yes --exit-idle-time=-1
sink_name="aura_ci_null_$$"
source_name="aura_ci_mic_$$"
sink_module=$(pactl load-module module-null-sink sink_name="$sink_name")
source_module=$(pactl load-module module-null-source source_name="$source_name")
test_logs=$(mktemp -d)

pactl set-default-sink "$sink_name"
pactl set-default-source "$source_name"

# Refresh the fixed local remote URL because mini-inf builds in a fresh
# worktree each time while retaining the expensive builder state separately.
flatpak remote-delete --user --force wayfinder-ci >/dev/null 2>&1 || true
flatpak remote-add --user --no-gpg-verify wayfinder-ci "$REPO_ROOT/flatpak/repo"
if flatpak info --user "$APP_ID" >/dev/null 2>&1; then
  flatpak update --user --noninteractive -y "$APP_ID"
else
  flatpak install --user --noninteractive -y wayfinder-ci "$APP_ID"
fi

# Do not export the host PULSE_SERVER path. Flatpak must inject its own
# sandbox-side socket path, which is the packaging boundary under test.
flatpak run "$APP_ID" --audio-output-self-test \
  | tee "$test_logs/audio-output.log"
grep -Fq "AUDIO_OUTPUT_SELF_TEST_OK" "$test_logs/audio-output.log"

flatpak run "$APP_ID" --audio-input-self-test \
  | tee "$test_logs/audio-input.log"
grep -Fq "AUDIO_INPUT_SELF_TEST_OK" "$test_logs/audio-input.log"

flatpak run "$APP_ID" --audio-processing-self-test \
  | tee "$test_logs/audio-processing.log"
grep -Fq "AUDIO_PROCESSING_SELF_TEST_OK" "$test_logs/audio-processing.log"

flatpak run "$APP_ID" --tls-self-test | tee "$test_logs/tls.log"
grep -Fq "TLS_SELF_TEST_OK" "$test_logs/tls.log"

xvfb-run -a flatpak run "$APP_ID" --ui-renderer-self-test \
  | tee "$test_logs/ui-renderer.log"
grep -Fq "UI_RENDERER_SELF_TEST_OK" "$test_logs/ui-renderer.log"

rm -f -- "$OUTPUT"
flatpak build-bundle "$REPO_ROOT/flatpak/repo" "$OUTPUT" \
  "$APP_ID" master \
  --runtime-repo=https://flathub.org/repo/flathub.flatpakrepo
test -s "$OUTPUT"
sha256sum "$OUTPUT"
