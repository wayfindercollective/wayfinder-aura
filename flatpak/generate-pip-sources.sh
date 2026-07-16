#!/bin/bash
# Generate pip sources for Flatpak manifest
# 
# This script uses flatpak-pip-generator to create the JSON sources
# that Flatpak needs to install Python packages offline.
#
# Prerequisites:
#   python -m pip install flatpak-pip-generator
#   # Or use the version from flatpak-builder-tools:
#   git clone https://github.com/flatpak/flatpak-builder-tools.git

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

echo "Generating Python package sources for Flatpak..."
echo "Project directory: $PROJECT_DIR"

# Create requirements file for Flatpak (Linux runtime dependencies only).
# Exclude PyQt6: provided by com.riverbankcomputing.PyQt.BaseApp.
# evdev is not listed directly: the sandbox cannot read /dev/input and hotkeys
# use portal/socket. It may still be generated as pynput's Linux dependency.
# Exclude pyautogui: macOS-only dependency.
cat > "$SCRIPT_DIR/flatpak-requirements.txt" << 'EOF'
# Core dependencies for Flatpak build
# Pin CustomTkinter below 6.x until the app is validated against the major bump.
customtkinter>=5.2.0,<6
requests>=2.28.0
cryptography>=41.0.0
sounddevice>=0.5.0
numpy>=1.24.0
scipy>=1.10.0
pynput>=1.7.6
pystray>=0.19.0
Pillow>=10.0.0
openai>=1.0.0
groq>=0.4.0
EOF

# Platform wheels for compiled packages (numpy/scipy/Pillow/cryptography/…).
# Flathub prefers sdists; full offline sdist builds need maturin + OpenBLAS and
# are tracked as a follow-up. Owner brief must request a temporary wheels
# exception for these packages on first submission.
PREFER_WHEELS="numpy,scipy,cryptography,Pillow,cffi,jiter,pydantic-core,pydantic_core"
if python3 -c "import flatpak_pip_generator" &> /dev/null; then
    echo "Using python3 -m flatpak_pip_generator (hybrid sdist + rust wheels)..."
    python3 -m flatpak_pip_generator \
        --requirements-file="$SCRIPT_DIR/flatpak-requirements.txt" \
        --output="$SCRIPT_DIR/python-deps" \
        --runtime='org.kde.Sdk//6.10' \
        --prefer-wheels="$PREFER_WHEELS"
    echo "Generated: $SCRIPT_DIR/python-deps.json"
elif command -v flatpak_pip_generator &> /dev/null; then
    echo "Using flatpak_pip_generator (hybrid sdist + rust wheels)..."
    flatpak_pip_generator \
        --requirements-file="$SCRIPT_DIR/flatpak-requirements.txt" \
        --output="$SCRIPT_DIR/python-deps" \
        --runtime='org.kde.Sdk//6.10' \
        --prefer-wheels="$PREFER_WHEELS"
    echo "Generated: $SCRIPT_DIR/python-deps.json"
elif command -v flatpak-pip-generator &> /dev/null; then
    echo "Using flatpak-pip-generator (hybrid sdist + rust wheels)..."
    flatpak-pip-generator \
        --requirements-file="$SCRIPT_DIR/flatpak-requirements.txt" \
        --output="$SCRIPT_DIR/python-deps" \
        --runtime='org.kde.Sdk//6.10' \
        --prefer-wheels="$PREFER_WHEELS"
    echo "Generated: $SCRIPT_DIR/python-deps.json"
elif [ -f "$HOME/flatpak-builder-tools/pip/flatpak-pip-generator.py" ]; then
    echo "Using flatpak-builder-tools (hybrid sdist + rust wheels)..."
    python3 "$HOME/flatpak-builder-tools/pip/flatpak-pip-generator.py" \
        --requirements-file="$SCRIPT_DIR/flatpak-requirements.txt" \
        --output="$SCRIPT_DIR/python-deps" \
        --runtime='org.kde.Sdk//6.10' \
        --prefer-wheels="$PREFER_WHEELS"
    echo "Generated: $SCRIPT_DIR/python-deps.json"
else
    echo ""
    echo "ERROR: flatpak-pip-generator not found!"
    echo ""
    echo "Install it with one of these methods:"
    echo ""
    echo "  Option 1: python -m pip install flatpak-pip-generator"
    echo ""
    echo "  Option 2: Clone the tools repo:"
    echo "    git clone https://github.com/flatpak/flatpak-builder-tools.git ~/flatpak-builder-tools"
    echo ""
    echo "Then run this script again."
    exit 1
fi

echo ""
echo "Done! Now update the manifest to include python-deps.json"
echo "Replace the python-deps module with:"
echo ""
echo "  - python-deps.json"




