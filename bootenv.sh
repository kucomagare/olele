#!/usr/bin/env bash
# Source this once per shell before running any build/run script in this
# repo — do NOT execute it directly:
#
#   source /path/to/olele/bootenv.sh
#
# Sets:
#   REPO_PATH             - this repo's root (wherever it's actually checked out)
#   VARIANT               - which project variant the *_all.sh scripts act on.
#                             This file is the ONLY place its default is set
#                             (see below) -- every other script requires
#                             VARIANT to already be in the environment.
#                             Override per-shell with "export VARIANT=<name>"
#                             before sourcing, or per-command with
#                             "VARIANT=<name> ./build_all.sh".
#   SYSTEM_CMAKE           - the system cmake, captured *before* Xilinx's
#                             settings scripts touch PATH (the Vitis-bundled
#                             cmake-3.24.2 needs libssl.so.10, which isn't
#                             installed here — build scripts should invoke
#                             "${SYSTEM_CMAKE:-cmake}" rather than bare cmake)
#   ARM_GNU_TOOLCHAIN_BIN   - arm-none-eabi-gcc toolchain dir, added to PATH
#   VIVADO_SETTINGS / VITIS_SETTINGS - sourced if present, so vivado/vitis/
#                             xsct/xsdb land on PATH
#
# Also defines navigation aliases: cdrepo, cdvivado, cdvitis, cdpcapp
# (jump to the repo root / vivado/$VARIANT / vitis/$VARIANT / pc_app/$VARIANT).
# The aliases resolve $VARIANT at use time, so changing VARIANT in an already-
# open shell re-points them without re-sourcing.
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

# Which <tool>/<variant>/ tree the orchestration scripts act on.
#   sizif    - AXI-Lite architecture (frozen reference)
#   marathon - DMA/AXI-Stream architecture (active development)
# The name on the right of ":-" below is the repo-wide default variant.
# This is the ONLY place that name should appear -- every other script and
# comment in the repo refers to it generically (as "the default variant" or
# "<project>"), so changing the default is a one-line edit here.
export VARIANT="${VARIANT:-marathon}"

XILINX_VERSION="2023.2"
XILINX_INSTALL_DIR="/tools/Xilinx"

export VIVADO_SETTINGS="$XILINX_INSTALL_DIR/Vivado/$XILINX_VERSION/settings64.sh"
export VITIS_SETTINGS="$XILINX_INSTALL_DIR/Vitis/$XILINX_VERSION/settings64.sh"
export ARM_GNU_TOOLCHAIN_BIN="$XILINX_INSTALL_DIR/Vitis/$XILINX_VERSION/gnu/aarch32/lin/gcc-arm-none-eabi/bin"

[[ -f "$VIVADO_SETTINGS" ]] && source "$VIVADO_SETTINGS"
[[ -f "$VITIS_SETTINGS" ]] && source "$VITIS_SETTINGS"
[[ -d "$ARM_GNU_TOOLCHAIN_BIN" ]] && export PATH="$ARM_GNU_TOOLCHAIN_BIN:$PATH"

# Navigation aliases. $VARIANT is deliberately NOT expanded here -- the
# aliases expand it when invoked, so "export VARIANT=<project>" in an
# already-open shell immediately re-points cdvivado/cdvitis/cdpcapp.
alias cdrepo="cd \"\$REPO_PATH\""
alias cdvivado="cd \"\$REPO_PATH/vivado/\$VARIANT\""
alias cdvitis="cd \"\$REPO_PATH/vitis/\$VARIANT\""
alias cdpcapp="cd \"\$REPO_PATH/pc_app/\$VARIANT\""

echo "bootenv: REPO_PATH=$REPO_PATH"
echo "bootenv: SYSTEM_CMAKE=$SYSTEM_CMAKE"
echo "bootenv: VARIANT=$VARIANT  (override: export VARIANT=<project>)"
echo "bootenv: aliases: cdrepo, cdvivado, cdvitis, cdpcapp"
