import socket
import struct
import select
import json
import numpy as np
import time
import threading
import queue
from pathlib import Path

import matplotlib
matplotlib.use("TkAgg")
import matplotlib.pyplot as plt


BOARD_CONNECTED = True  # True: connect over the real board network (192.168.1.100)
                        # False: loop back to the C++ server on this machine, no board needed

HOST = "192.168.1.100" if BOARD_CONNECTED else "127.0.0.1"
PORT = 5001

PLOT_BUFFER = 1000
FRAME_RATE = 24
SEND_RATE = 10

CHUNK_SIZE = 5  # was 2000 before ts+ch1+ch2 tripled bytes/sample (2 -> 6);
                  # 500*6+4=3004 bytes fits lwIP's TCP_SND_BUF=8192 with
                  # margin. Provisional for testing the new packet logic --
                  # revisit once throughput tuning resumes (see README).
TRIANGLE_PERIOD = 250

AMP_BASE = 2000
AMP_OSC  = 500
AMP_FREQ = 0.05

SEND_ENABLED = True
RECEIVE_ENABLED = True

PLOT_MIN = 0
PLOT_MAX = 5500


# ============================================================
# Packet/sample structure -- loaded from shared/packet_format.json, the
# single source of truth also used to generate packet_format.h for the
# firmware and the C++ relay (see shared/gen_packet_header.py). Change
# field widths/signedness there, not here.
# ============================================================

PACKET_FORMAT_PATH = Path(__file__).resolve().parent.parent.parent / "shared" / "packet_format.json"
with open(PACKET_FORMAT_PATH) as _f:
    PACKET_FORMAT = json.load(_f)

# (bits, signed) -> numpy dtype string. Big-endian ('>') to match the wire
# format directly -- numpy handles byte order transparently for arithmetic,
# plotting, etc., so there's no need for a separate "native" table.
NUMPY_DTYPE = {
    (8, False):  ">u1", (8, True):  ">i1",
    (16, False): ">u2", (16, True): ">i2",
    (32, False): ">u4", (32, True): ">i4",
}


def _build_dtype(fields):
    return np.dtype([(f["name"], NUMPY_DTYPE[(f["bits"], f["signed"])]) for f in fields])


PACKET_TYPES  = {int(k): v for k, v in PACKET_FORMAT["packet_types"].items()}
PACKET_DTYPES = {t: _build_dtype(v["fields"]) for t, v in PACKET_TYPES.items()}
PACKET_RECORD_SIZE = {t: d.itemsize for t, d in PACKET_DTYPES.items()}

DATA_TYPE  = next(t for t, v in PACKET_TYPES.items() if v["name"] == "data")
DATA_DTYPE = PACKET_DTYPES[DATA_TYPE]

_TS_BITS   = next(f["bits"] for f in PACKET_TYPES[DATA_TYPE]["fields"] if f["name"] == "ts")
TS_MODULUS = 1 << _TS_BITS

CH1_DTYPE = DATA_DTYPE.fields["ch1"][0]
CH2_DTYPE = DATA_DTYPE.fields["ch2"][0]


class PacketReceiver:
    def __init__(self):
        self.buffer = bytearray()

    def push(self, data):
        self.buffer.extend(data)

    def next_packet(self):
        if len(self.buffer) < 4:
            return None

        type_r, count = struct.unpack("!HH", self.buffer[:4])
        record_size = PACKET_RECORD_SIZE.get(type_r)

        if record_size is None:
            # Unknown type -- can't know how many body bytes belong to it.
            # Drop just the header and hope the stream resyncs (mirrors the
            # firmware's own best-effort recovery for the same situation).
            del self.buffer[:4]
            return None

        total_needed = 4 + count * record_size
        if len(self.buffer) < total_needed:
            return None

        body = bytes(self.buffer[4:total_needed])
        del self.buffer[:total_needed]

        if type_r != DATA_TYPE:
            # Other packet types (e.g. "config") aren't acted on yet --
            # placeholder, nothing to plot.
            return None

        return np.frombuffer(body, dtype=DATA_DTYPE)


