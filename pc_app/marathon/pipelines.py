# Processing functions only -- no threads/sockets/plot/board (see local_proc.py).
#
# Each pipeline has a "scipy" version (float64, what it SHOULD do) and a
# "manual" version (integer, fixed-width, what it WILL do as RTL) -- the gap
# between them (truncation bias, dead zones, overflow) is the point. `iir` is
# different: a bit-accurate model of the board's own filter (axi_tdm_filter.vhd).
#
# ADD A PIPELINE: write pipeN_scipy/pipeN_manual(x, state, params) and add
# PIPELINES["pipeN"] = {"manual": ..., "scipy": ...}. `state` persists across
# chunks per channel; `params` is one dict shared by all pipelines -- prefix
# your keys (see _pipe2_freqs). Convert with _from_wire()/_to_wire_centred(),
# not _to_signed() (see its docstring). Raising is fine -- local_proc reports
# it and passes the chunk through.

import numpy as np


_MASK = 0xFFFFFFFF
_SIGN = 0x80000000


def _to_signed(chunk):
    """Wire words reinterpreted as signed 32-bit -- exactly what the fabric
    does with `signed(x_native)` in axi_tdm_filter.vhd."""
    return ((chunk.astype(np.int64) + _SIGN) & _MASK) - _SIGN


def _to_wire(values, dtype):
    """Signed 32-bit results back to unsigned wire words."""
    return (values & _MASK).astype(dtype)


def _iir_scalar(x, y_init, shift):
    """One channel of y[n] = y[n-1] + (x[n]-y[n-1])>>SHIFT, scalar because
    the truncating shift is non-linear (no vectorised form matches the
    bits). Wraps rather than saturates, matching VHDL's numeric_std --
    including the one-sample startup overflow (y=0, x~2**31), which the
    hardware also does and recovers from within a few samples.
    """
    out = np.empty(x.shape[0], dtype=np.int64)
    y = y_init
    for i in range(x.shape[0]):
        d = ((x[i] - y + _SIGN) & _MASK) - _SIGN
        y = ((y + (d >> shift) + _SIGN) & _MASK) - _SIGN
        out[i] = y
    return out, y


_kernels = {}


def _compiled(fn, *probe):
    """njit(fn) if numba accepts it, else the plain Python fallback.

    Compiled once, on a throwaway input, so a failure lands here rather
    than mid-stream. nogil so the plot thread isn't blocked while a chunk
    filters. Missing numba degrades gracefully -- a slow local mode still
    beats no local mode.
    """
    if fn in _kernels:
        return _kernels[fn]
    impl = fn
    try:
        from numba import njit
        candidate = njit(cache=True, nogil=True)(fn)
        candidate(*probe)
        impl = candidate
        print(f"[local] numba kernel compiled: {fn.__name__}")
    except Exception as exc:                          # noqa: BLE001
        print(f"[local] numba unavailable for {fn.__name__} ({exc}); using "
              f"the Python loop -- fine at low sample rates, slow above "
              f"~100k samples/s")
    _kernels[fn] = impl
    return impl


def _get_kernel():
    """The single-pole shift kernel: y += (x - y) >> shift."""
    return _compiled(_iir_scalar, np.zeros(4, dtype=np.int64), 0, 4)


def iir(x, state, params):
    """Bit-accurate model of axi_tdm_filter.vhd (alpha = 1/2**SHIFT).

    SHIFT = 0 is a bypass in the hardware and is a bypass here for the same
    reason, not as a special case: y = y + (x - y) = x.
    """
    shift = int(params.get("shift", 4)) & 0x1F
    kernel = _get_kernel()
    out, state["y"] = kernel(_to_signed(x), state.get("y", 0), shift)
    return _to_wire(out, x.dtype)


def bypass(x, state, params):
    """Pass through untouched -- the local equivalent of the relay's
    loopback. Useful as a control: anything visible between the in and out
    traces here is the plot or the scheduler, not the algorithm."""
    return x


# --- pipe1: baseline-wander removal (high-pass) -------------------------
# Worked example: ECG's slow baseline drift is far larger than the QRS
# complex, so removing it is usually the first stage of any ECG chain.
# scipy = 2nd-order Butterworth @0.5Hz; manual = 1st-order shift filter at a
# comparable corner -- the differing rolloffs are the point of keeping both.

# The wire's own zero: signal_gen centres every chunk on dtype_max/2 in the
# UNSIGNED domain and clips there, so this is where "no signal" sits.
_CENTRE = 1 << 31


