#!/usr/bin/env bash
# Programs the bitstream, initializes the PS, then downloads and runs the
# firmware ELF on the board over JTAG via xsdb — everything the Vitis IDE's
# "Run" button does under the hood, no Vivado Hardware Manager or IDE app
# component needed. Board must be connected/powered.
#
# Best-effort standard xsdb sequence for Zynq-7000 without an FSBL, not yet
# run end-to-end in this repo — if it errors, fall back to the Vitis IDE
# ("Run As -> Launch on Hardware") described in README.md.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_PATH="${REPO_PATH:-$(cd "$SCRIPT_DIR/../.." && pwd)}"
ELF="$SCRIPT_DIR/build/app/lwip_tcp_perf_client.elf"
PS7_INIT_TCL="$SCRIPT_DIR/build/sizif_platform/hw/sdt/ps7_init.tcl"
DEFAULT_BIT="$REPO_PATH/vivado/sizif/build/tcp_client/tcp_client.runs/impl_1/CoraZ7_Eth_wrapper.bit"
BIT="${1:-$DEFAULT_BIT}"

exec > >(tee "$SCRIPT_DIR/run.log") 2>&1

for f in "$ELF" "$PS7_INIT_TCL" "$BIT"; do
    if [[ ! -f "$f" ]]; then
        echo "Missing required file: $f"
        exit 1
    fi
done

TCL_SCRIPT="$(mktemp --suffix=.tcl)"
trap 'rm -f "$TCL_SCRIPT"' EXIT

cat > "$TCL_SCRIPT" <<EOF
connect
targets -set -filter {name =~ "xc7z*"}
fpga -file {${BIT}}
source {${PS7_INIT_TCL}}
ps7_init
ps7_post_config
targets -set -filter {name =~ "*Cortex-A9 #0*"}
rst -processor
dow {${ELF}}
con
EOF

(cd "$SCRIPT_DIR/build" && xsdb "$TCL_SCRIPT")
