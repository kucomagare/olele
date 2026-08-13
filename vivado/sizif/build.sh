#!/usr/bin/env bash
# Regenerates the Vivado project for the "sizif" hardware version from
# cora_z7.tcl into ./build/ (gitignored). Run this, then open the project
# in the Vivado GUI (or drive it further in batch mode) to run
# synthesis/implementation and generate the bitstream/.xsa.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_DIR="$SCRIPT_DIR/build"

exec > >(tee "$SCRIPT_DIR/build.log") 2>&1

mkdir -p "$BUILD_DIR"
cd "$BUILD_DIR"

vivado -mode batch -source "$SCRIPT_DIR/cora_z7.tcl" -tclargs --origin_dir "$SCRIPT_DIR"

echo
echo "Project created under: $BUILD_DIR"
echo "Open it with: vivado $BUILD_DIR/tcp_client/tcp_client.xpr"
