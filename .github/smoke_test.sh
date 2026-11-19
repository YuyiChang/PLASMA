#!/usr/bin/env bash
# Shared entry point for macOS/Linux/Windows (Git Bash). Python handles native
# process IDs, per-run ports, bounded HTTP probes, and process-tree cleanup.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Git Bash paths such as /c/PLASMA_installed are not native Windows paths.
if command -v cygpath >/dev/null 2>&1; then
  export PLASMA_BIN="$(cygpath -w "${PLASMA_BIN:?set PLASMA_BIN}")"
fi
exec python "$SCRIPT_DIR/smoke_test.py"
