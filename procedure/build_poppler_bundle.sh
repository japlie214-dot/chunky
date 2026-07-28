#!/bin/bash
# ============================================================================
# build_poppler_bundle.sh  [DEPRECATED]
#
# This script produces a standalone `poppler_bundle.zip` containing ONLY
# poppler binaries + the pdf2image Python package. It is kept for
# environments that want the legacy two-bundle layout.
#
# The recommended path is now `build_bundle.py`, which produces a single
# `utils_bundle.zip` containing chunky_utils/ + poppler_bundle/ + pdf2image/
# and supports both ARM64 (default) and x86_64 target architectures.
#
# This legacy script uses the host's own poppler-utils install, so it
# produces binaries matching the host architecture. For Snowflake ARM
# warehouses, run this script ON an ARM64 host, or use build_bundle.py
# (which can cross-build ARM64 from x86_64 via Debian .deb downloads).
# ============================================================================
set -e

ARCH="${1:-$(uname -m)}"
BUNDLE_DIR="poppler_bundle"
echo "Building poppler_bundle.zip (legacy two-bundle layout, arch: ${ARCH})..."

# Clean
rm -rf "$BUNDLE_DIR" poppler_bundle.zip
mkdir -p "$BUNDLE_DIR/pdf2image_pkg" "$BUNDLE_DIR/poppler/bin" "$BUNDLE_DIR/poppler/lib"

# 1. Install poppler-utils (system package)
echo "Installing poppler-utils..."
apt-get update -qq && apt-get install -y -qq poppler-utils > /dev/null 2>&1

# 2. Install pdf2image (Python package)
echo "Installing pdf2image..."
pip install pdf2image -t "$BUNDLE_DIR/pdf2image_pkg" --quiet

# 3. Copy poppler binaries
echo "Copying poppler binaries..."
for bin in pdftoppm pdfinfo pdftotext; do
    BIN_PATH=$(which "$bin" 2>/dev/null || true)
    if [ -n "$BIN_PATH" ]; then
        cp "$BIN_PATH" "$BUNDLE_DIR/poppler/bin/"
        echo "  Copied: $bin"
    else
        echo "  WARNING: $bin not found"
    fi
done

# 4. Copy ALL shared library dependencies
echo "Copying shared libraries..."
for bin in "$BUNDLE_DIR/poppler/bin"/*; do
    ldd "$bin" 2>/dev/null | grep '=>' | awk '{print $3}' | while read lib; do
        if [ -f "$lib" ]; then
            cp -n "$lib" "$BUNDLE_DIR/poppler/lib/" 2>/dev/null || true
        fi
    done
done

# Also copy the dynamic linker matching the host arch
case "$ARCH" in
    x86_64|amd64)
        for lib in /lib64/ld-linux-x86-64.so.2 /lib/x86_64-linux-gnu/ld-linux-x86-64.so.2; do
            [ -f "$lib" ] && cp -n "$lib" "$BUNDLE_DIR/poppler/lib/" 2>/dev/null || true
        done
        ;;
    aarch64|arm64)
        for lib in /lib/ld-linux-aarch64.so.1 /lib/aarch64-linux-gnu/ld-linux-aarch64.so.1; do
            [ -f "$lib" ] && cp -n "$lib" "$BUNDLE_DIR/poppler/lib/" 2>/dev/null || true
        done
        ;;
esac

# 5. Zip
echo "Zipping..."
zip -r poppler_bundle.zip "$BUNDLE_DIR/" -q

SIZE=$(du -h poppler_bundle.zip | cut -f1)
echo ""
echo "✅ Built poppler_bundle.zip ($SIZE) — legacy two-bundle layout, arch: ${ARCH}."
echo ""
echo "⚠️  DEPRECATED: Prefer procedure/build_bundle.py for the single-bundle layout."
echo "    build_bundle.py defaults to ARM64 (Snowflake ARM warehouses) and can"
echo "    cross-build from x86_64 hosts without Docker or qemu."
echo ""
echo "Upload to Snowflake:"
echo "  PUT file://$(pwd)/poppler_bundle.zip @DEV_DB.DNA.STG_LIB AUTO_COMPRESS=FALSE;"
echo ""
echo "Contents:"
find "$BUNDLE_DIR" -type f | head -30
echo "..."
