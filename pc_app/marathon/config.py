# PC-side client tunables, kept out of net/plot/signal_gen code.

# True: real board (192.168.1.100). False: loop through tcp_server_app on
# 127.0.0.1 (echoes packets back) -- exercises the wire path, no board needed.
BOARD_CONNECTED = True

HOST = "192.168.1.100" if BOARD_CONNECTED else "127.0.0.1"
PORT = 5001

# Rolling window length, samples. Live-editable -- plot.py reallocates the
# 4 rolling buffers and re-fits the x-axis on change. 2048 = 1s at
# ECG_SAMPLING_RATE, a round unit of signal rather than of samples.
PLOT_BUFFER = 2048

# Read from the wire format, not hardcoded: marathon's 32-bit TDM slots
# (sizif used 16) would show nothing against a stale 0..65535 window.
import numpy as _np
from packet_format import CH1_DTYPE as _CH1_DTYPE
_WIRE_MAX = int(_np.iinfo(_CH1_DTYPE).max)

# Largest value a sample can carry -- the panel quotes this when it
# rejects a y-limit that would make the trace vanish.
WIRE_FULL_SCALE = _WIRE_MAX

# Starting size only (pixels) for both GUI windows -- freely resizable after.
WINDOW_W = 1280
WINDOW_H = 540

# Y-axis display range (a view), independent of ECG_AMPLITUDE (how much of
# the wire range the generated signal actually occupies). Defaults to full
# wire range; narrow to zoom.
PLOT_MIN = 0
PLOT_MAX = _WIRE_MAX

# "scope": most recent PLOT_BUFFER raw samples, full waveform detail --
# right choice at real ECG rates, shows P-QRS-T shape.
# "envelope": min/max pairs per packet, long window but shape-free -- built
# for the old ~400k samples/s stress-test rate where "scope" showed only a
# 2.5ms aliased sliver; doesn't apply at real ECG rates.
PLOT_MODE = "scope"

# --- Oscilloscope trigger -------------------------------------------------
# At high streaming rates the window turns over many times per frame (e.g.
# 1500x960 = 1.44M samples/s, a 2000-sample window is 1.39ms and turns over
# ~30x per frame at FRAME_RATE=24) -- the on-screen phase becomes arbitrary
# and the trace jumps rather than scrolls, not dropped/corrupt data.
#
# Trigger shows the window starting at the most recent upward crossing of a
# level instead of always the newest samples, so a repeating waveform holds
# still regardless of stream rate. Off by default -- real-time playback
# scrolls smoothly on its own; turn on when the stream outruns the window.
# Costs: capture buffers become PLOT_CAPTURE_FACTOR x PLOT_BUFFER for room
# to slide within; displayed length/x-axis/PLOT_BUFFER meaning unchanged.
PLOT_TRIGGER = False

# Trigger level, fraction of PLOT_MIN..PLOT_MAX. 0.6 sits above baseline but
# below the R peak so it fires once per beat; lower if free-running, raise
# if it locks onto a T wave or noise.
PLOT_TRIGGER_LEVEL = 0.6

# History kept behind the displayed window, x PLOT_BUFFER. 2 lets the
# trigger slide back up to one full window to find a crossing.
PLOT_CAPTURE_FACTOR = 2

# Grid on both plots (PLOT_GRID) and its density (PLOT_GRID_MODE): "normal"
# is tick gridlines only, "fine" subdivides each into 5 (like ECG paper's
# small squares) for reading intervals off the screen. PLOT_HSPACE is the
# vertical gap between the two axes (they share an x-axis, read together,
# so kept small). All live-editable from the plot bar.
PLOT_GRID = True
PLOT_GRID_MODE = "normal"
PLOT_GRID_MODES = ("normal", "fine")
PLOT_GRID_FINE_DIVISIONS = 5
PLOT_HSPACE = 0.07

# The live app deliberately has NO spectrum view -- a rolling-buffer FFT
# costs every frame forever for a measurement that doesn't need to be live.
# Moved to sat.py, which reads a Log dump and can take as long as it likes.

