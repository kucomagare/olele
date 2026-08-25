#!/usr/bin/env bash
# Starts the PC app (relay server + Python client) and flashes/runs one
# variant's firmware on the board over JTAG (which itself opens a PuTTY
# serial console for [STATS] output). Assumes build_all.sh has already
# been run successfully for that variant. Board must be connected/powered.
#
# VARIANT selects the tree (bootenv.sh defaults it to "sizif"):
#     VARIANT=marathon ./run_all.sh
#
# Only ONE variant can own the board at a time -- they flash the same part.
set -euo pipefail
trap 'echo "FAILED: $BASH_COMMAND (line $LINENO)" >&2' ERR

REPO_PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$REPO_PATH/bootenv.sh"

exec > >(tee "$REPO_PATH/run_all.log") 2>&1

echo "=== variant: $VARIANT ==="

if grep -q "^BOARD_CONNECTED = False" "$REPO_PATH/pc_app/$VARIANT/config.py"; then
    echo "WARNING: pc_app/$VARIANT/config.py has BOARD_CONNECTED = False"
    echo "         (PC-only loopback mode) -- it will not talk to the real"
    echo "         board. Edit that line to True if you want to test against"
    echo "         actual hardware. Continuing anyway..."
    echo
fi

echo "=== [1/2] PC app: starting relay server + Python client ==="
"$REPO_PATH/pc_app/$VARIANT/system.sh" start

echo "=== [2/2] Board: flashing + running firmware (opens PuTTY) ==="
"$REPO_PATH/vitis/$VARIANT/run.sh"

echo
echo "Board flashed and running; PC app started."
echo "  PC app status/logs: pc_app/$VARIANT/system.sh status|logs"
echo "  Stop PC app:         pc_app/$VARIANT/system.sh stop"
