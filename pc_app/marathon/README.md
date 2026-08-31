# pc_app/marathon

PC-side companion app for the "marathon" hardware/firmware version: a Python
client (ECG signal generator + live plot + runtime control panel) and a
small C++ relay server that sits between the Python client and the board.

```
python_client.py     entry point -- main() only, wires the pieces below together
config.py             all tunable knobs (SEND_RATE, CHUNK_SIZE, plot window, ...)
packet_format.py       loads packet_format.json, builds numpy dtypes, PacketReceiver
signal_gen.py          ECG signal generation (neurokit2) + packet building
plot.py                DualPlot (matplotlib, blitted + envelope-decimated)
control_panel.py       Tkinter panel packed beside the plot -- Start/Stop, mode,
                      live SEND_RATE, CHUNK_SIZE, heart rate, filter, FFT view
net.py                 owns the socket: connect, send/receive loop, auto-reconnect
runctl.py              the Start/Stop gate, shared by the GUI and both workers
local_proc.py          local processing mode: generate/process/plot in-process,
                      no socket, no relay, no board. Integer algorithms meant
                      to be translated to VHDL; "iir" models axi_tdm_filter.vhd
spectrum.py            rfft -> dBFS magnitudes + peak finder for the FFT view
tcp_server_app.cpp    C++ relay — forwards raw bytes between whichever two
                      peers are connected (identifies the board by source IP);
                      a 127.0.0.1 connection instead gets echoed straight back
                      to itself, no second peer needed (BOARD_CONNECTED=False)
build/                empty in git; venv, compiled binary, run/logs all land here
build.sh              one-time/incremental setup (compile server, create venv)
system.sh             day-to-day start/stop/status/logs for both processes
```

## Control panel

`control_panel.py`'s two panels are packed as siblings of the plot's canvas
widget inside the same Tk window `plot.py`'s `DualPlot` creates (see its
`__init__`) — no second Tk root/mainloop. `SignalControlPanel` is the
right-hand column: Start/Stop and the mode picker above five tabs —
**Basic** (rate, chunk, heart rate, amplitude, offset), **Waveform** (the
ECGSYN model kwargs), **Noise** (coloured noise layers and the two sine
injectors), **Board** (live metrics, the fabric's filter registers, UART
verbosity) and **Local** (the in-process algorithm and its shift).
`PlotControlPanel` is the bottom bar: view range, buffer, frame rate,
trigger, FFT and Log buffer. Everything is live: it writes straight onto `config`'s
module attributes, and `net.py`/`signal_gen.py` read those live (`import
config; config.SEND_RATE`, not a value frozen at import time), so a change
takes effect within one send cycle. `SEND_RATE × CHUNK_SIZE` is the
effective ECG playback rate in samples/s — the panel shows it next to
`ECG_SAMPLING_RATE` so you can see whether you're at real-time speed or
scrubbed away from it.

## Starting a run

The app comes up **idle**: window, plot and panel are all live, but nothing
is generated, connected or sent until **Start** (top of the signal panel) is
pressed. Set everything up first, then start — and "run / capture / stop /
change one thing / run again" becomes a workflow rather than a restart. Set
`AUTOSTART = True` in `config.py` for the old launch-and-go behaviour.

**Start/Stop and Pause/Resume are different controls.** Start/Stop is the
session: stopped means no socket open, nothing generated, and (in local
mode) a filter that begins from cleared state next time. Pause is
`SEND_ENABLED` only — the connection stays up and the receive path keeps
running, which is what you want to freeze the trace mid-run.

## Board mode vs local mode

The **Mode** radio next to Start picks where the processing happens.

- **Board** — the real path: TCP to the relay, which forwards to the board;
  the board filters in fabric and echoes back. (With
  `BOARD_CONNECTED = False` the relay loops it back instead, still over the
  real wire path.)
- **Local** — no socket, no relay, no board. `local_proc.py` generates,
  processes and plots inside this process.

Local mode exists to **develop processing algorithms before they are RTL**,
with a keystroke edit-run loop instead of a synthesis run — and to work at
all with no hardware present.

The rule that makes it worth anything: these algorithms are written the way
fabric has to compute them — **integer arithmetic, fixed width, explicit
wrapping, persistent per-channel state, one sample at a time**. A float64
numpy one-liner would be easier and would be a *different algorithm*: it
would not show the truncation bias, the dead zone or the overflow the
hardware actually exhibits, so "it worked in Python" would mean nothing once
it was RTL. The built-in `iir` is a bit-accurate model of
`axi_tdm_filter.vhd` — it reproduces the documented −15-count steady-state
bias at `SHIFT=4` and is an exact bypass at `SHIFT=0`, both for the same
reasons the fabric does.

To add one: write a function taking `(ch1, ch2, state, params)` and
returning `(out1, out2)`, register it in `local_proc.ALGORITHMS`, and it
appears in the **Local** tab's dropdown. Keep it integer.

Both workers are always alive and each idles unless it owns the current
mode, so switching is a live attribute write with no thread lifecycle to get
wrong. The first chunk in local mode takes a few seconds — a 60-second ECG
buffer is simulated and the numba kernel is compiled — after which it runs
at rate.

