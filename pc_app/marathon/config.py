# All tunable knobs for the PC-side client in one place -- edit here when
# iterating on send rate / chunk size / plot window / signal shape, etc.
# (kept separate from networking/plotting/signal-generation code so the
# values you actually hand-tune during testing aren't buried in the
# middle of a longer script).

BOARD_CONNECTED = True  # True: connect over the real board network (192.168.1.100) --
                        # requires the actual board, tcp_server_app forwards to/from it.
                        # False: connect to tcp_server_app on 127.0.0.1, which echoes
                        # every packet straight back to the sender (see the ip ==
                        # "127.0.0.1" branch in tcp_server_app.cpp) -- no board needed,
                        # exercises the real wire path end to end.

HOST = "192.168.1.100" if BOARD_CONNECTED else "127.0.0.1"
PORT = 5001

PLOT_BUFFER = 2048  # Rolling window length, in samples. Live-editable from
                    # the panel -- plot.py reallocates the four rolling
                    # buffers and re-fits the x-axis when this changes,
                    # keeping the most recent samples from the old buffers.
                    # 2048 = exactly one second at ECG_SAMPLING_RATE, so the
                    # window is a round unit of signal rather than a round
                    # number of samples.

# Derived from the wire format rather than hardcoded: marathon uses 32-bit
# TDM slots (sizif used 16), and a stale 0..65535 window would show nothing
# at all against samples centred on 2**31. Reading it from the same JSON
# that generates the firmware's C header means changing the slot width
# again can never leave this behind.
import numpy as _np
from packet_format import CH1_DTYPE as _CH1_DTYPE
_WIRE_MAX = int(_np.iinfo(_CH1_DTYPE).max)

# Full scale of the wire, as a public name: the largest value a sample can
# carry, and so the widest the plot's y-range can usefully be. The panel
# quotes it when it rejects a y-limit, since "0..4294967295" is the missing
# piece of information when a hand-typed range makes the trace vanish.
WIRE_FULL_SCALE = _WIRE_MAX

# Initial size of both GUI windows (the live client and SAT), in pixels.
# A plain fixed size on purpose -- read once when each window is created.
# Only the STARTING size: drag either window to whatever you like afterwards,
# and nothing here constrains it.
WINDOW_W = 1280
WINDOW_H = 540

PLOT_MIN = 0            # Y-axis display range, both channels. Live-editable
PLOT_MAX = _WIRE_MAX    # from the panel -- purely a *view* (what part of the
                    # signal is visible), independent of ECG_AMPLITUDE
                    # (config.py below), which controls how much of the
                    # wire dtype's range the *generated* signal actually
                    # occupies. Defaults to the full wire range so nothing
                    # is clipped from view;
                    # narrow it to zoom into a portion of the signal.

# "scope"    -- show the most recent PLOT_BUFFER raw samples, refreshed at
#               FRAME_RATE. Short window, full waveform detail. This is
#               what you want to see the signal's shape.
# "envelope" -- roll min/max pairs from every packet into the buffer.
#               Long window (PLOT_BUFFER/(2*SEND_RATE) seconds), shows
#               amplitude over time but not shape.
#
# At real ECG rates (SEND_RATE*CHUNK_SIZE ~ 500 samples/s) "scope" is the
# right choice -- it's what shows ECG morphology (P-QRS-T shape). The
# aliasing problem "envelope" mode was built for only bites at the old
# ~400k samples/s stress-test rate: PLOT_BUFFER samples of raw signal
# there was 2.5 ms of real time, so "scope" showed only that sliver while
# "envelope" covered ~0.6 s at the cost of collapsing each packet to its
# extremes.
PLOT_MODE = "scope"

