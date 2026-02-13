#!/bin/bash
# Build AppImage for Wayfinder Aura
# Creates a portable single-file executable for Linux
#
# This script supports two build modes:
#   --full     Bundle whisper.cpp, llama.cpp, ydotool, wtype, models (self-contained, ~1.5GB)
#   --lite     Bundle only the Python app (lightweight, ~100MB, user provides dependencies)
#
# Prerequisites:
#   - PyInstaller (pip install pyinstaller)
#   - appimagetool (auto-downloaded if missing)
#   For --full mode:
#   - cmake, make, vulkan-headers/vulkan-devel (for building whisper.cpp/llama.cpp)
#   - ydotool and wtype installed on the build system
#
# Usage:
#   ./scripts/build-appimage.sh          # Default: lite build
#   ./scripts/build-appimage.sh --full   # Full self-contained build
#   ./scripts/build-appimage.sh --lite   # Explicit lite build

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_ROOT"

VERSION="1.0.0"
APP_NAME="WayfinderAura"
APPDIR="AppDir"
ARCH=$(uname -m)
BUILD_MODE="${1:---lite}"

echo "╔════════════════════════════════════════════════════════════╗"
echo "║           Wayfinder Aura - AppImage Builder               ║"
echo "╚════════════════════════════════════════════════════════════╝"
echo ""
echo "Version:    $VERSION"
echo "Arch:       $ARCH"
echo "Build mode: $BUILD_MODE"
echo "Date:       $(date '+%Y-%m-%d %H:%M:%S')"
echo ""

# ─── Validate prerequisites ───────────────────────────────────────────────────

if ! command -v python3 &> /dev/null; then
    echo "❌ python3 not found"
    exit 1
fi

if ! command -v pyinstaller &> /dev/null; then
    echo "❌ pyinstaller not found. Install with: pip install pyinstaller"
    exit 1
fi

# ─── Get appimagetool ─────────────────────────────────────────────────────────

if ! command -v appimagetool &> /dev/null; then
    echo "⚠ appimagetool not found. Downloading..."
    wget -q -O /tmp/appimagetool "https://github.com/AppImage/AppImageKit/releases/download/continuous/appimagetool-x86_64.AppImage"
    chmod +x /tmp/appimagetool
    APPIMAGETOOL="/tmp/appimagetool"
else
    APPIMAGETOOL="appimagetool"
fi

# ─── Step 1: Build with PyInstaller ──────────────────────────────────────────

echo "🔨 Step 1: Building with PyInstaller..."
pyinstaller wayfinder-aura.spec --clean --noconfirm --log-level WARN

if [ ! -f "dist/wayfinder-aura" ]; then
    echo "❌ PyInstaller build failed!"
    exit 1
fi
echo "   ✓ PyInstaller binary built"

# ─── Step 2: Create AppDir structure ─────────────────────────────────────────

echo "📁 Step 2: Creating AppDir structure..."
rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/bin"
mkdir -p "$APPDIR/usr/lib"
mkdir -p "$APPDIR/usr/share/applications"
mkdir -p "$APPDIR/usr/share/icons/hicolor/256x256/apps"
mkdir -p "$APPDIR/usr/share/icons/hicolor/128x128/apps"
mkdir -p "$APPDIR/usr/share/icons/hicolor/64x64/apps"
mkdir -p "$APPDIR/usr/share/metainfo"

# ─── Step 3: Populate AppDir ─────────────────────────────────────────────────

echo "📦 Step 3: Populating AppDir..."

# Copy the main executable
cp dist/wayfinder-aura "$APPDIR/usr/bin/"

# Copy icons
cp assets/icon.png "$APPDIR/usr/share/icons/hicolor/256x256/apps/wayfinder-aura.png"
cp assets/icon.png "$APPDIR/usr/share/icons/hicolor/128x128/apps/wayfinder-aura.png"
cp assets/icon.png "$APPDIR/usr/share/icons/hicolor/64x64/apps/wayfinder-aura.png"
cp assets/icon.png "$APPDIR/wayfinder-aura.png"

# ─── Desktop file ────────────────────────────────────────────────────────────

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

# ─── AppStream metadata ─────────────────────────────────────────────────────

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
      <li>Hotkey triggered recording (default: F9)</li>
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
    <release version="${VERSION}" date="$(date '+%Y-%m-%d')"/>
  </releases>
