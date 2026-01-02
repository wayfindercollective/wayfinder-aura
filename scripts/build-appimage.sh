#!/bin/bash
# Build AppImage for Wayfinder Aura
# Creates a portable single-file executable for Linux
#
# Prerequisites:
#   - PyInstaller (pip install pyinstaller)
#   - appimagetool (https://github.com/AppImage/AppImageKit)
#
# Usage: ./scripts/build-appimage.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_ROOT"

VERSION="1.0.0"
APP_NAME="WayfinderAura"
APPDIR="AppDir"

echo "╔════════════════════════════════════════════════════════════╗"
echo "║           Wayfinder Aura - AppImage Builder               ║"
echo "╚════════════════════════════════════════════════════════════╝"
echo ""
echo "Version: $VERSION"
echo "Date:    $(date '+%Y-%m-%d %H:%M:%S')"
echo ""

# Check for appimagetool
if ! command -v appimagetool &> /dev/null; then
    echo "⚠ appimagetool not found. Downloading..."
    wget -q -O /tmp/appimagetool "https://github.com/AppImage/AppImageKit/releases/download/continuous/appimagetool-x86_64.AppImage"
    chmod +x /tmp/appimagetool
    APPIMAGETOOL="/tmp/appimagetool"
else
    APPIMAGETOOL="appimagetool"
fi

# Step 1: Build with PyInstaller
echo "🔨 Step 1/4: Building with PyInstaller..."
pyinstaller wayfinder-aura.spec --clean --noconfirm --log-level WARN

if [ ! -f "dist/wayfinder-aura" ]; then
    echo "❌ PyInstaller build failed!"
    exit 1
fi

# Step 2: Create AppDir structure
echo "📁 Step 2/4: Creating AppDir structure..."
rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/bin"
mkdir -p "$APPDIR/usr/share/applications"
mkdir -p "$APPDIR/usr/share/icons/hicolor/256x256/apps"
mkdir -p "$APPDIR/usr/share/icons/hicolor/128x128/apps"
mkdir -p "$APPDIR/usr/share/icons/hicolor/64x64/apps"
mkdir -p "$APPDIR/usr/share/metainfo"

# Step 3: Populate AppDir
echo "📦 Step 3/4: Populating AppDir..."

# Copy the executable
cp dist/wayfinder-aura "$APPDIR/usr/bin/"

# Copy icons
cp assets/icon.png "$APPDIR/usr/share/icons/hicolor/256x256/apps/wayfinder-aura.png"
cp assets/icon.png "$APPDIR/usr/share/icons/hicolor/128x128/apps/wayfinder-aura.png"
cp assets/icon.png "$APPDIR/usr/share/icons/hicolor/64x64/apps/wayfinder-aura.png"
cp assets/icon.png "$APPDIR/wayfinder-aura.png"

# Create desktop file
cat > "$APPDIR/wayfinder-aura.desktop" << EOF
[Desktop Entry]
Type=Application
Name=Wayfinder Aura
Comment=Local voice dictation using whisper.cpp
Exec=wayfinder-aura
Icon=wayfinder-aura
Categories=Utility;Accessibility;Audio;
Keywords=voice;dictation;speech;transcription;whisper;
Terminal=false
StartupNotify=true
EOF

cp "$APPDIR/wayfinder-aura.desktop" "$APPDIR/usr/share/applications/"

# Create AppStream metadata
cat > "$APPDIR/usr/share/metainfo/wayfinder-aura.appdata.xml" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<component type="desktop-application">
  <id>wayfinder-aura</id>
  <metadata_license>MIT</metadata_license>
  <project_license>MIT</project_license>
  <name>Wayfinder Aura</name>
  <summary>Local voice dictation using whisper.cpp</summary>
  <description>
    <p>
      Wayfinder Aura is a privacy-focused voice dictation application for Linux
      that uses whisper.cpp for local speech-to-text transcription.
    </p>
    <p>Features:</p>
    <ul>
      <li>100% local transcription - your audio never leaves your computer</li>
      <li>Hotkey triggered recording (default: F3)</li>
      <li>Wayland and X11 support</li>
      <li>GPU acceleration with Vulkan</li>
      <li>Optional LLM post-processing for text cleanup</li>
    </ul>
  </description>
  <launchable type="desktop-id">wayfinder-aura.desktop</launchable>
  <url type="homepage">https://github.com/user/wayfinder-aura</url>
  <provides>
    <binary>wayfinder-aura</binary>
  </provides>
  <releases>
    <release version="$VERSION" date="$(date '+%Y-%m-%d')"/>
  </releases>
</component>
EOF

# Create AppRun
cat > "$APPDIR/AppRun" << 'EOF'
#!/bin/bash
SELF=$(readlink -f "$0")
HERE=${SELF%/*}
export PATH="${HERE}/usr/bin:${PATH}"
export LD_LIBRARY_PATH="${HERE}/usr/lib:${LD_LIBRARY_PATH}"
exec "${HERE}/usr/bin/wayfinder-aura" "$@"
EOF
chmod +x "$APPDIR/AppRun"

# Step 4: Build AppImage
echo "🎯 Step 4/4: Building AppImage..."
ARCH=$(uname -m)
OUTPUT_NAME="Wayfinder_Voice-${VERSION}-${ARCH}.AppImage"

"$APPIMAGETOOL" "$APPDIR" "$OUTPUT_NAME" 2>/dev/null || {
    # If appimagetool fails (needs FUSE), try with --appimage-extract-and-run
    "$APPIMAGETOOL" --appimage-extract-and-run "$APPDIR" "$OUTPUT_NAME"
}

# Cleanup
rm -rf "$APPDIR"

if [ -f "$OUTPUT_NAME" ]; then
    echo ""
    echo "╔════════════════════════════════════════════════════════════╗"
    echo "║                  AppImage Built! ✅                        ║"
    echo "╚════════════════════════════════════════════════════════════╝"
    echo ""
    echo "📦 Output: $(pwd)/$OUTPUT_NAME"
    echo "📊 Size:   $(du -h "$OUTPUT_NAME" | cut -f1)"
    echo ""
    echo "🚀 Run with:"
    echo "   chmod +x $OUTPUT_NAME"
    echo "   ./$OUTPUT_NAME"
    echo ""
else
    echo "❌ AppImage build failed!"
    exit 1
fi
