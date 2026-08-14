#!/usr/bin/env bash
# One-time/incremental setup for the "sizif" PC app: compiles the C++
# relay server and creates the Python venv, both under ./build/ (gitignored).
# Safe to re-run — skips steps that are already done.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_DIR="$SCRIPT_DIR/build"
SHARED_DIR="$SCRIPT_DIR/../../shared"
exec > >(tee "$SCRIPT_DIR/build.log") 2>&1
mkdir -p "$BUILD_DIR"

# --- Packet format header (generated from shared/packet_format.json) ---
PACKET_FORMAT_JSON="$SHARED_DIR/packet_format.json"
PACKET_FORMAT_H="$SCRIPT_DIR/packet_format.h"
python3 "$SHARED_DIR/gen_packet_header.py" "$PACKET_FORMAT_JSON" "$PACKET_FORMAT_H"

# --- C++ relay server ---
SERVER_BIN="$BUILD_DIR/tcp_server_app"
SERVER_SRC="$SCRIPT_DIR/tcp_server_app.cpp"
if [[ ! -x "$SERVER_BIN" || "$SERVER_SRC" -nt "$SERVER_BIN" || "$PACKET_FORMAT_JSON" -nt "$SERVER_BIN" ]]; then
    echo "Building tcp_server_app..."
    g++ -std=c++17 -O2 -pthread "$SERVER_SRC" -o "$SERVER_BIN"
else
    echo "tcp_server_app up to date."
fi

# --- Python venv ---
VENV_DIR="$BUILD_DIR/venv"
if [[ ! -d "$VENV_DIR" ]]; then
    echo "Creating venv..."
    python3 -m venv "$VENV_DIR"
    "$VENV_DIR/bin/pip" install --upgrade pip
    "$VENV_DIR/bin/pip" install numba numpy matplotlib scipy pyqt5
else
    echo "venv already exists at $VENV_DIR"
fi

echo
echo "Done. Run ./system.sh start to launch just the PC app, or"
echo "../../run_all.sh from the repo root to also flash+run the board."
