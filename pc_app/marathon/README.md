# pc_app/marathon

PC-side companion app for the "marathon" hardware/firmware version: a Python
client (ECG signal generator + live plot + runtime control panel) and a
small C++ relay server that sits between the Python client and the board.

```
python_client.py     entry point -- main() only, wires the pieces below together
config.py             all tunable knobs (SEND_RATE, CHUNK_SIZE, plot window, ...)
                      and, in its SAT_* section, every default SAT opens with
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
                      logged buffer: spectra, tone attenuation, the hardware
                      scored against a model, and the pipelines' own
                      amplitude/phase response
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

**Every default it starts from lives in `config.py`**, in the `SAT_*`
section near the bottom — field values, dropdown contents, figure size,
overlay colours, and the measurement constants. The CLI's flag defaults and
the window's field defaults both read those same names, so a change lands in
both; they used to carry their own copies of the same eight numbers, which
is exactly the arrangement where the two drift apart unnoticed. Unlike the
client's knobs above it, nothing in that section is written at runtime.

It opens a **window shaped like the client's** — matplotlib's own canvas
with a Tk control column beside it — but nothing in it is live. There is no
blitting, no animated artist and no frame rate, because there is no stream;
every control just recomputes from the file and redraws once. The report
sits underneath in a panel you can select and copy out of.

**View** switches between the two things it draws. `capture` is a grid per
channel: time, magnitude spectrum, and — when **Phase** is on — a third
column of phase against frequency. `response` is a Bode plot of the
pipelines themselves, amplitude over phase on log frequency, and is
described further down.

### The capture view's phase column

The rfft that gives the magnitude spectrum gives the phase for free, but the
two are not equally useful and **Phase** picks which you get:

- **`out-in`** (the default) — output phase minus input phase, per bin: the
  phase the recorded processing actually applied. Both traces share the
  record's start, so that arbitrary origin cancels and what is left is the
  filter. On a `pipe2` capture it draws a clean ramp to about −45° across
  the ECG band with a step at the 50 Hz notch.
- **`raw`** — the plain FFT angle of each trace, which is what "the phase
  from an FFT" usually names. **Expect it to look like noise, because it
  largely is.** It is dominated by where the record happens to start, which
  rotates every bin by −2π·f·t₀. Measured on this repo's own dumps: `raw`
  scatters across the full ±180° with 170° of jitter between adjacent bins,
  against 4.6° for `out-in` — a 37× difference. The information in `raw` is
  in *differences between the curves*, never in the values; two traces whose
  raw phase lands on top of each other are the same signal.
- **`off`** — hide the column and give the width back to the other two.

### Degrees are not a shift — **Phase units**

"Is the output shifted, and by how much" is a question in milliseconds, and
degrees do not answer it: 45° is 125 ms at 1 Hz and 1.25 ms at 100 Hz. The
**Phase units** control converts, and applies to *both* views' phase axes:

- **`deg`** — the angle itself.
- **`phase ms`** — `−φ/(2πf)`: how late the *sine* at that frequency comes
  out. Reads directly as "the 10 Hz component is 3 ms late". Only as good as
  the unwrapping, so trust it in a passband and not past a wrap.
- **`group ms`** — `−dφ/dω`: how late a narrow *band* around that frequency
  comes out, which is the delay of the waveform's shape rather than of the
  carrier. **This is the one that matters for an ECG**: constant group delay
  just moves the QRS, group delay that varies with frequency reshapes it —
  which is what an IIR chain does and a linear-phase FIR does not. The
  report prints it regardless of the display units, as
  `output lags input by +2.03 ms (median group delay), spread 5.70 ms`.

Group delay is taken from ratios of complex bins, `angle(h[i+L]·conj(h[i]))`,
which is already wrapped into (−π, π] and so needs no unwrapping of its own.

**`L` is not 1, and that matters.** A Hann window's transform spans three
bins, so neighbouring bins of a windowed spectrum are correlated and a slope
measured between two of them is partly measured against itself — it comes
out flattened. Checked against `scipy.signal.group_delay` on a known 2nd-order
Butterworth, lag 1 read **4.09 ms where the truth was 5.72**, and 5.76
against 6.60: ~25% low across the band. At lag 2 and beyond it lands on the
truth, so the main lobe is the whole story; the code uses 4 for margin and a
quieter baseline (`PHASE_GROUP_LAG`), which measures 5.82 / 6.50 / 5.16 /
1.28 against a true 5.72 / 6.60 / 4.95 / 1.30. A synthetic 7-sample delay
reads back as 3.428 ms against an exact 3.418.

The response view passes **lag 1 deliberately**: its multisine sits on exact
bins and is never windowed, so nothing correlates its neighbours. There the
maths is exact — verified against an analytic `exp(−2πifτ)` — and `iir` at
`shift=4` measures 7.3 ms against its theoretical 2⁴/2048 = 7.8 ms.

Bins are gated on **both** traces being within 60 dB of their own peak. The
input-only gate that came first let a notched tone through — strong going
in, at the noise floor coming out — and its meaningless phase difference
drew a scatter of outliers sitting exactly on the notch, which is the one
place on the plot you would most want to trust. The panel also takes its own
x-limit rather than the spectrum's, since the gate typically leaves phase
over a fraction of the band and stretching to Nyquist crushes every point
into the left few percent.

It sits above a notebook whose **tabs change with it**, so each view gets the
controls that act on it and no others. Pages are forgotten rather than
destroyed, so a setting in a hidden tab keeps its value and switching back
finds it unchanged. **Apply / Recompute** and **Defaults** stay pinned below
and apply to every tab at once.

| Tab | View | Controls |
|---|---|---|
| **Dump** | both | which capture; Rescan / Open… / Delete all logs |
| **Plot** | capture | **Size**, **F max**, **dB min**, **Peak from**, **Phase**, **Phase units** — how the spectra are computed and shown |
| **Model** | capture | **Ch1/Ch2 pipe + impl**, **Shift**, **Settle** — the reference-model comparison |
| **Curves** | response | which pipelines to draw, a row per pipeline with manual and scipy side by side, + All / None; then **pipe2 HP / notch / Q / LP** and *Corners from capture* |
| **Measure** | response | **Size**, **Tones**, **Drive**, **Averages**, **Rate** — how hard it looks |
| **Plot** | response | **F min**, **F max**, **dB min**, **Phase units**, the floor and design overlays, **Overlay capture** / **Overlay from** |

Two notes on that table. **Dump is in both views deliberately**: the response
view reads the loaded capture's sample rate, its filter corners and — for the
overlays — its samples, so dropping it there would mean leaving the view to
change dumps. As a tab it costs no space until you want it, which was the
actual problem with it. And **dB min is one setting behind two widgets**, so
it means the same thing in both views: the bottom of the magnitude axis,
dBFS in one and dB of gain in the other.

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
./sat.py --response pipe2                        # window, response view
./sat.py --no-plot --response pipe1:scipy,iir    # the numbers only
./sat.py --response pipe2 --overlay both         # ...with the capture on it
./sat.py --response pipe2 --response-fmin 40 --response-fmax 60 \
         --response-points 200                   # zoomed onto the notch
./sat.py --response pipe2 --set pipe2_notch_hz=60   # move a corner
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

## Pipeline response — the `response` view

The other two answers are about a recording. This one is about the filters:
tick any set of pipelines in **Response curves** and their amplitude and
phase are drawn over each other on one pair of log-frequency axes. Colour is
the pipeline, a dashed line is the integer (`manual`) arithmetic — so the
gap between a solid and a dashed line of the same colour is what fixed point
cost, read straight off the plot.

It is **measured, not derived.** Only the `scipy` implementations have a
transfer function to read off; the `manual` ones truncate and `iir` wraps,
so neither is linear and neither has one. Each pipeline is instead driven
with a random-phase multisine — equal-amplitude tones sitting on exact FFT
bins, so there is no leakage and no window is needed (a window would cost
the phase its meaning) — and the response is the output spectrum over the
input spectrum. Two periods are run and thrown away before the one that is
transformed, and several realisations are averaged, which is the standard
estimator for a system that is not quite linear.

Three details that are load-bearing rather than decoration:

- **Drive is part of the measurement.** An integer pipeline's response
  depends on level: drive it small enough to sit inside the truncation dead
  zone and it measures as no filter at all. The plot title carries the level
  it was measured at for that reason.
- **Tones cluster on the configured corners.** A Q=30 notch at 50 Hz is
  ~1.7 Hz wide, and a log grid of a hundred tones across ten octaves puts
  about one tone in it — the deepest feature of the response would be the
  one least likely to be sampled. Every frequency the capture was recorded
  with gets a cluster of tones on and around it.
- **The bins carrying no tone are measured too.** Nothing put energy there,
  so whatever comes out is the pipeline's own truncation noise and
  distortion. That is the **floor** overlay, and attenuation below it is not
  attenuation you actually get.

What it is for, concretely — `pipe2` at `fs = 2048`, 25% drive:

| | `scipy` | `manual` |
|---|---|---|
| high-pass corner (0.5 Hz asked for) | 0.50 Hz | **0.64 Hz** — quantised to a power-of-two shift |
| 50 Hz notch depth | −203 dB | **−57 dB** — Q20 coefficients cannot place the zero exactly |
| noise + distortion floor | −190 dB | −157 dB |

None of those three are visible in a time trace, and the notch one is a
145 dB difference between the filter that was designed and the filter that
will be built.

**Design curve** overlays the transfer function computed straight from the
designed coefficients, for the pipelines that keep an `sos`. It is there as
a check on the *measurement*: if it does not sit on top of the measured
`scipy` curve, distrust the measurement rather than the filter.

### Narrowing the band, and moving the corners

**F min** / **F max** bound what is *measured*, not only what is drawn: the
excitation is confined to them, so the same number of tones is spent over
less frequency. That is how you get resolution where you want it. The 50 Hz
notch across the full band is a spike two tones wide; at **F min 40, F max
60, Tones 200** it resolves properly, and the plot then shows the thing that
matters — `scipy` plunging past −120 dB while `manual` bottoms out at
**−57 dB**, because Q20 coefficients cannot place the zero exactly on the
unit circle. Below a decade of span the axis switches to plain numbers.

Both ends stop at a hard limit, and they are **different limits with
different answers**:

- **Low end: one bin, `rate/Size`.** There is no tone below it to excite.
  You do not have to work this out — **lowering F min raises Size for you**
  until it fits, and writes the value it used back into the Size box so the
  cost stays visible (about 1.6 s and 200 MB per curve at the longest period
  offered, which reaches 0.001 Hz at 2048 Hz). Picking a Size by hand sets a
  floor F min can raise but not fall below, so relaxing F min again drops
  back to your choice instead of leaving the measurement stuck slow.
- **High end: Nyquist, half the *Rate*.** Nothing reaches past it, because a
  sampled signal does not carry anything above it. F max cannot move this
  and neither can Size — **Rate** is the only control that does, and raising
  it asks a genuinely different question: what would this filter do at
  8192 Hz? The corners stay where they are in Hz, so the digital filter
  really is a different one.

Asking for more than either is not an error and is not silently ignored: the
axis stops where the measurement does, so there is never blank space that
reads as missing data, and the report names the wall you hit.

Going to the low end is worth doing — only below ~0.1 Hz do the two
high-pass implementations separate into their real asymptotes, `scipy` at
40 dB/decade heading to +180° against `manual`'s 20 dB/decade and +90°,
which is a 2nd-order Butterworth against a single pole.

One subtlety in the phase trace: it **breaks at a true null**. Where the
magnitude is at or below the measured noise floor — `scipy`'s notch reaches
−213 dB — there is no output to carry a phase, and the bin holds numerical
noise. Letting that steer `np.unwrap` drags every later point by up to a
whole turn, which draws two curves that agree everywhere 360° apart. Those
bins are excluded, so the gap sits exactly where the magnitude plot shows
the reason for it.

The **pipe2 HP / notch / Q / LP** fields sweep the pipeline's own corners.
They open filled in with what the loaded capture recorded — falling back to
the pipeline's own constants, read off `pipelines` by name so there is one
definition of a default — and refill whenever a dump is loaded, so they
always show the filter you are actually looking at. Type a number to ask
what it would do somewhere else: move the notch to 60 Hz for a 60 Hz mains
region, or drop the low-pass and watch the phase change. **Corners from
capture** puts them back.

These deliberately do **not** reach the capture view's reference-model
comparison, which keeps the sidecar's values. Scoring a recording against a
filter that never ran on it would not mean anything; asking what a filter
*does* at a different corner is a fair question, and that is what this view
is. `--set pipe2_notch_hz=60` is the same knob from the command line.

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
