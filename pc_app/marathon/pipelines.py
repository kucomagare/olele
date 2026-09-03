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
#     def pipe2_scipy(x, state, params): ...   # x: ONE channel, wire dtype,
#     def pipe2_manual(x, state, params): ...  # same dtype/length out
#     PIPELINES["pipe2"] = {"manual": pipe2_manual, "scipy": pipe2_scipy}
#
# `state` is a dict that persists across chunks (filter memory); it is cleared
# for you when the selection changes. `params` carries the panel's knobs plus
# "fs", the signal's native rate. Called once per channel per chunk, so two
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


_kernel = None


def _get_kernel():
    """Resolve the per-sample kernel once, preferring a numba-compiled one.

    A scalar Python loop is fine at the real-time default (SEND_RATE 50 x
    CHUNK_SIZE 8 = 400 samples/s) and hopeless at the rates the board has
    been pushed to (1400 x 960 = 1.34M samples/s). numba is already a
    dependency (build.sh), and njit turns this exact shape of loop into
    something comparable with C. If it is missing or refuses to compile,
    fall back rather than fail: a slow local mode still beats no local mode.
    """
    global _kernel
    if _kernel is not None:
        return _kernel
    _kernel = _iir_scalar
    try:
        from numba import njit
        # nogil=True matters more than the speed here: this loop is the one
        # place the local worker spends real time, and without it the plot
        # thread cannot run at all while a chunk is being filtered. The
        # kernel touches no Python objects, so dropping the GIL is safe.
        compiled = njit(cache=True, nogil=True)(_iir_scalar)
        # Force compilation now, on a throwaway input, so a failure lands
        # here as a caught exception instead of mid-stream.
        compiled(np.zeros(4, dtype=np.int64), 0, 4)
        _kernel = compiled
        print("[local] numba kernel compiled")
    except Exception as exc:                          # noqa: BLE001
        print(f"[local] numba unavailable ({exc}); using the Python kernel "
              f"-- fine at low sample rates, slow above ~100k samples/s")
    return _kernel


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
