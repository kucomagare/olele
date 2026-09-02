#!/usr/bin/env bash
# Builds one variant's whole stack from tracked source, no GUI steps:
#   vivado/$VARIANT -> bitstream + .xsa
#   vitis/$VARIANT  -> platform + app (firmware ELF)
#   pc_app/$VARIANT -> relay server binary + venv
# Does NOT flash/run anything on hardware or start the PC app -- see
# vitis/$VARIANT/run.sh and pc_app/$VARIANT/system.sh for that, run separately.
#
# VARIANT selects which <tool>/<variant>/ tree to build; its default lives
# only in bootenv.sh. Build another one with:
#     VARIANT=<project> ./build_all.sh
set -euo pipefail
trap 'echo "FAILED: $BASH_COMMAND (line $LINENO)" >&2' ERR

REPO_PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$REPO_PATH/bootenv.sh"

exec > >(tee "$REPO_PATH/build_all.log") 2>&1

echo "=== variant: $VARIANT ==="

echo "=== [1/5] Vivado: create project ==="
"$REPO_PATH/vivado/$VARIANT/build.sh"

echo "=== [2/5] Vivado: synth + impl + bitstream + .xsa (batch, no GUI) ==="
"$REPO_PATH/vivado/$VARIANT/build_bitstream.sh"

XSA="$REPO_PATH/vivado/$VARIANT/build/tcp_client/CoraZ7_Eth_wrapper.xsa"

echo "=== [3/5] Vitis: platform + throwaway app (Python API) ==="
"$REPO_PATH/vitis/$VARIANT/build_platform.sh" "$XSA"

echo "=== [4/5] Vitis: firmware app ==="
"$REPO_PATH/vitis/$VARIANT/build_app.sh"

echo "=== [5/5] PC app: relay server + venv ==="
"$REPO_PATH/pc_app/$VARIANT/build.sh"

echo
echo "All builds complete."
echo "  Flash + run firmware on hardware: VARIANT=$VARIANT ./run_all.sh"
echo "  Start the PC app:                 pc_app/$VARIANT/system.sh start"
echo "  Changed only firmware C / relay C++ since? Use ./compile_all.sh"
echo "  instead of rerunning this script (skips the slow Vivado/platform steps)."