def _from_wire(x):
    """Wire words as a zero-centred signal (NOT _to_signed(), which maps
    the unsigned midpoint to the signed wrap boundary -- fine for `iir`
    since the hardware does the same, but wrong for a filter computing a
    real frequency response: it would see a discontinuity every time the
    signal crosses centre).
    """
    return x.astype(np.int64) - _CENTRE


def _to_wire_centred(values, dtype):
    """Zero-centred results back to wire words. CLIPS rather than wraps
    (matching signal_gen's own output) -- wrapping would turn an overshoot
    into a full-scale spike at the opposite rail, reading as a broken
    filter. `iir` still wraps, since there the wrap IS what's modelled.
    """
    return np.clip(np.asarray(values, dtype=np.int64) + _CENTRE,
                   0, _MASK).astype(dtype)


def pipe1_scipy(x, state, params):
    """2nd-order Butterworth high-pass. Float64, via scipy.signal."""
    from scipy import signal

    fs = float(params.get("fs", 2048.0))
    fc = float(params.get("hp_hz", 0.5))
    if "sos" not in state:
        # Designed once per run, not per chunk -- fs/fc rarely change and
        # sosfilt_zi isn't cheap enough to redo 50x a second.
        state["sos"] = signal.butter(2, fc / (fs / 2.0),
                                     btype="highpass", output="sos")
        # Start from rest, not sosfilt_zi's steady state -- no history yet.
        state["zi"] = np.zeros_like(signal.sosfilt_zi(state["sos"]))

    xs = _from_wire(x).astype(np.float64)
    y, state["zi"] = signal.sosfilt(state["sos"], xs, zi=state["zi"])
    return _to_wire_centred(np.rint(y), x.dtype)


def pipe1_manual(x, state, params):
    """1st-order high-pass as x - lowpass(x), in integer arithmetic.

    Reuses the same shift-and-accumulate kernel the fabric already
    implements: subtracting a single-pole lowpass IS a high-pass, cheap in RTL.
    """
    shift = int(params.get("hp_shift", 9)) & 0x1F
    kernel = _get_kernel()
    xs = _from_wire(x)

    if "y" not in state:
        # Seed with the first sample, not 0 -- `iir` starts at 0 to
        # reproduce hardware startup; this is a design model and a 2**31
        # step would swamp the plot for the first seconds.
        state["y"] = int(xs[0]) if xs.size else 0

    lp, state["y"] = kernel(xs, state["y"], shift)
    return _to_wire_centred(xs - lp, x.dtype)


# --- pipe2: the ECG diagnostic band -------------------------------------
# HP 0.2Hz (baseline wander) -> notch 50Hz (mains) -> LP 150Hz (EMG+), in
# that order since drift is by far the largest thing on the wire.
#
# ECG-specific choices: 0.2Hz/2nd-order HP keeps the near-DC ST segment
# from tilting (AHA: 0.05Hz diagnostic / 0.5Hz monitoring; 0.2Hz is the
# usual compromise). Notch Q=30 (~1.7Hz wide) stays narrow since QRS has
# real energy either side of 50Hz -- a notch is a last resort, not a
# substitute for a decent electrode. 150Hz LP is the standard adult
# diagnostic bandwidth.
#
# Any stage at 0 or >=Nyquist is skipped. These are FALLBACKS -- the app
# and SAT both pass config.PIPE2_* (Local tab) at runtime.
PIPE2_HP_HZ = 0.2
PIPE2_NOTCH_HZ = 50.0
PIPE2_NOTCH_Q = 30.0
PIPE2_LP_HZ = 150.0


def _pipe2_freqs(params):
    # pipe2_-prefixed: params is one flat dict shared by every pipeline,
    # and pipe1 already owns an unprefixed "hp_hz".
    return (float(params.get("pipe2_hp_hz", PIPE2_HP_HZ)),
            float(params.get("pipe2_notch_hz", PIPE2_NOTCH_HZ)),
            float(params.get("pipe2_notch_q", PIPE2_NOTCH_Q)),
            float(params.get("pipe2_lp_hz", PIPE2_LP_HZ)),
            float(params.get("fs", 2048.0)))


