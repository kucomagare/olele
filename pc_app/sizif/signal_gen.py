# ECG signal generation (neurokit2) and packet building. No socket I/O
# here on purpose -- net.py owns the connection and calls into this
# module just to get bytes to send.
#
# ch1/ch2 are each pulled from an independently-simulated ECG buffer
# (built once, cached, regenerated only when config.ECG_HEART_RATE
# changes) so both plotted channels show real ECG morphology rather than
# one being a scaled copy of the other. Swap this for a second lead or a
# different signal later if you need channels to actually differ
# clinically -- this is a placeholder pairing, not a modeled two-lead ECG.

import numpy as np
import neurokit2 as nk

import config
from packet_format import DATA_TYPE, DATA_DTYPE, TS_MODULUS, CH1_DTYPE, CH2_DTYPE

import struct

# Headroom inside [PLOT_MIN, PLOT_MAX] so the scaled trace doesn't touch
# the plot's edges.
_SCALE_MARGIN = 0.1

# (heart_rate the buffers were built with, ch1 buffer, ch2 buffer)
_cache = (None, None, None)


def _simulate(random_state):
    raw = nk.ecg_simulate(
        duration=config.ECG_DURATION_S,
        sampling_rate=config.ECG_SAMPLING_RATE,
        heart_rate=config.ECG_HEART_RATE,
        noise=config.ECG_NOISE,
        random_state=random_state,
    )
    raw = np.asarray(raw, dtype=np.float64)

    lo, hi = raw.min(), raw.max()
    span = hi - lo
    out_lo = config.PLOT_MIN + _SCALE_MARGIN * (config.PLOT_MAX - config.PLOT_MIN)
    out_hi = config.PLOT_MAX - _SCALE_MARGIN * (config.PLOT_MAX - config.PLOT_MIN)
    scaled = out_lo + (raw - lo) * (out_hi - out_lo) / span if span > 0 else np.full_like(raw, out_lo)
    return scaled


def _buffers():
    """Return (ch1_buffer, ch2_buffer), regenerating if ECG_HEART_RATE has
    changed since the last call."""
    global _cache
    built_hr, ch1_buf, ch2_buf = _cache
    if built_hr != config.ECG_HEART_RATE:
        ch1_buf = _simulate(random_state=1).astype(CH1_DTYPE)
        ch2_buf = _simulate(random_state=2).astype(CH2_DTYPE)
        _cache = (config.ECG_HEART_RATE, ch1_buf, ch2_buf)
    return ch1_buf, ch2_buf


def generate_ecg_chunk(counter):
    """Slice CHUNK_SIZE consecutive samples out of the ECG buffer starting
    at `counter`, wrapping around. `counter` is the running sample index
    (same one used for the ts field), so playback position tracks it
    exactly regardless of how CHUNK_SIZE changes at runtime."""
    ch1_buf, ch2_buf = _buffers()
    n = config.CHUNK_SIZE
    pos = counter % len(ch1_buf)

    if pos + n <= len(ch1_buf):
        ch1 = ch1_buf[pos:pos + n]
        ch2 = ch2_buf[pos:pos + n]
    else:
        wrap = pos + n - len(ch1_buf)
        ch1 = np.concatenate((ch1_buf[pos:], ch1_buf[:wrap]))
        ch2 = np.concatenate((ch2_buf[pos:], ch2_buf[:wrap]))
    return ch1, ch2


def build_data_packet(ts_start, ch1, ch2):
    n = len(ch1)
    rec = np.zeros(n, dtype=DATA_DTYPE)
    rec["ts"]  = (np.arange(n, dtype=np.int64) + ts_start) % TS_MODULUS
    rec["ch1"] = ch1
    rec["ch2"] = ch2
    header = struct.pack("!HH", DATA_TYPE, n)
    return header + rec.tobytes()


def generate_signal_packet(counter, now):
    """Returns (packet_bytes, ch1, ch2) for one send cycle."""
    ch1, ch2 = generate_ecg_chunk(counter)
    packet = build_data_packet(counter, ch1, ch2)
    return packet, ch1, ch2
