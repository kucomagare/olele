import socket
import struct
import select
import numpy as np
import time
import threading
import queue

import matplotlib
matplotlib.use("TkAgg")
import matplotlib.pyplot as plt


BOARD_CONNECTED = False  # True: connect over the real board network (192.168.1.100)
                        # False: loop back to the C++ server on this machine, no board needed

HOST = "192.168.1.100" if BOARD_CONNECTED else "127.0.0.1"
PORT = 5001

PLOT_BUFFER = 10000
FRAME_RATE = 24
SEND_RATE = 220

CHUNK_SIZE = 2000
TRIANGLE_PERIOD = 250

AMP_BASE = 2000
AMP_OSC  = 500
AMP_FREQ = 0.05

SEND_ENABLED = True
RECEIVE_ENABLED = True

PLOT_MIN = 0
PLOT_MAX = 5500


class PacketReceiver:
    def __init__(self):
        self.buffer = bytearray()

    def push(self, data):
        self.buffer.extend(data)

    def next_packet(self):
        if len(self.buffer) < 4:
            return None

        type_r, len_r = struct.unpack("!HH", self.buffer[:4])
        total_needed = 4 + len_r * 2

        if len(self.buffer) < total_needed:
            return None

        payload_bytes = self.buffer[4:total_needed]
        values = struct.unpack("!" + "H"*len_r, payload_bytes)

        del self.buffer[:total_needed]
        return np.array(values, dtype=np.uint16)


def generate_triangle_chunk(counter, now):
    amp = AMP_BASE + AMP_OSC * np.sin(2 * np.pi * AMP_FREQ * now)
    t = (np.arange(CHUNK_SIZE) + counter) % TRIANGLE_PERIOD
    tri = amp * (2 * np.abs(t - TRIANGLE_PERIOD/2) / TRIANGLE_PERIOD)
    return tri.astype(np.uint16)


def send_all(sock, data):
    view = memoryview(data)
    while view:
        try:
            sent = sock.send(view)
            view = view[sent:]
        except BlockingIOError:
            select.select([], [sock], [], 0.01)


def send_signal(sock, counter, now):
    payload = generate_triangle_chunk(counter, now)
    header = struct.pack("!HH", 1, CHUNK_SIZE)
    data = struct.pack("!" + "H"*CHUNK_SIZE, *payload)
    packet = header + data

    send_all(sock, packet)

    return payload


class DualPlot:
    def __init__(self, buffer_size):
        self.buffer_size = buffer_size

        plt.ion()
        self.fig, self.ax = plt.subplots(1, 1)
        plt.show(block=False)

        self.in_buffer  = np.zeros(buffer_size, dtype=np.uint16)
        self.out_buffer = np.zeros(buffer_size, dtype=np.uint16)

        self.line_in,  = self.ax.plot(self.in_buffer,  color="blue", label="Input")
        self.line_out, = self.ax.plot(self.out_buffer, color="red",  label="Output")

        self.ax.set_title("Streaming Input & Output")
        self.ax.legend()
        self.ax.set_ylim(PLOT_MIN, PLOT_MAX)

    def update_input(self, values):
        n = len(values)
        if n == 0:
            return
        self.in_buffer = np.roll(self.in_buffer, -n)
        self.in_buffer[-n:] = values
        self.line_in.set_ydata(self.in_buffer)

    def update_output(self, values):
        n = len(values)
        if n == 0:
            return
        self.out_buffer = np.roll(self.out_buffer, -n)
        self.out_buffer[-n:] = values
        self.line_out.set_ydata(self.out_buffer)

    def refresh(self):
        self.fig.canvas.draw()
        self.fig.canvas.flush_events()
        plt.pause(0.001)


def tcp_thread(sock, plot_in_q, plot_out_q, stop_event):
    receiver = PacketReceiver()

    counter = 0
    SEND_PERIOD = 1.0 / SEND_RATE
    next_send = time.perf_counter()

    packets_sent = 0
    last_report = time.time()

    while not stop_event.is_set():
        now = time.perf_counter()

        if SEND_ENABLED and now >= next_send:
            try:
                sent_signal = send_signal(sock, counter, now)
                packets_sent += 1
                try:
                    plot_in_q.put_nowait(sent_signal)
                except queue.Full:
                    pass
            except BlockingIOError:
                pass
            except OSError as e:
                print(f"[tcp_thread] Connection lost while sending: {e}")
                stop_event.set()
                break

            counter += CHUNK_SIZE
            next_send += SEND_PERIOD

        t = time.time()
        if t - last_report >= 1.0:
            print("Python sent:", packets_sent, "pkts/s")
            packets_sent = 0
            last_report = t

        if RECEIVE_ENABLED:
            try:
                data = sock.recv(4096)
                if not data:
                    print("[tcp_thread] Server closed the connection")
                    stop_event.set()
                    break
                receiver.push(data)
            except BlockingIOError:
                pass
            except OSError as e:
                print(f"[tcp_thread] Connection lost while receiving: {e}")
                stop_event.set()
                break

            received_signal = receiver.next_packet()
            if received_signal is not None:
                try:
                    plot_out_q.put_nowait(received_signal)
                except queue.Full:
                    pass

        time.sleep(0.0005)


def main():
    print("Connecting...")
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.connect((HOST, PORT))
    except OSError as e:
        print(f"Could not connect to {HOST}:{PORT} ({e})")
        return
    sock.setblocking(False)
    print("Connected!")

    plotter = DualPlot(PLOT_BUFFER)

    plot_in_q  = queue.Queue(maxsize=1000)
    plot_out_q = queue.Queue(maxsize=1000)

    stop_event = threading.Event()

    t_tcp = threading.Thread(target=tcp_thread,
                             args=(sock, plot_in_q, plot_out_q, stop_event),
                             daemon=True)
    t_tcp.start()

    FRAME_PERIOD = 1.0 / FRAME_RATE
    next_frame = time.perf_counter()

    try:
        while not stop_event.is_set():
            now = time.perf_counter()

            try:
                while True:
                    values = plot_in_q.get_nowait()
                    plotter.update_input(values)
            except queue.Empty:
                pass

            try:
                while True:
                    values = plot_out_q.get_nowait()
                    plotter.update_output(values)
            except queue.Empty:
                pass

            if now >= next_frame:
                plotter.refresh()
                next_frame += FRAME_PERIOD

            time.sleep(0.001)

        print("tcp_thread stopped, exiting.")
    except KeyboardInterrupt:
        stop_event.set()
    finally:
        t_tcp.join(timeout=2.0)
        sock.close()


if __name__ == "__main__":
    main()
