# Owns the TCP connection end-to-end: connecting, sending the synthetic
# signal on a schedule, receiving/parsing the echo, and reconnecting on
# any drop -- mirrors the firmware's own tcp_client_error ->
# tcp_client_start() auto-reconnect (lwip_comm_client_raw.c), so a
# dropped link (board reset, cable pulled, relay restarted) doesn't
# require manually restarting this whole app.

import socket
import time
import queue

import config
from config import HOST, PORT, RECONNECT_DELAY
from packet_format import (CONFIG_OP_READ, CONFIG_OP_WRITE, CONFIG_TYPE,
                            DATA_TYPE, LOG_ALL, METRICS_TYPE, PacketReceiver,
                            build_config_packet)
from signal_gen import generate_signal_packet

# Receive-path sizing. These are not arbitrary: the relay (tcp_server_app.cpp)
# forwards with a blocking send() capped at SEND_TIMEOUT_SEC = 2, so if this
# loop stops draining its socket for two seconds the relay drops the
# connection outright. The old code did one recv(4096) per loop pass and then
# slept unconditionally, which capped intake at roughly 4-7 MB/s -- fine at
# small chunk sizes, and exactly the wall hit at SEND_RATE=800 x CHUNK_SIZE=800
# (7.68 MB/s each way).
RECV_BUF = 262144    # bytes per recv() -- 64x the old 4096
RECV_BURST = 64      # max recv() calls per loop pass, so a fast sender
                     # cannot starve the send path below
PACKET_BURST = 64    # max packets reassembled per loop pass, same reasoning
SOCK_RCVBUF = 4 * 1024 * 1024

# How much send backlog the scheduler will replay after a stall before giving
# up on the rest. Expressed as a DURATION, not a packet count: the point is to
# tell routine jitter apart from a real stall, and that boundary lives in
# milliseconds, not packets. A packet count means the threshold shrinks as
# SEND_RATE rises -- at 1400 pkt/s the original 8 packets worked out to 5.7 ms,
# which is less than a single plot refresh (~8.8 ms measured), so ordinary
# frames were tripping a limiter meant for multi-second stalls and losing 7.6%
# of the stream to it.
#
# 50 ms comfortably absorbs a plot refresh, a GC pause or a scheduler slice
# while still abandoning anything pathological. The floor keeps it sane at low
# SEND_RATE, where 50 ms can be less than one packet period.
SEND_CATCHUP_MAX_S = 0.05
SEND_CATCHUP_MIN_PKTS = 8

# Config requests queued by the GUI, drained by the net thread. A Queue rather
# than a bare variable because the GUI thread writes it and the net thread
# reads it, and a request must not be lost if two arrive between passes.
config_out_q = queue.Queue(maxsize=16)

# Latest config read-back and metrics from the board, or None. Plain attribute
# assignment, which is atomic in CPython, so the GUI can poll them without a
# lock -- worst case it renders one refresh out of date.
last_config = None
last_metrics = None


def request_config(op=CONFIG_OP_READ, n_channels=0, shift=0, ctrl=0,
                   log_mask=LOG_ALL):
    """Queue a config packet for the board. Called from the GUI thread.

    Returns False if the queue is full, which means the link is down and
    requests are piling up -- better to tell the caller than to block the GUI.
    """
    try:
        config_out_q.put_nowait((op, n_channels, shift, ctrl, log_mask))
        return True
    except queue.Full:
        return False


def send_some(sock, view):
    """Push as much of `view` as the socket will take right now; return what
    is left over (None when it all went out).

    Deliberately never blocks. This runs in the same loop that drains the
    receive side, so waiting here for the send buffer to open stalls
    reception too -- which backs up the relay, which is what filled the send
    buffer to begin with. The version this replaces called
    select(..., timeout=0.01) on a full buffer: a 10 ms stop in a loop whose
    packet period at 1400 pkt/s is 0.71 ms, i.e. ~14 packets lost per
    occurrence, and it showed up as the process missing its rate while
    sitting at under half a core.
    """
    while view:
        try:
            sent = sock.send(view)
        except BlockingIOError:
            return view
        if sent == 0:
            return view
        view = view[sent:]
    return None


