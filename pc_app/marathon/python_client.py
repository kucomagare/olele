import threading
import queue
import time

import config
import runctl
from plot import DualPlot
from net import tcp_thread
from local_proc import local_thread


def main():
    # The gate is cleared at import, so the window below comes up with
    # nothing generated, connected or sent -- see runctl.py. AUTOSTART is
    # the opt-out.
    if config.AUTOSTART:
        runctl.start()

    plotter = DualPlot(config.PLOT_BUFFER)

    plot_in_q  = queue.Queue(maxsize=1000)
    plot_out_q = queue.Queue(maxsize=1000)

    stop_event = threading.Event()

    # Both workers are started unconditionally and each idles unless it
    # owns the current per-channel modes -- so switching board/local is
    # an attribute write from the panel with no thread lifecycle to get
    # wrong, and no way to end up with two of them driving the plot at once.
    #
    # net.tcp_thread owns the socket entirely, including the initial connect
    # and any reconnects -- the plot window comes up immediately and just
    # waits (retrying in the background) if the board/relay isn't reachable
    # yet, rather than this script exiting.
    workers = [
        threading.Thread(target=tcp_thread, name="net",
                         args=(plot_in_q, plot_out_q, stop_event), daemon=True),
        threading.Thread(target=local_thread, name="local",
                         args=(plot_in_q, plot_out_q, stop_event), daemon=True),
    ]
    for t in workers:
        t.start()

    # Pay the one-time costs now, off the GUI thread, instead of on the
    # first Start: nk.ecg_simulate() of a 60 s buffer is ~0.9 s and the numba
    # compile another ~0.3 s, and both used to run inside the worker on the
    # first chunk -- holding the GIL, freezing the window, and making Start
    # look like it had done nothing.
    def _warm():
        try:
            import numpy as np
            from signal_gen import generate_ecg_chunk
            import local_proc
            generate_ecg_chunk(0)
            # One channel's worth is enough -- the numba compile is per
            # kernel, not per channel, and every pipeline that uses it goes
            # through the same one.
            local_proc.iir(np.zeros(8, dtype=">u4"),
                           local_proc.new_state(), {"shift": 4})
        except Exception as exc:                      # noqa: BLE001
            # Warming is an optimisation, never a requirement -- if it fails,
            # the worker just pays the cost itself the way it used to.
            print(f"[warmup] skipped: {exc}")
        finally:
            runctl.warm.set()
            print("[warmup] signal buffer and filter kernel ready")

    threading.Thread(target=_warm, name="warmup", daemon=True).start()

    next_frame = time.perf_counter()
    next_ui = next_frame

    try:
        while not stop_event.is_set():
            now = time.perf_counter()

            try:
                while True:
                    ch1, ch2 = plot_in_q.get_nowait()
                    plotter.update_input(ch1, ch2)
            except queue.Empty:
                pass

            try:
                while True:
                    ch1, ch2 = plot_out_q.get_nowait()
                    plotter.update_output(ch1, ch2)
            except queue.Empty:
                pass

            # GUI events are pumped on their OWN schedule, far faster than
            # the plot redraws. They used to be pumped only inside refresh()
            # -- so a click could wait a whole frame period to be seen, and
            # at a low FRAME_RATE the panel felt broken rather than slow.
            # flush_events() is 0.08 ms, so 100 Hz costs under 1% of a core
            # and makes the buttons respond immediately regardless of how
            # slowly the plot is drawing.
            if now >= next_ui:
                plotter.pump_events()
                ui_period = 1.0 / config.UI_POLL_RATE
                next_ui += ui_period
                if now - next_ui > ui_period:
                    next_ui = now + ui_period

            if now >= next_frame:
                plotter.refresh()
                # Read live each cycle (not cached once before the loop) so
                # a FRAME_RATE change from the control panel takes effect
                # on the very next frame -- same pattern as net.py's
                # SEND_RATE.
                period = 1.0 / config.FRAME_RATE
                next_frame += period
                # Drop a missed-frame backlog instead of replaying it. A
                # fixed increment alone means that after any stall (the old
                # ~1.1 s warm-up was one) the deadline sits many frames in
                # the past, and this branch then fires every loop pass,
                # drawing flat out until it catches up -- hundreds of ms of
                # an unresponsive window, at 100% CPU, for frames nobody will
                # ever see. Same failure and same fix as net.py's send
                # catch-up limiter.
                if now - next_frame > period:
                    next_frame = now + period

            time.sleep(0.001)

        print("workers stopped, exiting.")
    except KeyboardInterrupt:
        stop_event.set()
    finally:
        for t in workers:
            t.join(timeout=2.0)


if __name__ == "__main__":
    main()
