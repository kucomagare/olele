#!/usr/bin/env python3
"""SAT -- Static Analysis Tool. Offline analysis of a logged buffer: spectra,
the hardware against a model, and the pipelines' own amplitude/phase response.

    ./sat.py                         # open the window on the newest dump
    ./sat.py FILE.csv                # ...on a particular one
    ./sat.py --list                  # what dumps exist
    ./sat.py --no-plot               # numbers only, for a terminal or a pipe
    ./sat.py --no-plot --model iir --peak-fmin 40
    ./sat.py --phase raw             # plain FFT angle beside the spectra
    ./sat.py --phase-units "group ms"   # how late is each frequency, in ms
    ./sat.py --response pipe2        # Bode plot: pipe2 manual vs scipy
    ./sat.py --response pipe1:scipy,pipe2:scipy,iir     # any mix, overlaid
    ./sat.py --response pipe2 --overlay both            # capture on top
    ./sat.py --response pipe2 --response-fmin 40 --response-fmax 60 \
             --response-points 200                      # zoom on the notch
    ./sat.py --response pipe2 --set pipe2_notch_hz=60   # move a corner

Every flag is also a control in the window (sat_gui.py); flags remain for
scripting and headless boxes.

Separate from the live client: a rolling-buffer FFT there would cost every
frame forever for a measurement that isn't actually live -- easier on a
file, where the same capture can be examined many ways.

Reads the pair the plot bar's "Log buffer" button writes:
    plotdump_<stamp>.csv            index, time_s, and the four traces
    plot_config_data_<stamp>.txt    every setting, board registers, metrics
The sidecar is the ground truth for the samples next to it.
"""

import argparse
import re
import sys
from pathlib import Path

import numpy as np

# Run from anywhere: sibling modules (spectrum, pipelines) live next to
# this file, not necessarily in the working directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import spectrum

TRACES = ("ch1_in", "ch1_out", "ch2_in", "ch2_out")
DEFAULT_LOGS = Path(__file__).resolve().parent / "build" / "logs"


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def find_dumps(log_dir):
    return sorted(Path(log_dir).glob("plotdump_*.csv"))


def sidecar_for(csv_path):
    """Settings snapshot written alongside a dump, by naming convention."""
    name = csv_path.name.replace("plotdump_", "plot_config_data_")
    return csv_path.with_name(name).with_suffix(".txt")


def read_sidecar(path):
    """Parse `key = value` lines into a dict, section heading as prefix
    (so board-register "shift" can't collide with a config knob)."""
    out = {}
    if not path.exists():
        return out
    section = ""
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            continue
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()
        out[key] = val
        out[f"{section}::{key}"] = val
    return out


