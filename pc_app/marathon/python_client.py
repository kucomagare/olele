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
    # owns config.PROCESSING_MODE -- so switching between board and local is
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

    next_frame = time.perf_counter()

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

            if now >= next_frame:
                plotter.refresh()
                # Read live each cycle (not cached once before the loop) so
                # a FRAME_RATE change from the control panel takes effect
                # on the very next frame -- same pattern as net.py's
                # SEND_RATE.
                next_frame += 1.0 / config.FRAME_RATE

            time.sleep(0.001)

        print("workers stopped, exiting.")
    except KeyboardInterrupt:
        stop_event.set()
    finally:
        for t in workers:
            t.join(timeout=2.0)


if __name__ == "__main__":
    main()
