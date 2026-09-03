# PC-side client tuning knobs -- edit here, or via the live control panel.

BOARD_CONNECTED = True  # False = loopback via tcp_server_app on 127.0.0.1, no board needed.

HOST = "192.168.1.100" if BOARD_CONNECTED else "127.0.0.1"
PORT = 5001

PLOT_BUFFER = 2000  # Rolling window (samples), live-editable; plot.py reallocates on change.

PLOT_MIN = 0        # Y-axis view range -- independent of ECG_AMPLITUDE (signal's own
PLOT_MAX = 65535    # scale). Defaults to full uint16 range so nothing is clipped.

# "scope" shows ECG morphology (default, real rates). "envelope" (min/max per
# packet) only useful at old stress-test rates (~400k smp/s) to avoid aliasing.
PLOT_MODE = "scope"

# Min/max pairs per chunk before plotting (0 = raw). Only matters in "envelope" mode.
PLOT_ENVELOPE_BLOCKS = 1

FRAME_RATE = 24   # Plot redraws/s. Real CPU cost even with blitting (measured
                  # 103%->36% dropping 24->2, no throughput change) -- UI knob, not perf.

# SEND_RATE * CHUNK_SIZE = effective playback rate (samples/s); both live-editable.
# Ceiling is MAX_CHUNK_SIZE below -- old AXI-Lite hw ceiling (~825 pkt/s) only
# matters cranked well past real-time.
SEND_RATE = 50
CHUNK_SIZE = 3

MAX_CHUNK_SIZE = 2000  # Mirrors MAX_SAMPLES/MAX_PAYLOAD_SAMPLES in relay/firmware.

# ECG signal generation (neurokit2) -- see signal_gen.py.
ECG_SAMPLING_RATE = 2048  # Hz, native rate of the simulated buffer.
ECG_HEART_RATE = 120      # bpm, live-editable.
ECG_DURATION_S = 60       # seconds of buffer before the stream loops.
ECG_NOISE = 0.01          # nk.ecg_simulate's own Laplace noise, distinct from
                          # ECG_NOISE_{COLOR}_* below.
ECG_METHOD = "ecgsyn"     # "simple" silently IGNORES heart_rate_std/lfhfratio/ti/ai/bi.
ECG_HEART_RATE_STD = 1    # bpm, beat-to-beat HRV jitter.
ECG_LFHFRATIO = 0.5       # HRV power-spectrum low/high ratio; visible only
                          # when ECG_HEART_RATE_STD > 0.
ECG_TI = (-70, -15, 0, 15, 100)     # P,Q,R,S,T angular positions (degrees).
ECG_AI = (1.2, -5, 30, -7.5, 0.75)  # P,Q,R,S,T relative heights. Uniform scaling
                                     # has NO effect (nk renormalizes) -- use
                                     # ECG_AMPLITUDE for actual scale.
ECG_BI = (0.25, 0.1, 0.1, 0.1, 0.4) # P,Q,R,S,T wave widths (Gaussian sigma).
ECG_RANDOM_SEED = 1         # Base seed; ch2 uses +1 so channels differ but stay reproducible.

# Extra colored noise (nk.signal_noise), distinct from ECG_NOISE above. Each
# _LEVEL is peak-to-peak as a fraction of the CLEAN ECG's own ptp (measured
# once, so levels don't compound). See signal_gen.py.
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

# Two sine interference generators (e.g. mains hum), added identically to both
# channels (unlike the noise layers above). _LEVEL is ptp as a fraction of the
# clean ECG's own ptp.
ECG_SINE1_ENABLED = False
ECG_SINE1_FREQ = 50.0    # Hz -- EU/UK/most-of-world mains
ECG_SINE1_PHASE = 0.0    # degrees
ECG_SINE1_LEVEL = 0.1

ECG_SINE2_ENABLED = False
ECG_SINE2_FREQ = 60.0    # Hz -- US/North America mains
ECG_SINE2_PHASE = 0.0    # degrees
ECG_SINE2_LEVEL = 0.1

# Fraction (0-1) of the wire dtype's range the signal's ptp occupies, centered
# at the midpoint -- see signal_gen.py's _scale_to_wire().
ECG_AMPLITUDE = 0.75

SEND_ENABLED = True
RECEIVE_ENABLED = True

RECONNECT_DELAY = 1.0  # seconds between reconnect attempts
