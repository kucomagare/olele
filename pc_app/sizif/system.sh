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

SERVER_MATCH="tcp_server_app"
CLIENT_MATCH="python_client.py"

# is_running <pid_file> <expect_substring>
# expect_substring guards against a stale PID file whose PID got reused
# by an unrelated process (rare, but possible after a reboot) -- kill -0
# alone only proves *some* process has that PID, not that it's ours.
is_running() {
    local pid_file="$1" expect="$2"
    [[ -f "$pid_file" ]] || return 1
    local pid
    pid="$(cat "$pid_file")"
    [[ -n "$pid" ]] || return 1
    kill -0 "$pid" 2>/dev/null || return 1
    ps -p "$pid" -o args= 2>/dev/null | grep -qF "$expect"
}

start_server() {
    if is_running "$SERVER_PID_FILE" "$SERVER_MATCH"; then
        echo "C++ server already running (PID $(cat "$SERVER_PID_FILE"))"
        return
    fi
    # Delegate to build.sh rather than duplicating a partial copy of its
    # staleness check here -- it also regenerates packet_format.h from
    # packet_format.json before compiling, which a standalone check on
    # just SERVER_SRC's mtime would miss (a stale header could silently
    # get compiled in, or compilation could fail outright on a fresh
    # checkout with no header yet). Already fast/idempotent when nothing
    # changed, so there's no real cost to always calling it.
    "$SCRIPT_DIR/build.sh"
    echo "Starting C++ server..."
    nohup "$SERVER_BIN" > "$SERVER_LOG" 2>&1 &
    echo $! > "$SERVER_PID_FILE"
    sleep 1
    if ! is_running "$SERVER_PID_FILE" "$SERVER_MATCH"; then
        echo "Server failed to start, check $SERVER_LOG"
        exit 1
    fi
    echo "C++ server started (PID $(cat "$SERVER_PID_FILE"))"
}

start_client() {
    if is_running "$CLIENT_PID_FILE" "$CLIENT_MATCH"; then
        echo "Python client already running (PID $(cat "$CLIENT_PID_FILE"))"
        return
    fi
    if [[ ! -d "$VENV_DIR" ]]; then
        echo "venv not found at $VENV_DIR — run ./build.sh first"
        exit 1
    fi
    echo "Starting Python client..."
    # Invoke the venv's own interpreter by path rather than a bare
    # version-named command (e.g. "python3.8") -- the latter resolves
    # via PATH regardless of whether the venv was actually activated,
    # so it can silently run a completely different, non-venv Python.
    nohup "$VENV_DIR/bin/python3" "$SCRIPT_DIR/python_client.py" > "$CLIENT_LOG" 2>&1 &
    echo $! > "$CLIENT_PID_FILE"
    sleep 1
    if ! is_running "$CLIENT_PID_FILE" "$CLIENT_MATCH"; then
        echo "Python client failed to start, check $CLIENT_LOG"
        exit 1
    fi
    echo "Python client started (PID $(cat "$CLIENT_PID_FILE"))"
}

stop_proc() {
    local pid_file="$1" name="$2" expect="$3"
    if ! is_running "$pid_file" "$expect"; then
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
    stop_proc "$CLIENT_PID_FILE" "Python client" "$CLIENT_MATCH"
    stop_proc "$SERVER_PID_FILE" "C++ server" "$SERVER_MATCH"
}

status_all() {
    if is_running "$SERVER_PID_FILE" "$SERVER_MATCH"; then
        echo "C++ server:    running (PID $(cat "$SERVER_PID_FILE"))"
    else
        echo "C++ server:    stopped"
    fi
    if is_running "$CLIENT_PID_FILE" "$CLIENT_MATCH"; then
        echo "Python client: running (PID $(cat "$CLIENT_PID_FILE"))"
    else
        echo "Python client: stopped"
    fi
}

case "${1:-}" in
    start)   start_all ;;
    stop)    stop_all ;;
    restart) stop_all; start_all ;;
    # Restart just one side -- e.g. after editing config.py or another
    # Python module, without touching the C++ relay or the board.
    restart-client)
        stop_proc "$CLIENT_PID_FILE" "Python client" "$CLIENT_MATCH"
        start_client
        ;;
    restart-server)
        stop_proc "$SERVER_PID_FILE" "C++ server" "$SERVER_MATCH"
        start_server
        ;;
    status)  status_all ;;
    logs)
        echo "--- server log ($SERVER_LOG) ---"
        tail -n 20 "$SERVER_LOG" 2>/dev/null || echo "(no log yet)"
        echo
        echo "--- client log ($CLIENT_LOG) ---"
        tail -n 20 "$CLIENT_LOG" 2>/dev/null || echo "(no log yet)"
        ;;
    *)
        echo "Usage: $0 {start|stop|restart|restart-client|restart-server|status|logs}"
        exit 1
        ;;
esac
