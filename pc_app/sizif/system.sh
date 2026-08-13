#!/usr/bin/env bash
# Unified start/stop/restart/status for the PC-side system (C++ TCP relay
# server + Python client). Binary, venv, run state and logs all live under
# ./build/ (gitignored) — run ./build.sh first if build/ is empty.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

BUILD_DIR="$SCRIPT_DIR/build"
exec > >(tee "$SCRIPT_DIR/system.log") 2>&1
RUN_DIR="$BUILD_DIR/run"
LOG_DIR="$BUILD_DIR/logs"
mkdir -p "$RUN_DIR" "$LOG_DIR"

SERVER_BIN="$BUILD_DIR/tcp_server_app"
SERVER_SRC="$SCRIPT_DIR/tcp_server_app.cpp"
VENV_DIR="$BUILD_DIR/venv"
SERVER_PID_FILE="$RUN_DIR/server.pid"
CLIENT_PID_FILE="$RUN_DIR/client.pid"
SERVER_LOG="$LOG_DIR/server.log"
CLIENT_LOG="$LOG_DIR/client.log"

STOP_TIMEOUT=5  # seconds to wait for graceful shutdown before SIGKILL

is_running() {
    local pid_file="$1"
    [[ -f "$pid_file" ]] || return 1
    local pid
    pid="$(cat "$pid_file")"
    [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null
}

build_server() {
    if [[ ! -x "$SERVER_BIN" || "$SERVER_SRC" -nt "$SERVER_BIN" ]]; then
        echo "Building tcp_server_app..."
        g++ -std=c++17 -O2 -pthread "$SERVER_SRC" -o "$SERVER_BIN"
    fi
}

start_server() {
    if is_running "$SERVER_PID_FILE"; then
        echo "C++ server already running (PID $(cat "$SERVER_PID_FILE"))"
        return
    fi
    build_server
    echo "Starting C++ server..."
    nohup "$SERVER_BIN" > "$SERVER_LOG" 2>&1 &
    echo $! > "$SERVER_PID_FILE"
    sleep 1
    if ! is_running "$SERVER_PID_FILE"; then
        echo "Server failed to start, check $SERVER_LOG"
        exit 1
    fi
    echo "C++ server started (PID $(cat "$SERVER_PID_FILE"))"
}

start_client() {
    if is_running "$CLIENT_PID_FILE"; then
        echo "Python client already running (PID $(cat "$CLIENT_PID_FILE"))"
        return
    fi
    if [[ ! -d "$VENV_DIR" ]]; then
        echo "venv not found at $VENV_DIR — run ./build.sh first"
        exit 1
    fi
    echo "Starting Python client..."
    (
        source "$VENV_DIR/bin/activate"
        nohup python3.8 "$SCRIPT_DIR/python_client.py" > "$CLIENT_LOG" 2>&1 &
        echo $! > "$CLIENT_PID_FILE"
    )
    sleep 1
    if ! is_running "$CLIENT_PID_FILE"; then
        echo "Python client failed to start, check $CLIENT_LOG"
        exit 1
    fi
    echo "Python client started (PID $(cat "$CLIENT_PID_FILE"))"
}

stop_proc() {
    local pid_file="$1" name="$2"
    if ! is_running "$pid_file"; then
        echo "$name not running"
        rm -f "$pid_file"
        return
    fi
    local pid
    pid="$(cat "$pid_file")"
    echo "Stopping $name (PID $pid)..."
    kill -INT "$pid" 2>/dev/null || true

    local waited=0
    while kill -0 "$pid" 2>/dev/null; do
        sleep 0.5
        waited=$((waited + 1))
        if (( waited >= STOP_TIMEOUT * 2 )); then
            echo "$name did not stop in time, sending SIGKILL"
            kill -KILL "$pid" 2>/dev/null || true
            break
        fi
    done
    rm -f "$pid_file"
    echo "$name stopped"
}

start_all() {
    start_server
    start_client
}

stop_all() {
    stop_proc "$CLIENT_PID_FILE" "Python client"
    stop_proc "$SERVER_PID_FILE" "C++ server"
}

status_all() {
    if is_running "$SERVER_PID_FILE"; then
        echo "C++ server:    running (PID $(cat "$SERVER_PID_FILE"))"
    else
        echo "C++ server:    stopped"
    fi
    if is_running "$CLIENT_PID_FILE"; then
        echo "Python client: running (PID $(cat "$CLIENT_PID_FILE"))"
    else
        echo "Python client: stopped"
    fi
}

case "${1:-}" in
    start)   start_all ;;
    stop)    stop_all ;;
    restart) stop_all; start_all ;;
    status)  status_all ;;
    logs)
        echo "--- server log ($SERVER_LOG) ---"
        tail -n 20 "$SERVER_LOG" 2>/dev/null || echo "(no log yet)"
        echo
        echo "--- client log ($CLIENT_LOG) ---"
        tail -n 20 "$CLIENT_LOG" 2>/dev/null || echo "(no log yet)"
        ;;
    *)
        echo "Usage: $0 {start|stop|restart|status|logs}"
        exit 1
        ;;
esac
