#!/usr/bin/env bash
# Fast-path recompile for the "sizif" stack: only the firmware C code and
# the C++ relay server, skipping the slow hardware/platform steps
# (Vivado synth+impl, Vitis platform creation) that build_all.sh does.
#
# Use this after editing vitis/sizif/app/src/*.c or
# pc_app/sizif/tcp_server_app.cpp -- run build_all.sh instead only if the
# Vivado bitstream or the Vitis platform itself changed.
#
# Requires build_all.sh to have been run at least once already (needs the
# Vitis platform export and does not create it).
set -euo pipefail

REPO_PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$REPO_PATH/bootenv.sh"

exec > >(tee "$REPO_PATH/compile_all.log") 2>&1

echo "=== [1/2] Vitis: rebuild firmware app from C source ==="
"$REPO_PATH/vitis/sizif/build_app.sh"

echo "=== [2/2] PC app: rebuild C++ relay server (skips venv if present) ==="
"$REPO_PATH/pc_app/sizif/build.sh"

echo
echo "Recompile complete."
echo "  Flash + run firmware on hardware: ./run_all.sh (or vitis/sizif/run.sh)"
echo "  Start the PC app:                 pc_app/sizif/system.sh start"
