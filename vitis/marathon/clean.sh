#!/usr/bin/env bash
# Wipes ./build/ back to empty so you can test that everything regenerates
# cleanly from tracked source files alone (app/ + the build_*.sh scripts).
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
exec > >(tee "$SCRIPT_DIR/clean.log") 2>&1
rm -rf "$SCRIPT_DIR/build"
mkdir -p "$SCRIPT_DIR/build"
touch "$SCRIPT_DIR/build/.gitkeep"
rm -rf "$SCRIPT_DIR/logs" "$SCRIPT_DIR/.Xil"
echo "$(realpath --relative-to="$REPO_ROOT" "$SCRIPT_DIR/build")/ wiped clean."