</component>
EOF

# ─── Full build: Bundle native dependencies ──────────────────────────────────

if [ "$BUILD_MODE" = "--full" ]; then
    echo ""
    echo "🔧 Full build: Bundling native dependencies..."

    # ── whisper.cpp ──
    WHISPER_DIR="/tmp/whisper.cpp-appimage"
    if [ ! -d "$WHISPER_DIR" ]; then
        echo "   📥 Cloning whisper.cpp..."
        git clone --depth 1 https://github.com/ggerganov/whisper.cpp.git "$WHISPER_DIR"
    fi
    echo "   🔨 Building whisper.cpp (Vulkan)..."
    cmake -S "$WHISPER_DIR" -B "$WHISPER_DIR/build" \
        -DGGML_VULKAN=ON \
        -DCMAKE_BUILD_TYPE=Release \
        -DBUILD_SHARED_LIBS=OFF \
        2>/dev/null
    cmake --build "$WHISPER_DIR/build" --config Release -j$(nproc) 2>/dev/null
    if [ -f "$WHISPER_DIR/build/bin/whisper-cli" ]; then
        cp "$WHISPER_DIR/build/bin/whisper-cli" "$APPDIR/usr/bin/"
        echo "   ✓ whisper-cli bundled"
    else
        echo "   ⚠ whisper-cli build failed, skipping"
    fi

    # ── llama.cpp ──
    LLAMA_DIR="/tmp/llama.cpp-appimage"
    if [ ! -d "$LLAMA_DIR" ]; then
        echo "   📥 Cloning llama.cpp..."
        git clone --depth 1 https://github.com/ggerganov/llama.cpp.git "$LLAMA_DIR"
    fi
    echo "   🔨 Building llama.cpp (Vulkan)..."
    cmake -S "$LLAMA_DIR" -B "$LLAMA_DIR/build" \
        -DGGML_VULKAN=ON \
        -DCMAKE_BUILD_TYPE=Release \
        -DBUILD_SHARED_LIBS=OFF \
        2>/dev/null
    cmake --build "$LLAMA_DIR/build" --config Release -j$(nproc) 2>/dev/null
    for binary in llama-cli llama-simple; do
        if [ -f "$LLAMA_DIR/build/bin/$binary" ]; then
            cp "$LLAMA_DIR/build/bin/$binary" "$APPDIR/usr/bin/"
            echo "   ✓ $binary bundled"
        fi
    done

    # ── ydotool ──
    if command -v ydotool &> /dev/null; then
        cp "$(which ydotool)" "$APPDIR/usr/bin/"
        echo "   ✓ ydotool bundled"
    else
        echo "   ⚠ ydotool not found on system, skipping"
    fi

    # ── wtype ──
    if command -v wtype &> /dev/null; then
        cp "$(which wtype)" "$APPDIR/usr/bin/"
        echo "   ✓ wtype bundled"
    else
        echo "   ⚠ wtype not found on system, skipping"
    fi

    # ── Shared libraries for bundled binaries ──
    echo "   📚 Bundling shared libraries..."
    for bin in "$APPDIR/usr/bin/whisper-cli" "$APPDIR/usr/bin/llama-cli" "$APPDIR/usr/bin/llama-simple"; do
        if [ -f "$bin" ]; then
            # Copy required shared libraries (excluding glibc/ld-linux)
            ldd "$bin" 2>/dev/null | grep "=> /" | awk '{print $3}' | while read lib; do
                libname=$(basename "$lib")
                # Skip glibc, ld-linux, and other system libs the host provides
                case "$libname" in
                    libc.so*|libm.so*|libpthread.so*|libdl.so*|librt.so*|ld-linux*|libstdc++*) continue ;;
                esac
                if [ ! -f "$APPDIR/usr/lib/$libname" ]; then
                    cp "$lib" "$APPDIR/usr/lib/" 2>/dev/null || true
                fi
            done
        fi
    done
    echo "   ✓ Shared libraries bundled"

    # ── Models (optional, controlled by env var) ──
    if [ "${BUNDLE_MODELS:-0}" = "1" ]; then
        echo "   📦 Bundling models..."
        mkdir -p "$APPDIR/usr/share/whisper-models"
        mkdir -p "$APPDIR/usr/share/llm-models"

        # Whisper model
        WHISPER_MODEL="${WHISPER_MODEL_PATH:-$HOME/whisper.cpp/models/ggml-small.en.bin}"
        if [ -f "$WHISPER_MODEL" ]; then
            cp "$WHISPER_MODEL" "$APPDIR/usr/share/whisper-models/"
            echo "   ✓ Whisper model bundled: $(basename "$WHISPER_MODEL")"
        else
            echo "   ⚠ Whisper model not found at $WHISPER_MODEL"
        fi

        # LLM model
        LLM_MODEL="${LLM_MODEL_PATH:-$HOME/.local/share/wayfinder-aura/llm-models/qwen2.5-1.5b-instruct-q4_k_m.gguf}"
        if [ -f "$LLM_MODEL" ]; then
            cp "$LLM_MODEL" "$APPDIR/usr/share/llm-models/"
            echo "   ✓ LLM model bundled: $(basename "$LLM_MODEL")"
        else
            echo "   ⚠ LLM model not found at $LLM_MODEL"
        fi
    else
        echo "   ℹ Models not bundled (set BUNDLE_MODELS=1 to include)"
    fi