# --- Oscilloscope trigger -------------------------------------------------
# Why this exists: the window shows PLOT_BUFFER samples, but at high
# streaming rates far more than that arrives between two frames. At
# SEND_RATE=1500 x CHUNK_SIZE=960 the stream is 1.44M samples/s, so a
# 2000-sample window is 1.39 ms of signal and turns over ~30 times per frame
# at FRAME_RATE=24. Which phase of the waveform is on screen at frame time is
# then arbitrary, and the trace appears to jump rather than scroll -- not
# dropped or corrupt data, just no phase reference. (At the old
# 50 x 10 = 500 samples/s only ~21 samples arrived per frame, 1% of the
# window, which is why it used to look stable.)
#
# A real scope fixes this by triggering: instead of always showing the newest
# PLOT_BUFFER samples, show the window that STARTS at the most recent
# upward crossing of a level. Same phase every frame, so a repeating
# waveform stands still no matter how fast the stream runs.
#
# Off by default: at real-time playback rates the window scrolls smoothly on
# its own and a trigger only costs you the newest samples (it slides the view
# back to the last crossing). Turn it on -- from the panel, live -- when the
# stream outruns the window and the trace starts jumping.
#
# Cost: the capture buffers become PLOT_CAPTURE_FACTOR x PLOT_BUFFER so
# there is somewhere to slide the window within. The displayed length, the
# x-axis and PLOT_BUFFER's meaning are all unchanged.
PLOT_TRIGGER = False

# Trigger level as a fraction of the PLOT_MIN..PLOT_MAX view range. For ECG
# the R-wave upstroke is the obvious feature to lock to; 0.6 sits above the
# baseline but below the R peak, so it fires once per beat. Lower it if the
# trace free-runs (never triggering), raise it if it locks onto a T wave or
# onto noise.
PLOT_TRIGGER_LEVEL = 0.6

# How much history to keep behind the displayed window, as a multiple of
# PLOT_BUFFER. 2 means the trigger can slide the window back by up to one
# full window to find a crossing. Larger costs memory and a slightly longer
# search for no real benefit.
PLOT_CAPTURE_FACTOR = 2


# NOTE: the live app deliberately has NO spectrum view. It was built, it
# worked, and it was the wrong place for it: an FFT on a rolling buffer costs
# something on every frame forever, and what the measurement is actually for
# -- inject a tone, read the attenuation, compare against a model -- does not
# need to happen live at all. It moved to sat.py (Static Analysis Tool),
# which reads a Log buffer dump and can take as long as it likes.

# Each received chunk is reduced to this many min/max pairs before being
# pushed into the plot buffer (so 2*PLOT_ENVELOPE_BLOCKS points per
# packet). Set to 0 to plot raw samples.
#
# Why it defaults to 1: at SEND_RATE=800 / CHUNK_SIZE=500 the raw stream is
# 400k samples/s, which turned a 1000-point buffer over 400 times a second
# -- the window showed 2.5 ms of signal and was pure aliasing, at ~90% of a
# CPU core. At 1 block the window covers PLOT_BUFFER/(2*SEND_RATE) seconds
# (~0.6 s at 800 pkt/s) and shows the signal's envelope.
#
# Raise it for more detail within each packet (costs proportionally more
# plot work); note CHUNK_SIZE samples span only CHUNK_SIZE/sample-rate of
# real time, so past a point you are magnifying noise.
PLOT_ENVELOPE_BLOCKS = 1

# Minimum axes pixels per sample for the traces to be drawn "steps-mid"
# (each sample a flat segment centred on its x position) rather than as
# plain joined points.
#
# steps-mid exists to stop the plot implying an interpolated value between
# two samples that was never measured -- worth having, but it can only be
# SEEN when a sample occupies more than a pixel or so. Above that density the
# steps are sub-pixel and the two styles render identically, while steps-mid
# still costs its price: it doubles the vertex count, measured at 3.03 ms per
# 2000-point line against 1.83 ms without, i.e. ~4.8 ms of every frame for
# the four traces (2026-08-31, PLOT_BUFFER=2000, ~800 px wide).
#
# So it is chosen per frame from the axes' actual pixel width: sparse
# windows get the honest stepped rendering, dense ones get the cheap one
# that looks the same. Set to 0 to force steps-mid always.
PLOT_STEPS_MIN_PX = 2.0

