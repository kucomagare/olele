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
                      live SEND_RATE, CHUNK_SIZE, heart rate, filter
net.py                 owns the socket: connect, send/receive loop, auto-reconnect
runctl.py              the Start/Stop gate, shared by the GUI and both workers
pipelines.py           the processing functions themselves, and everything
                      needed to add one. Integer algorithms meant to be
                      translated to VHDL; "iir" models axi_tdm_filter.vhd
local_proc.py          runs them: local processing mode (generate/process/plot
                      in-process, no socket, no relay, no board), dispatch,
                      filter state, worker thread
spectrum.py            rfft -> dBFS magnitudes + peak finder (used by sat.py)
sat.py                 SAT (Static Analysis Tool) -- OFFLINE analysis of a
                      logged buffer: spectra, tone attenuation, and the
                      hardware scored against a model
sat_gui.py             its window -- same shape as the client's, but static:
                      no blitting, no frame rate, every control recomputes once
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
right-hand column: Start/Stop, the mode picker and a status line above five
tabs — **Basic**, **Waveform**, **Noise**, **Board** (live metrics, the
fabric's filter registers, UART verbosity) and **Local** (each channel's
pipeline and implementation). Everything on the **Noise** tab is per channel:
five colour layers and four sine generators, each choosing which channels it
reaches, with its own frequency, phase and level per channel — so the same
interference can arrive on two leads at a different amplitude and phase,
which is what a two-channel rejection scheme has to cope with. `PlotControlPanel` is the bottom bar: view range,
buffer, frame rate, trigger, grid (off / normal / fine — fine subdivides each
tick into 5, like ECG paper) and Log buffer.

Fields are **batched**: typing or ticking changes only the widget, and
**Apply** (or Enter in any field) commits the lot in one pass — a `●` on the
button means something is waiting. **Defaults** puts every field that panel
owns back to `config.py`'s startup value and applies it on the press — the
run is left alone (Start/Stop, Pause and the Board/Local mode stay put). Both
panels have their own pair; the Board tab keeps its own Apply because that
one writes hardware registers. Applied values go straight onto `config`'s
module attributes, and `net.py`/`signal_gen.py` read those live (`import
config; config.SEND_RATE`, not a value frozen at import time), so a change
takes effect within one send cycle. `SEND_RATE × CHUNK_SIZE` is the
effective ECG playback rate in samples/s — the panel shows it next to
`ECG_SAMPLING_RATE` so you can see whether you're at real-time speed or
scrubbed away from it.

## Starting a run

The app comes up **idle**: window, plot and panel are all live, but nothing
is generated, connected or sent until **Start** (top of the signal panel) is
pressed. A **status line** under the buttons says what it is actually doing
— `stopped (warming up)`, `running: local, iir`, `running: board,
connecting`, `, paused` — because "I pressed Start and nothing happened" has
several very different causes and none of them used to be visible outside
the console. Set `AUTOSTART = True` for the old launch-and-go behaviour.

**Start/Stop and Pause/Resume are different controls.** Start/Stop is the
session: stopped means no socket open, nothing generated, and (in local
mode) a filter that begins from cleared state next time. Pause is
`SEND_ENABLED` only — the connection stays up and the receive path keeps
running, which is what you want to freeze the trace mid-run.

The one-time costs (a ~0.9 s neurokit2 buffer and a ~0.3 s numba compile)
are paid in the background at launch, not on the first Start.

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
bias at `SHIFT=4` and is an exact bypass at `SHIFT=0`.

To add one, edit **`pipelines.py`** only: write `fn(x, state, params)` for
one channel (wire dtype in and out) and add a line to `PIPELINES`. A design
pipeline is registered as a `{"scipy": ..., "manual": ...}` pair — the float
version says what the filter should do, the integer one what it will do as
RTL — and it appears in both GUIs' dropdowns and in SAT with no other edit.
`bypass` and `iir` are registered as bare functions: single fixed things
with no implementation to choose.

Both workers are always alive and each idles unless it owns the current
mode, so switching is a live attribute write with no thread lifecycle to get
wrong.

## Offline analysis — SAT (`sat.py`)

The live client deliberately has **no spectrum view**. It had one; it was
the wrong place for it. An FFT over a rolling buffer costs something on
every frame, forever, to answer a question that is not actually live: inject
a tone, read the attenuation, compare against a model. None of that needs to
happen at 24 fps, and all of it is easier when nothing is moving.

So the client stays lean and only has to stream and draw, and the
measurement happens offline in **SAT** (Static Analysis Tool) on a **Log
buffer** dump, where taking a second is free and the same capture can be
examined ten different ways. Click the **SAT** button in the client's plot
bar to launch it as a separate process, or run it directly:

```bash
source build/venv/bin/activate
./sat.py                            # window, on the newest dump
./sat.py FILE.csv                   # ...on a particular one
```