# Each received chunk reduced to this many min/max pairs before the plot
# buffer (2*PLOT_ENVELOPE_BLOCKS points/packet). 0 = plot raw. Default 1:
# at the old 400k samples/s stress rate a 1000-pt buffer turned over 400x/s
# (pure aliasing); at 1 block the window covers a real span and shows the
# envelope. Raise for more per-packet detail (costs more plot work).
PLOT_ENVELOPE_BLOCKS = 1

# Minimum axes px/sample for "steps-mid" rendering (flat segment per
# sample, vs. plain joined points) -- steps-mid stops the plot implying an
# interpolated value, but is only visible above ~1px/sample and doubles
# vertex count (measured ~4.8ms/frame extra across 4 traces at 2000pts).
# Chosen per frame from actual pixel width. 0 = always steps-mid.
PLOT_STEPS_MIN_PX = 2.0

# Seconds of silence from the far end before the client flags it, while
# streaming. A clean send path proves nothing about the far end -- the
# relay discards bytes with no partner peer, so a wedged board can still
# show zero send-side drops (see research_info/dma-measurements.md, the
# 2026-09-01 wedge). 3s is well past any normal stall/retransmit burst.
RX_WATCHDOG_S = 3.0

# Tk event-loop pump rate, Hz -- independent of FRAME_RATE so lowering the
# latter (to save CPU) doesn't also make clicks/typing feel unresponsive.
# flush_events() costs 0.08ms, so 100Hz is under 1% of a core.
UI_POLL_RATE = 100.0

# Plot redraws/s, live-editable (python_client.py reads it each loop, not
# frozen at import). Real CPU cost even with blitting -- measured
# 2026-08-17 pre-blitting: dropping to 2 took CPU 103%->36% with zero
# throughput change (that was firmware-bound) -- a UI-smoothness knob only.
FRAME_RATE = 60

# SEND_RATE x CHUNK_SIZE is the effective ECG playback rate pulled from the
# simulated buffer (see ECG_SAMPLING_RATE) -- both live-editable, these are
# just startup defaults. Equal to ECG_SAMPLING_RATE = real-world speed; the
# defaults below (64*8=512 vs ECG_SAMPLING_RATE 2048) deliberately don't --
# a 4x scrub speed, not a wire-format constraint. Board hardware ceiling
# (~825 pkt/s AXI-Lite path) is far above any real-ECG-rate concern; see
# research_info/dma-architecture.md.
SEND_RATE = 64

# *** MARATHON CONSTRAINT: CHUNK_SIZE must stay a multiple of 8. *** A DMA
# buffer needs both a whole number of frames and a multiple of 32 bytes;
# with 32-bit slots, 8 frames satisfies both for any channel count. One
# packet per DMA buffer, so this is the frames-per-packet count.
CHUNK_SIZE = 8

# Mirrors MAX_SAMPLES in tcp_server_app.cpp / MAX_PAYLOAD_SAMPLES in
# lwip_comm_client_raw.c -- the wire/firmware hard ceiling. Panel clamps to
# this; also a multiple of 8.
MAX_CHUNK_SIZE = 2000

# Machine-readable form of the MARATHON CONSTRAINT above so the panel can
# enforce it (snaps typed values down to a multiple). sizif has none (its
# equivalent is 1); read via getattr() so a copied panel works either way.
CHUNK_SIZE_GRANULARITY = 8

# ECG signal generation (neurokit2). ch1/ch2 are each an independent
# nk.ecg_simulate() call, cached and re-sliced per packet -- see
# signal_gen.py for how SEND_RATE/CHUNK_SIZE map onto this buffer.
ECG_SAMPLING_RATE = 2048  # Hz, native rate of the simulated buffer (clinical
                          # standard; cf. 250-360 Hz Holter, 1000 Hz research).
ECG_HEART_RATE = 120      # bpm, live-editable -- regenerates the buffer.