fi

# ─── AppRun launcher ─────────────────────────────────────────────────────────

cat > "$APPDIR/AppRun" << 'APPRUN_EOF'
#!/bin/bash
SELF=$(readlink -f "$0")
HERE=${SELF%/*}

# Set up paths for bundled binaries
export PATH="${HERE}/usr/bin:${PATH}"
export LD_LIBRARY_PATH="${HERE}/usr/lib:${LD_LIBRARY_PATH}"

# Tell the app we're running from an AppImage
export APPDIR="${HERE}"

# Inherit host Vulkan ICD if available (for GPU acceleration)
if [ -z "$VK_ICD_FILENAMES" ]; then
    for icd in /usr/share/vulkan/icd.d/*.json /etc/vulkan/icd.d/*.json; do
        if [ -f "$icd" ]; then
            if [ -z "$VK_ICD_FILENAMES" ]; then
                VK_ICD_FILENAMES="$icd"
            else
                VK_ICD_FILENAMES="$VK_ICD_FILENAMES:$icd"
            fi
        fi
    done
    export VK_ICD_FILENAMES
fi

exec "${HERE}/usr/bin/wayfinder-aura" "$@"
APPRUN_EOF
chmod +x "$APPDIR/AppRun"

# ─── Step 4: Build AppImage ──────────────────────────────────────────────────

echo "🎯 Step 4: Building AppImage..."
OUTPUT_NAME="Wayfinder_Aura-${VERSION}-${ARCH}.AppImage"

"$APPIMAGETOOL" "$APPDIR" "$OUTPUT_NAME" 2>/dev/null || {
    # If appimagetool fails (needs FUSE), try with --appimage-extract-and-run
    "$APPIMAGETOOL" --appimage-extract-and-run "$APPDIR" "$OUTPUT_NAME"
}

# Cleanup
rm -rf "$APPDIR"

if [ -f "$OUTPUT_NAME" ]; then
    echo ""
    echo "╔════════════════════════════════════════════════════════════╗"
    echo "║                  AppImage Built!                          ║"
    echo "╚════════════════════════════════════════════════════════════╝"
    echo ""
    echo "📦 Output:     $(pwd)/$OUTPUT_NAME"
    echo "📊 Size:       $(du -h "$OUTPUT_NAME" | cut -f1)"
    echo "🏗️  Build mode: $BUILD_MODE"
    echo ""
    echo "🚀 Run with:"
    echo "   chmod +x $OUTPUT_NAME"
    echo "   ./$OUTPUT_NAME"
    echo ""
    if [ "$BUILD_MODE" = "--lite" ]; then
        echo "ℹ  Lite build - your friend will need these installed on their system:"
        echo "   • whisper.cpp (with Vulkan support)"
        echo "   • ydotool + ydotoold daemon running"
        echo "   • A Whisper model (e.g., ggml-large-v3-turbo.bin)"
        echo ""
        echo "   For a self-contained build, use: $0 --full"
    fi
else
    echo "❌ AppImage build failed!"
    exit 1
fi
