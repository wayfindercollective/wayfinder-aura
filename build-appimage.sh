#!/bin/bash
# Compatibility wrapper for the maintained AppImage builder.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$SCRIPT_DIR/scripts/build-appimage.sh" "$@"
