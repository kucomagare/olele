# Owns the TCP connection end-to-end: connect, send on schedule, receive/
# parse the echo, reconnect on drop -- mirrors the firmware's own
# tcp_client_error -> tcp_client_start() auto-reconnect.

import socket
import time
import queue

import collections

import config
import local_proc
import pipelines
import runctl
from config import HOST, PORT, RECONNECT_DELAY
from packet_format import (CONFIG_OP_READ, CONFIG_OP_WRITE, CONFIG_TYPE,
                            DATA_TYPE, LOG_ALL, METRICS_TYPE, PacketReceiver,
                            build_config_packet)
from sched import RateScheduler
from signal_gen import generate_signal_packet

# Receive-path sizing: the relay's blocking send() has a 2s timeout, so this
# loop must keep draining. One recv(4096)/pass + unconditional sleep used to
# cap intake at ~4-7 MB/s -- hit exactly at SEND_RATE=800 x CHUNK_SIZE=800.
RECV_BUF = 262144    # bytes per recv(), 64x the old 4096
RECV_BURST = 64      # max recv() calls per loop pass
PACKET_BURST = 64    # max packets reassembled per loop pass
SOCK_RCVBUF = 4 * 1024 * 1024

# Idle-sleep upper bound; the loop clamps it to time-until-next-packet.
# Needed above ~1750 pkt/s, where a "0.5ms" sleep really takes ~570us and
# would otherwise cap the send rate itself.
SEND_IDLE_SLEEP_S = 0.0005

# Config requests queued by the GUI, drained by the net thread. A Queue,
# not a bare variable, so a request can't be lost between passes.
config_out_q = queue.Queue(maxsize=16)

# Latest config read-back and metrics from the board, or None. Plain
# attribute assignment (atomic in CPython) -- GUI polls without a lock,
# worst case one stale refresh.
last_config = None
last_metrics = None

# Coarse link state for the panel's status line: idle (not started, or
# local mode), connecting (retrying), connected.
link_state = "idle"

# Seconds since the last inbound DATA packet while streaming (0 otherwise).
# WHY: a healthy send path proves nothing -- the relay silently discards
# bytes when it has no partner peer. On 2026-09-01 the client showed
# "1700 pkts/s, 0 drops" for minutes while the board was wedged and not
# even answering ping. Only the receive side knew, by being silent.
rx_stale_s = 0.0


def request_config(op=CONFIG_OP_READ, n_channels=0, shift=0, ctrl=0,
                   log_mask=LOG_ALL):
    """Queue a config packet for the board (called from the GUI thread).
    Returns False if the queue is full -- link is down, tell the caller
    rather than block the GUI."""
    try:
        config_out_q.put_nowait((op, n_channels, shift, ctrl, log_mask))
        return True
    except queue.Full:
        return False


