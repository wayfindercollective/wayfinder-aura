#!/bin/bash
# Generate pip sources for Flatpak manifest
# 
# This script uses flatpak-pip-generator to create the JSON sources
# that Flatpak needs to install Python packages offline.
#
# Prerequisites:
#   pip install flatpak-pip-generator
#   # Or use the version from flatpak-builder-tools:
#   git clone https://github.com/nickssmith/flatpak-pip-generator.git
#   # Or the official one:
#   git clone https://github.com/nickssmith/flatpak-builder-tools.git

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

echo "Generating Python package sources for Flatpak..."
echo "Project directory: $PROJECT_DIR"

# Create requirements file for Flatpak (excluding system packages)
cat > "$SCRIPT_DIR/flatpak-requirements.txt" << 'EOF'
# Core dependencies for Flatpak build
customtkinter>=5.2.0
sounddevice>=0.5.0
numpy>=1.24.0
scipy>=1.10.0
pystray>=0.19.0
Pillow>=10.0.0
# Note: evdev is excluded - Flatpak uses portals instead of direct device access
# Note: dbus-python and PyGObject come from the SDK
EOF

# Check if flatpak-pip-generator is available
if command -v flatpak-pip-generator &> /dev/null; then
    echo "Using flatpak-pip-generator..."
    flatpak-pip-generator \
        --requirements-file="$SCRIPT_DIR/flatpak-requirements.txt" \
        --output="$SCRIPT_DIR/python-deps" \
        --runtime='org.freedesktop.Sdk//24.08'
    echo "Generated: $SCRIPT_DIR/python-deps.json"
elif [ -f "$HOME/flatpak-builder-tools/pip/flatpak-pip-generator.py" ]; then
    echo "Using flatpak-builder-tools..."
    python3 "$HOME/flatpak-builder-tools/pip/flatpak-pip-generator.py" \
        --requirements-file="$SCRIPT_DIR/flatpak-requirements.txt" \
        --output="$SCRIPT_DIR/python-deps"
    echo "Generated: $SCRIPT_DIR/python-deps.json"
else
    echo ""
    echo "ERROR: flatpak-pip-generator not found!"
    echo ""
    echo "Install it with one of these methods:"
    echo ""
    echo "  Option 1: pip install flatpak-pip-generator"
    echo ""
    echo "  Option 2: Clone the tools repo:"
    echo "    git clone https://github.com/nickssmith/flatpak-builder-tools.git ~/flatpak-builder-tools"
    echo ""
    echo "Then run this script again."
    exit 1
fi

echo ""
echo "Done! Now update the manifest to include python-deps.json"
echo "Replace the python-deps module with:"
echo ""
echo "  - name: python-deps"
echo "    buildsystem: simple"
echo "    build-commands:"
echo "      - pip3 install --verbose --exists-action=i --no-index --find-links=\"file://\${PWD}\" --prefix=\${FLATPAK_DEST} -r requirements.txt"
echo "    sources:"
echo "      - type: file"
echo "        path: flatpak-requirements.txt"
echo "        dest-filename: requirements.txt"
echo "      - python-deps.json"



