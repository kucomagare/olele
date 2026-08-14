#!/usr/bin/env bash
# Programs the bitstream, initializes the PS, then downloads and runs the
# firmware ELF on the board over JTAG via xsdb — everything the Vitis IDE's
# "Run" button does under the hood, no Vivado Hardware Manager or IDE app
# component needed. Board must be connected/powered.
#
# Standard xsdb sequence for Zynq-7000 without an FSBL. Target names
# confirmed on this board's actual `targets` output:
#   1  APU
#      2  ARM Cortex-A9 MPCore #0
#      3  ARM Cortex-A9 MPCore #1
#   4  xc7z010
#      5  Legacy Debug Hub
# `fpga -file` needs the `xc7z*` (PL config) target; `ps7_init`/
# `ps7_post_config` need the `APU` target (system/SLCR register access via
# the debug context) -- running them against `xc7z*` instead fails with
# "Context does not support memory read. Unsupported command". The final
# `dow`/`con` need a specific core, `*Cortex-A9 MPCore #0*` (not the
# shorter `*Cortex-A9 #0*`, which doesn't substring-match this target's
# real name and silently selects nothing / the wrong target).
#
# Also auto-launches a PuTTY serial console for the firmware's UART output
# ([STATS] lines) before the xsdb sequence runs, so it's ready to catch
# output from the very first line. The board's onboard Digilent USB-JTAG
# chip enumerates as two ttyUSB devices -- if00 is JTAG (used internally by
# hw_server), if01 is the UART -- addressed via the stable
# /dev/serial/by-id/ symlink rather than a raw /dev/ttyUSBn path, since
# ttyUSB numbering isn't guaranteed to stay consistent across
# reconnects/reboots. Best-effort: if putty or the serial device isn't
# found, this just warns and continues with the JTAG flash/run.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_PATH="${REPO_PATH:-$(cd "$SCRIPT_DIR/../.." && pwd)}"
ELF="$SCRIPT_DIR/build/app/lwip_tcp_perf_client.elf"
PS7_INIT_TCL="$SCRIPT_DIR/build/sizif_platform/hw/sdt/ps7_init.tcl"
DEFAULT_BIT="$REPO_PATH/vivado/sizif/build/tcp_client/tcp_client.runs/impl_1/CoraZ7_Eth_wrapper.bit"
BIT="${1:-$DEFAULT_BIT}"
UART_BAUD="115200"

exec > >(tee "$SCRIPT_DIR/run.log") 2>&1

for f in "$ELF" "$PS7_INIT_TCL" "$BIT"; do
    if [[ ! -f "$f" ]]; then
        echo "Missing required file: $f"
        exit 1
    fi
done

PUTTY_PID_FILE="$SCRIPT_DIR/build/putty.pid"
PUTTY_LOG_DIR="$SCRIPT_DIR/build/putty_logs"
UART_DEV="$(find /dev/serial/by-id -iname "*Digilent*if01*" 2>/dev/null | head -1)"
if command -v putty >/dev/null 2>&1 && [[ -n "$UART_DEV" ]]; then
    mkdir -p "$PUTTY_LOG_DIR"
    PUTTY_LOG="$PUTTY_LOG_DIR/session_$(date +%Y%m%d_%H%M%S).log"
    echo "Launching PuTTY serial console on $UART_DEV @ ${UART_BAUD} baud"
    echo "Logging session output to: $PUTTY_LOG"
    putty -serial "$UART_DEV" -sercfg "${UART_BAUD},8,n,1,N" -log "$PUTTY_LOG" >/dev/null 2>&1 &
    echo $! > "$PUTTY_PID_FILE"
else
    echo "Skipping PuTTY auto-launch (putty and/or the board's UART device not found)."
fi

TCL_SCRIPT="$(mktemp --suffix=.tcl)"
trap 'rm -f "$TCL_SCRIPT"' EXIT

cat > "$TCL_SCRIPT" <<EOF
connect
targets -set -filter {name =~ "xc7z*"}
fpga -file {${BIT}}
targets -set -filter {name =~ "APU*"}
source {${PS7_INIT_TCL}}
ps7_init
ps7_post_config
targets -set -filter {name =~ "*Cortex-A9 MPCore #0*"}
rst -processor
dow {${ELF}}
con
puts "RUN.SH: FLASH+RUN COMPLETE"
EOF

(cd "$SCRIPT_DIR/build" && xsdb "$TCL_SCRIPT")
