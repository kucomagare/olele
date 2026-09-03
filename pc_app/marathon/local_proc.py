# Local processing mode: generate, process and plot entirely inside this
# process, with no socket, no relay and no board.
#
# WHY. Two different reasons, and both matter:
#
#   1. DEVELOP THE ALGORITHM BEFORE IT IS RTL. An algorithm that will end up
#      in VHDL wants to be tried against real-looking signals long before a
#      bitstream exists. Here the edit-run loop is a keystroke; on the board
#      it is a synthesis run. Write it here, watch it, then translate.
#
#   2. WORK WITH NO HARDWARE. The board, the cable and the relay all stop
#      being prerequisites for touching the signal path.
#
# PIPELINES, AND WHY EACH HAS TWO IMPLEMENTATIONS
# ------------------------------------------------
# A "pipeline" is one signal-processing idea (pipe1, pipe2, ...). Each one is
# written TWICE, and both versions are kept:
#
#   "scipy"  -- float64, using scipy.signal. Fast to write, easy to reason
#               about, and the right place to decide what the filter should
#               DO: cutoffs, orders, response shape.
#   "manual" -- hand-written the way the FABRIC will have to compute it:
#               integer arithmetic, fixed width, explicit wrapping,
#               persistent state, one sample at a time. This is the version
#               that gets translated to VHDL.
#
# Keeping both is the point. The scipy one says what the filter is supposed
# to do; the manual one says what it will actually do once quantised. The gap
# between them -- truncation bias, dead zones, overflow -- is exactly the
# thing that "it worked in Python" hides when only the float version exists,
# and it is visible here by flipping one dropdown.
#
# `iir` is neither of those: it is a bit-accurate model of the filter the
# board is ALREADY running (axi_tdm_filter.vhd), right down to reading the
# wire words as SIGNED 32-bit -- which is not a detail: the PC centres
# samples on 2**31, so as signed they sit at the very bottom of the range and
# every subtraction is near an overflow boundary. It is the reference for
# "did the hardware compute what I think it did", which is why it stays its
# own entry rather than becoming pipe1's manual side.
#
# ADDING A PIPELINE: write the two functions and add one registry line.
# Everything is PER CHANNEL -- the function is handed one channel's samples
# and is called once per channel per chunk, so two local channels means two
# calls. Both GUIs pick it up with no edit of their own.
#
#     def pipe2_scipy(x, state, params):
#         '''x: one channel, wire-dtype array. Returns the same dtype.
#         `state` is a plain dict that persists across chunks -- put filter
#         memory in it; it is cleared automatically when the selection
#         changes. `params` carries the panel's knobs plus "fs".'''
#
#     def pipe2_manual(x, state, params):
#         ...
#
#     PIPELINES["pipe2"] = {"manual": pipe2_manual, "scipy": pipe2_scipy}
#
# Use _from_wire()/_to_wire_centred() for the conversion, NOT _to_signed() --
# see _from_wire's docstring for why that one is for `iir` only.
#
# The same functions are what SAT scores a recorded capture against
# (sat.py --model), so anything added here is immediately usable offline.

import queue
import time

import numpy as np

import config
import runctl
from packet_format import CH1_DTYPE, CH2_DTYPE
from sched import RateScheduler
from signal_gen import generate_ecg_chunk

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


def params_for(ch):
    """The knobs handed to a pipeline for channel `ch`.

    fs is the signal's native rate, not SEND_RATE * CHUNK_SIZE: the samples
    were drawn from a buffer simulated at ECG_SAMPLING_RATE, so that is the
    rate a filter designed in Hz has to be designed against, whatever rate
    they happen to be streamed at.
    """
    return {
        "shift": config.LOCAL_SHIFT,
        "fs": float(config.ECG_SAMPLING_RATE),
    }


def process_channel(x, ch, state):
    """Run channel `ch`'s configured pipeline over one chunk of that channel.

    The single dispatch point, shared by local_thread and by net.py's
    per-channel substitution, so "which filter is channel 2 running" has one
    answer rather than two that can disagree.
    """
    pipe = config.CH_PIPE[ch]
    # Fixed entries ignore the implementation, so it is normalised out of the
    # identity below -- otherwise changing an inactive dropdown would reset a
    # running bypass/iir for no reason.
    sel = (pipe, config.CH_IMPL[ch] if has_implementations(pipe) else None)
    if state.get("_sel") != sel:
        # A scipy zi array and an integer accumulator are not interchangeable,
        # so switching pipeline or implementation mid-run has to start from a
        # clean slate rather than reinterpret the previous one's memory.
        state.clear()
        state["_sel"] = sel

    fn = resolve(*sel)
    if fn is None:
        return x
    try:
        return fn(x, state, params_for(ch))
    except Exception as exc:                          # noqa: BLE001
        # A pipeline under development WILL raise. Losing the whole app to it
        # (and with it the panel that would let you fix the parameter that
        # caused it) is the wrong trade -- report, pass this chunk through,
        # keep running.
        print(f"[local] ch{ch + 1} {label(pipe, config.CH_IMPL[ch])} "
              f"raised: {exc}")
        return x


