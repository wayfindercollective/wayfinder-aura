#!/bin/bash
# Build AppImage for Wayfinder Aura
# Creates a portable .AppImage from the PyInstaller build
#
# Usage:
#   ./build-appimage.sh          # Build AppImage (runs PyInstaller first if needed)
#   ./build-appimage.sh --skip-build  # Use existing dist/wayfinder-aura

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

VERSION="1.0.0"
APP_NAME="Wayfinder_Aura"
ARCH="x86_64"
APPIMAGE_NAME="${APP_NAME}-${VERSION}-${ARCH}.AppImage"
APPDIR="${SCRIPT_DIR}/AppDir"
APPIMAGETOOL_URL="https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage"
APPIMAGETOOL="${SCRIPT_DIR}/appimagetool-x86_64.AppImage"

echo "╔════════════════════════════════════════════════════════════╗"
echo "║         Wayfinder Aura - AppImage Builder                 ║"
echo "╚════════════════════════════════════════════════════════════╝"
echo ""
echo "Version:    $VERSION"
echo "Target:     $APPIMAGE_NAME"
echo "Date:       $(date '+%Y-%m-%d %H:%M:%S')"
echo ""

# ──────────────────────────────────────────────
# Step 1: Build with PyInstaller (unless skipped)
# ──────────────────────────────────────────────
SKIP_BUILD=false
for arg in "$@"; do
    case $arg in
        --skip-build)
            SKIP_BUILD=true
            shift
            ;;
    esac
done

if [ "$SKIP_BUILD" = false ]; then
    echo "📦 Step 1: Building with PyInstaller..."
    echo ""
    bash "$SCRIPT_DIR/build.sh"
    echo ""
fi

# Verify PyInstaller output exists
if [ ! -f "dist/wayfinder-aura" ]; then
    echo "❌ Error: dist/wayfinder-aura not found."
    echo "   Run ./build.sh first, or remove --skip-build flag."
    exit 1
fi

echo "✅ PyInstaller binary found: dist/wayfinder-aura ($(du -h dist/wayfinder-aura | cut -f1))"
echo ""

# ──────────────────────────────────────────────
# Step 2: Download appimagetool if needed
# ──────────────────────────────────────────────
echo "📦 Step 2: Checking appimagetool..."

if [ ! -f "$APPIMAGETOOL" ]; then
    echo "   Downloading appimagetool..."
    curl -L -o "$APPIMAGETOOL" "$APPIMAGETOOL_URL"
    chmod +x "$APPIMAGETOOL"
    echo "   ✅ Downloaded appimagetool"
else
    echo "   ✅ appimagetool already present"
fi
echo ""

# ──────────────────────────────────────────────
# Step 3: Create AppDir structure
# ──────────────────────────────────────────────
echo "📦 Step 3: Creating AppDir structure..."

# Clean previous AppDir
rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/bin"
mkdir -p "$APPDIR/usr/share/applications"
mkdir -p "$APPDIR/usr/share/icons/hicolor/256x256/apps"

# Copy the PyInstaller binary
cp "dist/wayfinder-aura" "$APPDIR/usr/bin/wayfinder-aura"
chmod +x "$APPDIR/usr/bin/wayfinder-aura"

# Create icon (use existing if available, otherwise generate a simple one)
ICON_PATH="$APPDIR/usr/share/icons/hicolor/256x256/apps/wayfinder-aura.png"
if [ -f "assets/icon.png" ]; then
    cp "assets/icon.png" "$ICON_PATH"
    echo "   ✅ Using existing icon from assets/"
else
    echo "   ⚠  No assets/icon.png found, generating placeholder icon..."
    # Generate a simple icon using Python (ImageMagick-free approach)
    python3 -c "
from PIL import Image, ImageDraw, ImageFont
import sys

# Create a 256x256 icon with the app's indigo color
img = Image.new('RGBA', (256, 256), (0, 0, 0, 0))
draw = ImageDraw.Draw(img)

# Background circle with indigo color (#7B8BD9)
draw.ellipse([16, 16, 240, 240], fill=(123, 139, 217, 255))

# Inner circle for depth
draw.ellipse([40, 40, 216, 216], fill=(99, 115, 193, 255))

