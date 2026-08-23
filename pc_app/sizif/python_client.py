import threading
import queue
import time

import config
from plot import DualPlot
from net import tcp_thread


def main():
    plotter = DualPlot(config.PLOT_BUFFER)

    plot_in_q  = queue.Queue(maxsize=1000)
    plot_out_q = queue.Queue(maxsize=1000)

    stop_event = threading.Event()

    # net.tcp_thread owns the socket entirely, including the initial
    # connect and any reconnects -- the plot window comes up immediately
    # and just waits (retrying in the background) if the board/relay
    # isn't reachable yet, rather than this script exiting.
    t_tcp = threading.Thread(target=tcp_thread,
                             args=(plot_in_q, plot_out_q, stop_event),
                             daemon=True)
    t_tcp.start()

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

        print("tcp_thread stopped, exiting.")
    except KeyboardInterrupt:
        stop_event.set()
    finally:
        t_tcp.join(timeout=2.0)


if __name__ == "__main__":
    main()
