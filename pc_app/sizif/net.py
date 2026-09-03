# Owns the TCP connection: connect, send on schedule, receive/parse echo,
# auto-reconnect on drop (mirrors firmware's tcp_client_error -> tcp_client_start()).

import socket
import select
import time
import queue

import config
from config import HOST, PORT, RECONNECT_DELAY
from packet_format import PacketReceiver
from signal_gen import generate_signal_packet


def send_all(sock, data):
    view = memoryview(data)
    while view:
        try:
            sent = sock.send(view)
            view = view[sent:]
        except BlockingIOError:
            select.select([], [sock], [], 0.01)


def _connect():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # Disable Nagle -- left on (all 3 hops) it capped throughput at ~33 pkt/s
    # regardless of SEND_RATE.
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    sock.connect((HOST, PORT))
    sock.setblocking(False)
    return sock


def tcp_thread(plot_in_q, plot_out_q, stop_event):
    while not stop_event.is_set():
        try:
            sock = _connect()
            print(f"Connected to {HOST}:{PORT}")
        except OSError as e:
            print(f"[net] Could not connect to {HOST}:{PORT} ({e}), "
                  f"retrying in {RECONNECT_DELAY}s")
            if stop_event.wait(RECONNECT_DELAY):
                break
            continue

        try:
            _run_session(sock, plot_in_q, plot_out_q, stop_event)
        finally:
            try:
                sock.close()
            except OSError:
                pass


def _run_session(sock, plot_in_q, plot_out_q, stop_event):
    """One connection's send/receive; returns on loss so tcp_thread reconnects."""
    receiver = PacketReceiver()

    counter = 0
    next_send = time.perf_counter()

    packets_sent = 0
    last_report = time.time()

    while not stop_event.is_set():
        now = time.perf_counter()

        if config.SEND_ENABLED and now >= next_send:
            try:
                packet, ch1, ch2 = generate_signal_packet(counter, now)
                send_all(sock, packet)
                packets_sent += 1
                try:
                    plot_in_q.put_nowait((ch1, ch2))
                except queue.Full:
                    pass
            except BlockingIOError:
                pass
            except OSError as e:
                print(f"[net] Connection lost while sending: {e}, reconnecting...")
                return

            counter += config.CHUNK_SIZE
            # Read live so a panel SEND_RATE change takes effect next send.
            next_send += 1.0 / config.SEND_RATE

        t = time.time()
        if t - last_report >= 1.0:
            print("Python sent:", packets_sent, "pkts/s")
            packets_sent = 0
            last_report = t

        if config.RECEIVE_ENABLED:
            try:
                data = sock.recv(4096)
                if not data:
                    print("[net] Server closed the connection, reconnecting...")
                    return
                receiver.push(data)
            except BlockingIOError:
                pass
            except OSError as e:
                print(f"[net] Connection lost while receiving: {e}, reconnecting...")
                return

            received = receiver.next_packet()
            if received is not None:
                try:
                    plot_out_q.put_nowait((received["ch1"], received["ch2"]))
                except queue.Full:
                    pass

        time.sleep(0.0005)
