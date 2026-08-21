#!/bin/bash
# Build AppImage for Wayfinder Aura
# Creates a portable single-file executable for Linux
#
# This script supports two build modes:
#   --full     Bundle whisper.cpp, llama.cpp, wtype, and optional models
#   --lite     Bundle only the Python app (lightweight, ~100MB, user provides dependencies)
#
# Prerequisites:
#   - PyInstaller (pip install pyinstaller)
#   - appimagetool (auto-downloaded if missing)
#   For --full mode:
#   - cmake, make
#   - optional: vulkan-headers/vulkan-devel + glslc for GPU-native whisper.cpp/llama.cpp
#   - wtype installed on the build system (optional; host ydotool is never bundled)
#
# Usage:
#   ./scripts/build-appimage.sh          # Default: lite build
#   ./scripts/build-appimage.sh --full   # Full self-contained build
#   ./scripts/build-appimage.sh --lite   # Explicit lite build

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_ROOT"

VERSION="1.1.8-beta.10"
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
PYTHON_BIN="${PYTHON_BIN:-python3}"

# Release CPU baseline shared with the Flatpak.  Never let ggml inherit the
# build host's `-march=native`: GitHub runners and developer workstations may
# expose AVX-512 instructions that do not exist on the Steam Deck's Zen 2 CPU.
# Modern ggml also becomes scalar-only with GGML_NATIVE=OFF unless these SIMD
# families are enabled explicitly, so spell out the performant Deck baseline.
GGML_CPU_BASELINE_OPTS=(
    -DGGML_NATIVE=OFF
    -DGGML_SSE42=ON
    -DGGML_AVX=ON
    -DGGML_AVX2=ON
    -DGGML_FMA=ON
    -DGGML_F16C=ON
    -DGGML_BMI2=ON
    -DGGML_AVX512=OFF
)

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

if ! command -v "$PYTHON_BIN" &> /dev/null; then
    echo "❌ Python interpreter not found: $PYTHON_BIN"
    exit 1
fi

if ! "$PYTHON_BIN" -m PyInstaller --version &> /dev/null; then
    echo "❌ PyInstaller is not installed for $PYTHON_BIN"
    echo "   Install with: $PYTHON_BIN -m pip install pyinstaller"
    exit 1
fi

if ! "$PYTHON_BIN" - <<'PY' &> /dev/null
import tkinter
PY
then
    echo "❌ Tkinter support not found for $PYTHON_BIN. Install the matching Tk package first:"
    echo "   Fedora/Bazzite: sudo dnf install python3-tkinter"
    echo "   Ubuntu/Debian:  sudo apt install python3-tk"
    exit 1
fi

# Importing Tk is not enough: portable Python distributions commonly ship a
# no-Xft Tk that creates windows but exposes only legacy bitmap fonts. That
# turns every requested family and size into the same tiny `fixed` face. Probe
# the real renderer on this display (or a temporary Xvfb display in headless
# release CI) and refuse to build a visually broken artifact.
probe_tk_renderer() {
    PYTHONPATH="$PROJECT_ROOT/src${PYTHONPATH:+:$PYTHONPATH}" \
        "$@" -c 'from wayfinder.utils.tk_renderer import require_compatible_tk_renderer as require; result = require(); print(f"Tk renderer: {result.patchlevel} / {result.font_system} / {result.actual_family}")'
}

if [ -n "${DISPLAY:-}" ]; then
    TK_PROBE=("$PYTHON_BIN")
elif command -v xvfb-run &> /dev/null; then
    TK_PROBE=(xvfb-run -a "$PYTHON_BIN")
else
    echo "❌ Tk renderer validation needs DISPLAY or xvfb-run" >&2
    exit 1
fi

if ! probe_tk_renderer "${TK_PROBE[@]}"; then
    echo "❌ Tk must be built with Xft and resolve the bundled DejaVu Sans family" >&2
    echo "   Use the Ubuntu AppImage build container or another Xft-enabled Python/Tk." >&2
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

