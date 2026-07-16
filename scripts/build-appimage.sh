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
#   - cmake, make
#   - optional: vulkan-headers/vulkan-devel + glslc for GPU-native whisper.cpp/llama.cpp
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

VERSION="1.1.1"
APP_NAME="WayfinderAura"
APP_ID="io.wayfindercollective.WayfinderAura"
APPDIR="AppDir"
ARCH=$(uname -m)
BUILD_MODE="--lite"
SKIP_BUILD=0
SOURCE_DESKTOP="flatpak/${APP_ID}.desktop"
SOURCE_METAINFO="flatpak/${APP_ID}.metainfo.xml"
WHISPER_REPO="https://github.com/ggerganov/whisper.cpp.git"
WHISPER_TAG="v1.9.1"
WHISPER_COMMIT="f049fff95a089aa9969deb009cdd4892b3e74916"
LLAMA_REPO="https://github.com/ggml-org/llama.cpp.git"
LLAMA_TAG="b9608"
LLAMA_COMMIT="70b54e140c90a92285ba699d77e1e32e0868a0e2"
BUILD_JOBS="${BUILD_JOBS:-$(nproc)}"

# ─── Parse arguments ─────────────────────────────────────────────────────────

while [[ $# -gt 0 ]]; do
    case "$1" in
        --full|--lite)
            BUILD_MODE="$1"
            ;;
        --skip-build)
            SKIP_BUILD=1
            ;;
        --help|-h)
            echo "Usage: $0 [--lite|--full] [--skip-build]"
            echo ""
            echo "  --lite        Bundle only the Python app (default)"
            echo "  --full        Bundle native inference binaries and optional models"
            echo "  --skip-build  Reuse dist/wayfinder-aura instead of running PyInstaller"
            exit 0
            ;;
        *)
            echo "❌ Unknown argument: $1" >&2
            echo "Usage: $0 [--lite|--full] [--skip-build]" >&2
            exit 2
            ;;
    esac
    shift
done

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

if ! python3 - <<'PY' &> /dev/null
import tkinter
PY
then
    echo "❌ python3 tkinter support not found. Install the system Tk package first:"
    echo "   Fedora/Bazzite: sudo dnf install python3-tkinter"
    echo "   Ubuntu/Debian:  sudo apt install python3-tk"
    exit 1
fi

if [ ! -f "$SOURCE_DESKTOP" ]; then
    echo "❌ Desktop metadata not found: $SOURCE_DESKTOP"
    exit 1
fi

if [ ! -f "$SOURCE_METAINFO" ]; then
    echo "❌ AppStream metadata not found: $SOURCE_METAINFO"
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

clone_pinned_repo() {
    local name="$1"
    local url="$2"
    local tag="$3"
    local commit="$4"
    local dest="$5"

    if [ ! -d "$dest/.git" ]; then
        echo "   📥 Cloning $name ($tag)..."
        rm -rf "$dest"
        git clone --depth 1 --branch "$tag" "$url" "$dest"
    fi

    local actual
    actual="$(git -C "$dest" rev-parse HEAD)"
    if [ "$actual" != "$commit" ]; then
        echo "   📌 Pinning $name to $commit..."
        git -C "$dest" fetch --depth 1 origin "$commit"
        git -C "$dest" checkout --detach "$commit"
    fi
}

cmake_native_build() {
    local name="$1"
    local src="$2"
    local build_dir="$3"
    local vulkan="$4"

    rm -rf "$build_dir"
    if cmake -S "$src" -B "$build_dir" \
        -DGGML_VULKAN="$vulkan" \
        -DCMAKE_BUILD_TYPE=Release \
        -DBUILD_SHARED_LIBS=OFF; then
        cmake --build "$build_dir" --config Release -j"$BUILD_JOBS"
        return $?
    fi
    echo "   ⚠ $name configure failed with GGML_VULKAN=$vulkan"
    return 1
}

# ─── Step 1: Build with PyInstaller ──────────────────────────────────────────

if [ "$SKIP_BUILD" = "1" ]; then
    echo "🔨 Step 1: Reusing existing PyInstaller output (--skip-build)..."
else
    echo "🔨 Step 1: Building with PyInstaller..."
    pyinstaller wayfinder-aura.spec --clean --noconfirm --log-level WARN
fi

