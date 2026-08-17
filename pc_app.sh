#!/usr/bin/env bash
# Thin root-level passthrough to pc_app/sizif/system.sh, so the PC side can
# be driven from the repo root without cd'ing or remembering the path:
#
#   ./pc_app.sh restart-client   # after editing config.py or another module
#   ./pc_app.sh restart-server   # after editing tcp_server_app.cpp
#   ./pc_app.sh status|logs|start|stop|restart
#
# Deliberately NOT named *_all.sh: every other root script acts on the whole
# stack (board included), this one only ever touches the PC side. Use it
# when you want to re-run the Python client against new settings without
# reflashing the board -- run_all.sh always does both.
#
# No `tee` of its own: system.sh already logs to pc_app/sizif/system.log,
# and a second copy at the repo root would just be litter.
set -euo pipefail
trap 'echo "FAILED: $BASH_COMMAND (line $LINENO)" >&2' ERR

REPO_PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

exec "$REPO_PATH/pc_app/sizif/system.sh" "$@"
