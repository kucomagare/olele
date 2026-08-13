#!/usr/bin/env bash
# Wipes ./build/ back to empty so you can test that everything regenerates
# cleanly from tracked source files alone (cora_z7.tcl + hdl/ + xdc/ + dcp/).
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec > >(tee "$SCRIPT_DIR/clean.log") 2>&1
rm -rf "$SCRIPT_DIR/build"
mkdir -p "$SCRIPT_DIR/build"
touch "$SCRIPT_DIR/build/.gitkeep"
echo "build/ wiped clean."