def _connect():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # Disable Nagle: each packet is 4 + 6*CHUNK_SIZE bytes and so ends in a
    # partial segment, which Nagle holds back until the previous data is
    # ACKed. With it on at all three hops (here, the C++ relay, and the
    # board's lwIP) the pipeline degenerated into one packet per round
    # trip -- a hard ~33 pkt/s ceiling regardless of SEND_RATE.
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    # Ask for a large kernel receive buffer so a scheduling hiccup here has
    # somewhere to absorb into instead of immediately backing up into the
    # relay's 2-second send timeout. The kernel may grant less (and may halve
    # what it reports); that is fine, it is a cushion, not a guarantee.
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, SOCK_RCVBUF)
    except OSError:
        pass
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
    next_send = time.perf_counter()

    packets_sent = 0
    backlog_dropped = 0
    send_stalls = 0
    # Remainder of a packet the socket could not take in one go. A partial
    # packet must be finished before another is started -- the wire format is
    # a stream of [type][length][payload] frames with no resync marker, so a
    # half-written one would desynchronise the relay and the board.
    pending = None
    last_report = time.time()

    while not stop_event.is_set():
        now = time.perf_counter()

        # Config requests go out ahead of stream data and are not rate-limited:
        # they are a handful of bytes and the whole point is that they stay
        # responsive while the data stream is saturating the link.
        if pending is None:
            try:
                op, nch, sh, ctrl, lmask = config_out_q.get_nowait()
            except queue.Empty:
                pass
            else:
                try:
                    leftover = send_some(sock, memoryview(
                        build_config_packet(op, nch, sh, ctrl, lmask)))
                    if leftover is not None:
                        pending = leftover
                except OSError as e:
                    print(f"[net] Connection lost sending config: {e}, reconnecting...")
                    return

        # Flush a leftover before starting anything new.
        if pending is not None:
            try:
                pending = send_some(sock, pending)
            except OSError as e:
                print(f"[net] Connection lost while sending: {e}, reconnecting...")
                return
            if pending is not None:
                send_stalls += 1
            else:
                packets_sent += 1

        if pending is None and config.SEND_ENABLED and now >= next_send:
            try:
                packet, ch1, ch2 = generate_signal_packet(counter, now)
                pending = send_some(sock, memoryview(packet))
                if pending is None:
                    packets_sent += 1
                else:
                    send_stalls += 1
                try:
                    plot_in_q.put_nowait((ch1, ch2))
                except queue.Full:
                    pass
            except OSError as e:
                print(f"[net] Connection lost while sending: {e}, reconnecting...")
                return

            counter += config.CHUNK_SIZE
            # Read live each cycle (not cached once before the loop) so a
            # SEND_RATE change from the control panel takes effect on the
            # very next send.
            period = 1.0 / config.SEND_RATE
            next_send += period

            # Fixed-increment schedule: after any stall next_send sits in the
            # past, and this loop then fires one packet per iteration until it
            # catches up -- replaying the whole backlog at loop speed, far
            # above SEND_RATE, for as long again as the stall lasted.
            # Measured: a ~10 s stall at 1300 pkt/s produced ~30 s of
            # 1600-2250 pkt/s before settling back to 1300, which is not a
            # rate error but does wreck any throughput reading taken during
            # it. A few packets of catch-up are worth keeping (they absorb
            # ordinary jitter and hold the long-run average honest); beyond
            # that, abandon the backlog -- packets due seconds ago cannot be
            # sent "on time" retroactively, and replaying them only overruns
            # the board.
            catchup_limit = max(SEND_CATCHUP_MIN_PKTS * period, SEND_CATCHUP_MAX_S)
            if now - next_send > catchup_limit:
                backlog_dropped += int((now - next_send) / period)
                next_send = now + period

        t = time.time()
        elapsed = t - last_report
        if elapsed >= 1.0:
            # The dropped count is the interesting half: a nonzero value is
            # the direct signal that this loop stalled, and how badly.
            note = f" (dropped {backlog_dropped} late)" if backlog_dropped else ""
            # send_stalls counts passes where the socket would not take the
            # whole packet. A few are normal; a lot means the far end (relay,
            # link, or board) is the limit, not this loop.
            if send_stalls:
                note += f" ({send_stalls} send-stalls)"
            # Normalized by the window that actually elapsed, not assumed to
            # be exactly 1000 ms: the check runs once per loop pass, so the
            # window always overshoots by a varying amount. The firmware's
            # [S] line has always done this (see main.c); this one did not,
            # which is why a dead-on 1400 pkt/s setpoint kept alternating
            # 1400/1401 -- measurement noise, not the stream.
            print(f"Python sent: {packets_sent / elapsed:.0f} pkts/s{note}")
            packets_sent = 0
            backlog_dropped = 0
            send_stalls = 0
            last_report = t

        did_work = False

        if config.RECEIVE_ENABLED:
            # Drain the socket until it is actually empty rather than taking
            # one buffer per loop pass -- see the RECV_* comment above for why
            # the single-read version became a throughput limit.
            try:
                for _ in range(RECV_BURST):
                    data = sock.recv(RECV_BUF)
                    if not data:
                        print("[net] Server closed the connection, reconnecting...")
                        return
                    receiver.push(data)
                    did_work = True
                    if len(data) < RECV_BUF:
                        break   # short read == socket drained
            except BlockingIOError:
                pass
            except OSError as e:
                print(f"[net] Connection lost while receiving: {e}, reconnecting...")
                return

            # Reassembly was a second, independent one-per-pass cap at the
            # same order of magnitude, so draining the socket alone would just
            # have moved the backlog into receiver's buffer.
            for _ in range(PACKET_BURST):
                received = receiver.next_packet()
                if received is None:
                    break
                did_work = True
                ptype, records = received
                if ptype == DATA_TYPE:
                    try:
                        plot_out_q.put_nowait((records["ch1"], records["ch2"]))
                    except queue.Full:
                        pass
                elif ptype == CONFIG_TYPE and len(records):
                    global last_config
                    last_config = dict(zip(records.dtype.names,
                                           (int(v) for v in records[0])))
                    print(f"[net] config read-back: {last_config}")
                elif ptype == METRICS_TYPE and len(records):
                    global last_metrics
                    last_metrics = dict(zip(records.dtype.names,
                                            (int(v) for v in records[0])))

        # Yield only when there was nothing to do. The unconditional sleep
        # here put a hard ~1 kHz ceiling on loop passes (Linux rounds a 0.5 ms
        # sleep up to roughly 1 ms), which is what turned the two caps above
        # into a bandwidth limit instead of just a latency one.
        if not did_work:
            time.sleep(0.0005)
