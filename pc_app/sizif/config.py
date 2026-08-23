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

PLOT_BUFFER = 2000  # Rolling window length, in samples. Live-editable from
                    # the panel -- plot.py reallocates the four rolling
                    # buffers and re-fits the x-axis when this changes,
                    # keeping the most recent samples from the old buffers.
                    # At ECG_SAMPLING_RATE=500 Hz, 500 samples = 1 s of ECG.

PLOT_MIN = 0        # Y-axis display range, both channels. Live-editable
PLOT_MAX = 65535    # from the panel -- purely a *view* (what part of the
                    # signal is visible), independent of ECG_AMPLITUDE
                    # (config.py below), which controls how much of the
                    # wire dtype's range (uint16 -> 0..65535) the
                    # *generated* signal actually occupies. Defaults to the
                    # full uint16 range so nothing is clipped from view;
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
FRAME_RATE = 24   # Plot redraws/s. Live-editable from the panel --
                  # python_client.py reads config.FRAME_RATE each loop
                  # cycle rather than a value frozen at import time.
                  # Costs real CPU: even with blitting, refresh() still
                  # does real canvas work every frame. Measured
                  # 2026-08-17 (pre-blitting): dropping this to 2 took
                  # python_client from 103% to 36% CPU, with zero change
                  # to throughput (that was a firmware-side limit) -- this
                  # is a UI-smoothness knob, not a performance one, lower
                  # it only if you need the CPU.
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
CHUNK_SIZE = 3

MAX_CHUNK_SIZE = 2000  # mirrors MAX_SAMPLES in tcp_server_app.cpp and
                        # MAX_PAYLOAD_SAMPLES in lwip_comm_client_raw.c --
                        # the wire/firmware hard ceiling. The control panel
                        # clamps to this.

# ECG signal generation (neurokit2). ch1/ch2 are each an independent
# nk.ecg_simulate() call, cached and re-sliced per packet -- see
# signal_gen.py for how SEND_RATE/CHUNK_SIZE map onto this buffer.
ECG_SAMPLING_RATE = 2048  # Hz, native rate of the simulated buffer. A
                          # standard clinical rate (compare 250-360 Hz for
                          # older Holter/MIT-BIH gear, up to 1000 Hz for
                          # research-grade capture).
ECG_HEART_RATE = 120      # bpm. Live-editable from the panel; signal_gen.py
                          # regenerates the buffer when this changes.
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

# Fraction (0.0-1.0) of each channel's wire dtype range (uint16 -> 0..65535)
# that the signal's peak-to-peak amplitude occupies, centered at the
# midpoint. 1.0 -> spans the full 0..65535; 0.0 -> flat line at 32767.
# Live-editable from the panel -- see signal_gen.py's _scale_to_wire().
# Deliberately tied to the wire dtype's own max rather than an arbitrary
# plot constant, so it can never produce an out-of-range packet value.
ECG_AMPLITUDE = 0.75

SEND_ENABLED = True
RECEIVE_ENABLED = True

RECONNECT_DELAY = 1.0  # seconds between reconnect attempts after a dropped/failed connection
