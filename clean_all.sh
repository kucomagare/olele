#!/usr/bin/env bash
# Wipes build/ in every <tool>/$VARIANT dir, for testing that build_all.sh
# regenerates everything from tracked source alone.
#
# Acts on ONE variant only -- another variant's build/ is left alone, so a
# slow Vivado rebuild isn't triggered by accident. Clean another with:
#     VARIANT=<project> ./clean_all.sh
set -euo pipefail

REPO_PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Not sourced from bootenv.sh here (this script needs no Xilinx tools), so
# VARIANT isn't set by it either. The default variant name lives only in
# bootenv.sh -- source it first (or export VARIANT yourself) rather than
# relying on this script to guess.
if [[ -z "${VARIANT:-}" ]]; then
    echo "VARIANT is not set. Run 'source bootenv.sh' first (sets the" >&2
    echo "default), or pass it explicitly: VARIANT=<project> ./clean_all.sh" >&2
    exit 1
fi

exec > >(tee "$REPO_PATH/clean_all.log") 2>&1

echo "=== variant: $VARIANT ==="

"$REPO_PATH/vivado/$VARIANT/clean.sh"
"$REPO_PATH/vitis/$VARIANT/clean.sh"
"$REPO_PATH/pc_app/$VARIANT/clean.sh"

# Mop up stray Vivado run artifacts from old runs of build_bitstream.sh
# invoked via build_all.sh (repo root was its cwd at the time) -- it now
# cd's into vivado/$VARIANT/build/ first, so this shouldn't recur, but clean
# up anything already here. Root-level *.log here is otherwise these
# orchestration scripts' own logs (build_all.log etc.), left alone.
rm -f "$REPO_PATH"/vivado*.jou "$REPO_PATH"/vivado*.log
rm -rf "$REPO_PATH/.Xil"

echo
echo "All build/ dirs wiped clean for variant: $VARIANT"
