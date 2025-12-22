#!/bin/bash
# Build script for Wayfinder Voice
# Creates a standalone executable using PyInstaller

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "🔨 Building Wayfinder Voice..."
echo ""

# Clean previous builds
rm -rf build/ dist/

# Build using spec file
pyinstaller wayfinder-voice.spec --clean

# Check if build succeeded
if [ -f "dist/wayfinder-voice" ]; then
    echo ""
    echo "✅ Build successful!"
    echo ""
    echo "📦 Executable location:"
    echo "   $(pwd)/dist/wayfinder-voice"
    echo ""
    echo "📊 Size: $(du -h dist/wayfinder-voice | cut -f1)"
    echo ""
    echo "🚀 To run:"
    echo "   ./dist/wayfinder-voice"
    echo ""
    echo "📋 To install system-wide:"
    echo "   sudo cp dist/wayfinder-voice /usr/local/bin/"
    echo ""
else
    echo "❌ Build failed!"
    exit 1
fi

