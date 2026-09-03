# The processing functions, and everything needed to add one. No threads,
# sockets, queues, plot or board here -- that is local_proc.py.
#
# Each pipeline (pipe1, pipe2, ...) is written twice and both are kept:
#   "scipy"  float64 via scipy.signal -- what the filter SHOULD do.
#   "manual" integer, fixed width, explicit wrapping, one sample at a time --
#            what it WILL do once it is RTL. This is what gets translated.
# The gap between them (truncation bias, dead zones, overflow) is the thing a
# float-only version hides; here it is one dropdown away.
#
# `iir` is neither: a bit-accurate model of the filter the board already runs
# (axi_tdm_filter.vhd), kept as the reference for "did the hardware compute
# what I think it did".
#
# ADD A PIPELINE -- two functions and one registry line, nothing else. Both
# GUIs and SAT build their dropdowns from PIPELINES.
#
#     def pipeN_scipy(x, state, params): ...   # x: ONE channel, wire dtype,
#     def pipeN_manual(x, state, params): ...  # same dtype/length out
#     PIPELINES["pipeN"] = {"manual": pipeN_manual, "scipy": pipeN_scipy}
#
# `state` is a dict that persists across chunks (filter memory); it is cleared
# for you when the selection changes. `params` carries the panel's knobs plus
# "fs", the signal's native rate -- it is ONE dict shared by every pipeline,
# so prefix your own keys with the pipeline name (see _pipe2_freqs). Called once per channel per chunk, so two
# local channels means two calls with two separate states.
#
# Convert with _from_wire()/_to_wire_centred(), NOT _to_signed() -- see
# _from_wire's docstring. Raising is fine: local_proc reports it and passes
# the chunk through.

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
    """One channel of  y[n] = y[n-1] + (x[n] - y[n-1]) >> SHIFT.

    Written as a scalar recursion because that is what it IS -- the
    truncating shift makes it non-linear, so there is no vectorised form
    that gives the same bits. Both wraps below are the VHDL's: `signed`
    arithmetic in numeric_std truncates back to the operand width, so a
    subtraction that overflows wraps rather than saturating. It does
    overflow, once, on the very first sample of a run: y starts at 0 and x
    is near 2**31, so x - y does not fit. The hardware wraps there too, and
    the filter recovers within a few samples -- reproducing it is the point.
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
    """njit(fn) if numba will take it, else fn itself.

    Compiled once, on a throwaway input, so a failure lands here rather than
    mid-stream. nogil matters more than the speed: these loops are where the
    local worker spends its time, and without it the plot thread cannot run
    while a chunk is being filtered. They touch no Python objects, so it is
    safe. A missing numba falls back rather than failing -- a slow local mode
    still beats no local mode.
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
#
# The worked example. ECG sits on a slow baseline drift (breathing, electrode
# movement) that is far larger than the QRS complex, so removing it is the
# usual first stage of any ECG chain -- which makes it a fair template for
# the ones that follow.
#
# The two implementations are deliberately NOT identical: scipy's is a
# 2nd-order Butterworth at 0.5 Hz, the manual one a 1st-order shift filter at
# a comparable corner. Their rolloffs differ, and seeing that difference when
# you flip the dropdown is the point -- if they matched exactly, keeping both
# would tell you nothing.

# The wire's own zero. signal_gen builds every chunk centred on dtype_max/2
# in the UNSIGNED domain and clips there (signal_gen.py), and the plot shows
# 0..2**32-1, so this is where "no signal" actually sits.
_CENTRE = 1 << 31


def _from_wire(x):
    """Wire words as a zero-centred integer signal.

    NOT _to_signed(). That one reinterprets offset-binary as two's
    complement, which is what axi_tdm_filter.vhd does and therefore what
    `iir` has to do -- but it maps the MIDDLE of the unsigned range to the
    EXTREMES of the signed one, so a signal centred on 2**31 comes out
    oscillating across the -2**31/+2**31 wrap boundary. `iir` tolerates that
    because the hardware does exactly the same thing; a filter that is
    supposed to compute a real frequency response cannot -- it sees a
    discontinuity every time the signal crosses its own centre.
    """
    return x.astype(np.int64) - _CENTRE