# Seconds of silence from the far end before the client says so, while it is
# streaming and expecting an echo.
#
# Why this exists: a healthy send path proves nothing about the far end. The
# relay accepts every byte and discards it when it has no partner peer, so on
# 2026-09-01 the client reported "1700 pkts/s", zero drops and zero
# send-stalls for minutes while the board was wedged and not answering ping.
# Every send-side counter was honest; the conclusion drawn from them was
# still wrong. Only the receive path knew, by being silent.
#
# 3 s is comfortably longer than any stall the catch-up limiter tolerates
# (50 ms) or a TCP retransmission burst on this link, so it fires on "the far
# end is gone", not on "the far end hiccuped".
RX_WATCHDOG_S = 3.0

# How often the Tk event loop is pumped, Hz -- button clicks, typing, hover.
# Deliberately independent of FRAME_RATE: events used to be pumped only when
# the plot redrew, so lowering FRAME_RATE (which is the recommended way to
# save CPU) also made the panel feel broken, and a click could sit unhandled
# for a whole frame period. flush_events() costs 0.08 ms, so 100 Hz is under
# 1% of a core.
UI_POLL_RATE = 100.0
FRAME_RATE = 60   # Plot redraws/s. Live-editable from the panel --
                  # python_client.py reads config.FRAME_RATE each loop
                  # cycle rather than a value frozen at import time.
                  # Costs real CPU: even with blitting, refresh() still
                  # does real canvas work every frame. Measured
                  # 2026-08-17 (pre-blitting): dropping this to 2 took
                  # python_client from 103% to 36% CPU, with zero change
                  # to throughput (that was a firmware-side limit) -- this
                  # is a UI-smoothness knob, not a performance one, lower
                  # it only if you need the CPU.
# Wire cost is 4 + 12*CHUNK_SIZE bytes per packet (ts+ch1+ch2, 4 bytes each
# now that marathon uses 32-bit TDM slots).
#
# HARDWARE CEILING, for reference (measured 2026-08-17, CHUNK_SIZE=500,
# full detail in research_info/architecture-roadmap.md): the board plateaus
# flat at ~825 pkt/s = 412,500 samples/s = 2.48 MB/s, set by per-sample
# AXI-Lite round trips in axi_process_sample(). Overshooting it degrades
# gracefully (resyncs, throughput loss) rather than corrupting data -- see
# the firmware's ring-overflow/resync handling -- but there's no reason to
# get near it at real ECG rates; it only matters if SEND_RATE/CHUNK_SIZE
# get cranked up well past real-time.
#
# SEND_RATE x CHUNK_SIZE is the effective ECG playback rate (samples/s)
# pulled out of the simulated buffer -- see ECG_SAMPLING_RATE below. Both
# fields are live-editable from the control panel at runtime (net.py and
# signal_gen.py read config.SEND_RATE / config.CHUNK_SIZE directly rather
# than a value frozen at import time), so these are just the startup
# defaults, not a ceiling.
#
# SEND_RATE * CHUNK_SIZE is the effective sample rate. When it equals
# ECG_SAMPLING_RATE the stream runs at real-world speed: one wall-clock
# second of stream is one second of ECG.
#
# The defaults below do NOT do that, deliberately: 24 * 1024 = 24576/s
# against ECG_SAMPLING_RATE 2048, so the waveform plays back 12x faster than
# life. That is a scrub speed, not a wire-format constraint -- nothing
# breaks, the beats just arrive twelve times too often. Set SEND_RATE = 2 (2
# * 1024 = 2048) if you want real time back at this chunk size.
#
# Upper bound on CHUNK_SIZE is MAX_CHUNK_SIZE below, not TCP_SND_BUF: at
# 65535 the old (TCP_SND_BUF-4)/6 = 1364 limit no longer binds. Historical
# note: this pipeline was previously stress-tested at SEND_RATE=800 /
# CHUNK_SIZE=500 (~412k samples/s) against the AXI-Lite ceiling documented
# in research_info/architecture-roadmap.md -- that's a different exercise
# from streaming real ECG and is no longer the default.
SEND_RATE = 64
#
# *** MARATHON CONSTRAINT: keep this a multiple of 8. ***
# A DMA buffer must be both a whole number of frames (or channel assignment
# rotates on the next buffer) and a multiple of 32 bytes (or cache
# flush/invalidate spills onto neighbouring data). With 32-bit slots a frame
# is 4*(N+1) bytes, so 8 frames is always 32*(N+1) -- satisfying both, for
# any channel count. The firmware sends one packet per DMA buffer, so the
# frames-per-packet count is what has to obey it.
CHUNK_SIZE = 8

