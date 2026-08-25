#!/usr/bin/env bash
# Stops the PC app (relay server + Python client) and the PuTTY serial
# console that run.sh launched. The board's firmware itself has no "stop"
# -- it keeps running until reset/reflashed (rerun ./run_all.sh) or
# power-cycled.
#
# Stops ONE variant's PC app. If you started the other one, stop that too:
#     VARIANT=marathon ./stop_all.sh
set -euo pipefail

REPO_PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Not sourced from bootenv.sh here (no Xilinx tools needed), so the default
# is repeated. Keep it in sync with bootenv.sh.
VARIANT="${VARIANT:-sizif}"

exec > >(tee "$REPO_PATH/stop_all.log") 2>&1

echo "=== variant: $VARIANT ==="

"$REPO_PATH/pc_app/$VARIANT/system.sh" stop

PUTTY_PID_FILE="$REPO_PATH/vitis/$VARIANT/build/putty.pid"
if [[ -f "$PUTTY_PID_FILE" ]]; then
    PUTTY_PID="$(cat "$PUTTY_PID_FILE")"
    # Verify the PID is actually still putty before signaling it -- a
    # stale PID file whose PID got reused by an unrelated process is
    # unlikely but possible (e.g. after a reboot).
    if [[ -n "$PUTTY_PID" ]] && kill -0 "$PUTTY_PID" 2>/dev/null \
        && ps -p "$PUTTY_PID" -o args= 2>/dev/null | grep -qF putty; then
        echo "Stopping PuTTY (PID $PUTTY_PID)..."
        kill "$PUTTY_PID" 2>/dev/null || true
    else
        echo "PuTTY not running"
    fi
    rm -f "$PUTTY_PID_FILE"
else
    echo "PuTTY not running (no PID file)"
fi