def _to_wire_centred(values, dtype):
    """Zero-centred results back to wire words.

    CLIPS rather than wraps, matching what signal_gen already does to its own
    output. Wrapping would turn one oversized excursion into a full-scale
    spike at the opposite rail, which reads as a broken filter rather than as
    the clipped signal it is. `iir` still wraps, because there the wrap is
    the thing being modelled.
    """
    return np.clip(np.asarray(values, dtype=np.int64) + _CENTRE,
                   0, _MASK).astype(dtype)


def pipe1_scipy(x, state, params):
    """2nd-order Butterworth high-pass. Float64, via scipy.signal."""
    from scipy import signal

    fs = float(params.get("fs", 2048.0))
    fc = float(params.get("hp_hz", 0.5))
    if "sos" not in state:
        # Designed once per run, not per chunk: the coefficients only depend
        # on fs/fc, and sosfilt_zi is not cheap enough to redo 50x a second.
        state["sos"] = signal.butter(2, fc / (fs / 2.0),
                                     btype="highpass", output="sos")
        # Start from rest rather than from sosfilt_zi's steady-state gain:
        # the run begins with no history, and pretending otherwise puts a
        # step at the front of the first chunk.
        state["zi"] = np.zeros_like(signal.sosfilt_zi(state["sos"]))

    xs = _from_wire(x).astype(np.float64)
    y, state["zi"] = signal.sosfilt(state["sos"], xs, zi=state["zi"])
    return _to_wire_centred(np.rint(y), x.dtype)


def pipe1_manual(x, state, params):
    """1st-order high-pass as x - lowpass(x), in integer arithmetic.

    Reuses the same shift-and-accumulate kernel the fabric already
    implements, which is the whole reason to write it this way: subtracting a
    single-pole lowpass is a high-pass, and a single-pole lowpass is one
    add and one shift per sample -- cheap in RTL, and already proven here.
    """
    shift = int(params.get("hp_shift", 9)) & 0x1F
    kernel = _get_kernel()
    xs = _from_wire(x)

    if "y" not in state:
        # Seed the lowpass with the first sample instead of 0. `iir` starts
        # at 0 because the HARDWARE does and reproducing its startup
        # transient is that function's job; this one is a design model, and a
        # 2**31 step at the front of every run would swamp the plot for the
        # first seconds and tell you nothing about the filter.
        state["y"] = int(xs[0]) if xs.size else 0

    lp, state["y"] = kernel(xs, state["y"], shift)
    return _to_wire_centred(xs - lp, x.dtype)


# --- pipe2: the ECG diagnostic band -------------------------------------
#
# Three stages in this order: 0.2 Hz high-pass (baseline wander), 50 Hz notch
# (mains), 150 Hz low-pass (EMG and everything above the signal). The order is
# not arbitrary -- drift is by far the largest thing on the wire, and removing
# it first keeps the two later stages away from their headroom limits.
#
# Why these numbers, for ECG specifically:
#   0.2 Hz HP, 2nd order. The ST segment is nearly DC, so the high-pass sits
#       directly on top of the diagnosis: too high a corner or too steep a
#       rolloff tilts ST and invents depression/elevation that is not there.
#       (AHA: 0.05 Hz diagnostic, 0.5 Hz monitoring; 0.2 Hz with a gentle
#       2nd-order response is the usual compromise.)
#   50 Hz notch, Q = 30 (~1.7 Hz wide). Narrow on purpose -- the QRS has real
#       energy either side of 50 Hz and a wide notch takes a bite out of it.
#       The price of narrow is ringing after each QRS, which is why a notch is
#       a last resort rather than a substitute for a decent electrode.
#   150 Hz LP. The standard adult diagnostic bandwidth. Lower, and QRS
#       amplitude and notching start to go.
#
# Any stage set to 0, or at/above Nyquist, is skipped: at fs = 256 there is
# nothing at 150 Hz to remove.
#
# These are FALLBACKS, for a caller that passes no params. The app and SAT
# both hand in config.PIPE2_* (Local tab), so changing a corner there is what
# actually takes effect at runtime.
PIPE2_HP_HZ = 0.2
PIPE2_NOTCH_HZ = 50.0
PIPE2_NOTCH_Q = 30.0
PIPE2_LP_HZ = 150.0


