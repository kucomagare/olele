# ECG signal generation (neurokit2) and packet building. No socket I/O
# here -- net.py owns the connection and calls in just for bytes to send.
#
# ch1/ch2 are each pulled from an independently-simulated ECG buffer, so
# both plotted channels show real morphology rather than one being a
# scaled copy of the other. Placeholder pairing, not a modeled two-lead
# ECG -- swap for a real second lead if channels need to differ clinically.

import struct
import threading

import numpy as np
import neurokit2 as nk

import config
from packet_format import DATA_TYPE, DATA_DTYPE, TS_MODULUS, CH1_DTYPE, CH2_DTYPE

# Generation (nk.ecg_simulate, expensive) and amplitude scaling (cheap) are
# deliberately decoupled: the raw buffer is cached and only rebuilt when
# its _config_signature() changes; ECG_AMPLITUDE applies fresh per chunk
# in _scale_to_wire(), so dragging the amplitude control is instant.
#
# Note: nk.ecg_simulate's own `ai` kwarg (per-wave heights) is NOT a usable
# amplitude knob -- neurokit renormalizes regardless of a uniform `ai`
# scale (verified: 0.5x-3x all landed within ~3% of the same ptp).
# Amplitude is implemented here instead, as a post-generation fit into a
# band centered on each channel's dtype range. `ai`'s RATIOS between
# components do still reshape the waveform (verified), so it stays
# exposed as a panel field -- just not as an amplitude control.
#
# (signature tuple the raw buffers were built with, then per-channel
#  (raw array, raw min, raw max))
_cache = (None, None, None)

# Regeneration runs on its OWN thread; old buffers keep serving until the
# new ones are ready. nk.ecg_simulate(60s @ 2048Hz) is ~0.74s x2, and it
# used to run inline on whichever thread asked for the next chunk --
# holding the GIL and freezing the window on every heart-rate/noise/
# waveform edit. The change now lands a fraction of a second later instead
# of blocking; buffers still rebuild from scratch, so the waveform still
# jumps at the swap -- that was already true.
_regen_lock = threading.Lock()
_regen_busy = False


def _regen(signature):
    """Build both channels' raw buffers for `signature` and publish them.

    Runs on a worker thread. Publishing is a single tuple assignment
    (atomic in CPython), so readers never see a half-swapped pair.
    """
    global _cache, _regen_busy
    try:
        while True:
            ch1_raw, ch1_ecg_ptp = _simulate_raw(config.ECG_RANDOM_SEED, 1)
            ch2_raw, _ = _simulate_raw(config.ECG_RANDOM_SEED + 1, 2)

            # Reference ptp is ch1's CLEAN ECG swing for BOTH channels, so
            # equal levels mean equal amplitude regardless of noise/ECG-off state.
            ch1_raw = ch1_raw + _sine_contribution(len(ch1_raw), ch1_ecg_ptp, 1)
            ch2_raw = ch2_raw + _sine_contribution(len(ch2_raw), ch1_ecg_ptp, 2)

            if config.ECG_ENABLED:
                ch1_entry = (ch1_raw, ch1_raw.min(), ch1_raw.max())
                ch2_entry = (ch2_raw, ch2_raw.min(), ch2_raw.max())
            else:
                # FIXED span [-0.5, 0.5], not the buffer's own extremes --
                # auto-fitting would stretch any level to fill the plot,
                # making sine level invisible with the ECG gone.
                ch1_entry = (ch1_raw, -0.5, 0.5)
                ch2_entry = (ch2_raw, -0.5, 0.5)

            _cache = (signature, ch1_entry, ch2_entry)

            # Loop rather than return: settings may have moved again while
            # that ran (a dragged control streams edits) -- last edit
            # always wins instead of leaving the cache one edit behind.
            latest = _config_signature()
            if latest == signature:
                return
            signature = latest
    except Exception as exc:                          # noqa: BLE001
        # Bad params must not kill regen for the rest of the session --
        # report, keep serving the previous (stale but valid) buffers.
        print(f"[signal] regeneration failed, keeping the previous buffers: {exc}")
    finally:
        with _regen_lock:
            _regen_busy = False

# Colored-noise layers: any combination active simultaneously (config.py's
# ECG_NOISE_*_ENABLED/_LEVEL), summed before adding to the ECG. Config
# names built from these per channel, same pattern as the sine generators.
NOISE_COLOURS = (
    ("VIOLET", -2),
    ("BLUE", -1),
    ("WHITE", 0),
    ("PINK", 1),
    ("BROWN", 2),
)