MAX_CHUNK_SIZE = 2000  # mirrors MAX_SAMPLES in tcp_server_app.cpp and
                        # MAX_PAYLOAD_SAMPLES in lwip_comm_client_raw.c --
                        # the wire/firmware hard ceiling. The control panel
                        # clamps to this. Also a multiple of 8, so the whole
                        # range below is reachable.

# Machine-readable form of the MARATHON CONSTRAINT documented above, so the
# control panel can enforce it instead of leaving it to a comment nobody
# reads mid-experiment. The panel snaps any typed Chunk size down to a
# multiple of this. sizif has no such constraint (its equivalent is 1) --
# reading it with getattr() keeps a copied panel working either way.
CHUNK_SIZE_GRANULARITY = 8

# ECG signal generation (neurokit2). ch1/ch2 are each an independent
# nk.ecg_simulate() call, cached and re-sliced per packet -- see
# signal_gen.py for how SEND_RATE/CHUNK_SIZE map onto this buffer.
ECG_SAMPLING_RATE = 2048  # Hz, native rate of the simulated buffer. A
                          # standard clinical rate (compare 250-360 Hz for
                          # older Holter/MIT-BIH gear, up to 1000 Hz for
                          # research-grade capture).
ECG_HEART_RATE = 120      # bpm. Live-editable from the panel; signal_gen.py
                          # regenerates the buffer when this changes.
# Panel limits for ECG_SAMPLING_RATE. Here rather than hardcoded in
# control_panel.py because the default above used to exceed the panel's own
# ceiling: 2048 was fine at startup but silently snapped to 2000 the moment
# the field was touched, with no way back. Keeping the bound next to the value
# it bounds makes that drift visible.
#
# Measured 2026-08-26 with ECG_DURATION_S=60, method=ecgsyn:
#
#     rate     samples   generate   buffer (2ch)   2000-sample window
#     2048     122,880      0.45 s        2.0 MB            977 ms
#     8192     491,520      0.61 s        7.9 MB            244 ms
#    16384     983,040      0.76 s       15.7 MB            122 ms
#    32768   1,966,080      1.17 s       31.5 MB             61 ms
#    65536   3,932,160      2.08 s       62.9 MB             30 ms
#
# 65536 works, but two things scale badly with it and neither is obvious:
#
#  - Regeneration runs on the GUI thread and goes 0.45 s -> 2.08 s. Every
#    heart-rate or waveform change freezes the window for two seconds, and at
#    high SEND_RATE that is long enough to show up as "(dropped N late)" in
#    the client log. Not a fault, but do not go hunting for it.
#  - PLOT_BUFFER has to scale with the rate or the window stops meaning
#    anything: at 65536 Hz, 2000 samples is 30 ms of ECG, about 6% of one beat
#    at 120 bpm. One beat needs ~32,768 samples, three needs ~98,000 -- and
#    matplotlib's per-refresh cost is roughly linear in points drawn (~8.8 ms
#    at 2000 points, measured), so that is the real limit, not the generator.
#
# Note this is the rate the waveform is GENERATED at, not the rate it is
# streamed at -- streaming is SEND_RATE x CHUNK_SIZE. Raising this alone makes
# the waveform finer-grained and the plot's time axis shorter; it does not put
# more samples per second on the wire.
# Switches the ECG waveform itself off while leaving every other generator
# (the five noise colours and both sine sources) running -- what you get is
# the "nice generators" on their own, with no heartbeat under them.
#
# It does NOT skip the simulation. The ECG's own peak-to-peak is what every
# noise/sine _LEVEL is a fraction of, so it is still computed and used as the
# reference; only its contribution to the output is zeroed. That way toggling
# this removes the ECG and changes nothing else -- levels keep their meaning
# and amplitudes do not jump when you switch it back on.
ECG_ENABLED = True

