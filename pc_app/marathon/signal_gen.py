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
import adc_sim
from packet_format import DATA_TYPE, DATA_DTYPE, TS_MODULUS, CH1_DTYPE, CH2_DTYPE

# Generation is cached (_config_signature()); ECG_AMPLITUDE_MV rescales
# fresh per chunk in generate_ecg_chunk(), so it stays instant to drag.
#
# Note: nk.ecg_simulate's `ai` kwarg can't do amplitude (neurokit
# renormalizes regardless) -- kept exposed only for its wave-shape ratios.
#
# _cache: (signature, ch1_raw, ch1_ptp, ch1_extra_mv,
#                      ch2_raw, ch2_ptp, ch2_extra_mv)
# ch*_raw is neurokit's arbitrary-unit ECG (rescaled at chunk time);
# ch*_extra_mv is noise+sine, already absolute mV.
_cache = (None, None, None, None, None, None, None)

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
            ch1_raw, ch1_ptp = _simulate_raw(config.ECG_RANDOM_SEED, 1)
            ch2_raw, ch2_ptp = _simulate_raw(config.ECG_RANDOM_SEED + 1, 2)

            ch1_extra = (_noise_sum_mv(len(ch1_raw), config.ECG_RANDOM_SEED, 1)
                         + _sine_contribution(len(ch1_raw), 1))
            ch2_extra = (_noise_sum_mv(len(ch2_raw), config.ECG_RANDOM_SEED + 1, 2)
                         + _sine_contribution(len(ch2_raw), 2))

            _cache = (signature, ch1_raw, ch1_ptp, ch1_extra,
                                  ch2_raw, ch2_ptp, ch2_extra)

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
# ECG_NOISE_*_ENABLED/_LEVEL_MV), summed before adding to the ECG. Config
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
    return prefix + "ENABLED", prefix + "LEVEL_MV"

# Sine interference generators, four of them, each configured per channel:
# ECG_SINE<n>_CH<c>_{ENABLED,FREQ,PHASE,LEVEL_MV}. Count lives here once.
SINE_COUNT = 4


def sine_attrs(n, ch):
    """The four config names for generator `n` (1-based) on channel `ch`."""
    prefix = f"ECG_SINE{n}_CH{ch}_"
    return tuple(prefix + f for f in ("ENABLED", "FREQ", "PHASE", "LEVEL_MV"))


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
    """(ecg_raw, ecg_raw_ptp) in neurokit's own units -- NOT rescaled to
    ECG_AMPLITUDE_MV here, see generate_ecg_chunk()."""
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

    if not config.ECG_ENABLED:
        return np.zeros_like(raw), 0.0  # noise/sine keep running regardless

    return raw, raw.max() - raw.min()


def _noise_sum_mv(n_samples, random_state, channel):
    """Sum of every enabled colored-noise layer for ONE channel, absolute
    mV peak-to-peak (config.py's ECG_NOISE_*_LEVEL_MV)."""
    total = np.zeros(n_samples)
    for i, (colour, beta) in enumerate(NOISE_COLOURS):
        enabled_attr, level_attr = noise_attrs(colour, channel)
        if not getattr(config, enabled_attr):
            continue
        level_mv = getattr(config, level_attr)
        if level_mv <= 0:
            continue
        # Distinct random_state per layer (and offset from the ECG's own
        # seed) so simultaneous layers -- or the same colour on both
        # channels -- don't correlate.
        noise = np.asarray(
            nk.signal_noise(
                duration=config.ECG_DURATION_S,
                sampling_rate=config.ECG_SAMPLING_RATE,
                beta=beta,
                random_state=random_state * 1000 + i,
            ),
            dtype=np.float64,
        )
        # Length matches duration*sampling_rate exactly in practice, but
        # pad/truncate defensively rather than assume it always will.
        n = min(n_samples, len(noise))
        noise_ptp = noise[:n].max() - noise[:n].min()
        if noise_ptp > 0:
            total[:n] += noise[:n] * (level_mv / noise_ptp)
    return total