def noise_attrs(colour, ch):
    """The two config names for `colour` on channel `ch`."""
    prefix = f"ECG_NOISE_{colour}_CH{ch}_"
    return prefix + "ENABLED", prefix + "LEVEL"

# Sine interference generators, four of them, each configured per channel:
# ECG_SINE<n>_CH<c>_{ENABLED,FREQ,PHASE,LEVEL}. Count lives here once.
SINE_COUNT = 4


def sine_attrs(n, ch):
    """The four config names for generator `n` (1-based) on channel `ch`."""
    prefix = f"ECG_SINE{n}_CH{ch}_"
    return tuple(prefix + f for f in ("ENABLED", "FREQ", "PHASE", "LEVEL"))


_SINE_GENERATORS = tuple((n, ch) for n in range(1, SINE_COUNT + 1)
                         for ch in (1, 2))


def _config_signature():
    """Every config value that affects _simulate_raw()'s output, compared
    against the last-built signature to decide whether the (expensive)
    simulator needs to re-run. Extend this, not the cache tuple shape,
    when adding a new generation parameter."""
    noise_layers = tuple(
        tuple(getattr(config, attr) for attr in noise_attrs(colour, ch))
        for colour, _beta in NOISE_COLOURS for ch in (1, 2)
    )
    sine_generators = tuple(
        tuple(getattr(config, attr) for attr in sine_attrs(n, ch))
        for n, ch in _SINE_GENERATORS
    )
    return (
        config.ECG_DURATION_S, config.ECG_SAMPLING_RATE, config.ECG_HEART_RATE,
        config.ECG_ENABLED,
        config.ECG_HEART_RATE_STD, config.ECG_NOISE, config.ECG_METHOD,
        config.ECG_LFHFRATIO, tuple(config.ECG_TI), tuple(config.ECG_AI),
        tuple(config.ECG_BI), config.ECG_RANDOM_SEED, noise_layers, sine_generators,
    )


def _simulate_raw(random_state, channel):
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

    # Measured on the CLEAN signal, once, before any noise is added -- so
    # each layer's LEVEL keeps meaning "N% of the clean ECG's own swing"
    # regardless of how many other layers are also active.
    raw_ptp = raw.max() - raw.min()

    # ECG off: drop the waveform but keep raw_ptp, so the noise/sine
    # layers below stay scaled to the swing the ECG *would* have had.
    # Zeroing before measuring would make raw_ptp 0 and skip the whole
    # noise block, leaving silence instead of the generators.
    if not config.ECG_ENABLED:
        raw = np.zeros_like(raw)
        # Nothing to be a fraction OF, so the reference becomes full
        # scale -- every generator's LEVEL is then its own ptp as a share
        # of the plot's whole range, independent of which others are running.
        raw_ptp = 1.0
    if raw_ptp > 0:
        total_noise = np.zeros_like(raw)
        for i, (colour, beta) in enumerate(NOISE_COLOURS):
            enabled_attr, level_attr = noise_attrs(colour, channel)
            if not getattr(config, enabled_attr):
                continue
            level = getattr(config, level_attr)
            if level <= 0:
                continue
            # Distinct random_state per layer (and offset from the ECG's
            # own seed) so simultaneous layers -- or the same colour on
            # both channels -- don't correlate.
            noise = np.asarray(
                nk.signal_noise(
                    duration=config.ECG_DURATION_S,
                    sampling_rate=config.ECG_SAMPLING_RATE,
                    beta=beta,
                    random_state=random_state * 1000 + i,
                ),
                dtype=np.float64,
            )
            # Length matches duration*sampling_rate exactly in practice,
            # but pad/truncate defensively rather than assume it always will.
            n = min(len(raw), len(noise))
            noise_ptp = noise[:n].max() - noise[:n].min()
            if noise_ptp > 0:
                total_noise[:n] += noise[:n] * (level * raw_ptp / noise_ptp)
        raw = raw + total_noise

    # raw_ptp returned alongside the buffer: it's the CLEAN swing the sine
    # layer added in _raw_buffers() needs. Recomputing it from the
    # returned buffer would give a different number once noise is mixed
    # in, and zero when ECG_ENABLED is off -- silently sizing the sine to nothing.
    return raw, raw_ptp


