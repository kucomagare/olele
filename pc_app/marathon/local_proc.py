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
# THE RULE THAT MAKES IT WORTH ANYTHING: these algorithms are written the way
# the FABRIC will have to compute them -- integer arithmetic, fixed width,
# explicit wrapping, persistent per-channel state, one sample at a time. A
# float64 numpy one-liner would be easier and would be a different algorithm:
# it would not show the truncation bias, the dead zone, or the overflow that
# the hardware actually exhibits, so "it worked in Python" would mean nothing
# once it was RTL. The reference implementation below (`iir`) is a
# bit-accurate model of axi_tdm_filter.vhd, right down to reading the wire
# words as SIGNED 32-bit -- which is not a detail: the PC centres samples on
# 2**31, so as signed they sit at the very bottom of the range and every
# subtraction is near an overflow boundary.
#
# ADDING AN ALGORITHM: write a function with the signature below, register it
# in ALGORITHMS, and it appears in the Local tab's dropdown. Keep it integer.
#
#     def my_algo(ch1, ch2, state, params):
#         '''ch1/ch2: wire-dtype arrays. Returns (out1, out2), same dtype.
#         `state` is a plain dict that persists across chunks -- put filter
#         memory in it. `params` carries the panel's knobs.'''

import queue
import time

import numpy as np

import config
import runctl
from net import SEND_CATCHUP_MAX_S, SEND_CATCHUP_MIN_PKTS
from packet_format import CH1_DTYPE, CH2_DTYPE
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
        compiled = njit(cache=True)(_iir_scalar)
        # Force compilation now, on a throwaway input, so a failure lands
        # here as a caught exception instead of mid-stream.
        compiled(np.zeros(4, dtype=np.int64), 0, 4)
        _kernel = compiled
        print("[local] numba kernel compiled")
    except Exception as exc:                          # noqa: BLE001
        print(f"[local] numba unavailable ({exc}); using the Python kernel "
              f"-- fine at low sample rates, slow above ~100k samples/s")
    return _kernel


def iir(ch1, ch2, state, params):
    """Bit-accurate model of axi_tdm_filter.vhd (alpha = 1/2**SHIFT).

    SHIFT = 0 is a bypass in the hardware and is a bypass here for the same
    reason, not as a special case: y = y + (x - y) = x.
    """
    shift = int(params.get("shift", 4)) & 0x1F
    kernel = _get_kernel()
    o1, state["y1"] = kernel(_to_signed(ch1), state["y1"], shift)
    o2, state["y2"] = kernel(_to_signed(ch2), state["y2"], shift)
    return _to_wire(o1, ch1.dtype), _to_wire(o2, ch2.dtype)


def bypass(ch1, ch2, state, params):
    """Pass through untouched -- the local equivalent of the relay's
    loopback. Useful as a control: anything visible between the in and out
    traces here is the plot or the scheduler, not the algorithm."""
    return ch1, ch2


ALGORITHMS = {
    "iir": iir,
    "bypass": bypass,
}


def new_state():
    """Fresh per-channel filter memory. Zeroed, matching the fabric's state
    RAM, which is initialised to zero at configuration."""
    return {"y1": 0, "y2": 0}


def local_thread(plot_in_q, plot_out_q, stop_event):
    """Mirror of net.tcp_thread for PROCESSING_MODE == "local".

    Runs the same schedule (CHUNK_SIZE samples every 1/SEND_RATE seconds,
    both read live) so a rate typed into the panel means the same thing in
    both modes and a capture taken here is directly comparable with one
    taken off the board. The two threads are both always alive and each
    idles unless it owns the current mode -- switching modes is then just an
    attribute write, with no thread lifecycle to get wrong.
    """
    state = new_state()
    counter = 0
    active = False
    next_send = time.perf_counter()
    chunks = 0
    samples = 0
    backlog_dropped = 0
    last_report = time.time()

    while not stop_event.is_set():
        if not runctl.is_running() or config.PROCESSING_MODE != "local":
            if active:
                print("[local] stopped")
                active = False
            # Wait on the gate rather than spinning: this returns the
            # instant Start is pressed, and costs nothing meanwhile. The
            # timeout is what lets a mode change (which is not an Event) be
            # noticed, and what lets the thread see stop_event.
            runctl.wait_for_start(0.1)
            continue

        if not active:
            # Every run starts from a clean filter, like a board that has
            # just had its state cleared -- otherwise the first seconds of a
            # run carry the tail of the previous one and an A/B comparison
            # silently starts from the wrong place.
            state = new_state()
            next_send = time.perf_counter()
            chunks = samples = backlog_dropped = 0
            last_report = time.time()
            active = True
            print(f"[local] running -- algorithm={config.LOCAL_ALGORITHM} "
                  f"shift={config.LOCAL_SHIFT}")

        now = time.perf_counter()

        if config.SEND_ENABLED and now >= next_send:
            ch1, ch2 = generate_ecg_chunk(counter)
            try:
                plot_in_q.put_nowait((ch1, ch2))
            except queue.Full:
                pass

            if config.RECEIVE_ENABLED:
                algo = ALGORITHMS.get(config.LOCAL_ALGORITHM, bypass)
                params = {"shift": config.LOCAL_SHIFT}
                try:
                    out1, out2 = algo(ch1, ch2, state, params)
                except Exception as exc:              # noqa: BLE001
                    # An algorithm under development WILL raise. Losing the
                    # whole app to it (and with it the panel that would let
                    # you fix the parameter that caused it) is the wrong
                    # trade -- report, fall back to passthrough for this
                    # chunk, keep running.
                    print(f"[local] {config.LOCAL_ALGORITHM} raised: {exc}")
                    out1, out2 = ch1, ch2
                try:
                    plot_out_q.put_nowait((out1, out2))
                except queue.Full:
                    pass

            counter += config.CHUNK_SIZE
            chunks += 1
            samples += config.CHUNK_SIZE

            period = 1.0 / config.SEND_RATE
            next_send += period
            # Same duration-based catch-up bound as net.py, and for the same
            # reason -- see the SEND_CATCHUP_* comment there. A fixed-
            # increment schedule replays its whole backlog at loop speed
            # after any stall, which wrecks a throughput reading taken
            # during it.
            catchup_limit = max(SEND_CATCHUP_MIN_PKTS * period, SEND_CATCHUP_MAX_S)
            if now - next_send > catchup_limit:
                backlog_dropped += int((now - next_send) / period)
                next_send = now + period

        t = time.time()
        elapsed = t - last_report
        if elapsed >= 1.0:
            note = f" (dropped {backlog_dropped} late)" if backlog_dropped else ""
            # Normalized by the window that actually elapsed, like net.py's
            # line and the firmware's [S] -- the check runs once per loop
            # pass, so the window always overshoots by a varying amount.
            print(f"Local: {chunks / elapsed:.0f} chunks/s, "
                  f"{samples / elapsed:.0f} samples/s{note}")
            chunks = samples = backlog_dropped = 0
            last_report = t

        # Sleep to the next due chunk rather than a fixed tick: unlike
        # net.py there is no socket to drain here, so there is nothing
        # useful to do in between. Capped so stop_event and a mode change
        # are still noticed promptly at very low SEND_RATE.
        delay = next_send - time.perf_counter()
        if delay > 0:
            time.sleep(min(delay, 0.05))