def _sine_contribution(n_samples, channel):
    """Sum of the four generators for ONE channel, absolute mV."""
    total = np.zeros(n_samples)
    t = np.arange(n_samples) / config.ECG_SAMPLING_RATE
    for n, ch in _SINE_GENERATORS:
        if ch != channel:
            continue
        enabled_attr, freq_attr, phase_attr, level_attr = sine_attrs(n, ch)
        if not getattr(config, enabled_attr):
            continue
        level_mv = getattr(config, level_attr)
        if level_mv <= 0:
            continue
        freq = getattr(config, freq_attr)
        phase_rad = np.deg2rad(getattr(config, phase_attr))
        # sin()'s single-sided amplitude is half the configured peak-to-peak.
        total += (level_mv / 2.0) * np.sin(2 * np.pi * freq * t + phase_rad)
    return total


def _raw_buffers():
    """(ch1_raw, ch1_ptp, ch1_extra_mv, ch2_raw, ch2_ptp, ch2_extra_mv) --
    see _cache's comment. Blocks only on cold start; otherwise hands back
    the previous buffers while a regen runs on its own thread."""
    global _regen_busy
    sig, ch1_raw, ch1_ptp, ch1_extra, ch2_raw, ch2_ptp, ch2_extra = _cache
    current_sig = _config_signature()

    if sig == current_sig:
        return ch1_raw, ch1_ptp, ch1_extra, ch2_raw, ch2_ptp, ch2_extra

    if ch1_raw is None:
        # Cold start: nothing to serve, so this one has to be synchronous.
        # python_client.py pays it in the background at launch precisely
        # so it doesn't land on a user action. _regen() publishes into _cache.
        _regen(current_sig)
        _, ch1_raw, ch1_ptp, ch1_extra, ch2_raw, ch2_ptp, ch2_extra = _cache
        return ch1_raw, ch1_ptp, ch1_extra, ch2_raw, ch2_ptp, ch2_extra

    with _regen_lock:
        if not _regen_busy:
            _regen_busy = True
            threading.Thread(target=_regen, args=(current_sig,),
                             name="signal-regen", daemon=True).start()
        # Already running? It'll notice the newer signature when it
        # finishes and go round again (see _regen's loop) -- starting a
        # second thread here would just have both simulating at once.

    return ch1_raw, ch1_ptp, ch1_extra, ch2_raw, ch2_ptp, ch2_extra


def generate_ecg_chunk(counter):
    """Slice CHUNK_SIZE samples at `counter` (wrapping), rescale ECG to
    ECG_AMPLITUDE_MV, add noise+sine (mV), digitize via adc_sim."""
    (ch1_raw, ch1_ptp, ch1_extra,
     ch2_raw, ch2_ptp, ch2_extra) = _raw_buffers()
    n = config.CHUNK_SIZE
    pos = counter % len(ch1_raw)

    def _slice(buf):
        if pos + n <= len(buf):
            return buf[pos:pos + n]
        wrap = pos + n - len(buf)
        return np.concatenate((buf[pos:], buf[:wrap]))

    # ptp == 0 -> zero scale, not a divide by zero.
    ch1_scale = (config.ECG_AMPLITUDE_MV / ch1_ptp) if ch1_ptp > 0 else 0.0
    ch2_scale = (config.ECG_AMPLITUDE_MV / ch2_ptp) if ch2_ptp > 0 else 0.0

    ch1_mv = _slice(ch1_raw) * ch1_scale + _slice(ch1_extra)
    ch2_mv = _slice(ch2_raw) * ch2_scale + _slice(ch2_extra)

    ch1 = adc_sim.digitize(ch1_mv, 1, CH1_DTYPE)
    ch2 = adc_sim.digitize(ch2_mv, 2, CH2_DTYPE)
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