# Pinned tool. The old AppImageKit "continuous" asset was fetched with no
# integrity check: whoever could replace it would alter every AppImage cut from
# then on (security audit 2026-08-17, F-E).
#
# The DIGEST is the pin, not the tag — GitHub release assets stay mutable, and
# this very asset has already been replaced upstream once (the 1.9.1 tag now
# serves a build dated after the tag). A mismatch here means the bytes changed:
# re-verify what upstream is shipping, then bump version + digest together.
APPIMAGETOOL_VERSION="1.9.1"
APPIMAGETOOL_SHA256="ed4ce84f0d9caff66f50bcca6ff6f35aae54ce8135408b3fa33abfc3cb384eb0"
APPIMAGETOOL_URL="https://github.com/AppImage/appimagetool/releases/download/${APPIMAGETOOL_VERSION}/appimagetool-x86_64.AppImage"

# Deliberately ignore any appimagetool already on PATH: a CI image or dev box
# could carry a different version, or a replaced binary earlier on PATH, and it
# would build the release with none of the pinning claimed above. Set
# APPIMAGETOOL to opt out explicitly (unverified, and it says so).
if [ -n "${APPIMAGETOOL:-}" ]; then
    echo "⚠ Using APPIMAGETOOL override: $APPIMAGETOOL (NOT hash-verified)"
