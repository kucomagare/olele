#!/usr/bin/env bash
# Fast-path recompile for one variant's stack: only the firmware C code and
# the C++ relay server, skipping the slow hardware/platform steps
# (Vivado synth+impl, Vitis platform creation) that build_all.sh does.
#
# Use this after editing vitis/$VARIANT/app/src/*.c or
# pc_app/$VARIANT/tcp_server_app.cpp -- run build_all.sh instead only if the
# Vivado bitstream or the Vitis platform itself changed.
#
# VARIANT selects the tree; its default lives only in bootenv.sh:
#     VARIANT=<project> ./compile_all.sh
#
# Requires build_all.sh to have been run at least once already (needs the
# Vitis platform export and does not create it).
set -euo pipefail
trap 'echo "FAILED: $BASH_COMMAND (line $LINENO)" >&2' ERR

REPO_PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$REPO_PATH/bootenv.sh"

exec > >(tee "$REPO_PATH/compile_all.log") 2>&1

echo "=== variant: $VARIANT ==="

echo "=== [1/2] Vitis: rebuild firmware app from C source ==="
"$REPO_PATH/vitis/$VARIANT/build_app.sh"

echo "=== [2/2] PC app: rebuild C++ relay server (skips venv if present) ==="
"$REPO_PATH/pc_app/$VARIANT/build.sh"

echo
echo "Recompile complete."
echo "  Flash + run firmware on hardware: VARIANT=$VARIANT ./run_all.sh"
echo "  Start the PC app:                 pc_app/$VARIANT/system.sh start"