def pipe2_scipy(x, state, params):
    """0.2 Hz HP -> 50 Hz notch -> 150 Hz LP, float64 via scipy.signal.

    One sos cascade rather than three filter calls: same maths, one state
    array, and it is the form the manual version is written against.
    """
    from scipy import signal

    hp_hz, notch_hz, notch_q, lp_hz, fs = _pipe2_freqs(params)
    if "sos" not in state:
        nyq = fs / 2.0
        sections = []
        if 0 < hp_hz < nyq:
            sections.append(signal.butter(2, hp_hz / nyq, btype="highpass",
                                          output="sos"))
        if 0 < notch_hz < nyq:
            b, a = signal.iirnotch(notch_hz, notch_q, fs=fs)
            sections.append(signal.tf2sos(b, a))
        if 0 < lp_hz < nyq:
            sections.append(signal.butter(2, lp_hz / nyq, btype="lowpass",
                                          output="sos"))
        # All corners out of range (fs too low): an all-pass section, so
        # the chain stays a filter instead of a special case.
        state["sos"] = (np.vstack(sections) if sections
                        else np.array([[1., 0., 0., 1., 0., 0.]]))
        # From rest, not sosfilt_zi's steady state -- same reason as pipe1.
        state["zi"] = np.zeros((state["sos"].shape[0], 2))

    xs = _from_wire(x).astype(np.float64)
    y, state["zi"] = signal.sosfilt(state["sos"], xs, zi=state["zi"])
    return _to_wire_centred(np.rint(y), x.dtype)


def _biquad_scalar(x, g, b1, a1, a2, shift, x1, x2, y1, y2):
    """Integer biquad, Q-format coefficients, one shift per sample:

        y[n] = (g*(x[n] + x[n-2]) + b1*x[n-1] - a1*y[n-1] - a2*y[n-2]) >> shift

    One kernel, not two: a notch and a Butterworth low-pass are both
    b = [g, b1, g], so the same recursion covers both stages.

    Coefficients pre-scaled by 2**shift so the sum stays exact until one
    final truncating shift -- shifting each term separately would throw
    away the precision the narrow notch depends on.
    """
    out = np.empty(x.shape[0], dtype=np.int64)
    for i in range(x.shape[0]):
        xi = x[i]
        yi = (g * (xi + x2) + b1 * x1 - a1 * y1 - a2 * y2) >> shift
        x2, x1 = x1, xi
        y2, y1 = y1, yi
        out[i] = yi
    return out, x1, x2, y1, y2


_BIQUAD_SHIFT = 20  # coefficient fraction bits; 15 is too coarse for Q = 30


def _quantise(b0, b1, a1, a2):
    one = 1 << _BIQUAD_SHIFT
    return (int(round(b0 * one)), int(round(b1 * one)),
            int(round(a1 * one)), int(round(a2 * one)))


def _notch_coeffs(f0, q, fs):
    """RBJ notch, normalised to unity gain away from f0."""
    w0 = 2.0 * np.pi * f0 / fs
    cos_w0, alpha = np.cos(w0), np.sin(w0) / (2.0 * q)
    a0 = 1.0 + alpha
    return _quantise(1.0 / a0, -2.0 * cos_w0 / a0,
                     -2.0 * cos_w0 / a0, (1.0 - alpha) / a0)


def _lowpass_coeffs(fc, fs):
    """RBJ 2nd-order Butterworth low-pass (Q = 1/sqrt(2)). A biquad, not
    another shift filter: a single pole's corner is quantised to
    fs/(2*pi*2**s), which at fs=2048 jumps from 163Hz (s=1) straight to
    bypass (s=0) -- no 150Hz to be had -- and 6dB/octave barely touches
    EMG at 400Hz. The high-pass at the other end of the band is the
    opposite case; see pipe2_manual.
    """
    w0 = 2.0 * np.pi * fc / fs
    cos_w0 = np.cos(w0)
    alpha = np.sin(w0) / np.sqrt(2.0)
    a0 = 1.0 + alpha
    return _quantise((1.0 - cos_w0) / 2.0 / a0, (1.0 - cos_w0) / a0,
                     -2.0 * cos_w0 / a0, (1.0 - alpha) / a0)


def _shift_for(fc, fs):
    """Nearest single-pole shift for a corner at fc: fc = fs/(2*pi*2**s)."""
    if fc <= 0:
        return None
    return min(max(int(round(np.log2(fs / (2.0 * np.pi * fc)))), 0), 30)


def _biquad_kernel():
    return _compiled(_biquad_scalar, np.zeros(4, dtype=np.int64),
                     1, 0, 0, 0, 1, 0, 0, 0, 0)


