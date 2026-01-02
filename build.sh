#!/bin/bash
# Build script for Wayfinder Aura
# Creates a standalone executable using PyInstaller

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

VERSION="1.0.0"
BUILD_TYPE="${1:-release}"

echo "╔════════════════════════════════════════════════════════════╗"
echo "║           Wayfinder Aura - Build System                   ║"
echo "╚════════════════════════════════════════════════════════════╝"
echo ""
echo "Version:    $VERSION"
echo "Build Type: $BUILD_TYPE"
echo "Date:       $(date '+%Y-%m-%d %H:%M:%S')"
echo ""

# Check dependencies
check_dependency() {
    if ! command -v "$1" &> /dev/null; then
        echo "❌ Missing dependency: $1"
        echo "   Install with: pip install $2"
        exit 1
    fi
}

check_dependency "pyinstaller" "pyinstaller"

# Parse arguments
CLEAN_BUILD=true
VERBOSE=false

while [[ $# -gt 0 ]]; do
    case $1 in
        --no-clean)
            CLEAN_BUILD=false
            shift
            ;;
        --verbose|-v)
            VERBOSE=true
            shift
            ;;
        --help|-h)
            echo "Usage: $0 [options]"
            echo ""
            echo "Options:"
            echo "  --no-clean    Skip cleaning previous builds"
            echo "  --verbose     Show detailed build output"
            echo "  --help        Show this help message"
            exit 0
            ;;
        *)
            shift
            ;;
    esac
done

# Clean previous builds
if [ "$CLEAN_BUILD" = true ]; then
    echo "🧹 Cleaning previous builds..."
    rm -rf build/ dist/
fi

# Build using spec file
echo "🔨 Building executable..."
echo ""

if [ "$VERBOSE" = true ]; then
    pyinstaller wayfinder-aura.spec --clean --noconfirm
else
    pyinstaller wayfinder-aura.spec --clean --noconfirm --log-level WARN
fi

# Check if build succeeded
if [ -f "dist/wayfinder-aura" ]; then
    echo ""
    echo "╔════════════════════════════════════════════════════════════╗"
    echo "║                    Build Successful! ✅                    ║"
    echo "╚════════════════════════════════════════════════════════════╝"
    echo ""
    echo "📦 Executable: $(pwd)/dist/wayfinder-aura"
    echo "📊 Size:       $(du -h dist/wayfinder-aura | cut -f1)"
    echo ""
    echo "🚀 Run locally:"
    echo "   ./dist/wayfinder-aura"
    echo ""
    echo "📋 Install system-wide:"
    echo "   sudo install -m 755 dist/wayfinder-aura /usr/local/bin/"
    echo ""
    echo "📄 Create desktop entry:"
    echo "   cp wayfinder-aura.desktop ~/.local/share/applications/"
    echo ""
else
    echo ""
    echo "╔════════════════════════════════════════════════════════════╗"
    echo "║                    Build Failed! ❌                        ║"
    echo "╚════════════════════════════════════════════════════════════╝"
    echo ""
    echo "Check the build log above for errors."
    echo "Common issues:"
    echo "  - Missing dependencies: pip install -r requirements.txt"
    echo "  - Missing PyQt6: pip install PyQt6"
    echo "  - Missing scipy: pip install scipy"
    exit 1
fi
