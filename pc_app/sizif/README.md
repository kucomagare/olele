# pc_app/sizif

PC-side companion app for the "sizif" hardware/firmware version: a Python
client (synthetic signal generator + live plot) and a small C++ relay
server that sits between the Python client and the board.

```
python_client.py     Python client — generates a test signal, streams it,
                      plots transmitted vs. received (echoed) data
tcp_server_app.cpp    C++ relay — forwards raw bytes between whichever two
                      peers are connected (identifies the board by source IP)
build/                empty in git; venv, compiled binary, run/logs all land here
build.sh              one-time/incremental setup (compile server, create venv)
system.sh             day-to-day start/stop/status/logs for both processes
```

## 1. Set up

```bash
./build.sh
```

Compiles `tcp_server_app` into `build/tcp_server_app` and creates a venv at
`build/venv` with `numba numpy matplotlib scipy pyqt5`. Safe to re-run —
skips whatever's already up to date.

## 2. Before running

Open `python_client.py` and check `BOARD_CONNECTED`:
- `True` → connects over the real board network (`192.168.1.100`)
- `False` → loops back to `tcp_server_app` on this machine, no board needed

Confirmed-good defaults already in the file: `SEND_RATE=220`,
`CHUNK_SIZE=2000` (see repo history/notes for why — this was the stable
operating point found during hardware-in-the-loop testing of the previous
version of this system).

Run from a real desktop session (not plain SSH without X forwarding) —
matplotlib needs `$DISPLAY`.

## 3. Run

```bash
./system.sh start     # builds tcp_server_app if stale, starts both in background
./system.sh status
./system.sh logs
./system.sh restart
./system.sh stop      # SIGINT, escalates to SIGKILL after 5s
```

Closing the plot window does not stop the script — use
`./system.sh stop` or Ctrl+C if run manually.

## Wire protocol reminder

Every message: 4-byte big-endian header `[type:u16][length:u16]` followed
by `length` big-endian `uint16` samples. `type` is opaque/forwarded as-is.
`tcp_server_app.cpp`'s `MAX_SAMPLES` must match the firmware's
`MAX_PAYLOAD_SAMPLES` (currently 2000 on both sides).