def pipe2_manual(x, state, params):
    """The same chain in integer arithmetic -- this is what becomes RTL.

    Two different structures on purpose, because the two ends of the band
    are different problems in fixed point:

      HP 0.2 Hz   x - single_pole_lowpass(x): one add, one shift, no
                  multiplier. A biquad this close to DC needs poles at
                  radius ~0.9994, and quantising those is where fixed-point
                  filters go to ring. Costs a corner quantised to a power
                  of two -- 0.159 Hz at fs = 2048, not 0.2.
      notch, LP   fixed-point biquads. Their poles sit well away from z=1,
                  so Q20 coefficients are plenty, and the LP gets a real
                  12 dB/octave instead of a single pole's 6.

    Flip to scipy to see what the quantisation costs.
    """
    hp_hz, notch_hz, notch_q, lp_hz, fs = _pipe2_freqs(params)
    nyq = fs / 2.0
    xs = _from_wire(x)

    if "init" not in state:
        state["init"] = True
        state["hp_shift"] = _shift_for(hp_hz, fs) if 0 < hp_hz < nyq else None
        state["notch"] = (_notch_coeffs(notch_hz, notch_q, fs)
                          if 0 < notch_hz < nyq else None)
        state["lp"] = _lowpass_coeffs(lp_hz, fs) if 0 < lp_hz < nyq else None
        # HP seeded with the first sample, not 0 -- same reason as pipe1.
        state["hp_y"] = int(xs[0]) if xs.size else 0
        state["ns"] = (0, 0, 0, 0)      # notch    x1, x2, y1, y2
        state["ls"] = (0, 0, 0, 0)      # low-pass x1, x2, y1, y2

    if state["hp_shift"] is not None:
        lp, state["hp_y"] = _get_kernel()(xs, state["hp_y"], state["hp_shift"])
        xs = xs - lp

    biquad = _biquad_kernel()
    for coeffs, key in ((state["notch"], "ns"), (state["lp"], "ls")):
        if coeffs is None:
            continue
        g, b1, a1, a2 = coeffs
        x1, x2, y1, y2 = state[key]
        xs, x1, x2, y1, y2 = biquad(xs, g, b1, a1, a2, _BIQUAD_SHIFT,
                                    x1, x2, y1, y2)
        state[key] = (x1, x2, y1, y2)

    return _to_wire_centred(xs, x.dtype)


# An entry is EITHER a bare function (bypass/iir -- nothing to choose
# between: bypass has no fixed-point version to differ, iir models
# specific existing hardware rather than a design in progress) OR a
# {implementation: function} dict (pipe1+, designs that always come as a
# scipy/manual pair). Both GUIs read this shape to decide whether to show
# an implementation dropdown.
PIPELINES = {
    "bypass": bypass,
    "iir": iir,
    "pipe1": {"manual": pipe1_manual, "scipy": pipe1_scipy},
    "pipe2": {"manual": pipe2_manual, "scipy": pipe2_scipy},
}

# What a design pipeline always offers, in dropdown order -- named here so
# the two GUIs and sat.py agree without each writing the pair out again.
IMPLS = ("manual", "scipy")

DEFAULT_PIPE = "iir"
DEFAULT_IMPL = "manual"


def has_implementations(pipe):
    """True when this entry offers a scipy/manual choice at all."""
    return isinstance(PIPELINES.get(pipe), dict)


def implementations(pipe):
    """Which implementations an entry offers -- empty for the fixed ones."""
    entry = PIPELINES.get(pipe)
    return sorted(entry) if isinstance(entry, dict) else []


def label(pipe, impl):
    """How a selection is displayed/logged: "iir" for the fixed entries,
    "pipe1/scipy" for the rest -- naming an implementation that was never
    chosen would be misleading."""
    return f"{pipe}/{impl}" if has_implementations(pipe) else pipe


def resolve(pipe, impl):
    """The function for a (pipeline, implementation) pair, or None. `impl`
    is ignored for the fixed entries, and falls back to the first
    implementation when the requested one is stale, so filtering never
    silently stops."""
    entry = PIPELINES.get(pipe)
    if entry is None:
        return None
    if not isinstance(entry, dict):
        return entry
    if impl in entry:
        return entry[impl]
    return entry[sorted(entry)[0]]


def new_state():
    """Fresh filter memory for ONE channel -- empty, since each pipeline's
    state shape (an integer, an sos/zi pair, ...) isn't interchangeable
    with another's."""
    return {}
