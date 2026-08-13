#!/usr/bin/env bash
# Source this once per shell before running any build/run script in this
# repo — do NOT execute it directly:
#
#   source /path/to/olele/bootenv.sh
#
# Sets:
#   REPO_PATH             - this repo's root (wherever it's actually checked out)
#   SYSTEM_CMAKE           - the system cmake, captured *before* Xilinx's
#                             settings scripts touch PATH (the Vitis-bundled
#                             cmake-3.24.2 needs libssl.so.10, which isn't
#                             installed here — build scripts should invoke
#                             "${SYSTEM_CMAKE:-cmake}" rather than bare cmake)
#   ARM_GNU_TOOLCHAIN_BIN   - arm-none-eabi-gcc toolchain dir, added to PATH
#   VIVADO_SETTINGS / VITIS_SETTINGS - sourced if present, so vivado/vitis/
#                             xsct/xsdb land on PATH
#
# Adjust XILINX_VERSION / XILINX_INSTALL_DIR below if your install differs.

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    echo "bootenv.sh must be sourced, not executed: 'source bootenv.sh'" >&2
    exit 1
fi

export REPO_PATH
REPO_PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

export SYSTEM_CMAKE
SYSTEM_CMAKE="$(command -v cmake || true)"

XILINX_VERSION="2023.2"
XILINX_INSTALL_DIR="/tools/Xilinx"

export VIVADO_SETTINGS="$XILINX_INSTALL_DIR/Vivado/$XILINX_VERSION/settings64.sh"
export VITIS_SETTINGS="$XILINX_INSTALL_DIR/Vitis/$XILINX_VERSION/settings64.sh"
export ARM_GNU_TOOLCHAIN_BIN="$XILINX_INSTALL_DIR/Vitis/$XILINX_VERSION/gnu/aarch32/lin/gcc-arm-none-eabi/bin"

[[ -f "$VIVADO_SETTINGS" ]] && source "$VIVADO_SETTINGS"
[[ -f "$VITIS_SETTINGS" ]] && source "$VITIS_SETTINGS"
[[ -d "$ARM_GNU_TOOLCHAIN_BIN" ]] && export PATH="$ARM_GNU_TOOLCHAIN_BIN:$PATH"

echo "bootenv: REPO_PATH=$REPO_PATH"
echo "bootenv: SYSTEM_CMAKE=$SYSTEM_CMAKE"
