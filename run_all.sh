#!/usr/bin/env bash
# Starts the PC app (relay server + Python client) and flashes/runs the
# "sizif" firmware on the board over JTAG (which itself opens a PuTTY
# serial console for [STATS] output). Assumes build_all.sh has already
# been run successfully. Board must be connected/powered.
set -euo pipefail
trap 'echo "FAILED: $BASH_COMMAND (line $LINENO)" >&2' ERR

REPO_PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$REPO_PATH/bootenv.sh"

exec > >(tee "$REPO_PATH/run_all.log") 2>&1

if grep -q "^BOARD_CONNECTED = False" "$REPO_PATH/pc_app/sizif/config.py"; then
    echo "WARNING: pc_app/sizif/config.py has BOARD_CONNECTED = False"
    echo "         (PC-only loopback mode) -- it will not talk to the real"
    echo "         board. Edit that line to True if you want to test against"
    echo "         actual hardware. Continuing anyway..."
    echo
fi

echo "=== [1/2] PC app: starting relay server + Python client ==="
"$REPO_PATH/pc_app/sizif/system.sh" start

echo "=== [2/2] Board: flashing + running firmware (opens PuTTY) ==="
"$REPO_PATH/vitis/sizif/run.sh"

echo
echo "Board flashed and running; PC app started."
echo "  PC app status/logs: pc_app/sizif/system.sh status|logs"
echo "  Stop PC app:         pc_app/sizif/system.sh stop"
