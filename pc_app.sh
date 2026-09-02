#!/usr/bin/env bash
# Thin root-level passthrough to pc_app/$VARIANT/system.sh, so the PC side can
# be driven from the repo root without cd'ing or remembering the path:
#
#   ./pc_app.sh restart-client   # after editing config.py or another module
#   ./pc_app.sh restart-server   # after editing tcp_server_app.cpp
#   ./pc_app.sh status|logs|start|stop|restart
#
# VARIANT picks which variant's PC app; its default lives only in
# bootenv.sh. All arguments are passed through to system.sh untouched, so
# VARIANT must be an env var, not a positional argument:
#     VARIANT=<project> ./pc_app.sh start
#
# Deliberately NOT named *_all.sh: every other root script acts on the whole
# stack (board included), this one only ever touches the PC side. Use it
# when you want to re-run the Python client against new settings without
# reflashing the board -- run_all.sh always does both.
#
# No `tee` of its own: system.sh already logs to pc_app/$VARIANT/system.log,
# and a second copy at the repo root would just be litter.
set -euo pipefail
trap 'echo "FAILED: $BASH_COMMAND (line $LINENO)" >&2' ERR

REPO_PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Not sourced from bootenv.sh here (no Xilinx tools needed), so VARIANT
# isn't set by it either. The default variant name lives only in
# bootenv.sh -- source it first (or export VARIANT yourself) rather than
# relying on this script to guess.
if [[ -z "${VARIANT:-}" ]]; then
    echo "VARIANT is not set. Run 'source bootenv.sh' first (sets the" >&2
    echo "default), or pass it explicitly: VARIANT=<project> ./pc_app.sh ..." >&2
    exit 1
fi

exec "$REPO_PATH/pc_app/$VARIANT/system.sh" "$@"
