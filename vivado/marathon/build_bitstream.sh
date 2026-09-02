#!/usr/bin/env bash
# Batch-mode synthesis + implementation + bitstream + .xsa export for
# vivado/<variant> -- no Vivado GUI needed. Requires the project to already
# exist (./build.sh first). Output: the existing build/tcp_client project
# gets its runs built, plus build/tcp_client/CoraZ7_Eth_wrapper.xsa.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_DIR="$SCRIPT_DIR/build"
XPR="$BUILD_DIR/tcp_client/tcp_client.xpr"
XSA="$BUILD_DIR/tcp_client/CoraZ7_Eth_wrapper.xsa"

exec > >(tee "$SCRIPT_DIR/build_bitstream.log") 2>&1

if [[ ! -f "$XPR" ]]; then
    echo "Project not found at $XPR -- run ./build.sh first."
    exit 1
fi

# cd into build/ first so vivado's own cwd-relative artifacts (vivado.jou,
# vivado.log, .Xil/) land there (gitignored, wiped by clean.sh) instead of
# wherever this script happened to be invoked from -- e.g. the repo root
# when run via './proj build', or vivado/<variant>/ when run directly.
cd "$BUILD_DIR"

TCL_SCRIPT="$(mktemp --suffix=.tcl)"
trap 'rm -f "$TCL_SCRIPT"' EXIT

cat > "$TCL_SCRIPT" <<EOF
open_project {${XPR}}

launch_runs synth_1 -jobs $(nproc)
wait_on_run synth_1
if {[get_property PROGRESS [get_runs synth_1]] != "100%"} {
    error "synth_1 did not complete successfully"
}

launch_runs impl_1 -to_step write_bitstream -jobs $(nproc)
wait_on_run impl_1
if {[get_property PROGRESS [get_runs impl_1]] != "100%"} {
    error "impl_1 did not complete successfully"
}

open_run impl_1
write_hw_platform -fixed -include_bit -force -file {${XSA}}
EOF

vivado -mode batch -source "$TCL_SCRIPT"

echo
echo "Bitstream + hardware platform exported: $XSA"
echo "Next: vitis/$(basename "$SCRIPT_DIR")/build_platform.sh \"$XSA\""
