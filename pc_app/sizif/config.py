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

PLOT_BUFFER = 1500  # 3 s window at ECG_SAMPLING_RATE=500 Hz -- a few
                    # P-QRS-T complexes visible at once.

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
FRAME_RATE = 24   # Plot redraws/s. Costs real CPU: plot.py's refresh()
                  # does a full canvas.draw(), ~20-40 ms each on TkAgg, so
                  # 24 fps is ~60% of a core. Measured 2026-08-17: dropping
                  # this to 2 took python_client from 103% to 36% CPU. It
                  # did NOT change throughput (that was a firmware-side
                  # limit), so this is a UI-smoothness knob, not a
                  # performance one -- lower it only if you need the CPU.
                  # The proper fix is blitting in plot.py.
# Wire cost is 4 + 6*CHUNK_SIZE bytes per packet (ts+ch1+ch2, 2 bytes each).
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
# Defaults below are chosen so SEND_RATE * CHUNK_SIZE == ECG_SAMPLING_RATE
# (50 * 10 = 500) -- i.e. real-world speed: one wall-clock second of stream
# is one second of ECG. Detuning away from that (panel or here) just
# scrubs the playback faster/slower; it's not a wire-format constraint.
#
# Upper bound on CHUNK_SIZE is MAX_CHUNK_SIZE below, not TCP_SND_BUF: at
# 65535 the old (TCP_SND_BUF-4)/6 = 1364 limit no longer binds. Historical
# note: this pipeline was previously stress-tested at SEND_RATE=800 /
# CHUNK_SIZE=500 (~412k samples/s) against the AXI-Lite ceiling documented
# in research_info/architecture-roadmap.md -- that's a different exercise
# from streaming real ECG and is no longer the default.
SEND_RATE = 50
CHUNK_SIZE = 10

MAX_CHUNK_SIZE = 2000  # mirrors MAX_SAMPLES in tcp_server_app.cpp and
                        # MAX_PAYLOAD_SAMPLES in lwip_comm_client_raw.c --
                        # the wire/firmware hard ceiling. The control panel
                        # clamps to this.

# ECG signal generation (neurokit2). ch1/ch2 are each an independent
# nk.ecg_simulate() call, cached and re-sliced per packet -- see
# signal_gen.py for how SEND_RATE/CHUNK_SIZE map onto this buffer.
ECG_SAMPLING_RATE = 500   # Hz, native rate of the simulated buffer. A
                          # standard clinical rate (compare 250-360 Hz for
                          # older Holter/MIT-BIH gear, up to 1000 Hz for
                          # research-grade capture).
ECG_HEART_RATE = 70       # bpm. Live-editable from the panel; signal_gen.py
                          # regenerates the buffer when this changes.
ECG_DURATION_S = 60       # seconds of buffer before the stream loops.
ECG_NOISE = 0.01          # nk.ecg_simulate's amplitude-relative noise level.

SEND_ENABLED = True
RECEIVE_ENABLED = True

PLOT_MIN = 0
PLOT_MAX = 3000

RECONNECT_DELAY = 1.0  # seconds between reconnect attempts after a dropped/failed connection
