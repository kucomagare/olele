# ECG signal generation (neurokit2) and packet building. No socket I/O
# here on purpose -- net.py owns the connection and calls into this
# module just to get bytes to send.
#
# ch1/ch2 are each pulled from an independently-simulated ECG buffer (built
# once, cached, regenerated only when any generation-affecting config value
# changes -- see _config_signature()/_raw_buffers()) so both plotted
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
# when its "signature" (every config knob that affects generation -- see
# _config_signature()) changes; ECG_AMPLITUDE is applied fresh on every
# chunk in _scale_to_wire(), so dragging the amplitude control doesn't
# re-run the simulator and takes effect immediately.
#
# Note nk.ecg_simulate's own amplitude-shaping kwarg (`ai`, the McSharry
# ECGSYN model's per-wave heights) turned out not to be a usable amplitude
# knob -- neurokit renormalizes its output regardless of a UNIFORM `ai`
# scale, so peak-to-peak stays ~constant across a wide range of scales
# (verified: 0.5x-3x `ai` all landed within ~3% of the same ptp). Amplitude
# control is implemented here instead, as a post-generation fit into a band
# centered on each channel's dtype range. `ai`'s RATIOS between components
# do still reshape the waveform (verified), which is why it's still exposed
# as a panel field -- just not as an amplitude control.
#
# (signature tuple the raw buffers were built with, then per-channel
#  (raw array, raw min, raw max))
_cache = (None, None, None)


def _config_signature():
    """Every config value that affects _simulate_raw()'s output. Compared
    against the last-built signature in _raw_buffers() to decide whether
    the (expensive) simulator needs to re-run. Extend this, not the cache
    tuple shape, when adding a new generation parameter."""
    return (
        config.ECG_DURATION_S, config.ECG_SAMPLING_RATE, config.ECG_HEART_RATE,
        config.ECG_HEART_RATE_STD, config.ECG_NOISE, config.ECG_METHOD,
        config.ECG_LFHFRATIO, tuple(config.ECG_TI), tuple(config.ECG_AI),
        tuple(config.ECG_BI), config.ECG_RANDOM_SEED,
        config.ECG_EXTRA_NOISE_ENABLED, config.ECG_EXTRA_NOISE_BETA,
        config.ECG_EXTRA_NOISE_LEVEL,
    )


def _simulate_raw(random_state):
    raw = nk.ecg_simulate(
        duration=config.ECG_DURATION_S,
        sampling_rate=config.ECG_SAMPLING_RATE,
        heart_rate=config.ECG_HEART_RATE,
        heart_rate_std=config.ECG_HEART_RATE_STD,
        noise=config.ECG_NOISE,
        method=config.ECG_METHOD,
        lfhfratio=config.ECG_LFHFRATIO,
        ti=config.ECG_TI,
        ai=config.ECG_AI,
        bi=config.ECG_BI,
        random_state=random_state,
    )
    raw = np.asarray(raw, dtype=np.float64)

    if config.ECG_EXTRA_NOISE_ENABLED:
        # Distinct colored-noise signal, generated separately and added on
        # top -- not the same as ECG_NOISE above, which is baked into
        # nk.ecg_simulate() itself. Scaled relative to the ECG's own
        # peak-to-peak so ECG_EXTRA_NOISE_LEVEL means the same thing (e.g.
        # "10% of the signal") regardless of heart rate/amplitude settings.
        noise = np.asarray(
            nk.signal_noise(
                duration=config.ECG_DURATION_S,
                sampling_rate=config.ECG_SAMPLING_RATE,
                beta=config.ECG_EXTRA_NOISE_BETA,
                random_state=random_state,
            ),
            dtype=np.float64,
        )
        # signal_noise()'s length matches duration*sampling_rate exactly in
        # practice, but truncate defensively rather than assume it always
        # will.
        n = min(len(raw), len(noise))
        raw, noise = raw[:n], noise[:n]
        raw_ptp = raw.max() - raw.min()
        noise_ptp = noise.max() - noise.min()
        if noise_ptp > 0 and raw_ptp > 0:
            raw = raw + noise * (config.ECG_EXTRA_NOISE_LEVEL * raw_ptp / noise_ptp)

    return raw, raw.min(), raw.max()


def _raw_buffers():
    """Return (ch1_raw, ch1_lo, ch1_hi, ch2_raw, ch2_lo, ch2_hi), regenerating
    only if _config_signature() has changed since the last call -- this is
    the expensive nk.ecg_simulate() step."""
    global _cache
    sig, ch1_entry, ch2_entry = _cache
    current_sig = _config_signature()
    if sig != current_sig:
        ch1_entry = _simulate_raw(random_state=config.ECG_RANDOM_SEED)
        ch2_entry = _simulate_raw(random_state=config.ECG_RANDOM_SEED + 1)
        _cache = (current_sig, ch1_entry, ch2_entry)
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