def _pipe2_freqs(params):
    # Keys are prefixed with the pipeline's own name. `params` is one flat
    # dict shared by every pipeline, and pipe1 already reads "hp_hz" as its
    # own 0.5 Hz corner -- an unprefixed "hp_hz" here would have retuned
    # pipe1 from the Local tab without anything saying so.
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
        # Nothing applicable (fs below every corner): an all-pass section, so
        # the chain stays a filter instead of becoming a special case.
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

    Coefficients are pre-scaled by 2**shift so the sum stays exact until a
    single truncating shift at the end -- shifting each term separately throws
    away most of the precision the narrow notch depends on. Two multipliers
    and four registers in RTL, with a ~52-bit accumulator at these widths.
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
    """RBJ 2nd-order Butterworth low-pass (Q = 1/sqrt(2)).

    A biquad rather than another shift filter, because a single pole's corner
    is quantised to fs/(2*pi*2**s): at fs = 2048 that jumps from 163 Hz (s=1)
    straight to a bypass (s=0), so there is no 150 Hz to be had -- and 6 dB
    per octave leaves EMG at 400 Hz barely touched. The high-pass at the other
    end of the band is the opposite case; see pipe2_manual.
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

    Two different structures on purpose, because the two ends of the band are
    different problems in fixed point:

      HP 0.2 Hz   x - single_pole_lowpass(x): one add, one shift, no
                  multiplier. A biquad this close to DC needs poles at radius
                  ~0.9994, and quantising those is where fixed-point filters
                  go to ring. The price is a corner quantised to a power of
                  two -- 0.159 Hz at fs = 2048, not 0.2.
      notch, LP   fixed-point biquads. Their poles sit well away from z = 1,
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


# An entry is EITHER a bare function -- one fixed thing, no implementation to
# choose -- OR a {implementation: function} dict offering the scipy/manual
# pair. The distinction is real, not cosmetic:
#
#   bypass  is passthrough. There is nothing to quantise, so a "float" and a
#           "fixed-point" version of it would be the same function under two
#           names, and offering the choice would imply a difference that
#           cannot exist.
#   iir     is the board's own filter, already in fabric. It is a MODEL of
#           specific hardware, not a design being explored, so there is
#           nothing for a float version to be the design of.
#   pipe1+  are designs in progress, and those always come as the pair.
#
# Both GUIs read this shape directly: the implementation dropdown is offered
# when there is a choice and greyed out when there is not, in the main panel
# and in SAT alike.
PIPELINES = {
    "bypass": bypass,
    "iir": iir,
    "pipe1": {"manual": pipe1_manual, "scipy": pipe1_scipy},
    "pipe2": {"manual": pipe2_manual, "scipy": pipe2_scipy},
}

# What a design pipeline always offers, in dropdown order. Named here so the
# two GUIs and sat.py agree without each writing the pair out again.
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
    """How a selection is written wherever one is shown or logged.

    "iir" for the fixed entries and "pipe1/scipy" for the rest -- naming an
    implementation that was never chosen would be noise at best and a claim
    about what ran at worst.
    """
    return f"{pipe}/{impl}" if has_implementations(pipe) else pipe


def resolve(pipe, impl):
    """The function for a (pipeline, implementation) pair, or None.

    `impl` is ignored for the fixed entries, and falls back to the
    pipeline's first implementation when the requested one does not exist --
    a stale selection should still run the filter, not silently stop
    filtering.
    """
    entry = PIPELINES.get(pipe)
    if entry is None:
        return None
    if not isinstance(entry, dict):
        return entry
    if impl in entry:
        return entry[impl]
    return entry[sorted(entry)[0]]


def new_state():
    """Fresh filter memory for ONE channel.

    Empty rather than pre-seeded: each pipeline puts in whatever it needs
    (`iir` a single integer, pipe1_scipy an sos matrix and a zi array), and
    they are not interchangeable -- which is exactly why process_channel
    clears it when the selection changes.
    """
    return {}