else
    TOOL_CACHE="$PROJECT_ROOT/.build-tools"
    TOOL_PATH="$TOOL_CACHE/appimagetool-${APPIMAGETOOL_VERSION}.AppImage"
    mkdir -p "$TOOL_CACHE"

    if [ ! -f "$TOOL_PATH" ] || ! echo "$APPIMAGETOOL_SHA256  $TOOL_PATH" | sha256sum -c - &> /dev/null; then
        echo "⤓ Fetching pinned appimagetool ${APPIMAGETOOL_VERSION}..."
        wget -q -O "$TOOL_PATH.part" "$APPIMAGETOOL_URL"
        if ! echo "$APPIMAGETOOL_SHA256  $TOOL_PATH.part" | sha256sum -c - &> /dev/null; then
            rm -f "$TOOL_PATH.part"
            echo "❌ appimagetool checksum mismatch — refusing to build with an unverified tool"
            echo "   expected sha256: $APPIMAGETOOL_SHA256"
            echo "   from: $APPIMAGETOOL_URL"
            exit 1
        fi
        mv "$TOOL_PATH.part" "$TOOL_PATH"
    fi

    chmod +x "$TOOL_PATH"
    APPIMAGETOOL="$TOOL_PATH"
    echo "✓ appimagetool ${APPIMAGETOOL_VERSION} verified (sha256 ${APPIMAGETOOL_SHA256:0:12}…)"
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
    shift 4
    local targets=("$@")

    rm -rf "$build_dir"
    if cmake -S "$src" -B "$build_dir" \
        -DGGML_VULKAN="$vulkan" \
        "${GGML_CPU_BASELINE_OPTS[@]}" \
        -DCMAKE_BUILD_TYPE=Release \
        -DBUILD_SHARED_LIBS=OFF; then
        cmake --build "$build_dir" --config Release \
            --parallel "$BUILD_JOBS" --target "${targets[@]}"
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
    "$PYTHON_BIN" -m PyInstaller wayfinder-aura.spec --clean --noconfirm --log-level WARN
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
    WHISPER_HAS_VULKAN=1
    if ! cmake_native_build "whisper.cpp" "$WHISPER_DIR" "$WHISPER_BUILD" ON \
            whisper-cli whisper-server; then
        echo "   ↳ Falling back to CPU-only whisper.cpp"
        WHISPER_HAS_VULKAN=0
        WHISPER_BUILD="$WHISPER_DIR/build-cpu"
        cmake_native_build "whisper.cpp" "$WHISPER_DIR" "$WHISPER_BUILD" OFF \
            whisper-cli whisper-server
    fi
    for binary in whisper-cli whisper-server; do
        test -x "$WHISPER_BUILD/bin/$binary"
        cp "$WHISPER_BUILD/bin/$binary" "$APPDIR/usr/bin/$binary"
        echo "   ✓ $binary bundled ($(basename "$WHISPER_BUILD"))"
    done

    # A Vulkan binary can die during backend initialization before a no-GPU
    # flag is processed (observed on Steam Deck/RADV after suspend).  Always
    # ship an independently linked CPU twin when the primary is Vulkan.
    if [ "$WHISPER_HAS_VULKAN" = "1" ]; then
        WHISPER_CPU_BUILD="$WHISPER_DIR/build-cpu"
        echo "   🔨 Building whisper.cpp CPU safety twins..."
        cmake_native_build "whisper.cpp CPU" "$WHISPER_DIR" "$WHISPER_CPU_BUILD" OFF \
            whisper-cli whisper-server
        cp "$WHISPER_CPU_BUILD/bin/whisper-cli" "$APPDIR/usr/bin/whisper-cli-cpu"
        cp "$WHISPER_CPU_BUILD/bin/whisper-server" "$APPDIR/usr/bin/whisper-server-cpu"
        echo "   ✓ whisper-cli-cpu + whisper-server-cpu bundled"
    fi

    # ── llama.cpp ──
    LLAMA_DIR="/tmp/llama.cpp-appimage"
    clone_pinned_repo "llama.cpp" "$LLAMA_REPO" "$LLAMA_TAG" "$LLAMA_COMMIT" "$LLAMA_DIR"
    echo "   🔨 Building llama.cpp (Vulkan, CPU fallback)..."
    LLAMA_BUILD="$LLAMA_DIR/build-vulkan"
    LLAMA_HAS_VULKAN=1
    if ! cmake_native_build "llama.cpp" "$LLAMA_DIR" "$LLAMA_BUILD" ON \
            llama-cli llama-simple llama-server; then
        echo "   ↳ Falling back to CPU-only llama.cpp"
        LLAMA_HAS_VULKAN=0
        LLAMA_BUILD="$LLAMA_DIR/build-cpu"
        cmake_native_build "llama.cpp" "$LLAMA_DIR" "$LLAMA_BUILD" OFF \
            llama-cli llama-simple llama-server
    fi
    # llama-server keeps the cleanup model RESIDENT between dictations. MEASURED
    # on Qwen3-4B/Vulkan: 0.147s warm vs 0.61s for a per-call llama-simple (4.1x),
    # and it is the only path exposing sampling flags, which is what stops
    # caricature mode from looping until it burns the whole token budget.
    for binary in llama-cli llama-simple llama-server; do
        if [ -f "$LLAMA_BUILD/bin/$binary" ]; then
            cp "$LLAMA_BUILD/bin/$binary" "$APPDIR/usr/bin/"
            echo "   ✓ $binary bundled ($(basename "$LLAMA_BUILD"))"
        fi
    done
    if [ "$LLAMA_HAS_VULKAN" = "1" ]; then
        LLAMA_CPU_BUILD="$LLAMA_DIR/build-cpu"
        echo "   🔨 Building llama.cpp CPU safety twin..."
        cmake_native_build "llama.cpp CPU" "$LLAMA_DIR" "$LLAMA_CPU_BUILD" OFF \
            llama-simple llama-server
        cp "$LLAMA_CPU_BUILD/bin/llama-simple" "$APPDIR/usr/bin/llama-simple-cpu"
        cp "$LLAMA_CPU_BUILD/bin/llama-server" "$APPDIR/usr/bin/llama-server-cpu"
        echo "   ✓ llama-simple-cpu + llama-server-cpu bundled"
    fi

    # ── ydotool: deliberately NOT bundled ──
    # The ydotool client must protocol-match the HOST's ydotoold daemon: a
    # bundled client against a different distro's daemon silently falls back
    # to a throwaway direct-uinput device (types nothing, exits 0), and the
    # jammy CI client (0.1.8) predates the app's CLI syntax entirely. The
    # injector uses the host's client only; wtype ships bundled for Wayland
    # and Setup can self-provision a user-level ydotoold where available.

    # ── wtype ──
    if command -v wtype &> /dev/null; then
        cp "$(which wtype)" "$APPDIR/usr/bin/"
        echo "   ✓ wtype bundled"
    else
        echo "   ⚠ wtype not found on system, skipping"
    fi

    # ── Shared libraries for bundled binaries ──
    echo "   📚 Bundling shared libraries..."
    for bin in \
        "$APPDIR/usr/bin/whisper-cli" \
        "$APPDIR/usr/bin/whisper-server" \
        "$APPDIR/usr/bin/whisper-cli-cpu" \
        "$APPDIR/usr/bin/whisper-server-cpu" \
        "$APPDIR/usr/bin/llama-cli" \
        "$APPDIR/usr/bin/llama-simple" \
        "$APPDIR/usr/bin/llama-simple-cpu"; do
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
        WHISPER_MODEL="${WHISPER_MODEL_PATH:-$HOME/whisper.cpp/models/ggml-base.en.bin}"
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