# DC offset, as a fraction of the wire dtype's full scale, applied after
# ECG_AMPLITUDE scaling. 0.0 centres the signal in the range (the old fixed
# behaviour); +0.25 shifts it to three-quarter scale; -0.25 to quarter scale.
#
# A fraction rather than raw counts so it means the same thing on marathon's
# 32-bit wire as on sizif's 16-bit one. Offsetting far enough that the band
# leaves [0, max] will clip -- the clip in _scale_to_wire() was always there
# as a rounding guard and it will now do real work if you ask it to.
ECG_OFFSET = 0.0
ECG_OFFSET_MIN = -0.5
ECG_OFFSET_MAX = 0.5

ECG_SAMPLING_RATE_MIN = 50     # below this the QRS shape stops being
                                # recognizable
ECG_SAMPLING_RATE_MAX = 65536  # 2**16

ECG_DURATION_S = 60       # seconds of buffer before the stream loops.
ECG_NOISE = 0.01          # nk.ecg_simulate's own built-in amplitude-relative
                          # (Laplace) noise level. Live-editable from the
                          # panel's Noise tab. Separate from the
                          # ECG_NOISE_{VIOLET,BLUE,WHITE,PINK,BROWN}_*
                          # layers below, which are distinct colored-noise
                          # signals added on top afterward.
ECG_METHOD = "ecgsyn"     # "ecgsyn" (default) -- full McSharry dynamical
                          # model, everything below actually does something.
                          # "simple" -- cheaper Daubechies-wavelet
                          # approximation; verified it silently IGNORES
                          # heart_rate_std/lfhfratio/ti/ai/bi (no error, just
                          # no effect). "multileads" is NOT offered here --
                          # it returns a 12-lead DataFrame, a different
                          # shape than this pipeline's one-signal-per-
                          # channel model, and the panel's method selector
                          # is a readonly combobox so it can't be typed in.
ECG_HEART_RATE_STD = 1    # bpm, beat-to-beat heart-rate variability
                          # (verified: visibly different signal, not just
                          # noise -- real HRV jitter between beats).
ECG_LFHFRATIO = 0.5       # Low/high-frequency ratio of that HRV's power
                          # spectrum. Only visible when ECG_HEART_RATE_STD
                          # > 0 (verified).
ECG_TI = (-70, -15, 0, 15, 100)     # P,Q,R,S,T wave angular positions
                                     # (degrees) in the ECGSYN model.
ECG_AI = (1.2, -5, 30, -7.5, 0.75)  # P,Q,R,S,T RELATIVE wave heights.
                                     # CAVEAT (verified empirically):
                                     # scaling all five UNIFORMLY has no
                                     # effect on the final signal -- nk
                                     # renormalizes overall amplitude
                                     # regardless (that's why ECG_AMPLITUDE
                                     # exists as a separate post-scale).
                                     # Changing the RATIOS between them
                                     # (e.g. a taller T relative to R) does
                                     # visibly reshape the waveform.
ECG_BI = (0.25, 0.1, 0.1, 0.1, 0.4) # P,Q,R,S,T wave widths (Gaussian sigma).
ECG_RANDOM_SEED = 1         # Base seed. ch2 uses ECG_RANDOM_SEED + 1, so the
                            # two channels stay independent (different
                            # traces) but reproducible -- same seed always
                            # regenerates the same waveform.

