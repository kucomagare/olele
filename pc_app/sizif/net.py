# Owns the TCP connection end-to-end: connecting, sending the synthetic
# signal on a schedule, receiving/parsing the echo, and reconnecting on
# any drop -- mirrors the firmware's own tcp_client_error ->
# tcp_client_start() auto-reconnect (lwip_comm_client_raw.c), so a
# dropped link (board reset, cable pulled, relay restarted) doesn't
# require manually restarting this whole app.

import socket
import select
import time
import queue

from config import HOST, PORT, SEND_RATE, CHUNK_SIZE, SEND_ENABLED, RECEIVE_ENABLED, RECONNECT_DELAY
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
    # Disable Nagle: each packet is 4 + 6*CHUNK_SIZE bytes and so ends in a
    # partial segment, which Nagle holds back until the previous data is
    # ACKed. With it on at all three hops (here, the C++ relay, and the
    # board's lwIP) the pipeline degenerated into one packet per round
    # trip -- a hard ~33 pkt/s ceiling regardless of SEND_RATE.
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
    """One connection's worth of send/receive. Returns (instead of
    setting stop_event) on any connection loss, so the outer tcp_thread
    loop reconnects instead of the whole app exiting."""
    receiver = PacketReceiver()

    counter = 0
    send_period = 1.0 / SEND_RATE
    next_send = time.perf_counter()

    packets_sent = 0
    last_report = time.time()

    while not stop_event.is_set():
        now = time.perf_counter()

        if SEND_ENABLED and now >= next_send:
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

            counter += CHUNK_SIZE
            next_send += send_period

        t = time.time()
        if t - last_report >= 1.0:
            print("Python sent:", packets_sent, "pkts/s")
            packets_sent = 0
            last_report = t

        if RECEIVE_ENABLED:
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