# ── Product fonts (DejaVu Sans; JetBrains Mono for the overlay) ──
# Never installed on user machines — without them fontconfig substitutes a
# random sans per distro (Noto on Fedora, DejaVu on Debian) and the UI reads
# nothing like the design. AppRun points FONTCONFIG_FILE at a config that adds
# this dir on top of the host's fonts.
mkdir -p "$APPDIR/usr/share/fonts/wayfinder-aura"
cp -r "$PROJECT_ROOT/assets/fonts/." "$APPDIR/usr/share/fonts/wayfinder-aura/"
echo "   ✓ Product fonts bundled (DejaVu Sans, JetBrains Mono)"

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

# PyInstaller's one-file payload is large enough to exhaust or heavily pressure
# distro tmpfs mounts (notably Bazzite/SteamOS). Extract into the user's
# disk-backed cache instead; the bootloader still removes its per-run _MEI
# directory on exit. Fall back to the inherited TMPDIR if this is unavailable.
WF_RUNTIME_TMP="${XDG_CACHE_HOME:-${HOME}/.cache}/wayfinder-aura/runtime"
if mkdir -p "$WF_RUNTIME_TMP" 2>/dev/null && chmod 700 "$WF_RUNTIME_TMP" 2>/dev/null; then
    export TMPDIR="$WF_RUNTIME_TMP"
fi

# Make the bundled design fonts (Inter / JetBrains Mono) visible to
# fontconfig ON TOP of the host's fonts. fontconfig has no "extra dir" env
# var and its config can't expand arbitrary variables, so generate a tiny
# config with the absolute AppDir path baked in. Best-effort: if anything
# fails the app falls back to host font substitution (pre-1.1.5 behavior).
WF_FC_DIR="${XDG_RUNTIME_DIR:-/tmp/wayfinder-aura-$(id -u)}/wayfinder-aura-fontconfig"
if mkdir -p "$WF_FC_DIR" 2>/dev/null; then
    cat > "$WF_FC_DIR/fonts.conf" << FCEOF
<?xml version="1.0"?>
<!DOCTYPE fontconfig SYSTEM "fonts.dtd">
<fontconfig>
  <include ignore_missing="yes">/etc/fonts/fonts.conf</include>
  <dir>${HERE}/usr/share/fonts/wayfinder-aura</dir>
</fontconfig>
FCEOF
    if [ -s "$WF_FC_DIR/fonts.conf" ]; then
        export FONTCONFIG_FILE="$WF_FC_DIR/fonts.conf"
    fi
fi

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
OUTPUT_NAME="${OUTPUT_NAME:-Wayfinder_Aura-${VERSION}-${ARCH}.AppImage}"

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