def generate_triangle_chunk(counter, now, dtype=np.uint16, amp_scale=1.0):
    amp = amp_scale * (AMP_BASE + AMP_OSC * np.sin(2 * np.pi * AMP_FREQ * now))
    t = (np.arange(CHUNK_SIZE) + counter) % TRIANGLE_PERIOD
    tri = amp * (2 * np.abs(t - TRIANGLE_PERIOD/2) / TRIANGLE_PERIOD)
    return tri.astype(dtype)


def send_all(sock, data):
    view = memoryview(data)
    while view:
        try:
            sent = sock.send(view)
            view = view[sent:]
        except BlockingIOError:
            select.select([], [sock], [], 0.01)


def build_data_packet(ts_start, ch1, ch2):
    n = len(ch1)
    rec = np.zeros(n, dtype=DATA_DTYPE)
    rec["ts"]  = (np.arange(n, dtype=np.int64) + ts_start) % TS_MODULUS
    rec["ch1"] = ch1
    rec["ch2"] = ch2
    header = struct.pack("!HH", DATA_TYPE, n)
    return header + rec.tobytes()


def send_signal(sock, counter, now):
    # ch2 uses a smaller amplitude than ch1 (same generator/shape
    # otherwise) purely so the two are visually distinguishable on the
    # plot for now -- placeholder until each channel gets real, distinct
    # signal generation.
    ch1 = generate_triangle_chunk(counter, now, CH1_DTYPE, amp_scale=1.0)
    ch2 = generate_triangle_chunk(counter, now, CH2_DTYPE, amp_scale=0.5)

    packet = build_data_packet(counter, ch1, ch2)
    send_all(sock, packet)

    return ch1, ch2


class DualPlot:
    def __init__(self, buffer_size):
        self.buffer_size = buffer_size

        plt.ion()
        self.fig, self.ax = plt.subplots(1, 1)
        plt.show(block=False)

        self.ch1_in  = np.zeros(buffer_size, dtype=CH1_DTYPE)
        self.ch2_in  = np.zeros(buffer_size, dtype=CH2_DTYPE)
        self.ch1_out = np.zeros(buffer_size, dtype=CH1_DTYPE)
        self.ch2_out = np.zeros(buffer_size, dtype=CH2_DTYPE)

        self.line_ch1_in,  = self.ax.plot(self.ch1_in,  color="blue",       label="ch1 in")
        self.line_ch2_in,  = self.ax.plot(self.ch2_in,  color="dodgerblue", label="ch2 in", linestyle="--")
        self.line_ch1_out, = self.ax.plot(self.ch1_out, color="red",        label="ch1 out")
        self.line_ch2_out, = self.ax.plot(self.ch2_out, color="orangered",  label="ch2 out", linestyle="--")

        self.ax.set_title("Streaming Input & Output (ch1/ch2)")
        self.ax.legend()
        self.ax.set_ylim(PLOT_MIN, PLOT_MAX)

    @staticmethod
    def _rolled(buf, values):
        n = len(values)
        if n == 0:
            return buf
        buf = np.roll(buf, -n)
        buf[-n:] = values
        return buf

    def update_input(self, ch1, ch2):
        self.ch1_in = self._rolled(self.ch1_in, ch1)
        self.ch2_in = self._rolled(self.ch2_in, ch2)
        self.line_ch1_in.set_ydata(self.ch1_in)
        self.line_ch2_in.set_ydata(self.ch2_in)

    def update_output(self, ch1, ch2):
        self.ch1_out = self._rolled(self.ch1_out, ch1)
        self.ch2_out = self._rolled(self.ch2_out, ch2)
        self.line_ch1_out.set_ydata(self.ch1_out)
        self.line_ch2_out.set_ydata(self.ch2_out)

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

            received = receiver.next_packet()
            if received is not None:
                try:
                    plot_out_q.put_nowait((received["ch1"], received["ch2"]))
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
