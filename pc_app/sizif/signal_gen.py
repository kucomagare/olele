# ECG signal generation (neurokit2) and packet building. No socket I/O --
# net.py owns the connection. ch1/ch2 are independently-simulated buffers
# (cached, rebuilt on config change -- see _config_signature()), not a modeled 2-lead ECG.

import numpy as np
import neurokit2 as nk

import config
from packet_format import DATA_TYPE, DATA_DTYPE, TS_MODULUS, CH1_DTYPE, CH2_DTYPE

import struct

# Generation (expensive) and amplitude scaling (cheap) are decoupled: raw
# buffer cached, rebuilt only on signature change; ECG_AMPLITUDE applies
# fresh per chunk in _scale_to_wire().
#
# nk's own `ai` amplitude kwarg isn't usable -- verified 0.5x-3x uniform
# scaling all lands within ~3% ptp (nk renormalizes). Amplitude is done here
# as a post-gen fit instead; `ai`'s RATIOS still reshape the waveform.
#
# (signature tuple, then per-channel (raw array, raw min, raw max))
_cache = (None, None, None)

# Colored-noise layers: (beta, enabled attr, level attr) -- see config.py's ECG_NOISE_*.
_NOISE_LAYERS = (
    (-2, "ECG_NOISE_VIOLET_ENABLED", "ECG_NOISE_VIOLET_LEVEL"),
    (-1, "ECG_NOISE_BLUE_ENABLED", "ECG_NOISE_BLUE_LEVEL"),
    (0, "ECG_NOISE_WHITE_ENABLED", "ECG_NOISE_WHITE_LEVEL"),
    (1, "ECG_NOISE_PINK_ENABLED", "ECG_NOISE_PINK_LEVEL"),
    (2, "ECG_NOISE_BROWN_ENABLED", "ECG_NOISE_BROWN_LEVEL"),
)

# Sine interference generators: (enabled, freq, phase, level attrs). Unlike
# _NOISE_LAYERS, both channels get the IDENTICAL sine.
_SINE_GENERATORS = (
    ("ECG_SINE1_ENABLED", "ECG_SINE1_FREQ", "ECG_SINE1_PHASE", "ECG_SINE1_LEVEL"),
    ("ECG_SINE2_ENABLED", "ECG_SINE2_FREQ", "ECG_SINE2_PHASE", "ECG_SINE2_LEVEL"),
)


def _config_signature():
    """Every config value affecting _simulate_raw()'s output, compared against
    the cached one to gate a re-run. Extend this for new generation params."""
    noise_layers = tuple(
        (getattr(config, enabled_attr), getattr(config, level_attr))
        for _beta, enabled_attr, level_attr in _NOISE_LAYERS
    )
    sine_generators = tuple(
        (getattr(config, enabled_attr), getattr(config, freq_attr),
         getattr(config, phase_attr), getattr(config, level_attr))
        for enabled_attr, freq_attr, phase_attr, level_attr in _SINE_GENERATORS
    )
    return (
        config.ECG_DURATION_S, config.ECG_SAMPLING_RATE, config.ECG_HEART_RATE,
        config.ECG_HEART_RATE_STD, config.ECG_NOISE, config.ECG_METHOD,
        config.ECG_LFHFRATIO, tuple(config.ECG_TI), tuple(config.ECG_AI),
        tuple(config.ECG_BI), config.ECG_RANDOM_SEED, noise_layers, sine_generators,
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

    # Measured on the CLEAN signal once, so each layer's _LEVEL means "N% of clean swing".
    raw_ptp = raw.max() - raw.min()
    if raw_ptp > 0:
        total_noise = np.zeros_like(raw)
        for i, (beta, enabled_attr, level_attr) in enumerate(_NOISE_LAYERS):
            if not getattr(config, enabled_attr):
                continue
            level = getattr(config, level_attr)
            if level <= 0:
                continue
            # Distinct random_state per layer so simultaneous/same-color layers don't correlate.
            noise = np.asarray(
                nk.signal_noise(
                    duration=config.ECG_DURATION_S,
                    sampling_rate=config.ECG_SAMPLING_RATE,
                    beta=beta,
                    random_state=random_state * 1000 + i,
                ),
                dtype=np.float64,
            )
            # Defensive pad/truncate -- length isn't guaranteed to match exactly.
            n = min(len(raw), len(noise))
            noise_ptp = noise[:n].max() - noise[:n].min()
            if noise_ptp > 0:
                total_noise[:n] += noise[:n] * (level * raw_ptp / noise_ptp)
        raw = raw + total_noise

    return raw


def _sine_contribution(n, raw_ptp):
    """Sum of both sine generators, computed ONCE and added identically to
    both channels. raw_ptp is passed in (not measured per-channel) -- using
    each channel's own ptp broke the "identical sine" promise by a fraction
    of a percent (verified)."""
    total = np.zeros(n)
    if raw_ptp <= 0:
        return total
    t = np.arange(n) / config.ECG_SAMPLING_RATE
    for enabled_attr, freq_attr, phase_attr, level_attr in _SINE_GENERATORS:
        if not getattr(config, enabled_attr):
            continue
        level = getattr(config, level_attr)
        if level <= 0:
            continue
        freq = getattr(config, freq_attr)
        phase_rad = np.deg2rad(getattr(config, phase_attr))
        # level = fraction of ref ptp as the sine's OWN ptp; amplitude is half that.
        amplitude = level * raw_ptp / 2.0
        total += amplitude * np.sin(2 * np.pi * freq * t + phase_rad)
    return total


def _raw_buffers():
    """Return (ch1_raw, ch1_lo, ch1_hi, ch2_raw, ch2_lo, ch2_hi), regenerating
    (the expensive nk.ecg_simulate() step) only on a signature change."""
    global _cache
    sig, ch1_entry, ch2_entry = _cache
    current_sig = _config_signature()
    if sig != current_sig:
        ch1_raw = _simulate_raw(random_state=config.ECG_RANDOM_SEED)
        ch2_raw = _simulate_raw(random_state=config.ECG_RANDOM_SEED + 1)

        # Reference ptp is ch1's (arbitrary, but must be the SAME channel both times).
        sine = _sine_contribution(len(ch1_raw), ch1_raw.max() - ch1_raw.min())
        ch1_raw = ch1_raw + sine[:len(ch1_raw)]
        ch2_raw = ch2_raw + sine[:len(ch2_raw)]

        ch1_entry = (ch1_raw, ch1_raw.min(), ch1_raw.max())
        ch2_entry = (ch2_raw, ch2_raw.min(), ch2_raw.max())
        _cache = (current_sig, ch1_entry, ch2_entry)
    return ch1_entry + ch2_entry


def _scale_to_wire(raw_chunk, raw_lo, raw_hi, dtype):
    """Fit raw_chunk into a band centered on dtype's range, sized to
    ECG_AMPLITUDE. Uses the WHOLE buffer's min/max, not the chunk's, or each
    chunk would rescale to local extremes and lose true shape."""
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
    # Safety net for float rounding at the exact edges.
    return np.clip(scaled, 0, dtype_max).astype(dtype)


def generate_ecg_chunk(counter):
    """Slice CHUNK_SIZE samples at `counter` (wrapping), scale to wire units.
    `counter` doubles as ts, so playback tracks it even if CHUNK_SIZE changes."""
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
