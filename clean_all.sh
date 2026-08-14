#!/usr/bin/env bash
# Wipes build/ in every <tool>/sizif dir, for testing that build_all.sh
# regenerates everything from tracked source alone.
set -euo pipefail

REPO_PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

exec > >(tee "$REPO_PATH/clean_all.log") 2>&1

"$REPO_PATH/vivado/sizif/clean.sh"
"$REPO_PATH/vitis/sizif/clean.sh"
"$REPO_PATH/pc_app/sizif/clean.sh"

# Mop up stray Vivado run artifacts from old runs of build_bitstream.sh
# invoked via build_all.sh (repo root was its cwd at the time) -- it now
# cd's into vivado/sizif/build/ first, so this shouldn't recur, but clean
# up anything already here. Root-level *.log here is otherwise these
# orchestration scripts' own logs (build_all.log etc.), left alone.
rm -f "$REPO_PATH"/vivado*.jou "$REPO_PATH"/vivado*.log
rm -rf "$REPO_PATH/.Xil"

echo
echo "All build/ dirs wiped clean."