# Panel bound for ECG_SAMPLING_RATE, kept next to the value it bounds so
# drift is visible (the default once silently exceeded the panel's own
# ceiling and snapped to 2000 on first touch).
#
# Measured 2026-08-26, ECG_DURATION_S=60, method=ecgsyn -- generate time and
# memory both scale with rate; 65536 works but two costs aren't obvious:
# regeneration runs on the GUI thread (0.45s -> 2.08s, freezes the window,
# can show as "(dropped N late)" at high SEND_RATE) and PLOT_BUFFER must
# scale with it or the window stops meaning anything (at 65536Hz, 2000
# samples is 30ms, ~6% of one beat). This is generation rate, not stream
# rate -- streaming is SEND_RATE x CHUNK_SIZE.
ECG_SAMPLING_RATE_MIN = 50     # below this QRS shape stops being recognizable
ECG_SAMPLING_RATE_MAX = 65536  # 2**16

ECG_DURATION_S = 60       # seconds of buffer before the stream loops.
ECG_NOISE = 0.01          # nk.ecg_simulate's own Laplace noise level --
                          # distinct from the colored-noise layers below.
ECG_METHOD = "ecgsyn"     # "ecgsyn" -- full McSharry model, everything below
                          # applies. "simple" -- cheaper wavelet approx,
                          # silently ignores heart_rate_std/lfhfratio/ti/ai/bi
                          # (verified: no error, just no effect).
                          # "multileads" not offered (12-lead shape mismatch).
ECG_HEART_RATE_STD = 1    # bpm, beat-to-beat HRV (verified: real jitter).
ECG_LFHFRATIO = 0.5       # HRV low/high-freq ratio; visible only if
                          # ECG_HEART_RATE_STD > 0.
ECG_TI = (-70, -15, 0, 15, 100)     # P,Q,R,S,T angular positions (degrees).
ECG_AI = (1.2, -5, 30, -7.5, 0.75)  # P,Q,R,S,T relative heights. Scaling all
                                     # five uniformly has no effect (nk
                                     # renormalizes overall amplitude --
                                     # that's what ECG_AMPLITUDE is for);
                                     # changing ratios reshapes the waveform.
ECG_BI = (0.25, 0.1, 0.1, 0.1, 0.4) # P,Q,R,S,T widths (Gaussian sigma).
ECG_RANDOM_SEED = 1         # Base seed; ch2 uses +1 so channels stay
                            # independent but reproducible.

# Off switch for the ECG waveform only -- noise/sine generators keep
# running. Does NOT skip the simulation: the clean ECG's peak-to-peak is
# still computed as the reference every _LEVEL is a fraction of, only its
# contribution to the output is zeroed, so levels/amplitudes don't jump
# when toggled back on.
ECG_ENABLED = True

# DC offset, fraction of wire full scale, applied after ECG_AMPLITUDE. 0.0
# centres the signal; a fraction (not raw counts) so it means the same
# thing on marathon's 32-bit wire as sizif's 16-bit one. Pushing the band
# outside [0, max] clips via _scale_to_wire()'s existing rounding guard.
ECG_OFFSET = 0.0
ECG_OFFSET_MIN = -0.5
ECG_OFFSET_MAX = 0.5

# Colored noise added on top of the simulated ECG via nk.signal_noise()
# (distinct from ECG_NOISE, which is baked into ecg_simulate itself) -- see
# signal_gen.py's _simulate_raw(). Five layers ((1/f)**beta: -2 violet,
# -1 blue, 0 white, 1 pink, 2 brown), any combination enabled at its own
# level. Each _LEVEL is that layer's peak-to-peak as a fraction of the
# *clean* ECG's own peak-to-peak (measured once before noise is added, so
# levels don't compound). Per channel, decorrelated (distinct random_state
# per layer per channel) -- models e.g. one bad electrode rather than
# uniform noise everywhere.
ECG_NOISE_VIOLET_CH1_ENABLED = False
ECG_NOISE_VIOLET_CH1_LEVEL = 0.1
ECG_NOISE_VIOLET_CH2_ENABLED = False
ECG_NOISE_VIOLET_CH2_LEVEL = 0.1
ECG_NOISE_BLUE_CH1_ENABLED = False
ECG_NOISE_BLUE_CH1_LEVEL = 0.1
ECG_NOISE_BLUE_CH2_ENABLED = False
ECG_NOISE_BLUE_CH2_LEVEL = 0.1
ECG_NOISE_WHITE_CH1_ENABLED = False
ECG_NOISE_WHITE_CH1_LEVEL = 0.1
ECG_NOISE_WHITE_CH2_ENABLED = False
ECG_NOISE_WHITE_CH2_LEVEL = 0.1
ECG_NOISE_PINK_CH1_ENABLED = False
ECG_NOISE_PINK_CH1_LEVEL = 0.1
ECG_NOISE_PINK_CH2_ENABLED = False
ECG_NOISE_PINK_CH2_LEVEL = 0.1
ECG_NOISE_BROWN_CH1_ENABLED = False
ECG_NOISE_BROWN_CH1_LEVEL = 0.1
ECG_NOISE_BROWN_CH2_ENABLED = False
ECG_NOISE_BROWN_CH2_LEVEL = 0.1

