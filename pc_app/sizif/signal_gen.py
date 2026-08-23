# ECG signal generation (neurokit2) and packet building. No socket I/O
# here on purpose -- net.py owns the connection and calls into this
# module just to get bytes to send.
#
# ch1/ch2 are each pulled from an independently-simulated ECG buffer
# (built once, cached, regenerated only when config.ECG_HEART_RATE or
# config.ECG_SAMPLING_RATE changes -- see _raw_buffers()) so both plotted
# channels show real ECG morphology rather than one being a scaled copy of
# the other. Swap this for a second lead or a different signal later if you
# need channels to actually differ clinically -- this is a placeholder
# pairing, not a modeled two-lead ECG.

import numpy as np
import neurokit2 as nk

import config
from packet_format import DATA_TYPE, DATA_DTYPE, TS_MODULUS, CH1_DTYPE, CH2_DTYPE

import struct

# Generation (nk.ecg_simulate, expensive) and amplitude scaling (cheap) are
# deliberately decoupled: the raw float buffer is cached and only rebuilt
# when ECG_HEART_RATE or ECG_SAMPLING_RATE changes (see _raw_buffers());
# ECG_AMPLITUDE is applied fresh on every chunk, so dragging the amplitude
# control doesn't re-run the simulator and takes effect immediately.
#
# Note nk.ecg_simulate's own amplitude-shaping kwarg (`ai`, the McSharry
# ECGSYN model's per-wave heights) turned out not to be a usable amplitude
# knob -- neurokit renormalizes its output regardless of `ai`, so peak-to-
# peak stays ~constant across a wide range of `ai` scales (verified: 0.5x-
# 3x `ai` all landed within ~3% of the same ptp). Amplitude control is
# implemented here instead, as a post-generation fit into a band centered
# on each channel's dtype range.
#
# (heart_rate, sampling_rate the raw buffers were built with, then per-channel
#  (raw array, raw min, raw max))
_cache = (None, None, None, None)


def _simulate_raw(random_state):
    raw = nk.ecg_simulate(
        duration=config.ECG_DURATION_S,
        sampling_rate=config.ECG_SAMPLING_RATE,
        heart_rate=config.ECG_HEART_RATE,
        noise=config.ECG_NOISE,
        random_state=random_state,
    )
    raw = np.asarray(raw, dtype=np.float64)
    return raw, raw.min(), raw.max()


def _raw_buffers():
    """Return (ch1_raw, ch1_lo, ch1_hi, ch2_raw, ch2_lo, ch2_hi), regenerating
    only if ECG_HEART_RATE or ECG_SAMPLING_RATE has changed since the last
    call -- this is the expensive nk.ecg_simulate() step."""
    global _cache
    built_hr, built_fs, ch1_entry, ch2_entry = _cache
    if built_hr != config.ECG_HEART_RATE or built_fs != config.ECG_SAMPLING_RATE:
        ch1_entry = _simulate_raw(random_state=1)
        ch2_entry = _simulate_raw(random_state=2)
        _cache = (config.ECG_HEART_RATE, config.ECG_SAMPLING_RATE, ch1_entry, ch2_entry)
    return ch1_entry + ch2_entry


def _scale_to_wire(raw_chunk, raw_lo, raw_hi, dtype):
    """Fit raw_chunk (using the *whole buffer's* min/max, not the chunk's --
    otherwise each chunk would rescale to its own local extremes and the
    waveform would lose its true relative shape) into a band centered on
    dtype's range, sized to config.ECG_AMPLITUDE fraction of that range.
    ECG_AMPLITUDE=1.0 -> spans the full dtype range (e.g. 0..65535 for
    uint16); 0.0 -> collapses to the midpoint. This is what ties the
    amplitude control to "the maximum size of the data amplitude in out
    packets" -- the ceiling is the wire dtype's own max, not an arbitrary
    plot constant."""
    dtype_max = np.iinfo(dtype).max
    amplitude = max(0.0, min(1.0, config.ECG_AMPLITUDE))
    center = dtype_max / 2.0
    half_span = center * amplitude
    out_lo, out_hi = center - half_span, center + half_span

    span = raw_hi - raw_lo
    if span > 0:
        scaled = out_lo + (raw_chunk - raw_lo) * (out_hi - out_lo) / span
    else:
        scaled = np.full_like(raw_chunk, center)
    # Safety net only -- out_lo/out_hi are already within [0, dtype_max] by
    # construction, this just guards float rounding at the exact edges.
    return np.clip(scaled, 0, dtype_max).astype(dtype)


def generate_ecg_chunk(counter):
    """Slice CHUNK_SIZE consecutive samples out of the ECG buffer starting
    at `counter`, wrapping around, then scale to wire units. `counter` is
    the running sample index (same one used for the ts field), so playback
    position tracks it exactly regardless of how CHUNK_SIZE changes at
    runtime."""
    ch1_raw, ch1_lo, ch1_hi, ch2_raw, ch2_lo, ch2_hi = _raw_buffers()
    n = config.CHUNK_SIZE
    pos = counter % len(ch1_raw)

    if pos + n <= len(ch1_raw):
        ch1_slice = ch1_raw[pos:pos + n]
        ch2_slice = ch2_raw[pos:pos + n]
    else:
        wrap = pos + n - len(ch1_raw)
        ch1_slice = np.concatenate((ch1_raw[pos:], ch1_raw[:wrap]))
        ch2_slice = np.concatenate((ch2_raw[pos:], ch2_raw[:wrap]))

    ch1 = _scale_to_wire(ch1_slice, ch1_lo, ch1_hi, CH1_DTYPE)
    ch2 = _scale_to_wire(ch2_slice, ch2_lo, ch2_hi, CH2_DTYPE)
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
