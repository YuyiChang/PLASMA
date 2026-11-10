#!/usr/bin/env bash
# Build the macOS library with the resolver fix; do not modify conda/Homebrew.
# Usage: bash .github/build_liblsl_macos.sh <work-dir> [existing-source-archive]
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
mkdir -p "${1:?provide a build directory}"
WORK="$(cd "$1" && pwd)"
ARCHIVE="${2:-$WORK/liblsl-1.17.5.tar.gz}"
if [ ! -f "$ARCHIVE" ]; then
    curl -L --fail --max-time 60 \
        https://codeload.github.com/sccn/liblsl/tar.gz/refs/tags/v1.17.5 -o "$ARCHIVE"
fi
echo "6f1f5a3fc4c4a162c86ced19c75d13a81d8ebe1f819c50caf257b1ee0b401d1a  $ARCHIVE" | shasum -a 256 -c -
tar -xzf "$ARCHIVE" -C "$WORK"
patch --batch -d "$WORK/liblsl-1.17.5" -p1 < "$ROOT/.github/patches/liblsl-unavailable-interface.patch"
cmake -S "$WORK/liblsl-1.17.5" -B "$WORK/build" \
    -DCMAKE_BUILD_TYPE=Release -DLSL_FRAMEWORK=OFF \
    -DCMAKE_OSX_DEPLOYMENT_TARGET=11.0 -DCMAKE_INSTALL_PREFIX="$WORK/install"
cmake --build "$WORK/build" --config Release --parallel 3
cmake --install "$WORK/build" --config Release
PYLSL_LIB="$WORK/install/lib/liblsl.dylib" python "$ROOT/.github/test_liblsl_interfaces.py"
