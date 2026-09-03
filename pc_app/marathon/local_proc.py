# Local processing mode: generate, process and plot inside this process --
# no socket, no relay, no board. For developing an algorithm before it is RTL
# (edit-run loop of a keystroke instead of a synthesis run), and for working
# with no hardware present.
#
# This file is the plumbing; the filters live in pipelines.py. Here: which
# pipeline a channel runs, what a chunk is handed, when state is cleared, the
# worker thread and its schedule. Split that way so adding a filter never
# means reading any of this, and so SAT can import the filters alone.

import queue
import time

import config
import pipelines
import runctl
from pipelines import has_implementations, label, new_state, resolve
from sched import RateScheduler
from signal_gen import generate_ecg_chunk


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
        # Prefixed by pipeline: params is one flat dict for all of them, and
        # pipe1 has its own "hp_hz".
        "pipe2_hp_hz": config.PIPE2_HP_HZ,
        "pipe2_notch_hz": config.PIPE2_NOTCH_HZ,
        "pipe2_notch_q": config.PIPE2_NOTCH_Q,
        "pipe2_lp_hz": config.PIPE2_LP_HZ,
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