# Simple microphone shape (white)
# Mic body
draw.rounded_rectangle([104, 60, 152, 140], radius=24, fill=(255, 255, 255, 255))
# Mic stand
draw.rectangle([122, 140, 134, 180], fill=(255, 255, 255, 255))
# Mic base
draw.rounded_rectangle([96, 176, 160, 192], radius=4, fill=(255, 255, 255, 255))
# Mic arc
draw.arc([80, 100, 176, 168], start=0, end=180, fill=(255, 255, 255, 255), width=6)

img.save('$ICON_PATH')
print('   ✅ Generated placeholder icon')
" 2>/dev/null || {
        echo "   ⚠  Could not generate icon (Pillow not available). Using empty placeholder."
        # Create a minimal 1x1 PNG as absolute fallback
        printf '\x89PNG\r\n\x1a\n' > "$ICON_PATH"
    }
fi

# Also copy icon to AppDir root (required by AppImage spec)
cp "$ICON_PATH" "$APPDIR/wayfinder-aura.png" 2>/dev/null || true

# Create .desktop file for AppImage
cat > "$APPDIR/usr/share/applications/wayfinder-aura.desktop" << 'DESKTOP'
[Desktop Entry]
Type=Application
Name=Wayfinder Aura
GenericName=Voice Dictation
Comment=Local voice dictation for Linux using whisper.cpp
Exec=wayfinder-aura
Icon=wayfinder-aura
Terminal=false
Categories=Utility;Audio;Accessibility;
Keywords=voice;dictation;speech;whisper;transcription;speech-to-text;
StartupNotify=true
StartupWMClass=wayfinder-aura
DESKTOP

# Symlink .desktop to AppDir root (required by AppImage spec)
cp "$APPDIR/usr/share/applications/wayfinder-aura.desktop" "$APPDIR/wayfinder-aura.desktop"

# Create AppRun launcher
cat > "$APPDIR/AppRun" << 'APPRUN'
#!/bin/bash
# AppRun launcher for Wayfinder Aura
# This is the entry point when the AppImage is executed

SELF="$(readlink -f "$0")"
APPDIR="${SELF%/*}"

# Set up library paths (in case of bundled libs)
export LD_LIBRARY_PATH="${APPDIR}/usr/lib:${APPDIR}/usr/lib64:${LD_LIBRARY_PATH}"
export PATH="${APPDIR}/usr/bin:${PATH}"

# Launch the application
exec "${APPDIR}/usr/bin/wayfinder-aura" "$@"
APPRUN
chmod +x "$APPDIR/AppRun"

echo "   ✅ AppDir created"
echo ""

# ──────────────────────────────────────────────
# Step 4: Build the AppImage
# ──────────────────────────────────────────────
echo "📦 Step 4: Building AppImage..."
echo ""

# Remove old AppImage if it exists
rm -f "$SCRIPT_DIR/$APPIMAGE_NAME"

# Build! (--no-appstream skips metadata validation)
ARCH="$ARCH" "$APPIMAGETOOL" --no-appstream "$APPDIR" "$SCRIPT_DIR/$APPIMAGE_NAME"

echo ""

# ──────────────────────────────────────────────
# Step 5: Verify and report
# ──────────────────────────────────────────────
if [ -f "$SCRIPT_DIR/$APPIMAGE_NAME" ]; then
    echo "╔════════════════════════════════════════════════════════════╗"
    echo "║               AppImage Build Successful! ✅                ║"
    echo "╚════════════════════════════════════════════════════════════╝"
    echo ""
    echo "📦 Output:  $SCRIPT_DIR/$APPIMAGE_NAME"
    echo "📊 Size:    $(du -h "$SCRIPT_DIR/$APPIMAGE_NAME" | cut -f1)"
    echo ""
    echo "🚀 To run:"
    echo "   chmod +x $APPIMAGE_NAME"
    echo "   ./$APPIMAGE_NAME"
    echo ""
    echo "📤 To share: Upload to Google Drive, GitHub Release, or scp."
    echo ""

    # Cleanup
    rm -rf "$APPDIR"
    echo "🧹 Cleaned up AppDir build directory"
else
    echo "❌ AppImage build failed!"
    exit 1
fi
