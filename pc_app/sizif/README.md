# pc_app/sizif

PC-side companion app for the "sizif" hardware/firmware version: a Python
client (synthetic signal generator + live plot) and a small C++ relay
server that sits between the Python client and the board.

```
python_client.py     entry point -- main() only, wires the pieces below together
config.py             all tunable knobs (SEND_RATE, CHUNK_SIZE, plot window, ...)
packet_format.py       loads packet_format.json, builds numpy dtypes, PacketReceiver
signal_gen.py          synthetic test-signal generation + packet building
plot.py                DualPlot (matplotlib, blitted + envelope-decimated)
net.py                 owns the socket: connect, send/receive loop, auto-reconnect
tcp_server_app.cpp    C++ relay — forwards raw bytes between whichever two
                      peers are connected (identifies the board by source IP)
build/                empty in git; venv, compiled binary, run/logs all land here
build.sh              one-time/incremental setup (compile server, create venv)
system.sh             day-to-day start/stop/status/logs for both processes
```

## Plot performance

`plot.py` is deliberately not a naive matplotlib loop. Measured 2026-08-17
at `SEND_RATE=800`/`CHUNK_SIZE=500`: the client sat at **107% CPU, 93.6% of
it in the plot thread against 12.1% in the network thread** — the display
cost roughly 8x the actual work. Two fixes:

- **Blitting.** `refresh()` restores a cached background and redraws only
  the four line artists, instead of `canvas.draw()` re-rendering axes,
  ticks and legend (~30 ms each, 24x/s). The background is re-cached on
  every real draw event, so resizing/zooming still works.
- **Envelope decimation.** Each chunk is reduced to `PLOT_ENVELOPE_BLOCKS`
  min/max pairs (`config.py`). At 400k samples/s a 1000-point buffer
  otherwise turned over 400x per second — 2.5 ms of visible signal, i.e.
  aliasing. min/max is used rather than stride decimation specifically so
  short transients survive: a single-sample spike is invisible to
  `values[::250]` but shows up in the envelope.

Per-packet plot work is now O(1) rather than O(CHUNK_SIZE), and the window
covers `PLOT_BUFFER/(2*SEND_RATE)` seconds (~0.6 s at 800 pkt/s).

`net.py`'s `tcp_thread` owns the connection end to end, including
reconnecting (with a `RECONNECT_DELAY` backoff, see `config.py`) on any
drop — mirrors the firmware's own `tcp_client_error` → `tcp_client_start()`
auto-reconnect, so a dropped link doesn't require restarting this app.
One behavior change from that: the plot window now comes up immediately
regardless of whether the initial connection succeeds, and just retries
silently in the background — it used to print an error and exit instead.

## 1. Set up

```bash
./build.sh
```

Compiles `tcp_server_app` into `build/tcp_server_app` and creates a venv at
`build/venv` with `numba numpy matplotlib scipy pyqt5`. Safe to re-run —
skips whatever's already up to date.

## 2. Before running

Open `config.py` and check `BOARD_CONNECTED`:
- `True` → connects over the real board network (`192.168.1.100`)
- `False` → loops back to `tcp_server_app` on this machine, no board needed

Confirmed-good defaults for the old single-channel protocol: `SEND_RATE=220`,
`CHUNK_SIZE=2000` (see repo history/notes for why — this was the stable
operating point found during hardware-in-the-loop testing of the previous
version of this system). Since the ts+ch1+ch2 sample structure tripled
bytes/sample (2 -> 6), `CHUNK_SIZE=2000` now produces an echo packet
(12004 bytes) bigger than lwIP's `TCP_SND_BUF` (8192, see `lwipopts.h` in
the built BSP) and permanently stalls `comm_process()`'s backpressure
check, desyncing the stream. `CHUNK_SIZE` is provisionally lowered to
`500` (3004 bytes, safe margin) to unblock testing the new packet logic —
still needs re-tuning for throughput once that's the focus again, either
by raising `CHUNK_SIZE` further (must stay under `(TCP_SND_BUF-4)/6` ≈
1364) or by increasing `TCP_SND_BUF`/`TCP_WND` in the platform's lwIP
config instead.

Run from a real desktop session (not plain SSH without X forwarding) —
matplotlib needs `$DISPLAY`.

## 3. Run

```bash
./system.sh start            # builds tcp_server_app if stale, starts both in background
./system.sh status
./system.sh logs
./system.sh restart          # both processes
./system.sh restart-client   # just the Python client -- after editing config.py or
                              # another Python module, without touching the relay or the board
./system.sh restart-server   # just the C++ relay -- after editing tcp_server_app.cpp
./system.sh stop             # SIGINT, escalates to SIGKILL after 5s
```

Closing the plot window does not stop the script — use
`./system.sh stop` or Ctrl+C if run manually.

## Wire protocol reminder

Every message: 4-byte big-endian header `[type:u16][length:u16]` followed
by `length` repeats of a fixed record whose shape depends on `type`.
`type` and each type's record fields (name, bit width, signed/unsigned)
are defined once in `../../shared/packet_format.json` -- edit that file to
change them, not this doc or the source directly. Currently:

- `type 0` ("data"): one record per sample, fields `ts, ch1, ch2` (16-bit
  unsigned each). `ts` is a monotonic counter set by the sender and echoed
  back unchanged by the firmware, so a received sample can be matched back
  to the transmitted one it came from.
- `type 1` ("config"): placeholder, not yet acted on by the firmware --
  reserved for future use.

`python_client.py` reads `packet_format.json` directly at runtime. The
firmware (`vitis/sizif/app/lwip_comm_client_raw.c`) and this relay
(`tcp_server_app.cpp`) can't -- being compiled, bare-metal C has no
filesystem -- so `../../shared/gen_packet_header.py` generates a C header
from the same JSON, run automatically by `build.sh` and
`vitis/sizif/build_app.sh` before each compile. `tcp_server_app.cpp`'s
`MAX_SAMPLES` must still match the firmware's `MAX_PAYLOAD_SAMPLES`
(currently 2000 on both sides) -- it bounds record *count*, not bytes; the
per-record byte size now comes from the generated header.
