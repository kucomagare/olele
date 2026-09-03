# Local processing mode: generate/process/plot in-process, no socket/relay/
# board -- for developing an algorithm before it's RTL or with no hardware.
# Plumbing only; filters live in pipelines.py.

import queue
import time

import config
import pipelines
import runctl
from pipelines import has_implementations, label, new_state, resolve
from sched import RateScheduler
from signal_gen import generate_ecg_chunk


def params_for(ch):
    """Knobs for channel `ch`'s pipeline.

    fs is ECG_SAMPLING_RATE (the buffer's native rate), not SEND_RATE *
    CHUNK_SIZE -- that's what a filter designed in Hz must use.
    """
    return {
        "shift": config.LOCAL_SHIFT,
        "fs": float(config.ECG_SAMPLING_RATE),
        # Prefixed by pipeline -- params is one flat dict; pipe1 has its own "hp_hz".
        "pipe2_hp_hz": config.PIPE2_HP_HZ,
        "pipe2_notch_hz": config.PIPE2_NOTCH_HZ,
        "pipe2_notch_q": config.PIPE2_NOTCH_Q,
        "pipe2_lp_hz": config.PIPE2_LP_HZ,
    }


def process_channel(x, ch, state):
    """Run ch's configured pipeline over one chunk. Single dispatch point
    shared with net.py's substitution, so the answer can't disagree."""
    pipe = config.CH_PIPE[ch]
    # Fixed entries ignore implementation, normalised out of the identity
    # below -- else toggling an inactive dropdown resets a running filter.
    sel = (pipe, config.CH_IMPL[ch] if has_implementations(pipe) else None)
    if state.get("_sel") != sel:
        # zi array vs integer accumulator aren't interchangeable --
        # switching pipeline/impl mid-run needs a clean slate.
        state.clear()
        state["_sel"] = sel

    fn = resolve(*sel)
    if fn is None:
        return x
    try:
        return fn(x, state, params_for(ch))
    except Exception as exc:                          # noqa: BLE001
        # A pipeline under development WILL raise -- don't lose the whole
        # app (and the panel needed to fix it) to that.
        print(f"[local] ch{ch + 1} {label(pipe, config.CH_IMPL[ch])} "
              f"raised: {exc}")
        return x


def local_thread(plot_in_q, plot_out_q, stop_event):
    """Mirror of net.tcp_thread for when EVERY channel is local.

    Same schedule as net.py so panel rate/captures match across modes.
    Both threads always run, idling unless they own the current mode --
    switching modes is then just an attribute write.
    """
    states = [new_state(), new_state()]
    counter = 0
    active = False
    schedule = RateScheduler(lambda: config.SEND_RATE)
    chunks = 0
    samples = 0
    plot_dropped = 0
    last_report = time.time()

    while not stop_event.is_set():
        # Owns the run only when every channel is local -- if one is on the
        # board, net.tcp_thread does local channels inline instead, since
        # both must travel in the same plot tuple.
        if not runctl.is_running() or not config.all_local():
            if active:
                print("[local] stopped")
                active = False
            if not runctl.is_running():
                runctl.wait_for_start(0.1)   # blocks for real; flag is false
            else:
                # Started but doesn't own the mode -- stop_event.wait, not
                # wait_for_start (which busy-loops once already set).
                stop_event.wait(0.1)
            continue

        if not active:
            # Fresh filter state each run, like a just-reset board --
            # else an A/B comparison starts from the previous run's tail.
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
                # Counted not swallowed -- same as net.py, these samples
                # genuinely go missing.
                plot_dropped += 1

            if config.RECEIVE_ENABLED:
                # process_channel owns dispatch/state-reset/error-handling
                # so this and net.py's substitution can't drift apart.
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
            print(f"Local: {chunks / elapsed:.0f} chunks/s, "
                  f"{samples / elapsed:.0f} samples/s{note}")
            chunks = samples = plot_dropped = 0
            last_report = t

        # Must ALWAYS yield here -- an earlier version only slept when ahead
        # of schedule, so PAUSED and BEHIND (deadline never catches up to
        # "now") both spun at 100% CPU holding the GIL, freezing the plot.
        if not config.SEND_ENABLED:
            time.sleep(0.02)   # paused: nothing to be on time for, just stay responsive
        else:
            delay = schedule.time_until()
            if delay > 0:
                time.sleep(min(delay, 0.05))   # ahead: sleep to deadline, capped for responsiveness
            else:
                time.sleep(0)   # behind: yield the GIL without throttling
