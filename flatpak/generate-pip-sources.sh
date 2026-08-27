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

runtime_requirements=$(mktemp)
scipy_build_requirements=$(mktemp)
constraints=$(mktemp)
cleanup() {
    rm -f -- "$runtime_requirements" "$scipy_build_requirements" "$constraints"
}
trap cleanup EXIT

# The generator copies the requirements file into /tmp before invoking pip, so
# relative constraint paths would resolve against /tmp. Materialize absolute
# constraint paths in temporary inputs instead.
cp "$SCRIPT_DIR/flatpak-constraints.txt" "$constraints"
{
    cat "$SCRIPT_DIR/flatpak-requirements.txt"
    printf '%s\n' "-c $constraints"
} > "$runtime_requirements"
{
    cat "$SCRIPT_DIR/scipy-build-requirements.txt"
    printf '%s\n' "-c $constraints"
} > "$scipy_build_requirements"

GENERATOR=()
if python3 -c "import flatpak_pip_generator" &> /dev/null; then
    GENERATOR=(python3 -m flatpak_pip_generator)
elif command -v flatpak_pip_generator &> /dev/null; then
    GENERATOR=(flatpak_pip_generator)
elif command -v flatpak-pip-generator &> /dev/null; then
    GENERATOR=(flatpak-pip-generator)
elif [ -f "$HOME/flatpak-builder-tools/pip/flatpak-pip-generator.py" ]; then
    GENERATOR=(python3 "$HOME/flatpak-builder-tools/pip/flatpak-pip-generator.py")
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

echo "Using ${GENERATOR[*]} (source archives for compiled packages)..."
"${GENERATOR[@]}" \
    --requirements-file="$runtime_requirements" \
    --output="$SCRIPT_DIR/python-deps" \
    --runtime='org.kde.Sdk//6.11'

"${GENERATOR[@]}" \
    --requirements-file="$SCRIPT_DIR/numpy-build-requirements.txt" \
    --output="$SCRIPT_DIR/python-numpy-build-tools" \
    --runtime='org.kde.Sdk//6.11'

"${GENERATOR[@]}" \
    --requirements-file="$scipy_build_requirements" \
    --output="$SCRIPT_DIR/python-scipy-build-tools" \
    --runtime='org.kde.Sdk//6.11'

if rg -n '(manylinux|musllinux)[^" ]*\.whl' \
    "$SCRIPT_DIR/python-deps.json" \
    "$SCRIPT_DIR/python-numpy-build-tools.json" \
    "$SCRIPT_DIR/python-scipy-build-tools.json"; then
    echo "ERROR: generated dependency manifests contain a platform wheel" >&2
    exit 1
fi

echo "Generated source-only Python manifests:"
echo "  $SCRIPT_DIR/python-deps.json"
echo "  $SCRIPT_DIR/python-numpy-build-tools.json"
echo "  $SCRIPT_DIR/python-scipy-build-tools.json"

echo ""
echo "Done. Regenerate Cargo source manifests separately whenever a pinned"
echo "Rust-backed package or Maturin changes; see flatpak/BUILDING.md."