def local_thread(plot_in_q, plot_out_q, stop_event):
    """Mirror of net.tcp_thread for when EVERY channel is local.

    Runs the same schedule (CHUNK_SIZE samples every 1/SEND_RATE seconds,
    both read live) so a rate typed into the panel means the same thing in
    both modes and a capture taken here is directly comparable with one
    taken off the board. The two threads are both always alive and each
    idles unless it owns the current mode -- switching modes is then just an
    attribute write, with no thread lifecycle to get wrong.
    """
    states = [new_state(), new_state()]
    counter = 0
    active = False
    # Same schedule as the board path, from the same module -- that is what
    # makes a rate typed into the panel mean the same thing in either mode.
    schedule = RateScheduler(lambda: config.SEND_RATE)
    chunks = 0
    samples = 0
    plot_dropped = 0
    last_report = time.time()

    while not stop_event.is_set():
        # Owns the run only when EVERY channel is local -- if even one is on
        # the board, net.tcp_thread owns it and does the local channels
        # inline, because they have to travel in the same plot tuple as the
        # channels that came back over the wire.
        if not runctl.is_running() or not config.all_local():
            if active:
                print("[local] stopped")
                active = False
            if not runctl.is_running():
                # Not started: block on the gate. wait_for_start(0.1) genuinely
                # sleeps here, since the flag is false, and returns the instant
                # Start is pressed.
                runctl.wait_for_start(0.1)
            else:
                # Started, but this thread does not own the mode. Event.wait()
                # returns immediately once the flag is already true -- calling
                # wait_for_start here would busy-loop, not idle, and starve the
                # thread that DOES own the mode of GIL time. Use stop_event.wait
                # instead: it sleeps up to 0.1s and still wakes early on Stop.
                stop_event.wait(0.1)
            continue

        if not active:
            # Every run starts from a clean filter, like a board that has
            # just had its state cleared -- otherwise the first seconds of a
            # run carry the tail of the previous one and an A/B comparison
            # silently starts from the wrong place.
            states = [new_state(), new_state()]
            schedule.reset()
            chunks = samples = plot_dropped = 0
            last_report = time.time()
            active = True
            print(f"[local] running -- "
                  f"ch1={label(config.CH_PIPE[0], config.CH_IMPL[0])} "
                  f"ch2={label(config.CH_PIPE[1], config.CH_IMPL[1])}")

        now = time.perf_counter()

        # Paused is not a stall -- see RateScheduler.hold().
        if not config.SEND_ENABLED:
            schedule.hold(now)

        if config.SEND_ENABLED and schedule.due(now):
            ch1, ch2 = generate_ecg_chunk(counter)
            try:
                plot_in_q.put_nowait((ch1, ch2))
            except queue.Full:
                # Counted, not swallowed -- same reason as net.py's: these are
                # samples that genuinely go missing, and a dump taken while it
                # is happening is short without saying so.
                plot_dropped += 1

            if config.RECEIVE_ENABLED:
                # Once per channel per chunk. process_channel owns the
                # dispatch, the state reset and the error handling, so this
                # loop and net.py's substitution cannot drift apart.
                out1 = process_channel(ch1, 0, states[0])
                out2 = process_channel(ch2, 1, states[1])
                try:
                    plot_out_q.put_nowait((out1, out2))
                except queue.Full:
                    plot_dropped += 1

            counter += config.CHUNK_SIZE
            chunks += 1
            samples += config.CHUNK_SIZE

            schedule.advance(now)

        t = time.time()
        elapsed = t - last_report
        if elapsed >= 1.0:
            backlog_dropped = schedule.take_dropped()
            note = f" (dropped {backlog_dropped} late)" if backlog_dropped else ""
            if plot_dropped:
                note += f" ({plot_dropped} plot-drops -- a dump now is short)"
            # Normalized by the window that actually elapsed, like net.py's
            # line and the firmware's [S] -- the check runs once per loop
            # pass, so the window always overshoots by a varying amount.
            print(f"Local: {chunks / elapsed:.0f} chunks/s, "
                  f"{samples / elapsed:.0f} samples/s{note}")
            chunks = samples = plot_dropped = 0
            last_report = t

        # Yield before looping. This MUST always yield -- an earlier version
        # slept only when it was ahead of schedule, which meant two states
        # where it never slept at all and spun at 100% of a core holding the
        # GIL, starving the plot thread until the whole window looked frozen:
        #
        #   * PAUSED. The deadline stopped advancing (nothing is generated),
        #     so the "time until the next chunk" it was sleeping on went
        #     steadily more negative and the sleep was skipped forever.
        #     Measured: 101% of a core while doing nothing at all. The
        #     schedule now holds its deadline at `now` while paused, but the
        #     sleep below still has to be unconditional -- the second case
        #     has nothing to do with pausing.
        #   * BEHIND. Whenever generation cannot keep up, the deadline is
        #     always in the past, so the same branch never fires.
        #
        # Three cases, deliberately different:
        if not config.SEND_ENABLED:
            # Paused: nothing is scheduled, so there is nothing to be on
            # time for. Wake often enough to notice Resume/Stop/a mode
            # change, and cost nothing meanwhile.
            time.sleep(0.02)
        else:
            delay = schedule.time_until()
            if delay > 0:
                # Ahead of schedule: sleep to the deadline. Capped so
                # stop_event and a mode change are still seen promptly at
                # very low SEND_RATE.
                time.sleep(min(delay, 0.05))
            else:
                # Behind: yield the GIL without throttling. sleep(0) drops
                # it long enough for the GUI thread to take a turn and
                # returns immediately, so the rate is still limited by the
                # work rather than by this call -- which a minimum sleep
                # would not be (0.5 ms would cap the loop at 2000 chunks/s).
                time.sleep(0)