def _sine_contribution(n_samples, raw_ptp, channel):
    """Sum of the four generators for ONE channel, over n_samples.

    raw_ptp is a parameter, not measured here: it's ch1's clean ECG swing
    for both channels, so equal levels on ch1 and ch2 mean equal
    amplitudes. Measuring each channel's own ptp (different seeds) made
    equal settings differ by a fraction of a percent.
    """
    total = np.zeros(n_samples)
    if raw_ptp <= 0:
        return total
    t = np.arange(n_samples) / config.ECG_SAMPLING_RATE
    for n, ch in _SINE_GENERATORS:
        if ch != channel:
            continue
        enabled_attr, freq_attr, phase_attr, level_attr = sine_attrs(n, ch)
        if not getattr(config, enabled_attr):
            continue
        level = getattr(config, level_attr)
        if level <= 0:
            continue
        freq = getattr(config, freq_attr)
        phase_rad = np.deg2rad(getattr(config, phase_attr))
        # level = fraction of the reference ptp that becomes the sine's
        # OWN peak-to-peak (same convention as the noise layers), so
        # amplitude (sin()'s single-sided swing) is half of that.
        total += (level * raw_ptp / 2.0) * np.sin(2 * np.pi * freq * t
                                                  + phase_rad)
    return total


def _raw_buffers():
    """Return (ch1_raw, ch1_lo, ch1_hi, ch2_raw, ch2_lo, ch2_hi).

    Blocks on the simulator only on cold start, when nothing is cached
    yet. After that a settings change starts a regeneration on its own
    thread (see _regen) and this keeps handing back the previous buffers
    until the new ones are published -- so a panel knob applies in a
    fraction of a second instead of freezing the window for the ~1.5s two
    channels of nk.ecg_simulate() take.
    """
    global _regen_busy
    sig, ch1_entry, ch2_entry = _cache
    current_sig = _config_signature()

    if sig == current_sig:
        return ch1_entry + ch2_entry

    if ch1_entry is None:
        # Cold start: nothing to serve, so this one has to be synchronous.
        # python_client.py pays it in the background at launch precisely
        # so it doesn't land on a user action. _regen() publishes into _cache.
        _regen(current_sig)
        _, ch1_entry, ch2_entry = _cache
        return ch1_entry + ch2_entry

    with _regen_lock:
        if not _regen_busy:
            _regen_busy = True
            threading.Thread(target=_regen, args=(current_sig,),
                             name="signal-regen", daemon=True).start()
        # Already running? It'll notice the newer signature when it
        # finishes and go round again (see _regen's loop) -- starting a
        # second thread here would just have both simulating at once.

    return ch1_entry + ch2_entry


def _scale_to_wire(raw_chunk, raw_lo, raw_hi, dtype):
    """Fit raw_chunk (using the *whole buffer's* min/max, not the
    chunk's -- otherwise each chunk would rescale to its own local
    extremes and the waveform would lose its true relative shape) into a
    band centered on dtype's range, sized to config.ECG_AMPLITUDE
    fraction of that range. ECG_AMPLITUDE=1.0 spans the full dtype range
    (e.g. 0..65535 for uint16); 0.0 collapses to the midpoint."""
    dtype_max = np.iinfo(dtype).max
    # ECG off: force amplitude=1.0, or it would be a second hidden gain
    # in front of every generator's own level, and the levels would stop
    # being the full-scale fractions they now claim to be.
    amplitude = (max(0.0, min(1.0, config.ECG_AMPLITUDE))
                 if config.ECG_ENABLED else 1.0)
    # Offset shifts the band's centre; amplitude still sizes it around
    # the midpoint, so the two controls stay independent.
    offset = max(config.ECG_OFFSET_MIN,
                 min(config.ECG_OFFSET_MAX, config.ECG_OFFSET))
    center = dtype_max / 2.0 + offset * dtype_max
    half_span = (dtype_max / 2.0) * amplitude
    out_lo, out_hi = center - half_span, center + half_span

    span = raw_hi - raw_lo
    if span > 0:
        scaled = out_lo + (raw_chunk - raw_lo) * (out_hi - out_lo) / span
    else:
        scaled = np.full_like(raw_chunk, center)
    # Safety net only -- out_lo/out_hi are already within [0, dtype_max]
    # by construction, this just guards float rounding at the exact edges.
    return np.clip(scaled, 0, dtype_max).astype(dtype)


def generate_ecg_chunk(counter):
    """Slice CHUNK_SIZE consecutive samples out of the ECG buffer starting
    at `counter`, wrapping around, then scale to wire units. `counter` is
    the running sample index (same one used for the ts field), so
    playback position tracks it exactly regardless of CHUNK_SIZE changes
    at runtime."""
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


def generate_signal_packet(counter):
    """Returns (packet_bytes, ch1, ch2) for one send cycle."""
    ch1, ch2 = generate_ecg_chunk(counter)
    packet = build_data_packet(counter, ch1, ch2)
    return packet, ch1, ch2
