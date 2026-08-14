#!/usr/bin/env bash
# Builds the whole "sizif" stack from tracked source, no GUI steps:
#   vivado/sizif -> bitstream + .xsa
#   vitis/sizif  -> platform + app (firmware ELF)
#   pc_app/sizif -> relay server binary + venv
# Does NOT flash/run anything on hardware or start the PC app -- see
# vitis/sizif/run.sh and pc_app/sizif/system.sh for that, run separately.
set -euo pipefail
trap 'echo "FAILED: $BASH_COMMAND (line $LINENO)" >&2' ERR

REPO_PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$REPO_PATH/bootenv.sh"

exec > >(tee "$REPO_PATH/build_all.log") 2>&1

echo "=== [1/5] Vivado: create project ==="
"$REPO_PATH/vivado/sizif/build.sh"

echo "=== [2/5] Vivado: synth + impl + bitstream + .xsa (batch, no GUI) ==="
"$REPO_PATH/vivado/sizif/build_bitstream.sh"

XSA="$REPO_PATH/vivado/sizif/build/tcp_client/CoraZ7_Eth_wrapper.xsa"

echo "=== [3/5] Vitis: platform + throwaway app (Python API) ==="
"$REPO_PATH/vitis/sizif/build_platform.sh" "$XSA"

echo "=== [4/5] Vitis: firmware app ==="
"$REPO_PATH/vitis/sizif/build_app.sh"

echo "=== [5/5] PC app: relay server + venv ==="
"$REPO_PATH/pc_app/sizif/build.sh"

echo
echo "All builds complete."
echo "  Flash + run firmware on hardware: ./run_all.sh (or vitis/sizif/run.sh)"
echo "  Start the PC app:                 pc_app/sizif/system.sh start"
echo "  Changed only firmware C / relay C++ since? Use ./compile_all.sh"
echo "  instead of rerunning this script (skips the slow Vivado/platform steps)."