It opens a **window shaped like the client's** — matplotlib's own canvas
with a Tk control column beside it — but nothing in it is live. There is no
blitting, no animated artist and no frame rate, because there is no stream;
every control just recomputes from the file and redraws once. A 2×2 grid
shows time on the left and spectrum on the right, per channel, with the
report underneath in a panel you can select and copy out of.

The controls, all of which are also flags:

| Control | What it does |
|---|---|
| **Dump** + Rescan / Open… / Delete all logs | which capture; newest first, or any file; delete-all asks for confirmation |
| **Size** | samples transformed, `capture` or 128…8192 |
| **F max**, **dB min** | spectrum axis limits (`F max` 0 = Nyquist) |
| **Peak from (Hz)** | where to start looking for the peak |
| **Algorithm**, **Shift**, **Settle** | the reference-model comparison |

`--peak-fmin` deserves a note, because it is the one that looks like a bug
the first time. With the ECG enabled its harmonics are genuinely stronger
than an injected tone at level 0.1, so the peak lands on the ECG (6 Hz at
120 bpm) and the reported attenuation is the filter's response *there*, not
at your tone. Set **Peak from** above the ECG band — 40 Hz — and the same
capture reports the tone.

For scripting or a box with no display, the flags still work:

```bash
./sat.py --list
./sat.py --no-plot --model iir --peak-fmin 40
```

It reads both halves of a dump — the CSV of samples and the
`plot_config_data_*.txt` sidecar — so the ground truth (heart rate, injected
tones, noise levels, and the board filter registers read back from fabric)
comes with the data instead of having to be remembered.

Two things it reports:

- **Spectra and tone attenuation.** The peak is located on the *input* and
  read on both, so the delta is a comparison at one frequency: a filter that
  works moves its output peak elsewhere entirely, and two independently
  located peaks would report a meaningless number. Inject a tone, read the
  delta, step the frequency, and you have the filter's response curve.
- **The hardware against a model** (`--model iir`). Runs the bit-accurate
  `pipelines` model on the recorded input and scores it against the
  recorded output — max/mean error in counts, the signed mean (which is the
  truncation bias), and correlation. This is the "did the hardware compute
  what I think it computed" check, and a file is the right place to ask it:
  both signals are already captured and aligned. `--shift` defaults to
  whatever the board's register actually held, from the sidecar.

## Why the panel responds immediately

Editing a generation parameter used to re-run `nk.ecg_simulate()` for both
channels inline, on whichever thread asked for the next chunk — holding the
GIL, so the whole window froze for **736 ms** on every heart-rate or noise
edit. `signal_gen._regen()` now runs on its own thread and the previous
buffers keep being served until the new ones are published, so an edit
returns in **under 1 ms**. Rapid edits coalesce: a regeneration in flight
re-checks the signature when it finishes, so the last edit wins.

## Plot performance

`plot.py` is deliberately not a naive matplotlib loop. Measured 2026-08-17
at `SEND_RATE=800`/`CHUNK_SIZE=500`: the client sat at **107% CPU, 93.6% of
it in the plot thread against 12.1% in the network thread** — the display
cost roughly 8x the actual work. Two fixes:

- **Blitting.** `refresh()` restores a cached background and redraws only
  the four line artists, instead of `canvas.draw()` re-rendering axes,
  ticks and legend (~30 ms each, 24x/s). The background is re-cached on
  every real draw event, so resizing/zooming still works.
- **Event pumping decoupled from redraw.** Tk events used to be serviced
  only inside `refresh()`, so a click could wait a whole frame period to be
  seen and lowering `FRAME_RATE` — the recommended way to save CPU — also
  made the panel feel broken. `pump_events()` now runs on its own
  `UI_POLL_RATE` (100 Hz, 0.08 ms a call, under 1% of a core). The frame
  scheduler also drops a missed-frame backlog instead of replaying it, the
  same fix as `net.py`'s send catch-up limiter: without it, any stall left
  the deadline many frames in the past and the loop drew flat out to catch
  up, hundreds of ms of unresponsive window for frames nobody would see.
- **Adaptive draw style.** `drawstyle="steps-mid"` renders each sample as a
  flat segment so the plot never implies an interpolated value that was
  never measured — but that can only be *seen* while a sample gets more than
  a pixel or so, and it doubles the vertex count regardless (3.03 ms per
  2000-point line vs 1.83 ms). `_apply_drawstyle()` picks it per frame from
  the axes' real pixel width against `PLOT_BUFFER`. Measured 2026-08-31: at
  `PLOT_BUFFER=2000` (0.59 px/sample) a frame goes **13.3 ms → 7.9 ms**; at
  4000, **19.7 → 9.1**. Set `PLOT_STEPS_MIN_PX = 0` to force steps-mid.
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