def load_dump(csv_path):
    """Return (traces dict, metadata dict). Metadata from the sidecar
    first, CSV `#` header lines second, so a lost sidecar still analyses."""
    meta = read_sidecar(sidecar_for(csv_path))

    # Parsed by hand, not genfromtxt: its comment handling blanks the `#`
    # lines before names=True finds the header, so it reads column names
    # off the wrong line and rejects every data row.
    header_meta, columns, rows = {}, None, []
    with open(csv_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            if line.startswith("#"):
                for k, v in re.findall(r"(\w+)=([^\s]+)", line):
                    header_meta.setdefault(k, v)
                continue
            fields = line.split(",")
            if columns is None:
                columns = fields
                continue
            rows.append(fields)

    # ValueError, not SystemExit: the GUI's Exception handler would let a
    # BaseException-derived SystemExit through and take the window down.
    if columns is None or not rows:
        raise ValueError(f"{csv_path}: no data rows")

    table = np.array(rows, dtype=np.float64)
    traces = {name: table[:, i] for i, name in enumerate(columns)
              if name in TRACES}
    if not traces:
        raise ValueError(f"{csv_path}: no recognised trace columns "
                         f"(expected {', '.join(TRACES)})")

    def number(*keys, default=None):
        for k in keys:
            for src in (meta, header_meta):
                if k in src:
                    try:
                        return float(src[k])
                    except ValueError:
                        pass
        return default

    info = {
        "path": csv_path,
        "samples": len(next(iter(traces.values()))),
        "rate": number("ECG_SAMPLING_RATE", "ecg_rate", default=2048.0),
        "shift": number("board filter registers (read back from fabric)::shift",
                        default=None),
        "heart_rate": number("ECG_HEART_RATE", "hr"),
        "amplitude": number("ECG_AMPLITUDE", "amplitude"),
        "send_rate": number("SEND_RATE", "send_rate"),
        "chunk": number("CHUNK_SIZE", "chunk"),
        "trigger": meta.get("PLOT_TRIGGER", header_meta.get("trigger")),
        # 4 generators x 2 channels; a pre-per-channel dump has none of
        # these keys and the list comes out empty.
        "sines": [(n, ch, meta.get(f"ECG_SINE{n}_CH{ch}_ENABLED"),
                   meta.get(f"ECG_SINE{n}_CH{ch}_FREQ"),
                   meta.get(f"ECG_SINE{n}_CH{ch}_PHASE"),
                   meta.get(f"ECG_SINE{n}_CH{ch}_LEVEL"))
                  for n in range(1, 5) for ch in (1, 2)],
        "meta": meta,
    }
    return traces, info


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

def wire_full_scale(traces, info=None):
    """Full-scale value of the wire format the dump was captured in.
    Prefers the sidecar's `wire_dtype`; inferring from sample values is only
    right if the capture happens to hit the top of its range (a quiet
    32-bit capture under 65535 would misread as 16-bit), so that's the
    fallback only."""
    if info:
        dtype = (info.get("meta", {}) or {}).get("wire_dtype")
        if dtype:
            try:
                return float(np.iinfo(np.dtype(dtype)).max)
            except (TypeError, ValueError):
                pass                       # unrecognised: fall through
    peak = max(float(np.max(v)) for v in traces.values())
    return 2.0 ** 32 - 1 if peak > 65535 else 65535.0


def analyse_channel(traces, ch, rate, full_scale, size, peak_fmin):
    """Spectra of one channel's in/out, plus the peak comparison between
    them. Returns (result dict, {label: (freqs, db)})."""
    curves, result = {}, {"channel": ch}
    win = {}
    for direction in ("in", "out"):
        key = f"{ch}_{direction}"
        if key not in traces:
            continue
        samples = traces[key][-size:] if size else traces[key]
        win[direction] = samples
        curves[direction] = spectrum.spectrum(samples, rate, full_scale)
    if "in" not in curves and "out" not in curves:
        return None, curves

    result["n"] = len(next(iter(win.values())))
    result["resolution"] = rate / result["n"]

    # Locate on the input when present, read both there -- a working filter
    # moves the output peak, so independently-located peaks would compare
    # two different frequencies.
    ref = "in" if "in" in curves else "out"
    f, db = curves[ref]
    i = spectrum.peak(f, db, peak_fmin)
    if i is None:
        return result, curves
    result["peak_hz"] = float(f[i])
    for direction, (_f, _db) in curves.items():
        if i < _db.size:
            result[f"{direction}_dbfs"] = float(_db[i])
    if "in_dbfs" in result and "out_dbfs" in result:
        result["delta_db"] = result["out_dbfs"] - result["in_dbfs"]
    return result, curves


def model_choices():
    """Every pipeline, and every pipeline:implementation pair, built from
    pipelines.py so a new pipeline is scorable immediately. Import deferred
    -- the filters pull in numba, not worth it without --model."""
    import pipelines
    out = []
    for pipe in sorted(pipelines.PIPELINES):
        impls = pipelines.implementations(pipe)
        if impls:
            out.extend(f"{pipe}:{impl}" for impl in impls)
        else:
            # bypass/iir have nothing to quantise -- no fake :impl choice.
            out.append(pipe)
    return out


def expand_curves(spec):
    """A curve selection into algorithm names. Accepts a comma-separated
    list of pipe[:impl], "design" (everything with both implementations --
    the comparison you usually want) or "all".

    A bare pipeline name that has implementations expands to all of them,
    since seeing manual and scipy on one axis is the point of asking.
    """
    if not spec:
        return []
    import pipelines
    choices = model_choices()
    if spec == "all":
        return choices
    if spec == "design":
        return [c for c in choices if ":" in c]

    out, unknown = [], []
    for name in (n.strip() for n in spec.split(",")):
        if not name:
            continue
        if name in choices:
            out.append(name)
            continue
        impls = pipelines.implementations(name)
        if impls:
            out.extend(f"{name}:{impl}" for impl in impls)
        else:
            unknown.append(name)
    if unknown:
        raise ValueError(f"unknown pipeline(s): {', '.join(unknown)} "
                         f"(have: {', '.join(choices)})")
    # Deduped, order kept -- naming pipe2 and pipe2:scipy is not a request
    # to draw pipe2:scipy twice.
    return list(dict.fromkeys(out))


# The knobs a pipeline takes at runtime: (sidecar key, params key, label).
# One list, read by model_params to reproduce a capture's configuration and
# by the response controls to offer them for editing -- so a pipeline whose
# knobs are added here becomes both reproducible and tunable at once.
TUNABLES = (
    ("PIPE2_HP_HZ", "pipe2_hp_hz", "pipe2 HP (Hz)"),
    ("PIPE2_NOTCH_HZ", "pipe2_notch_hz", "pipe2 notch (Hz)"),
    ("PIPE2_NOTCH_Q", "pipe2_notch_q", "pipe2 notch Q"),
    ("PIPE2_LP_HZ", "pipe2_lp_hz", "pipe2 LP (Hz)"),
)


def model_params(shift, fs, meta=None, overrides=None):
    """What a pipeline is handed for an offline run. Corner frequencies come
    from the capture's own sidecar when present (like fs) -- scoring against
    this machine's live config would compare against a filter the recording
    never saw. Missing keys (older dumps) fall back to pipeline defaults.

    `overrides` replaces them afterwards, and is for the response view only.
    That view asks what a pipeline DOES, which is a property of the pipeline
    and not of the recording, so sweeping a corner there is a fair question.
    Applying the same override to compare_model would not be: it would score
    the recording against a filter that never ran.
    """
    params = {"shift": shift, "fs": float(fs)}
    for sidecar_key, param_key, _label in TUNABLES:
        value = (meta or {}).get(sidecar_key)
        if value is None:
            continue
        try:
            params[param_key] = float(value)
        except (TypeError, ValueError):
            pass
    for key, value in (overrides or {}).items():
        if value is None:
            continue
        try:
            params[key] = float(value)
        except (TypeError, ValueError):
            pass
    return params


def resolve_tunables(meta):
    """Every tunable's effective value as {params key: float} -- what the
    capture recorded, falling back to the pipeline module's own constant.

    The fallback is read off pipelines by name rather than copied here, so
    there is one definition of a default: pipelines.PIPE2_HP_HZ is both what
    the filter uses when nothing says otherwise and what these fields show.
    A pipeline missing a constant simply has no default to show.
    """
    import pipelines
    out = {}
    for sidecar_key, param_key, _label in TUNABLES:
        value = (meta or {}).get(sidecar_key, getattr(pipelines, sidecar_key,
                                                      None))
        if value is None:
            continue
        try:
            out[param_key] = float(value)
        except (TypeError, ValueError):
            pass
    return out


def compare_model(traces, ch, algorithm, shift, settle, fs, meta=None):
    """Run a pipelines.py algorithm on the recorded input, score it against
    the recorded output -- did the hardware compute what I think it did.
    `settle` samples are skipped: the model starts from zeroed state, the
    board didn't, so early samples disagree for a reason unrelated to the
    algorithm."""
    import pipelines
    src, ref = f"{ch}_in", f"{ch}_out"
    if src not in traces or ref not in traces:
        return None

    dtype = ">u4"
    x = traces[src].astype(np.uint32).astype(dtype)
    # "pipe" or "pipe:impl" -- same resolution the live app uses, so a
    # capture scored here matches the same live selection.
    pipe, _, impl = algorithm.partition(":")
    fn = pipelines.resolve(pipe, impl or pipelines.DEFAULT_IMPL)
    if fn is None:
        return None
    # fs from the capture's own sidecar, not this machine's live config --
    # a dump analysed months later filters at the rate it was recorded.
    modelled = fn(x, pipelines.new_state(), model_params(shift, fs, meta))
    modelled = modelled.astype(np.float64)

    a = modelled[settle:]
    b = traces[ref][settle:]
    n = min(len(a), len(b))
    if n < 2:
        return None
    a, b = a[:n], b[:n]

    err = a - b
    span = max(float(np.ptp(b)), 1.0)
    corr = float(np.corrcoef(a, b)[0, 1]) if np.std(a) and np.std(b) else float("nan")
    return {
        "channel": ch, "algorithm": algorithm, "shift": shift, "compared": n,
        "max_abs": float(np.max(np.abs(err))),
        "mean_abs": float(np.mean(np.abs(err))),
        "mean_err": float(np.mean(err)),      # signed: the truncation bias
        "pct_of_span": float(np.max(np.abs(err))) / span * 100.0,
        "corr": corr,
        "modelled": modelled,
    }


# ---------------------------------------------------------------------------
# Frequency response
# ---------------------------------------------------------------------------
# MEASURED, not derived. An analytic transfer function exists only for the
# scipy implementations: the manual ones truncate, `iir` wraps, and neither
# is linear, so neither has one to read off. Driving the real pipeline
# function and dividing output spectrum by input spectrum is the only way to
# put all of them on the same axes -- and the gap between them is the whole
# reason both implementations exist.
#
# Excitation is a random-phase multisine: equal-amplitude tones placed on
# exact FFT bins, so there is no leakage and no window is needed (a window
# would cost the phase reading its meaning). Tones are log-spaced to match
# a Bode axis. Several periods are run per realisation and only the last is
# transformed, so what is measured is the steady state rather than the
# filter settling. Several realisations with fresh random phases are
# averaged, which is the standard estimator for a system that is not quite
# linear -- the best linear approximation.
#
# Bins carrying no tone are measured too: whatever lands there came from
# truncation and distortion, not from the response, and plotting it as a
# floor is how you see what fixed point costs.

WIRE_DTYPE = ">u4"          # the same words compare_model feeds a pipeline
# The lowest frequency a measurement can reach is one bin, rate/size, so the
# only way down the frequency axis is a longer period. At 2 kHz the top of
# this range reaches 0.001 Hz, and costs ~1.6 s and ~200 MB for one curve --
# which is why it is the top of the range.
RESPONSE_SIZES = (4096, 8192, 16384, 32768, 65536, 131072, 262144,
                  524288, 1048576, 2097152)
RESPONSE_SIZE_MAX = RESPONSE_SIZES[-1]
DEFAULT_RESPONSE_SIZE = 16384
DEFAULT_RESPONSE_POINTS = 96
DEFAULT_RESPONSE_DRIVE = 0.25
DEFAULT_RESPONSE_AVERAGES = 4

# Periods run and thrown away before the one that gets transformed. Two,
# not one: a 0.2 Hz high-pass at 2048 Hz has poles close enough to z=1 that
# it is still settling most of the way through the first period, and the
# leftover transient spreads across every bin -- it shows up as a floor
# around -125 dB, which is right where the integer pipelines' real
# truncation floor sits and would be read as one. A second period puts it
# below -190 dB. Not a control: there is no reason to want less.
RESPONSE_SETTLE_PERIODS = 2


def size_for_fmin(fs, fmin):
    """The smallest power-of-two period that resolves `fmin`.

    The lowest frequency any of this can reach is one bin, rate/size. No
    axis setting conjures a tone below that, so asking for a lower F min is
    really asking for a longer period -- this says how long.
    """
    if fmin <= 0:
        return 0
    return int(2 ** np.ceil(np.log2(max(fs / float(fmin), 2.0))))


def param_freqs(params):
    """The frequencies a pipeline was actually configured with: any params
    key ending in _hz (see model_params). Generic on purpose -- a pipeline
    added later gets the same treatment for naming its corners the same way.
    """
    out = []
    for key, value in (params or {}).items():
        if not key.endswith("_hz"):
            continue
        try:
            f = float(value)
        except (TypeError, ValueError):
            continue
        if f > 0:
            out.append(f)
    return out


# Tones placed either side of each configured corner, as a FRACTION of the
# corner frequency rather than a number of bins. A feature's width scales
# with where it sits -- a notch is f0/Q wide -- while a bin does not, so a
# fixed bin offset that traces the shape at one Size shrinks into the centre
# at a larger one and leaves the skirts to the log grid, which is coarse
# there. These span +-12%, which brackets a Q=30 notch's +-1.7% -3 dB points
# at any Size.
_CORNER_OFFSETS = (0.0, 0.002, 0.005, 0.01, 0.015, 0.02, 0.03, 0.05, 0.08, 0.12)


def response_bins(size, fs, points, fmin=0.0, fmax=0.0, corners=()):
    """Distinct FFT bin indices to place tones on: a log grid, plus a
    cluster around each frequency in `corners`.

    The cluster is not a refinement, it is the difference between measuring
    the filter and missing it. A Q=30 notch at 50 Hz is ~1.7 Hz wide, and a
    log grid of a hundred tones across ten octaves puts roughly one tone in
    it -- so the deepest feature of the response would be the one least
    likely to be sampled.

    Bin 0 (DC) and the Nyquist bin are both excluded: neither carries a
    phase that means anything. Rounding a log grid onto integer bins
    collapses duplicates at the low end, so the returned count differs from
    `points` -- that is the resolution running out, not an error.
    """
    lo_hz = max(fmin, fs / size)            # one bin is the finest there is
    hi_hz = min(fmax if fmax > 0 else fs / 2.0, fs / 2.0)
    k_lo = max(1, int(round(lo_hz * size / fs)))
    k_hi = min(size // 2 - 1, int(round(hi_hz * size / fs)))
    if k_hi <= k_lo:
        return np.zeros(0, dtype=int)

    grid = np.round(np.geomspace(k_lo, k_hi, max(2, int(points)))).astype(int)
    offsets = np.array(_CORNER_OFFSETS)
    for f in corners:
        near = np.concatenate([f * (1.0 - offsets), f * (1.0 + offsets)])
        grid = np.append(grid, np.round(near * size / fs).astype(int))
    return np.unique(grid[(grid >= k_lo) & (grid <= k_hi)])


def _excitation(size, bins, rng):
    """One realisation, scaled to unit peak: energy only on `bins`, random
    phase on each. Built in the frequency domain so the tones land exactly
    on bin centres by construction rather than by arithmetic luck."""
    spec = np.zeros(size // 2 + 1, dtype=np.complex128)
    spec[bins] = np.exp(2j * np.pi * rng.random(bins.size))
    x = np.fft.irfft(spec, n=size)
    peak = float(np.max(np.abs(x)))
    return x / peak if peak else x


def _group_onto(freqs, grid):
    """Which point of `grid` each frequency belongs to, split at the
    geometric midpoints -- the log-axis equivalent of nearest-neighbour.
    Collapsing thousands of raw bins onto the curve's own grid is what turns
    a smear into a readable line, and it puts everything drawn on the
    response axes on one set of x positions."""
    edges = np.sqrt(grid[:-1] * grid[1:])
    return np.digitize(freqs, edges)


def _crossings(freqs, db, level):
    """Frequencies where a curve crosses `level`, interpolated in log f
    (the axis it is plotted on, so the reading matches the picture)."""
    out = []
    for i in range(db.size - 1):
        a, b = db[i], db[i + 1]
        if (a - level) * (b - level) < 0:
            t = (level - a) / (b - a)
            lo, hi = np.log(freqs[i]), np.log(freqs[i + 1])
            out.append(float(np.exp(lo + t * (hi - lo))))
    return out


def measure_response(algorithm, params, fs, size=DEFAULT_RESPONSE_SIZE,
                     points=DEFAULT_RESPONSE_POINTS,
                     drive=DEFAULT_RESPONSE_DRIVE,
                     averages=DEFAULT_RESPONSE_AVERAGES,
                     fmin=0.0, fmax=0.0, seed=20250903):
    """Amplitude and phase of one pipeline, measured by running it.

    `algorithm` is "pipe" or "pipe:impl", the same spelling compare_model
    and the live app use. Returns a dict of curves and landmarks, or None
    when the pipeline is unknown or the settings leave no bins to excite.

    `drive` is peak excitation as a fraction of full scale, and it matters:
    a fixed-point pipeline's response is level-dependent, so a curve is
    only meaningful next to the level it was measured at.
    """
    import pipelines

    pipe, _, impl = algorithm.partition(":")
    fn = pipelines.resolve(pipe, impl or pipelines.DEFAULT_IMPL)
    if fn is None:
        return None
    size = int(size)
    bins = response_bins(size, fs, points, fmin, fmax, param_freqs(params))
    if bins.size < 2:
        return None

    averages = max(1, int(averages))
    amplitude = max(1.0, float(drive) * pipelines.WIRE_CENTRE)
    rng = np.random.default_rng(seed)
    # Everything between the tones. Nothing was put there, so anything
    # found there is the pipeline's own noise and distortion.
    quiet = np.setdiff1d(np.arange(1, size // 2), bins)

    h_sum = np.zeros(bins.size, dtype=np.complex128)
    floor_sq = np.zeros(quiet.size)
    drive_sum = 0.0
    state = None

    for _ in range(averages):
        period = _excitation(size, bins, rng) * amplitude
        words = pipelines.to_wire(np.rint(period), WIRE_DTYPE)
        # The same period fed repeatedly through one state, rather than one
        # tiled array: a pipeline carries its state across calls (that is
        # how local_proc feeds it chunk by chunk), so this is identical
        # arithmetic at a third of the peak memory -- which is what makes
        # the million-sample periods needed for sub-0.01 Hz affordable.
        state = pipelines.new_state()
        for _ in range(RESPONSE_SETTLE_PERIODS):
            fn(words, state, dict(params))
        out = np.asarray(fn(words, state, dict(params)))
        y = pipelines.from_wire(out).astype(np.float64)
        # The excitation as the pipeline actually saw it -- rounded, and
        # clipped if the drive is too high. Referencing the ideal float
        # signal instead would charge that rounding to the filter.
        x = pipelines.from_wire(words).astype(np.float64)

        spec_in = np.fft.rfft(x)
        spec_out = np.fft.rfft(y)
        h_sum += spec_out[bins] / spec_in[bins]
        floor_sq += np.abs(spec_out[quiet]) ** 2
        drive_sum += float(np.mean(np.abs(spec_in[bins])))

    h = h_sum / averages
    freqs = bins * (fs / size)
    mag_db = 20.0 * np.log10(np.maximum(np.abs(h), 1e-30))

    # Floor referred to the drive level, so it shares the magnitude axis:
    # "output this far below an input tone of the same size".
    per_tone = drive_sum / averages
    floor_freqs, floor_db = np.zeros(0), np.zeros(0)
    if quiet.size and per_tone > 0:
        floor_rms = np.sqrt(floor_sq / averages)
        # Collapsed onto the tone grid -- thousands of raw bins read as a
        # smear, one number per group reads as a floor.
        group = _group_onto(quiet * (fs / size), freqs)
        f_out, db_out = [], []
        for g in range(freqs.size):
            sel = group == g
            if not sel.any():
                continue
            f_out.append(freqs[g])
            rms = float(np.sqrt(np.mean(floor_rms[sel] ** 2)))
            db_out.append(20.0 * np.log10(max(rms, 1e-30) / per_tone))
        floor_freqs, floor_db = np.array(f_out), np.array(db_out)

    ref_db = float(np.max(mag_db))
    deepest = int(np.argmin(mag_db))

    # Phase only exists where there is an output to carry one. At a true
    # null -- scipy's notch reaches -213 dB, below its own noise floor --
    # the bin holds numerical noise, and its phase is a random number.
    # Letting np.unwrap chain through that drags every later point by up to
    # a whole turn, which is how two curves that agree everywhere end up
    # drawn 360 degrees apart. Excluded instead, leaving a gap in the phase
    # trace exactly where the magnitude trace shows the reason for it.
    floor_median = float(np.median(floor_db)) if floor_db.size else float("nan")
    cutoff = (floor_median + 6.0 if np.isfinite(floor_median)
              else ref_db - 200.0)
    defined = mag_db > max(cutoff, ref_db - 200.0)
    phase_deg = np.full(mag_db.shape, np.nan)
    if defined.any():
        phase_deg[defined] = np.degrees(np.unwrap(np.angle(h[defined])))

    return {
        "algorithm": algorithm,
        "pipe": pipe,
        "impl": impl or ("" if not pipelines.has_implementations(pipe)
                         else pipelines.DEFAULT_IMPL),
        "freqs": freqs, "mag_db": mag_db, "phase_deg": phase_deg,
        # The complex response itself, so the phase axis can be redrawn as
        # a delay without re-measuring (see phase_display).
        "h": h,
        "floor_freqs": floor_freqs, "floor_db": floor_db,
        "floor_median_db": floor_median,
        "ref_db": ref_db,
        "corners": _crossings(freqs, mag_db, ref_db - 3.0),
        "notch_hz": float(freqs[deepest]), "notch_db": float(mag_db[deepest]),
        # Present only where the pipeline designed one (the scipy
        # implementations). Lets the GUI draw the exact design curve over
        # the measured one -- if those two disagree, the measurement is
        # what's wrong, not the filter.
        "sos": np.asarray(state["sos"]) if state and "sos" in state else None,
        "tones": int(bins.size), "size": size, "drive": float(drive),
        "averages": averages, "fs": float(fs),
        # What the band was asked to be, so the report can say when the
        # request ran past what a period this long (low end) or a rate this
        # slow (high end) can actually deliver.
        "fmin_asked": float(fmin), "fmax_asked": float(fmax),
        # What it was actually asked for, so an overridden corner is visible
        # in the report rather than only inferable from the curve.
        "corners_asked": {k: float(v) for _s, k, _l in TUNABLES
                          if (v := params.get(k)) is not None},
    }


# ---------------------------------------------------------------------------
# The response the capture itself shows
# ---------------------------------------------------------------------------
# measure_response asks "what does this pipeline do". This asks "what did
# the thing that produced this recording actually do" -- output spectrum
# over input spectrum, straight from the two traces that are already in the
# dump. Same units as measure_response's curve (dB of gain, 0 = unity), so
# the two go on one axis and the distance between them is the model being
# right or wrong across the whole band rather than at one tone.
#
# It is a weaker measurement and cannot be otherwise. The excitation is an
# ECG: it puts energy where it likes, which is a couple of decades of band
# and very little above that, and five seconds of it is one short record
# rather than an averaged steady state. So the answer is only trustworthy
# where the input had something to say, and the gate below is what keeps
# the rest off the plot.

# How far below the strongest part of the input a band may sit before its
# in/out ratio is dropped as unmeasurable. Past this the ratio is noise
# over noise -- a confident line through the part of the capture that says
# the least, which is worse than a gap.
CAPTURE_GATE_DB = -60.0


def capture_gain(traces, ch, rate, grid, gate_db=CAPTURE_GATE_DB):
    """One channel's recorded in -> out as amplitude and phase on `grid`.

    Returns None when the channel has no in/out pair or nothing survives
    the gate. Magnitudes are gain in dB, directly comparable with
    measure_response; the wire's full scale cancels in the ratio, so unlike
    the dBFS spectrum this needs no reference level.
    """
    src, dst = f"{ch}_in", f"{ch}_out"
    if src not in traces or dst not in traces or grid is None or len(grid) < 2:
        return None
    x = np.asarray(traces[src], dtype=np.float64)
    y = np.asarray(traces[dst], dtype=np.float64)
    n = min(x.size, y.size)
    if n < 64:
        return None
    x, y = x[:n], y[:n]

    # DC out first: the wire centres on half full scale, and that offset is
    # a format artefact that would otherwise dominate the lowest bins.
    # Windowed identically, so the window cancels in the ratio and the
    # phase reading survives it.
    win = np.hanning(n)
    spec_in = np.fft.rfft((x - x.mean()) * win)
    spec_out = np.fft.rfft((y - y.mean()) * win)
    freqs = np.fft.rfftfreq(n, d=1.0 / rate)

    grid = np.asarray(grid, dtype=np.float64)
    group = _group_onto(freqs, grid)
    usable = freqs > 0

    # Per band: the least-squares (H1) estimate, sum(conj(X) Y) / sum(|X|^2).
    # Weighting by input power falls out of it, so a band's strong bins set
    # its answer and its weak ones cannot drag it around.
    out_f, out_mag, out_phase, energy = [], [], [], []
    for g in range(grid.size):
        sel = (group == g) & usable
        if not sel.any():
            continue
        sxx = float(np.sum(np.abs(spec_in[sel]) ** 2))
        if sxx <= 0:
            continue
        h = complex(np.sum(np.conj(spec_in[sel]) * spec_out[sel]) / sxx)
        out_f.append(float(grid[g]))
        out_mag.append(h)
        energy.append(sxx)
    if not out_f:
        return None

    energy = np.array(energy)
    keep = energy >= energy.max() * (10.0 ** (gate_db / 10.0))
    if not keep.any():
        return None
    h = np.array(out_mag)[keep]
    return {
        "channel": ch,
        "freqs": np.array(out_f)[keep],
        "mag_db": 20.0 * np.log10(np.maximum(np.abs(h), 1e-30)),
        "phase_deg": np.degrees(np.unwrap(np.angle(h))),
        # So the overlay follows the phase axis into delay units with the
        # curves it is drawn against, rather than staying in degrees.
        "h": h,
        "points": int(keep.sum()), "of": int(keep.size),
        "top_hz": float(np.array(out_f)[keep].max()),
        "samples": n, "resolution": rate / n, "gate_db": float(gate_db),
    }


# What the capture view's phase panel can show. "off" is a real setting --
# the panel costs a third of the window's width, and the magnitude spectrum
# alone is what you want most of the time.
PHASE_MODES = ("off", "out-in", "raw")
DEFAULT_PHASE_MODE = "out-in"

# How a phase curve is displayed. Degrees are the raw quantity; the two
# delays answer "so how far is the output actually shifted", which degrees
# do not -- 45 degrees is 125 ms at 1 Hz and 1.25 ms at 100 Hz.
PHASE_UNITS = ("deg", "phase ms", "group ms")
DEFAULT_PHASE_UNITS = "deg"

# How many bins apart the two points of a group-delay slope are taken.
#
# 1 is wrong wherever the spectrum was windowed. A Hann window's transform
# spans three bins, so neighbouring bins of a windowed spectrum are
# correlated, and a slope measured between two of them is partly a slope
# measured against itself -- it comes out flattened. Checked against
# scipy.signal.group_delay on a known 2nd-order Butterworth: at lag 1 the
# estimate read 4.09 ms where the truth was 5.72, and 5.76 against 6.60,
# a consistent ~25% low. At lag 2 and beyond it lands on the truth, so the
# main lobe is the whole story. 4 leaves margin and buys a longer, quieter
# baseline; the cost is that features narrower than four bins are smoothed.
#
# The response view passes 1 deliberately: its multisine sits on exact bins
# and is never windowed, so nothing correlates its neighbours and the
# maths is exact there (verified against an analytic exp(-2i*pi*f*tau)).
PHASE_GROUP_LAG = 4


def phase_display(freqs, deg, h, units, lag=1):
    """One phase curve as the chosen quantity: (x, y, axis label).

    "deg"       the angle itself, as handed in -- each view has its own
                wrap/unwrap policy and this preserves it.
    "phase ms"  -phi / (2 pi f): how late the SINE at this frequency comes
                out. Reads directly as "the 10 Hz component is 3 ms late".
                Needs absolute phase, so it is only as good as the caller's
                unwrapping -- fine in a passband, meaningless past a wrap.
    "group ms"  -d(phi)/d(omega): the delay of a narrow BAND around f, which
                is the delay of the waveform's shape rather than of the
                carrier. The one that matters for an ECG -- constant group
                delay moves the QRS, varying group delay changes it.

    Group delay is taken from consecutive complex ratios rather than by
    differentiating the degrees: angle(h[i+1] * conj(h[i])) is already
    wrapped into (-pi, pi], so it is right regardless of how the caller
    unwrapped, and it never needs an unwrap of its own. It lands on the
    midpoints between bins, which is why x comes back too.
    """
    freqs = np.asarray(freqs, dtype=np.float64)
    if units == "phase ms":
        with np.errstate(divide="ignore", invalid="ignore"):
            ms = -np.asarray(deg, dtype=np.float64) / (360.0 * freqs) * 1e3
        return freqs, ms, "Phase delay (ms)"
    lag = max(1, int(lag))
    if units != "group ms" or h is None or len(freqs) <= lag:
        return freqs, np.asarray(deg, dtype=np.float64), "Phase (deg)"

    h = np.asarray(h)
    step = np.angle(h[lag:] * np.conj(h[:-lag]))
    df = freqs[lag:] - freqs[:-lag]
    with np.errstate(divide="ignore", invalid="ignore"):
        ms = -step / (2.0 * np.pi * df) * 1e3
    # Geometric midpoint: the response view's tones are log-spaced, and on
    # that axis the arithmetic midpoint sits visibly off centre.
    mid = np.sqrt(freqs[:-lag] * freqs[lag:])
    # A step approaching half a turn is where this stops being measurable:
    # angle() folds anything past +-pi back into range, so the delay would
    # come out small and confident instead of large and unknown. Happens
    # across a gap the gate left, and genuinely at a notch, where the phase
    # really does flip. Tested on the step itself rather than on the bin
    # spacing -- a spacing test needs to know whether the grid is linear or
    # logarithmic, and this one does not.
    d = np.asarray(deg, dtype=np.float64)
    bad = (np.abs(step) > 0.9 * np.pi) | ~np.isfinite(d[:-lag]) | ~np.isfinite(d[lag:])
    ms[bad] = np.nan
    return mid, ms, "Group delay (ms)"


def phase_spectrum(traces, ch, rate, mode=DEFAULT_PHASE_MODE, size=0,
                   gate_db=CAPTURE_GATE_DB):
    """Phase against frequency for one channel of a capture, in degrees.

    The rfft that produces the magnitude spectrum produces this at no extra
    cost, but the two are not equally useful and the mode picks which:

    "out-in"  output phase minus input phase, per bin -- the phase the
              recorded processing actually applied. Both traces share the
              record's start, so that arbitrary origin cancels and what is
              left is the filter. This is the one that means something on
              its own, and it is the default for that reason.
    "raw"     the plain angle of each trace's own FFT, which is what "the
              phase from an FFT" usually names. It is dominated by where
              the record happens to start: a shift of t0 rotates every bin
              by -2*pi*f*t0, a ramp that wraps many times across the band.
              Measured on this repo's own dumps it spans the full +-180
              with a sign change between most adjacent bins, so expect a
              sawtooth. The information is in differences between curves,
              never in the values.

    Wrapped to +-180 rather than unwrapped: the gate below leaves gaps, and
    unwrapping across a gap invents a turn count nothing measured.

    Bins whose input sits more than `gate_db` below the strongest are left
    out -- the phase of noise is noise.
    """
    src, dst = f"{ch}_in", f"{ch}_out"
    if src not in traces:
        return None
    x = np.asarray(traces[src], dtype=np.float64)
    y = np.asarray(traces[dst], dtype=np.float64) if dst in traces else None
    if size:
        x = x[-size:]
        y = y[-size:] if y is not None else None
    n = len(x)
    if n < 8 or (mode == "out-in" and y is None):
        return None

    win = np.hanning(n)
    spec_in = np.fft.rfft((x - x.mean()) * win)
    spec_out = (np.fft.rfft((y - y.mean()) * win)
                if y is not None and len(y) == n else None)
    freqs = np.fft.rfftfreq(n, d=1.0 / rate)

    # Gated on BOTH traces, not just the input. A tone the processing
    # nulled -- a 50 Hz notch is exactly this -- passes an input-only gate
    # with plenty to spare while its output is down at the noise floor, and
    # the phase difference it yields is the phase of that noise. Those bins
    # came out as a scatter of outliers sitting right on the notch, which is
    # the one place on the plot you would most want to trust.
    ratio = 10.0 ** (gate_db / 20.0)
    mag = np.abs(spec_in)
    # A silent trace has no peak to be 60 dB down from, and "x >= 0" would
    # otherwise pass every bin and report a confident 0 degrees across the
    # band. Nothing to measure is None, not zero.
    if mag.max() <= 0:
        return None
    keep = (freqs > 0) & (mag >= mag.max() * ratio)
    if spec_out is not None:
        mag_out = np.abs(spec_out)
        if mag_out.max() <= 0:
            return None
        keep &= mag_out >= mag_out.max() * ratio
    if not keep.any():
        return None

    # Complex kept alongside the degrees: group delay is taken from ratios
    # of these (see phase_display), which is immune to how the degrees were
    # wrapped.
    if mode == "out-in":
        curves = [("out − in", spec_out[keep] * np.conj(spec_in[keep]))]
    else:
        curves = [("in", spec_in[keep])]
        if spec_out is not None:
            curves.append(("out", spec_out[keep]))
    curves = [(label, np.degrees(np.angle(h)), h) for label, h in curves]

    out = {"channel": ch, "mode": mode, "freqs": freqs[keep],
           "curves": curves, "points": int(keep.sum()),
           "of": int(freqs.size - 1), "gate_db": float(gate_db),
           # These bins came from a windowed transform, so a group-delay
           # slope has to skip past the window's own width -- see
           # PHASE_GROUP_LAG. Carried here so the GUI cannot forget.
           "group_lag": PHASE_GROUP_LAG}
    # The headline number for "is the output shifted, and by how much":
    # the typical group delay across the band that survived the gate.
    # Median, not mean -- a single wild interval at a notch would otherwise
    # set it.
    _x, ms, _label = phase_display(out["freqs"], curves[0][1], curves[0][2],
                                   "group ms", PHASE_GROUP_LAG)
    finite = ms[np.isfinite(ms)]
    out["group_ms"] = float(np.median(finite)) if finite.size else float("nan")
    out["group_spread_ms"] = (float(np.percentile(finite, 90)
                                    - np.percentile(finite, 10))
                              if finite.size else float("nan"))
    return out


def capture_spectrum(traces, ch, rate, full_scale, direction="in"):
    """One trace as dBFS against frequency -- the plain spectrum, for
    showing WHERE the signal is next to what the filter does to it. A level,
    not a gain: it belongs on its own axis, never on the response's."""
    key = f"{ch}_{direction}"
    if key not in traces:
        return None
    freqs, db = spectrum.spectrum(np.asarray(traces[key]), rate, full_scale)
    if freqs.size == 0:
        return None
    return {"channel": ch, "direction": direction, "freqs": freqs, "db": db}


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def format_report(info, results, models, responses=(), gains=(), phases=()):
    """The report as text -- returns rather than prints so the GUI can
    reuse it verbatim in its panel. Sections with nothing in them are
    left out entirely: the response view computes no model comparison and
    vice versa, and an empty heading reads like a failure."""
    out = []
    say = out.append

    p = info["path"]
    say(f"{p.name}   {info['samples']} samples @ {info['rate']:g} Hz")
    bits = []
    for label, key, fmt in (("heart rate", "heart_rate", "{:.0f} bpm"),
                            ("amplitude", "amplitude", "{:.2f}"),
                            ("send rate", "send_rate", "{:.0f} pkt/s"),
                            ("chunk", "chunk", "{:.0f}")):
        if info.get(key) is not None:
            bits.append(f"{label} {fmt.format(info[key])}")
    if info.get("shift") is not None:
        bits.append(f"board shift {info['shift']:.0f}")
    if info.get("trigger") is not None:
        bits.append(f"trigger {info['trigger']}")
    if bits:
        say("  " + ", ".join(bits))
    for n, ch, en, freq, phase, level in info["sines"]:
        if en and en.lower() == "true":
            say(f"  sine {n} ch{ch}: {freq} Hz at level {level}, "
                f"phase {phase} deg")

    if any(results):
        say("")
        say("  spectrum")
    for r in results:
        if not r or "peak_hz" not in r:
            say(f"    {r['channel'] if r else '?':8} no peak found")
            continue
        line = (f"    {r['channel']:8} N={r['n']:<6} {r['resolution']:.2f} Hz/bin"
                f"   peak {r['peak_hz']:8.2f} Hz")
        if "in_dbfs" in r:
            line += f"   in {r['in_dbfs']:7.2f}"
        if "out_dbfs" in r:
            line += f"   out {r['out_dbfs']:7.2f}"
        if "delta_db" in r:
            line += f"   delta {r['delta_db']:7.2f} dB"
        say(line)

    if any(models):
        say("")
        say("  model vs recorded output")
        for m in models:
            if not m:
                continue
            say(f"    {m['channel']:8} {m['algorithm']} shift={m['shift']}"
                f"   n={m['compared']}")
            say(f"             max |err| {m['max_abs']:.1f} counts "
                f"({m['pct_of_span']:.4f}% of span),"
                f" mean |err| {m['mean_abs']:.1f}")
            say(f"             mean signed err {m['mean_err']:+.1f} counts"
                f"   correlation {m['corr']:.6f}")

    if any(responses):
        say("")
        say("  frequency response (measured by driving the pipeline)")
        # Both ends have a hard limit, and they are different limits with
        # different answers -- the low end is the period length and can be
        # bought, the high end is the sample rate and cannot.
        first = next(r for r in responses if r)
        lo_hz, hi_hz = float(first["freqs"][0]), float(first["freqs"][-1])
        if 0 < first["fmin_asked"] < lo_hz * 0.999:
            say(f"    NOTE  F min {first['fmin_asked']:g} Hz would need a "
                f"{size_for_fmin(first['fs'], first['fmin_asked'])}-sample "
                f"period; {RESPONSE_SIZE_MAX} is the")
            say(f"          longest offered, so the plot starts at "
                f"{lo_hz:.4g} Hz. A lower Rate would also reach it: one bin "
                f"is rate/Size.")
        if first["fmax_asked"] > hi_hz * 1.001:
            say(f"    NOTE  F max {first['fmax_asked']:g} Hz is above Nyquist "
                f"({first['fs'] / 2:g} Hz), so the plot stops at "
                f"{hi_hz:.4g} Hz.")
            say(f"          Nothing sampled at {first['fs']:g} Hz carries "
                f"more. Only a higher Rate moves this -- no Size does.")
        for r in responses:
            if not r:
                continue
            say(f"    {r['algorithm']:14} {r['tones']} tones, N={r['size']}, "
                f"drive {r['drive'] * 100:.0f}% FS, {r['averages']} averages, "
                f"{r['fs'] / r['size']:.3f} Hz/bin")
            band = (f"-3 dB at {', '.join(f'{c:.4g}' for c in r['corners'])} Hz"
                    if r["corners"] else "flat -- never 3 dB down in range")
            say(f"             passband {r['ref_db']:+.2f} dB   {band}")
            line = (f"             deepest {r['notch_db']:+.2f} dB "
                    f"at {r['notch_hz']:.4g} Hz")
            if r["floor_db"].size:
                line += f"   noise+distortion floor {r['floor_median_db']:+.1f} dB"
            say(line)
            # Only for pipelines that read them -- iir and bypass take none,
            # and listing pipe2's corners under them would be a lie.
            asked = {k: v for k, v in r["corners_asked"].items()
                     if k.startswith(f"{r['pipe']}_")}
            if asked:
                say("             asked for " + ", ".join(
                    f"{k.split('_', 1)[1]} {v:g}" for k, v in asked.items()))

    if any(phases):
        say("")
        say("  phase spectrum")
        for p in phases:
            if not p:
                continue
            say(f"    {p['channel']:8} {p['mode']:8} {p['points']} of "
                f"{p['of']} bins above the {p['gate_db']:.0f} dB gate, to "
                f"{p['freqs'][-1]:.4g} Hz")
            if p["mode"] == "out-in" and np.isfinite(p["group_ms"]):
                # What "is the output shifted" actually comes to. The
                # spread is the part that matters clinically: a constant
                # group delay moves the QRS, a varying one reshapes it.
                say(f"             output lags input by {p['group_ms']:+.2f} ms "
                    f"(median group delay), spread "
                    f"{p['group_spread_ms']:.2f} ms over the band")
        if any(p and p["mode"] == "raw" for p in phases):
            say("      raw is the plain FFT angle, so it is dominated by "
                "where the record starts")
            say("      and reads as a sawtooth -- out-in cancels that origin "
                "and shows the filter")

    if any(gains):
        say("")
        say("  capture in -> out (the response this recording actually shows)")
        for g in gains:
            if not g:
                continue
            say(f"    {g['channel']:8} {g['points']} of {g['of']} bands above "
                f"{g['gate_db']:.0f} dB gate, usable to {g['top_hz']:.4g} Hz"
                f"   ({g['samples']} samples, {g['resolution']:.2f} Hz/bin)")
    return "\n".join(out)


def print_report(info, results, models, responses=(), gains=(), phases=()):
    print()
    print(format_report(info, results, models, responses, gains, phases))
    print()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", nargs="?", help="dump CSV (default: the newest)")
    ap.add_argument("--log-dir", default=str(DEFAULT_LOGS))
    ap.add_argument("--list", action="store_true", help="list dumps and exit")
    ap.add_argument("--fft-size", type=int, default=0,
                    help="samples to transform, from the newest end "
                         "(0 = the whole capture). Truncates, never zero-pads")
    ap.add_argument("--fmax", type=float, default=0.0,
                    help="frequency axis limit, Hz (0 = Nyquist)")
    ap.add_argument("--db-min", type=float, default=-120.0)
    ap.add_argument("--peak-fmin", type=float, default=1.0,
                    help="ignore bins below this when locating the peak")
    ap.add_argument("--phase", default=DEFAULT_PHASE_MODE, choices=PHASE_MODES,
                    help="phase-vs-frequency panel beside the capture view's "
                         "spectra. 'out-in' is the phase the recorded "
                         "processing applied, and is the one that means "
                         "something on its own; 'raw' is the plain FFT angle, "
                         "which is dominated by where the record starts and "
                         "reads as a sawtooth; 'off' hides the panel")
    ap.add_argument("--phase-units", default=DEFAULT_PHASE_UNITS,
                    choices=PHASE_UNITS,
                    help="what the phase axes plot, in both views. 'deg' is "
                         "the angle; 'phase ms' is how late the sine at each "
                         "frequency comes out; 'group ms' is how late a band "
                         "around it comes out, which is the delay of the "
                         "waveform's shape and the one that matters for an "
                         "ECG. The report always prints the group delay")
    ap.add_argument("--model", choices=model_choices(),
                    help="also run this pipeline on the recorded "
                         "input and score it against the recorded output. "
                         "bypass and iir take no implementation; design "
                         "pipelines are named pipe:impl (e.g. pipe1:scipy). "
                         "Applies to both channels unless overridden below")
    ap.add_argument("--model-ch1", choices=model_choices(),
                    help="score ch1 against this instead of --model. The two "
                         "channels can have been produced by different "
                         "pipelines, so they can be scored separately")
    ap.add_argument("--model-ch2", choices=model_choices(),
                    help="score ch2 against this instead of --model")
    ap.add_argument("--shift", type=int,
                    help="shift for --model (default: the board's, from the sidecar)")
    ap.add_argument("--settle", type=int, default=200,
                    help="samples to skip before scoring the model, since it "
                         "starts from zeroed state and the board did not")
    ap.add_argument("--response", metavar="LIST", nargs="?", const="design",
                    help="measure amplitude/phase of these pipelines and plot "
                         "them over each other. Comma-separated "
                         "pipe[:impl] names, 'design' for every pipeline that "
                         "has both implementations, or 'all' for the lot. "
                         "Opens the window on the response view; with "
                         "--no-plot, prints the summary instead")
    ap.add_argument("--response-size", type=int, default=DEFAULT_RESPONSE_SIZE,
                    help="excitation period in samples -- resolution is "
                         "rate/N, so the low end of the axis needs a big one")
    ap.add_argument("--response-points", type=int,
                    default=DEFAULT_RESPONSE_POINTS,
                    help="tones in the excitation, log-spaced (bins collapse "
                         "at the low end, so fewer usually land)")
    ap.add_argument("--response-drive", type=float,
                    default=DEFAULT_RESPONSE_DRIVE,
                    help="peak excitation as a fraction of full scale. A "
                         "fixed-point pipeline's response is level-dependent, "
                         "so this is part of the measurement, not a detail")
    ap.add_argument("--response-averages", type=int,
                    default=DEFAULT_RESPONSE_AVERAGES,
                    help="realisations averaged, each with fresh random phases")
    ap.add_argument("--response-fmin", type=float, default=0.0,
                    help="low end of the band the response is measured over, "
                         "Hz (0 = the lowest bin the size allows). Bounds the "
                         "excitation as well as the axis -- outside it, "
                         "nothing is asked and nothing is drawn")
    ap.add_argument("--response-fmax", type=float, default=0.0,
                    help="high end of the band the response is measured over, "
                         "Hz (0 = Nyquist). Narrowing the band spends the "
                         "same tones over less of it, so it is also how you "
                         "get resolution where you want it")
    ap.add_argument("--set", metavar="KEY=VALUE", action="append", default=[],
                    help="override a pipeline parameter for the response "
                         "measurement, e.g. --set pipe2_notch_hz=60. Repeat "
                         "for more. Response only: the model comparison keeps "
                         "the capture's own settings, since it is scored "
                         "against what actually ran. "
                         f"Known: {', '.join(k for _s, k, _l in TUNABLES)}")
    ap.add_argument("--overlay", default="none",
                    choices=("none", "gain", "spectrum", "both"),
                    help="draw the capture over the response. 'gain' is the "
                         "recording's own in/out ratio, in the same dB the "
                         "response is in, so the two compare directly. "
                         "'spectrum' is the input's dBFS level on a second "
                         "axis -- where the signal is, next to what the "
                         "filter does to it. A level, not a gain")
    ap.add_argument("--overlay-ch", default="both",
                    choices=("ch1", "ch2", "both"),
                    help="which channel(s) the overlay comes from")
    ap.add_argument("--no-plot", action="store_true",
                    help="print the report and exit instead of opening the "
                         "window -- for a terminal, a pipe, or a headless box")
    args = ap.parse_args(argv)

    dumps = find_dumps(args.log_dir)
    if args.list:
        if not dumps:
            print(f"no dumps in {args.log_dir}")
        for d in dumps:
            print(d)
        return 0
    if args.file:
        path = Path(args.file)
    elif dumps:
        path = dumps[-1]
    else:
        print(f"no dumps in {args.log_dir} -- press 'Log buffer' in the client "
              f"first, or pass a file", file=sys.stderr)
        return 1
    if not path.exists():
        print(f"{path}: not found", file=sys.stderr)
        return 1

    try:
        response_curves = expand_curves(args.response)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1

    overrides = {}
    for item in args.set:
        key, sep, value = item.partition("=")
        if not sep:
            print(f"--set wants KEY=VALUE, got {item!r}", file=sys.stderr)
            return 1
        try:
            overrides[key.strip()] = float(value)
        except ValueError:
            print(f"--set {key.strip()}: {value!r} is not a number",
                  file=sys.stderr)
            return 1

    if not args.no_plot:
        # The window is the normal path -- every flag below is also a
        # control in it. CLI stays for scripting/headless boxes.
        import sat_gui
        sat_gui.SATWindow(
            log_dir=Path(args.log_dir), path=path, fft_size=args.fft_size,
            fmax=args.fmax, db_min=args.db_min, peak_fmin=args.peak_fmin,
            phase=args.phase, phase_units=args.phase_units,
            model=args.model_ch1 or args.model,
            model_ch2=args.model_ch2 or args.model,
            shift=args.shift, settle=args.settle,
            view="response" if args.response else "capture",
            curves=response_curves,
            response_size=args.response_size,
            response_points=args.response_points,
            response_drive=args.response_drive,
            response_averages=args.response_averages,
            response_fmin=args.response_fmin,
            response_fmax=args.response_fmax, overrides=overrides,
            overlay=args.overlay, overlay_ch=args.overlay_ch).run()
        return 0

    try:
        traces, info = load_dump(path)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    full_scale = wire_full_scale(traces, info)

    results, curves_by_ch = [], {}
    for ch in ("ch1", "ch2"):
        if f"{ch}_in" not in traces and f"{ch}_out" not in traces:
            continue
        r, curves = analyse_channel(traces, ch, info["rate"], full_scale,
                                     args.fft_size, args.peak_fmin)
        results.append(r)
        curves_by_ch[ch] = curves

    # Per channel -- each channel may have run a different pipeline.
    selection = {"ch1": args.model_ch1 or args.model,
                 "ch2": args.model_ch2 or args.model}
    shift = args.shift
    if shift is None:
        shift = int(info["shift"]) if info.get("shift") is not None else 4

    models = []
    for ch in ("ch1", "ch2"):
        if selection[ch]:
            models.append(compare_model(traces, ch, selection[ch], shift,
                                        args.settle, info["rate"],
                                        info.get("meta")))

    # Independent of the capture's samples -- a pipeline's response is a
    # property of the pipeline. The dump still supplies the rate and the
    # corner frequencies it was recorded with, unless --set replaces them.
    params = model_params(shift, info["rate"], info.get("meta"), overrides)
    # Same as the window: a lower --response-fmin is really a request for a
    # longer period, so grow it rather than measure nothing down there.
    response_size = min(max(args.response_size,
                            size_for_fmin(info["rate"], args.response_fmin)),
                        RESPONSE_SIZE_MAX)
    responses = [measure_response(name, params, info["rate"],
                                  size=response_size,
                                  points=args.response_points,
                                  drive=args.response_drive,
                                  averages=args.response_averages,
                                  fmin=args.response_fmin,
                                  fmax=args.response_fmax)
                 for name in response_curves]

    # Onto the response's own x positions when there is one, so the two are
    # read off the same grid rather than two grids that nearly agree.
    gains = []
    if args.overlay in ("gain", "both"):
        grid = next((r["freqs"] for r in responses if r), None)
        if grid is None:
            grid = response_bins(
                response_size, info["rate"], args.response_points,
                args.response_fmin,
                args.response_fmax) * (info["rate"] / response_size)
        channels = ("ch1", "ch2") if args.overlay_ch == "both" else (args.overlay_ch,)
        gains = [capture_gain(traces, ch, info["rate"], grid) for ch in channels]

    phases = []
    if args.phase != "off":
        phases = [phase_spectrum(traces, ch, info["rate"], args.phase,
                                 args.fft_size)
                  for ch in ("ch1", "ch2")]

    print_report(info, results, models, responses, gains, phases)
    return 0


if __name__ == "__main__":
    sys.exit(main())