## FFT view

The **FFT** checkbox in the plot bar adds a second column of axes showing
the magnitude spectrum of the same (post-trigger) windows the scope traces
show, in vs out. Off, the time axes span the full width exactly as before.

This is what makes the sine injectors (`ECG_SINE1/2`, Noise tab) and the
low-pass filter measurable against each other: **inject a known tone, read
how far the out curve sits below the in curve at that frequency, and that
number is the filter's attenuation there.** Step the frequency and you have
plotted its response curve. Each spectrum carries a peak readout doing that
subtraction for you, and **Log buffer** records it (`chN_peak_hz`,
`chN_peak_in_dbfs`, `chN_peak_out_dbfs`, `chN_peak_delta_db`) alongside the
samples and the settings.

The readout is a line in the plot bar, not text drawn on the axes — that is
a performance decision with a measured reason. As a matplotlib `Text` artist
it cost **~19 ms per frame** for the two boxes, against 0.24 ms for all four
transforms: Agg re-lays out and re-rasterises every glyph on every blit. A
Tk label is free.

**FFT rate (Hz)** in the plot bar decouples the spectrum from `FRAME_RATE`
(default 6 Hz, 0 = every frame). Skipping an update also skips restoring and
blitting the two spectrum axes — their pixels just stay on screen — so the
column costs nothing on skipped frames. Measured at `PLOT_BUFFER=2000`:
turning the spectrum on costs **+5.3 ms/frame** at rate 0, about
**+1.3 ms/frame** amortised at the 6 Hz default. Raise it towards
`FRAME_RATE` if you want the spectrum to track a knob as you turn it.

Magnitudes are **dBFS** — 0 dB is a sine spanning the full wire range — so
readings mean the same thing regardless of `ECG_AMPLITUDE`. DC is removed
before transforming (the wire format centres on half full-scale, and that
offset would otherwise sit ~120 dB above everything else). The frequency
axis comes from `ECG_SAMPLING_RATE`, for the same reason the time axis
does — so an injected 50 Hz sine lands on 50 Hz. Note the spectrum is only
meaningful in `PLOT_MODE = "scope"`; in envelope mode the buffer holds
min/max pairs, not samples, and the readout says so.

## Plot performance

`plot.py` is deliberately not a naive matplotlib loop. Measured 2026-08-17
at `SEND_RATE=800`/`CHUNK_SIZE=500`: the client sat at **107% CPU, 93.6% of
it in the plot thread against 12.1% in the network thread** — the display
cost roughly 8x the actual work. Two fixes:

- **Blitting.** `refresh()` restores a cached background and redraws only
  the four line artists, instead of `canvas.draw()` re-rendering axes,
  ticks and legend (~30 ms each, 24x/s). The background is re-cached on
  every real draw event, so resizing/zooming still works.
- **Adaptive draw style.** `drawstyle="steps-mid"` renders each sample as a
  flat segment so the plot never implies an interpolated value that was
  never measured — but that can only be *seen* while a sample gets more than
  a pixel or so, and it doubles the vertex count regardless (3.03 ms per
  2000-point line vs 1.83 ms). `_apply_drawstyle()` picks it per frame from
  the axes' real pixel width against `PLOT_BUFFER`, so sparse windows get
  the honest stepped rendering and dense ones get the cheap style that looks
  identical. Measured 2026-08-31: at `PLOT_BUFFER=2000` (0.59 px/sample) a
  frame goes **13.3 ms → 7.9 ms**; at 4000, **19.7 ms → 9.1 ms**. Set
  `PLOT_STEPS_MIN_PX = 0` to force steps-mid always.
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
- `True` → connects over the real board network (`192.168.1.100`); requires
  the actual board, `tcp_server_app` forwards to/from it (see `PCB_IP` in
  `tcp_server_app.cpp`)
- `False` → connects to `tcp_server_app` on `127.0.0.1`, which echoes every
  packet straight back to the sender — no board needed, and the data still
  round-trips through the real relay/wire path (not a Python-side shortcut)

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
are defined once in `../../shared/<variant>/packet_format.json` -- edit that file to
change them, not this doc or the source directly. Currently:

- `type 0` ("data"): one record per sample, fields `ts, ch1, ch2` (16-bit
  unsigned each). `ts` is a monotonic counter set by the sender and echoed
  back unchanged by the firmware, so a received sample can be matched back
  to the transmitted one it came from.
- `type 1` ("config"): placeholder, not yet acted on by the firmware --
  reserved for future use.

`python_client.py` reads `packet_format.json` directly at runtime. The
firmware (`vitis/marathon/app/lwip_comm_client_raw.c`) and this relay
(`tcp_server_app.cpp`) can't -- being compiled, bare-metal C has no
filesystem -- so `../../shared/gen_packet_header.py` generates a C header
from the same JSON, run automatically by `build.sh` and
`vitis/marathon/build_app.sh` before each compile. `tcp_server_app.cpp`'s
`MAX_SAMPLES` must still match the firmware's `MAX_PAYLOAD_SAMPLES`
(currently 2000 on both sides) -- it bounds record *count*, not bytes; the
per-record byte size now comes from the generated header.