# Extra colored noise, generated separately via nk.signal_noise() and added
# on top of the simulated ECG (distinct from ECG_NOISE above, which is
# baked into nk.ecg_simulate() itself). See signal_gen.py's _simulate_raw().
#
# Five independent layers, one per named color (nk.signal_noise()'s
# (1/f)**beta exponent: -2 violet, -1 blue, 0 white, 1 pink/flicker,
# 2 brown) -- ANY COMBINATION can be enabled simultaneously, each at its
# own level; enabled layers are generated separately and summed before
# being added to the ECG. Each _LEVEL is that layer's peak-to-peak
# amplitude as a fraction of the *clean ECG signal's own* peak-to-peak
# (measured once, before any noise is added, so levels don't compound
# against each other or drift as more layers get enabled) -- e.g. 0.1 =
# that layer alone is 10% of the ECG's own swing. Not tied to uint16
# directly since it's relative to the signal, not the wire -- ECG_AMPLITUDE
# still governs the final (ECG + noise) mix's wire range.
ECG_NOISE_VIOLET_ENABLED = False
ECG_NOISE_VIOLET_LEVEL = 0.1
ECG_NOISE_BLUE_ENABLED = False
ECG_NOISE_BLUE_LEVEL = 0.1
ECG_NOISE_WHITE_ENABLED = False
ECG_NOISE_WHITE_LEVEL = 0.1
ECG_NOISE_PINK_ENABLED = False
ECG_NOISE_PINK_LEVEL = 0.1
ECG_NOISE_BROWN_ENABLED = False
ECG_NOISE_BROWN_LEVEL = 0.1

# Two independent sine-wave interference generators, added on top of the
# ECG (and any colored noise above) -- e.g. to simulate powerline hum
# (50/60 Hz) or another discrete periodic artifact, as opposed to the
# colored noise's broadband randomness. See signal_gen.py's
# _simulate_raw(). Evaluated at t = sample_index / ECG_SAMPLING_RATE, the
# same time base the raw ECG buffer itself is built on, so a given
# frequency is exact regardless of playback speed (SEND_RATE/CHUNK_SIZE)
# -- that's the "in sync with the sampling rate" this was asked for.
# Both channels get the identical sine (same freq/phase/level, no
# per-channel decorrelation like the colored-noise layers use) since real
# interference like mains hum affects every channel the same way.
#
# _LEVEL is the sine's amplitude, as a fraction of the *clean ECG signal's*
# own peak-to-peak (same convention as ECG_NOISE_*_LEVEL above) -- so the
# sine's own peak-to-peak swing is 2 * _LEVEL * (ECG's ptp).
# _PHASE is in degrees for readability; converted to radians in
# signal_gen.py.
ECG_SINE1_ENABLED = False
ECG_SINE1_FREQ = 50.0    # Hz -- default matches EU/UK/most-of-world mains
ECG_SINE1_PHASE = 0.0    # degrees
ECG_SINE1_LEVEL = 0.1

ECG_SINE2_ENABLED = False
ECG_SINE2_FREQ = 60.0    # Hz -- default matches US/North America mains
ECG_SINE2_PHASE = 0.0    # degrees
ECG_SINE2_LEVEL = 0.1

# Fraction (0.0-1.0) of each channel's wire dtype range (uint16 -> 0..65535)
# that the signal's peak-to-peak amplitude occupies, centered at the
# midpoint. 1.0 -> spans the full 0..65535; 0.0 -> flat line at 32767.
# Live-editable from the panel -- see signal_gen.py's _scale_to_wire().
# Deliberately tied to the wire dtype's own max rather than an arbitrary
# plot constant, so it can never produce an out-of-range packet value.
ECG_AMPLITUDE = 0.75

# ---------------------------------------------------------------------------
# Session control: what happens at launch, and where the processing runs
# ---------------------------------------------------------------------------

