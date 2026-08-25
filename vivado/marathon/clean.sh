#!/usr/bin/env bash
# Wipes ./build/ back to empty so you can test that everything regenerates
# cleanly from tracked source files alone (cora_z7.tcl + hdl/ + xdc/ + dcp/).
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
exec > >(tee "$SCRIPT_DIR/clean.log") 2>&1
rm -rf "$SCRIPT_DIR/build"
mkdir -p "$SCRIPT_DIR/build"
touch "$SCRIPT_DIR/build/.gitkeep"

# Mop up stray Vivado run artifacts from old runs that landed directly in
# this dir instead of build/ (build_bitstream.sh now cd's into build/
# first, so this shouldn't recur, but clean up anything already here).
rm -f "$SCRIPT_DIR"/vivado*.jou "$SCRIPT_DIR"/vivado*.log
rm -rf "$SCRIPT_DIR/.Xil"

echo "$(realpath --relative-to="$REPO_ROOT" "$SCRIPT_DIR/build")/ wiped clean."
