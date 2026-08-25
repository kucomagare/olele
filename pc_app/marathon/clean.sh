#!/usr/bin/env bash
# Wipes ./build/ back to empty (compiled server binary, venv, run/log
# state) so you can test that everything regenerates cleanly from tracked
# source files alone (python_client.py + its config/packet_format/
# signal_gen/plot/net modules + tcp_server_app.cpp + build.sh).
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
exec > >(tee "$SCRIPT_DIR/clean.log") 2>&1
rm -rf "$SCRIPT_DIR/build"
mkdir -p "$SCRIPT_DIR/build"
touch "$SCRIPT_DIR/build/.gitkeep"
echo "$(realpath --relative-to="$REPO_ROOT" "$SCRIPT_DIR/build")/ wiped clean."