# False: the app comes up idle -- window, plot and panel all live, but
# nothing generated, connected or sent until "Start" is pressed. That is
# deliberately the default: settings dialled in BEFORE a run beats settings
# corrected while packets are already on the wire, and it makes
# "run / capture / stop / change one thing / run again" a workflow rather
# than a restart. Set True for the old launch-and-go behaviour (unattended
# throughput runs, mostly).
AUTOSTART = False

# "board" -- the real path: TCP to the relay, which forwards to the board;
#            the board filters in fabric and echoes back (or, with
#            BOARD_CONNECTED = False, the relay loops it straight back).
# "local"  -- no socket, no relay, no board. The signal is generated,
#            processed and plotted inside this process by local_proc.py.
#
# Local mode exists to develop processing algorithms that will later be
# translated to VHDL, with a keystroke edit-run loop instead of a synthesis
# run -- and to work at all when no hardware is present. Its algorithms are
# written the way fabric has to compute (integer, fixed width, explicit
# wrapping, per-channel state), so what is seen here is what the RTL will
# do; see local_proc.py's header for the rule and how to add one.
#
# Live-switchable from the panel, PER CHANNEL. Index 0 is ch1, index 1 is ch2.
#
# WHAT "local" ACTUALLY MEANS ON THE WIRE. Both channels always travel in the
# same frame (ts + ch1 + ch2 -- see shared/marathon/packet_format.json), and
# the board's TDM filter has one shift register for all of them, so there is
# no way to send or filter a single channel on its own without changing the
# packet format, the generated C header and the firmware. A channel marked
# "local" is therefore still sent, and the board still filters it; the PC
# just DISCARDS what came back for that channel and substitutes its own
# pipeline output. The board's work on it is thrown away, which costs
# nothing here and keeps the hardware untouched until a pipeline is mature
# enough to be worth implementing in HDL.
#
# Thread ownership follows from these, rather than being a separate switch:
#   any channel "board"  -> net.tcp_thread owns the run and does the local
#                           channels inline (they have to travel in the same
#                           plot tuple as the ones that came back).
#   every channel "local" -> local_proc.local_thread owns it; no socket at all.
CH_MODE = ["board", "board"]


def any_board():
    """True when the socket is needed -- see net.py's _may_run()."""
    return any(m == "board" for m in CH_MODE)


def any_local():
    """True when at least one channel is processed here rather than on the
    board -- which is what decides whether net.py has to keep the sent
    inputs around for the round trip."""
    return any(m == "local" for m in CH_MODE)


def all_local():
    """True when nothing needs to leave this process."""
    return all(m == "local" for m in CH_MODE)


# Which local_proc pipeline each channel runs, and in which implementation.
# Both are keys into local_proc.PIPELINES, and the panel builds its dropdowns
# from that dict -- so adding a pipeline there needs no edit here beyond
# choosing a different default.
#
#   pipe   "bypass" passthrough (the control case)
#          "iir"    bit-accurate model of axi_tdm_filter.vhd, the filter the
#                   board actually runs -- the reference for "did the
#                   hardware compute what I think it did"
#          "pipe1"  baseline-wander removal (high-pass)
#   impl   "scipy"  float64 via scipy.signal -- what the filter SHOULD do
#          "manual" hand-written integer arithmetic -- what it WILL do once
#                   it is RTL. This is the version that gets translated.
#
# A pipeline that offers only one implementation (iir is hardware-only) falls
# back to the one it has rather than to passthrough.
CH_PIPE = ["iir", "iir"]
CH_IMPL = ["manual", "manual"]

# alpha = 1 / 2**LOCAL_SHIFT for the local filter. Local mode's counterpart
# of the board's shift register (Board tab), kept separate because there is
# no hardware to write to here. 0 is an exact bypass -- in the model for the
# same reason as in the fabric: y = y + (x - y) = x.
LOCAL_SHIFT = 4
LOCAL_SHIFT_MAX = 31   # the register field is 5 bits (cfg_reg1[4:0])

SEND_ENABLED = True
RECEIVE_ENABLED = True

RECONNECT_DELAY = 1.0  # seconds between reconnect attempts after a dropped/failed connection