# Four independent sine-wave interference generators (powerline hum, other
# discrete periodic artifacts vs. colored noise's broadband randomness) --
# see signal_gen.py's _sine_contribution(). Per-channel enable/freq/phase/
# level: real interference doesn't arrive identically on both leads, and a
# phase difference is exactly what a rejection scheme must cope with; set
# both channels the same for common-mode interference. Evaluated at
# t = sample_index / ECG_SAMPLING_RATE (ECG time base), so frequency is
# exact regardless of playback speed. _LEVEL is a fraction of ch1's clean
# ECG peak-to-peak (same convention as noise _LEVEL above, so equal levels
# = equal amplitudes on both channels). _PHASE in degrees.
ECG_SINE1_CH1_ENABLED = False
ECG_SINE1_CH1_FREQ = 50.0     # Hz -- EU/UK/most-of-world mains
ECG_SINE1_CH1_PHASE = 0.0     # degrees
ECG_SINE1_CH1_LEVEL = 0.1
ECG_SINE1_CH2_ENABLED = False
ECG_SINE1_CH2_FREQ = 50.0
ECG_SINE1_CH2_PHASE = 0.0
ECG_SINE1_CH2_LEVEL = 0.1

ECG_SINE2_CH1_ENABLED = False
ECG_SINE2_CH1_FREQ = 60.0     # Hz -- US/North America mains
ECG_SINE2_CH1_PHASE = 0.0
ECG_SINE2_CH1_LEVEL = 0.1
ECG_SINE2_CH2_ENABLED = False
ECG_SINE2_CH2_FREQ = 60.0
ECG_SINE2_CH2_PHASE = 0.0
ECG_SINE2_CH2_LEVEL = 0.1

ECG_SINE3_CH1_ENABLED = False
ECG_SINE3_CH1_FREQ = 100.0    # Hz -- 2nd harmonic of 50, what a notch at the
ECG_SINE3_CH1_PHASE = 0.0     # fundamental alone leaves behind
ECG_SINE3_CH1_LEVEL = 0.05
ECG_SINE3_CH2_ENABLED = False
ECG_SINE3_CH2_FREQ = 100.0
ECG_SINE3_CH2_PHASE = 0.0
ECG_SINE3_CH2_LEVEL = 0.05

ECG_SINE4_CH1_ENABLED = False
ECG_SINE4_CH1_FREQ = 150.0    # Hz -- 3rd harmonic, and pipe2's LP corner
ECG_SINE4_CH1_PHASE = 0.0
ECG_SINE4_CH1_LEVEL = 0.05
ECG_SINE4_CH2_ENABLED = False
ECG_SINE4_CH2_FREQ = 150.0
ECG_SINE4_CH2_PHASE = 0.0
ECG_SINE4_CH2_LEVEL = 0.05

# Fraction (0.0-1.0) of the wire dtype's range the signal's peak-to-peak
# amplitude occupies, centered at the midpoint -- see signal_gen.py's
# _scale_to_wire(). Tied to the wire dtype's own max so it can never
# produce an out-of-range packet value.
ECG_AMPLITUDE = 0.75

# ---------------------------------------------------------------------------
# Session control: what happens at launch, and where the processing runs
# ---------------------------------------------------------------------------

# False: app comes up idle (nothing generated/connected/sent) until "Start"
# -- settings dialled in before a run beats correcting them mid-stream, and
# makes run/capture/stop/tweak/rerun a workflow instead of a restart.
# True = old launch-and-go behaviour (unattended throughput runs).
AUTOSTART = False