if [ ! -f "dist/wayfinder-aura" ]; then
    echo "❌ dist/wayfinder-aura not found. Run without --skip-build first."
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
cp assets/icon.png "$APPDIR/usr/share/icons/hicolor/256x256/apps/${APP_ID}.png"
cp assets/icon.png "$APPDIR/usr/share/icons/hicolor/128x128/apps/${APP_ID}.png"
cp assets/icon.png "$APPDIR/usr/share/icons/hicolor/64x64/apps/${APP_ID}.png"
cp assets/icon.png "$APPDIR/${APP_ID}.png"
# Keep the legacy icon name for older desktop integrations and user scripts.
cp assets/icon.png "$APPDIR/wayfinder-aura.png"

# ─── Desktop file and AppStream metadata ─────────────────────────────────────

cp "$SOURCE_DESKTOP" "$APPDIR/${APP_ID}.desktop"
cp "$SOURCE_DESKTOP" "$APPDIR/usr/share/applications/${APP_ID}.desktop"
cp "$SOURCE_METAINFO" "$APPDIR/usr/share/metainfo/${APP_ID}.metainfo.xml"

# ─── Full build: Bundle native dependencies ──────────────────────────────────

if [ "$BUILD_MODE" = "--full" ]; then
    echo ""
    echo "🔧 Full build: Bundling native dependencies..."

    # ── whisper.cpp ──
    WHISPER_DIR="/tmp/whisper.cpp-appimage"
    clone_pinned_repo "whisper.cpp" "$WHISPER_REPO" "$WHISPER_TAG" "$WHISPER_COMMIT" "$WHISPER_DIR"
    echo "   🔨 Building whisper.cpp (Vulkan, CPU fallback)..."
    WHISPER_BUILD="$WHISPER_DIR/build-vulkan"
    if ! cmake_native_build "whisper.cpp" "$WHISPER_DIR" "$WHISPER_BUILD" ON; then
        echo "   ↳ Falling back to CPU-only whisper.cpp"
        WHISPER_BUILD="$WHISPER_DIR/build-cpu"
        cmake_native_build "whisper.cpp" "$WHISPER_DIR" "$WHISPER_BUILD" OFF
    fi
    if [ -f "$WHISPER_BUILD/bin/whisper-cli" ]; then
        cp "$WHISPER_BUILD/bin/whisper-cli" "$APPDIR/usr/bin/"
        echo "   ✓ whisper-cli bundled ($(basename "$WHISPER_BUILD"))"
    else
        echo "   ⚠ whisper-cli build failed, skipping"
    fi

    # ── llama.cpp ──
    LLAMA_DIR="/tmp/llama.cpp-appimage"
    clone_pinned_repo "llama.cpp" "$LLAMA_REPO" "$LLAMA_TAG" "$LLAMA_COMMIT" "$LLAMA_DIR"
    echo "   🔨 Building llama.cpp (Vulkan, CPU fallback)..."
    LLAMA_BUILD="$LLAMA_DIR/build-vulkan"
    if ! cmake_native_build "llama.cpp" "$LLAMA_DIR" "$LLAMA_BUILD" ON; then
        echo "   ↳ Falling back to CPU-only llama.cpp"
        LLAMA_BUILD="$LLAMA_DIR/build-cpu"
        cmake_native_build "llama.cpp" "$LLAMA_DIR" "$LLAMA_BUILD" OFF
    fi
    for binary in llama-cli llama-simple; do
        if [ -f "$LLAMA_BUILD/bin/$binary" ]; then
            cp "$LLAMA_BUILD/bin/$binary" "$APPDIR/usr/bin/"
            echo "   ✓ $binary bundled ($(basename "$LLAMA_BUILD"))"
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

# Use absolute paths so appimagetool works even with --appimage-extract-and-run
ABS_APPDIR="$(cd "$APPDIR" && pwd)"
ABS_OUTPUT="$(pwd)/$OUTPUT_NAME"

# AppImageUpdate self-update info (-u embeds update info + emits a .zsync alongside the AppImage)
UPDATE_INFO="gh-releases-zsync|wayfindercollective|wayfinder-aura|latest|Wayfinder_Aura-*${ARCH}.AppImage.zsync"

ARCH="$ARCH" "$APPIMAGETOOL" --no-appstream -u "$UPDATE_INFO" "$ABS_APPDIR" "$ABS_OUTPUT" || {
    ARCH="$ARCH" "$APPIMAGETOOL" --appimage-extract-and-run --no-appstream -u "$UPDATE_INFO" "$ABS_APPDIR" "$ABS_OUTPUT"
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
