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

import struct
import threading

import numpy as np
import neurokit2 as nk

import config
from packet_format import DATA_TYPE, DATA_DTYPE, TS_MODULUS, CH1_DTYPE, CH2_DTYPE

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

# Regeneration runs on its OWN thread and the old buffers keep being served
# until the new ones are ready.
#
# Why: nk.ecg_simulate() of ECG_DURATION_S=60 at 2048 Hz is ~0.74 s, twice
# (once per channel), and it used to run inline on whichever thread happened
# to ask for the next chunk -- holding the GIL, so the whole window froze.
# Every heart-rate, noise or waveform edit cost that, which is what made the
# panel feel like it was applying changes "super slowly": the change itself
# was instant, the freeze was the simulator.
#
# The change now shows up a fraction of a second later instead of blocking
# anything, which for a knob you are dragging is the difference between
# laggy and unusable. Note the buffers are rebuilt from scratch either way,
# so the waveform jumps at the swap -- that was already true.
_regen_lock = threading.Lock()
_regen_busy = False


def _regen(signature):
    """Build both channels' raw buffers for `signature` and publish them.

    Runs on a worker thread. Publishing is a single tuple assignment, which
    is atomic in CPython, so readers either see the whole old cache or the
    whole new one -- never a half-swapped pair.
    """
    global _cache, _regen_busy
    try:
        while True:
            ch1_raw, ch1_ecg_ptp = _simulate_raw(config.ECG_RANDOM_SEED, 1)
            ch2_raw, _ = _simulate_raw(config.ECG_RANDOM_SEED + 1, 2)

            # Reference ptp for sine amplitude is ch1's CLEAN ECG swing for
            # BOTH channels -- so a level set the same on both means the same
            # amplitude, and the two differ only where they were configured
            # to. Taken from _simulate_raw() rather than measured off
            # ch1_raw, so it means the same thing whether or not noise is
            # mixed in and whether or not the ECG itself is switched off.
            ch1_raw = ch1_raw + _sine_contribution(len(ch1_raw), ch1_ecg_ptp, 1)
            ch2_raw = ch2_raw + _sine_contribution(len(ch2_raw), ch1_ecg_ptp, 2)

            if config.ECG_ENABLED:
                ch1_entry = (ch1_raw, ch1_raw.min(), ch1_raw.max())
                ch2_entry = (ch2_raw, ch2_raw.min(), ch2_raw.max())
            else:
                # FIXED reference span, not the buffer's own extremes.
                # Auto-fitting stretches whatever is present to fill the
                # band, which is why the sine levels had no visible effect
                # once the ECG was gone -- a 10% sine and a 90% sine both
                # ended up filling the plot. Pinning the span to [-0.5, +0.5]
                # makes raw units full-scale fractions, so a level lands on
                # screen as exactly that share of the range.
                ch1_entry = (ch1_raw, -0.5, 0.5)
                ch2_entry = (ch2_raw, -0.5, 0.5)

            _cache = (signature, ch1_entry, ch2_entry)

            # Settings may have moved again while that ran -- someone
            # dragging a control generates a stream of them. Loop rather than
            # return so the last edit always wins, instead of leaving the
            # cache one edit behind until the next chunk happens to ask.
            latest = _config_signature()
            if latest == signature:
                return
            signature = latest
    except Exception as exc:                          # noqa: BLE001
        # A bad parameter combination must not kill regeneration for the
        # rest of the session: report it and keep serving the previous
        # buffers, which are still valid, just stale.
        print(f"[signal] regeneration failed, keeping the previous buffers: {exc}")
    finally:
        with _regen_lock:
            _regen_busy = False

# Colored-noise layers: (nk.signal_noise beta, config attr for "enabled",
# config attr for "level"). Any combination can be active simultaneously
# (see config.py's ECG_NOISE_*_ENABLED/_LEVEL) -- _simulate_raw() generates
# each enabled one separately and sums them before adding to the ECG.
# (colour, beta). Config names are built from these per channel, the same way
# the sine generators' are, so the set of colours is written once.
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

# Sine-wave interference generators, four of them, each configured per
# channel: ECG_SINE<n>_CH<c>_{ENABLED,FREQ,PHASE,LEVEL}. Built rather than
# written out, so the count is one number here and in the panel.
SINE_COUNT = 4


def sine_attrs(n, ch):
    """The four config names for generator `n` (1-based) on channel `ch`."""
    prefix = f"ECG_SINE{n}_CH{ch}_"
    return tuple(prefix + f for f in ("ENABLED", "FREQ", "PHASE", "LEVEL"))


_SINE_GENERATORS = tuple((n, ch) for n in range(1, SINE_COUNT + 1)
                         for ch in (1, 2))


