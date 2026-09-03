import threading
import queue
import time

import config
import runctl
from plot import DualPlot
from net import tcp_thread
from local_proc import local_thread


def main():
    # Gate cleared at import -- window comes up idle. AUTOSTART opts out.
    if config.AUTOSTART:
        runctl.start()

    plotter = DualPlot(config.PLOT_BUFFER)

    plot_in_q  = queue.Queue(maxsize=1000)
    plot_out_q = queue.Queue(maxsize=1000)

    stop_event = threading.Event()

    # Both workers start unconditionally, each idling unless it owns the
    # current per-channel mode -- switching board/local is then just an
    # attribute write. net.tcp_thread owns the socket (incl. reconnects),
    # so the window comes up even if the board/relay isn't reachable yet.
    workers = [
        threading.Thread(target=tcp_thread, name="net",
                         args=(plot_in_q, plot_out_q, stop_event), daemon=True),
        threading.Thread(target=local_thread, name="local",
                         args=(plot_in_q, plot_out_q, stop_event), daemon=True),
    ]
    for t in workers:
        t.start()

    # Pay the one-time costs now, off the GUI thread: ~0.9s ecg_simulate()
    # + ~0.3s numba compile used to run on first Start, freezing the window.
    def _warm():
        try:
            import numpy as np
            from signal_gen import generate_ecg_chunk
            import pipelines
            generate_ecg_chunk(0)
            # One channel is enough -- numba compiles per kernel, and every
            # pipeline using it shares the same one.
            pipelines.iir(np.zeros(8, dtype=">u4"),
                          pipelines.new_state(), {"shift": 4})
        except Exception as exc:                      # noqa: BLE001
            # Optimisation, not a requirement -- on failure the worker
            # just pays the cost itself, as before.
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

            # GUI events pumped on their own faster schedule -- pumping
            # only inside refresh() meant a click could wait a whole frame.
            if now >= next_ui:
                plotter.pump_events()
                ui_period = 1.0 / config.UI_POLL_RATE
                next_ui += ui_period
                if now - next_ui > ui_period:
                    next_ui = now + ui_period

            if now >= next_frame:
                plotter.refresh()
                # Read live each cycle so a FRAME_RATE change takes effect
                # next frame -- same pattern as net.py's SEND_RATE.
                period = 1.0 / config.FRAME_RATE
                next_frame += period
                # Drop a missed-frame backlog rather than replay it at
                # 100% CPU for frames nobody will see -- same fix as
                # net.py's send limiter.
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