def send_some(sock, view):
    """Push as much of `view` as the socket takes now; return the
    remainder (None if it all went out). Never blocks -- this loop also
    drains the receive side, so waiting here backs up the relay that
    filled the send buffer to begin with. The select(timeout=0.01) this
    replaces cost ~14 packets per stall at 1400 pkt/s.
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
    # TCP_NODELAY: with Nagle on at all three hops (here, the C++ relay,
    # the board's lwIP) every partial-segment packet waited for the
    # previous ACK -- a hard ~33 pkt/s ceiling regardless of SEND_RATE.
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    # Larger RCVBUF cushions a scheduling hiccup before it backs into the
    # relay's 2-second send timeout. Kernel may grant less; that's fine.
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, SOCK_RCVBUF)
    except OSError:
        pass
    sock.connect((HOST, PORT))
    sock.setblocking(False)
    return sock


def _may_run():
    """True when this thread owns the current mode and the session is
    started. Local channels are processed inline here (not by
    local_proc.local_thread) so both channels reach the plot in one tuple
    -- splitting them across threads would drift them apart on the shared
    time axis.
    """
    return runctl.is_running() and config.any_board()


def tcp_thread(plot_in_q, plot_out_q, stop_event):
    global link_state
    while not stop_event.is_set():
        if not _may_run():
            link_state = "idle"
            if not runctl.is_running():
                runctl.wait_for_start(0.1)   # blocks for real; flag is false
            else:
                # Started, but Local is selected -- stop_event.wait (not
                # wait_for_start, which would busy-loop once the flag is
                # already set and starve local_thread of GIL time).
                stop_event.wait(0.1)
            continue

        link_state = "connecting"
        try:
            sock = _connect()
            link_state = "connected"
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
            link_state = "idle" if not _may_run() else "connecting"
            try:
                sock.close()
            except OSError:
                pass


def _run_session(sock, plot_in_q, plot_out_q, stop_event):
    """One connection's worth of send/receive. Returns (instead of
    setting stop_event) on any connection loss, so the outer tcp_thread
    loop reconnects instead of the whole app exiting."""
    global last_config, last_metrics, rx_stale_s

    receiver = PacketReceiver()

    counter = 0
    # Reads config.SEND_RATE per chunk, so a rate change from the panel
    # takes effect on the very next packet rather than at the next reconnect.
    schedule = RateScheduler(lambda: config.SEND_RATE)

    # Per-channel filter memory for "local" channels, plus a FIFO of what
    # was sent: a locally-filtered channel needs the SENT chunk, but its
    # result has to leave here in the same tuple as the channels that came
    # BACK, and the board's reply lands packets later. The board echoes in
    # order and TCP preserves it, so plain FIFO is the whole matching rule.
    # maxlen bounds it if replies stop coming (board wedged, cable pulled).
    local_states = [pipelines.new_state(), pipelines.new_state()]
    sent_inputs = collections.deque(maxlen=256)

    packets_sent = 0
    send_stalls = 0
    # Generated and sent, but dropped before the PLOT queue -- the only
    # samples in the whole path that truly go missing, so a Log-buffer
    # dump taken mid-drop is short without saying so (and that dump is
    # what SAT draws conclusions from).
    plot_dropped = 0
    last_rx = time.perf_counter()
    # Remainder of a packet the socket couldn't take in one go -- must
    # finish before starting another: the wire is [type][length][payload]
    # frames with no resync marker, so a half-written one desyncs everything.
    pending = None
    # Whether `pending` is a data packet, so finishing it counts toward
    # the throughput line and finishing a config packet does not.
    pending_is_data = False
    last_report = time.time()

    while not stop_event.is_set():
        # Stop, or a switch to local mode, ends the session (tcp_thread's
        # finally closes the socket) rather than idling it open -- an
        # undrained socket trips the relay's 2s send timeout anyway.
        if not _may_run():
            print("[net] stopped, closing the connection")
            return

        now = time.perf_counter()

        # Work done this pass (send/recv/reassembly). Sending counts as
        # work too -- treating it as idle turned the sleep below into a
        # rate limiter; see that sleep for the numbers.
        did_work = False

        # Paused is not a stall -- see RateScheduler.hold().
        if not config.SEND_ENABLED:
            schedule.hold(now)

        # Watchdog only meaningful while actually streaming AND expecting
        # an echo -- with sending paused or receiving off, silence is correct.
        if config.SEND_ENABLED and config.RECEIVE_ENABLED:
            rx_stale_s = now - last_rx
        else:
            rx_stale_s = 0.0
            last_rx = now

        # Config requests go out ahead of stream data, unrated -- a
        # handful of bytes that must stay responsive under a saturated link.
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
                        pending_is_data = False
                    else:
                        did_work = True
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
                # Still blocked -- NOT progress (see the sleep at the
                # bottom): counting a refused write as work retries a full
                # socket at loop speed (measured: 74k stalls/s at 81% CPU).
                send_stalls += 1
            elif pending_is_data:
                # Only DATA counts toward pkts/s -- a config packet
                # finished across two passes used to be miscounted as
                # stream throughput.
                packets_sent += 1

        if pending is None and config.SEND_ENABLED and schedule.due(now):
            try:
                packet, ch1, ch2 = generate_signal_packet(counter)
                pending = send_some(sock, memoryview(packet))
                if pending is None:
                    packets_sent += 1
                    did_work = True
                else:
                    pending_is_data = True
                    send_stalls += 1
                if config.any_local():
                    # Only parked when something will actually consume it.
                    sent_inputs.append((ch1, ch2))
                try:
                    plot_in_q.put_nowait((ch1, ch2))
                except queue.Full:
                    plot_dropped += 1
            except OSError as e:
                print(f"[net] Connection lost while sending: {e}, reconnecting...")
                return

            counter += config.CHUNK_SIZE
            schedule.advance(now)

        t = time.time()
        elapsed = t - last_report
        if elapsed >= 1.0:
            backlog_dropped = schedule.take_dropped()
            note = f" (dropped {backlog_dropped} late)" if backlog_dropped else ""
            if send_stalls:
                # A few are normal; a lot means the far end (relay, link,
                # board) is the limit, not this loop.
                note += f" ({send_stalls} send-stalls)"
            if plot_dropped:
                note += f" ({plot_dropped} plot-drops -- a dump now is short)"
            if rx_stale_s > config.RX_WATCHDOG_S:
                # Sending can look flawless into a relay whose other peer
                # is gone -- say so, rather than report a perfect stream to nowhere.
                note += f"  [!] NOTHING RECEIVED for {rx_stale_s:.1f}s -- far end gone?"
            # Normalized by the window that actually elapsed, not assumed
            # 1000ms -- matches the firmware's own [S] line.
            print(f"Python sent: {packets_sent / elapsed:.0f} pkts/s{note}")
            packets_sent = 0
            send_stalls = 0
            plot_dropped = 0
            last_report = t

        if config.RECEIVE_ENABLED:
            # Drain until actually empty, not one buffer per pass -- see
            # the RECV_* comment above for why the single-read version
            # became a throughput limit.
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

            # A second, independent one-per-pass cap at the same order of
            # magnitude -- draining the socket alone would just move the
            # backlog into receiver's buffer.
            for _ in range(PACKET_BURST):
                received = receiver.next_packet()
                if received is None:
                    break
                did_work = True
                ptype, records = received
                if ptype == DATA_TYPE:
                    # Proof of life from the far end -- see rx_stale_s.
                    # Counted even for channels about to be discarded: the
                    # watchdog cares whether the BOARD answered, not
                    # whether we use what it said.
                    last_rx = time.perf_counter()
                    out1, out2 = records["ch1"], records["ch2"]
                    if config.any_local():
                        # Empty queue = no matching input (a reconnect
                        # dropped it) -- keep the board's samples rather
                        # than filter the wrong chunk.
                        src = sent_inputs.popleft() if sent_inputs else None
                        if src is not None:
                            if config.CH_MODE[0] == "local":
                                out1 = local_proc.process_channel(
                                    src[0], 0, local_states[0])
                            if config.CH_MODE[1] == "local":
                                out2 = local_proc.process_channel(
                                    src[1], 1, local_states[1])
                    try:
                        plot_out_q.put_nowait((out1, out2))
                    except queue.Full:
                        plot_dropped += 1
                elif ptype == CONFIG_TYPE and len(records):
                    last_config = dict(zip(records.dtype.names,
                                           (int(v) for v in records[0])))
                    print(f"[net] config read-back: {last_config}")
                elif ptype == METRICS_TYPE and len(records):
                    last_metrics = dict(zip(records.dtype.names,
                                            (int(v) for v in records[0])))

        # Sleep only when idle, never past the next send deadline. Two
        # measured traps: an unconditional sleep caps loop passes to
        # ~1kHz, turning the recv caps above into a throughput limit; a
        # FIXED sleep (really ~570us, not 500) is itself a rate limiter
        # above ~1750 pkt/s.
        if pending is not None:
            # Socket refused a full packet -- retrying at loop speed can't
            # help (measured: 74k stalls/s, 81% CPU at 2000 pkt/s
            # saturated). NOT clamped to the deadline: when the link is
            # the limit the deadline is always in the past, so the clamp
            # would come out 0 and spin.
            time.sleep(SEND_IDLE_SLEEP_S)
        elif not did_work:
            delay = SEND_IDLE_SLEEP_S
            if config.SEND_ENABLED:
                delay = min(delay, schedule.time_until())
            time.sleep(delay)