# "board" -- TCP to the relay -> board filters in fabric -> echoes back (or
#            loops straight back if BOARD_CONNECTED=False).
# "local" -- no socket/relay/board; generated, processed and plotted here
#            by local_proc.py. Exists to develop algorithms bound for VHDL
#            with a keystroke edit-run loop and no hardware required --
#            written the way fabric computes (integer, fixed width,
#            explicit wrapping, per-channel state); see pipelines.py.
# Live-switchable per channel, index 0 = ch1, 1 = ch2.
#
# Both channels always travel in the same frame and the board's TDM filter
# has one shift register for all of them, so "local" doesn't change what's
# sent/filtered on the wire -- the board still does its own work on that
# channel, the PC just discards the echo and substitutes its own pipeline
# output. Keeps hardware untouched until a pipeline is worth porting to HDL.
#
# Thread ownership follows from this: any channel "board" -> net.tcp_thread
# owns the run (local channels done inline, same plot tuple); all "local"
# -> local_proc.local_thread owns it, no socket at all.
CH_MODE = ["board", "board"]


def any_board():
    """True when the socket is needed -- see net.py's _may_run()."""
    return any(m == "board" for m in CH_MODE)


def any_local():
    """True when at least one channel is processed here -- decides whether
    net.py must keep sent inputs around for the round trip."""
    return any(m == "local" for m in CH_MODE)


def all_local():
    """True when nothing needs to leave this process."""
    return all(m == "local" for m in CH_MODE)


# Which pipeline each channel runs, and in which implementation. Both are
# keys into pipelines.PIPELINES; the panel builds its dropdowns from that
# dict, so adding a pipeline needs no edit here.
#   pipe: "bypass" passthrough; "iir" bit-accurate axi_tdm_filter.vhd model
#         (the "did hardware compute what I think" reference); "pipe1"
#         baseline-wander removal (high-pass).
#   impl: "scipy" float64, what the filter SHOULD do; "manual" hand-written
#         integer arithmetic, what it WILL do as RTL -- the translated one.
# A pipeline with only one implementation (iir is hardware-only) falls back
# to that one rather than to passthrough.
CH_PIPE = ["iir", "iir"]
CH_IMPL = ["manual", "manual"]

# alpha = 1/2**LOCAL_SHIFT for the local filter -- local mode's counterpart
# of the board's shift register, separate since there's no hardware to
# write to here. 0 is exact bypass (y = y + (x-y) = x), same as in fabric.
LOCAL_SHIFT = 4
LOCAL_SHIFT_MAX = 31   # register field is 5 bits (cfg_reg1[4:0])

# pipe2's 3 corner frequencies (Hz) + notch Q, live-editable from the Local
# tab, passed to the pipeline via local_proc.params_for() so both
# implementations see the same numbers. 0 skips that stage, as does a
# stage at/above Nyquist. Same values fall back in pipelines.py (PIPE2_*)
# for a caller with no params -- see there for why 0.2/50/150 for ECG.
PIPE2_HP_HZ = 0.2
PIPE2_NOTCH_HZ = 50.0
PIPE2_NOTCH_Q = 30.0
PIPE2_LP_HZ = 150.0

SEND_ENABLED = True
RECEIVE_ENABLED = True

RECONNECT_DELAY = 1.0  # seconds between reconnect attempts after a drop/fail


# --- Defaults ------------------------------------------------------------
# Snapshot of every knob above, taken at import before anything can edit
# one -- what the panels' "Defaults" buttons restore. Lists are copied, or
# restoring would hand back the object the panel has been mutating.
_DEFAULTS = {name: (list(value) if isinstance(value, list) else value)
             for name, value in list(globals().items())
             if name.isupper() and not name.startswith("_")}


def default_value(name):
    value = _DEFAULTS[name]
    return list(value) if isinstance(value, list) else value


def restore_defaults(names):
    """Put `names` back to their startup values. Returns those restored."""
    restored = [n for n in names if n in _DEFAULTS]
    for name in restored:
        globals()[name] = default_value(name)
    return restored
