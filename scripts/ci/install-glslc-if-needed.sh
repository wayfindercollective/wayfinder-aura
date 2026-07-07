#!/usr/bin/env bash
# Install glslc for Vulkan-native AppImage builds on older CI runners.

set -euo pipefail

SHADERC_REPO="https://github.com/google/shaderc.git"
SHADERC_TAG="${SHADERC_TAG:-v2024.4}"
SHADERC_COMMIT="${SHADERC_COMMIT:-caa54d9779d5605aca4e1a0c0c962a3d8f4aeb31}"
BUILD_JOBS="${BUILD_JOBS:-$(nproc)}"
INSTALL_PREFIX="${GLSLC_INSTALL_PREFIX:-/usr/local}"

if command -v glslc >/dev/null 2>&1; then
    echo "glslc already available: $(command -v glslc)"
    glslc --version
    exit 0
fi

WORK_DIR="${RUNNER_TEMP:-/tmp}/wayfinder-shaderc"
SOURCE_DIR="$WORK_DIR/shaderc"
BUILD_DIR="$WORK_DIR/build"

rm -rf "$WORK_DIR"
mkdir -p "$WORK_DIR"

git clone --depth 1 --branch "$SHADERC_TAG" "$SHADERC_REPO" "$SOURCE_DIR"

actual_commit="$(git -C "$SOURCE_DIR" rev-parse HEAD)"
if [ "$actual_commit" != "$SHADERC_COMMIT" ]; then
    git -C "$SOURCE_DIR" fetch --depth 1 origin "$SHADERC_COMMIT"
    git -C "$SOURCE_DIR" checkout --detach "$SHADERC_COMMIT"
fi

python3 "$SOURCE_DIR/utils/git-sync-deps"

cmake -S "$SOURCE_DIR" -B "$BUILD_DIR" \
    -DCMAKE_BUILD_TYPE=Release \
    -DSHADERC_SKIP_TESTS=ON \
    -DSHADERC_SKIP_EXAMPLES=ON
cmake --build "$BUILD_DIR" --target glslc --config Release -j"$BUILD_JOBS"

if [ -w "$INSTALL_PREFIX/bin" ]; then
    install -m 0755 "$BUILD_DIR/glslc/glslc" "$INSTALL_PREFIX/bin/glslc"
else
    sudo install -m 0755 "$BUILD_DIR/glslc/glslc" "$INSTALL_PREFIX/bin/glslc"
fi

glslc --version