def _config_signature():
    """Every config value that affects _simulate_raw()'s output. Compared
    against the last-built signature in _raw_buffers() to decide whether
    the (expensive) simulator needs to re-run. Extend this, not the cache
    tuple shape, when adding a new generation parameter."""
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

    # raw_ptp is measured on the CLEAN signal, once, before any noise is
    # added -- so each layer's _LEVEL keeps meaning "N% of the clean ECG's
    # own swing" regardless of how many other layers are also active,
    # instead of compounding against an already-noisy signal.
    raw_ptp = raw.max() - raw.min()

    # ECG off: drop the waveform but keep raw_ptp, so the noise and sine
    # layers below stay scaled to the swing the ECG *would* have had. Zeroing
    # before measuring would make raw_ptp 0, which skips the whole noise block
    # and would leave you with silence rather than the generators. Toggling
    # this therefore removes the heartbeat and changes nothing else.
    if not config.ECG_ENABLED:
        raw = np.zeros_like(raw)
        # With no ECG there is nothing to be a fraction OF, so the reference
        # becomes full scale: every generator's _LEVEL is then its own
        # peak-to-peak as a share of the plot's whole range. 0.5 means a sine
        # that fills half the plot, and it means that no matter which other
        # generators are running.
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
            # own seed/channel-2 seed) so multiple simultaneous layers --
            # or the same color on both channels -- don't correlate.
            noise = np.asarray(
                nk.signal_noise(
                    duration=config.ECG_DURATION_S,
                    sampling_rate=config.ECG_SAMPLING_RATE,
                    beta=beta,
                    random_state=random_state * 1000 + i,
                ),
                dtype=np.float64,
            )
            # signal_noise()'s length matches duration*sampling_rate exactly
            # in practice, but pad/truncate defensively rather than assume
            # it always will.
            n = min(len(raw), len(noise))
            noise_ptp = noise[:n].max() - noise[:n].min()
            if noise_ptp > 0:
                total_noise[:n] += noise[:n] * (level * raw_ptp / noise_ptp)
        raw = raw + total_noise

    # raw_ptp goes back with the buffer: it is the CLEAN ECG's swing, and the
    # sine layer added in _raw_buffers() needs the same reference the noise
    # layers used here. Recomputing it from the returned buffer would give a
    # different number once noise is mixed in, and zero when ECG_ENABLED is
    # off -- which silently sized the sine to nothing.
    return raw, raw_ptp


def _sine_contribution(n_samples, raw_ptp, channel):
    """Sum of the four generators for ONE channel, over n_samples.

    raw_ptp is a parameter, not measured here: it is ch1's clean ECG swing
    for both channels, so equal levels on ch1 and ch2 mean equal amplitudes.
    Measuring each channel's own ptp (they come from different seeds) made
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
        # level = fraction of the reference ptp that becomes the sine's OWN
        # peak-to-peak (same convention as the noise layers), so amplitude
        # (sin()'s single-sided swing) is half of that.
        total += (level * raw_ptp / 2.0) * np.sin(2 * np.pi * freq * t
                                                  + phase_rad)
    return total


def _raw_buffers():
    """Return (ch1_raw, ch1_lo, ch1_hi, ch2_raw, ch2_lo, ch2_hi).

    Never blocks on the simulator except the very first time, when there is
    nothing cached to serve. After that a settings change starts a
    regeneration on its own thread (see _regen) and this keeps handing back
    the previous buffers until the new ones are published -- so a knob on the
    panel applies in a fraction of a second instead of freezing the whole
    window for the ~1.5 s two channels of nk.ecg_simulate() take.
    """
    global _regen_busy
    sig, ch1_entry, ch2_entry = _cache
    current_sig = _config_signature()

    if sig == current_sig:
        return ch1_entry + ch2_entry

    if ch1_entry is None:
        # Cold start: nothing to serve, so this one has to be synchronous.
        # python_client.py pays it in the background at launch precisely so
        # it does not land on a user action. _regen() publishes into _cache.
        _regen(current_sig)
        _, ch1_entry, ch2_entry = _cache
        return ch1_entry + ch2_entry

    with _regen_lock:
        if not _regen_busy:
            _regen_busy = True
            threading.Thread(target=_regen, args=(current_sig,),
                             name="signal-regen", daemon=True).start()
        # If one is already running it will notice the newer signature when
        # it finishes and go round again -- see _regen's loop. Starting a
        # second thread here would just have both simulating at once.

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
    # ECG_AMPLITUDE sizes the ECG. With the ECG switched off it would just be a
    # second, hidden gain in front of every generator's own level, so the
    # levels would stop being the full-scale fractions they now claim to be.
    amplitude = (max(0.0, min(1.0, config.ECG_AMPLITUDE))
                 if config.ECG_ENABLED else 1.0)
    # Offset shifts the band's centre; amplitude still sizes it around the
    # midpoint, so the two controls stay independent (changing one does not
    # rescale the other).
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


def generate_signal_packet(counter):
    """Returns (packet_bytes, ch1, ch2) for one send cycle."""
    ch1, ch2 = generate_ecg_chunk(counter)
    packet = build_data_packet(counter, ch1, ch2)
    return packet, ch1, ch2
