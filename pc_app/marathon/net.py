# Owns the TCP connection end-to-end: connecting, sending the synthetic
# signal on a schedule, receiving/parsing the echo, and reconnecting on
# any drop -- mirrors the firmware's own tcp_client_error ->
# tcp_client_start() auto-reconnect (lwip_comm_client_raw.c), so a
# dropped link (board reset, cable pulled, relay restarted) doesn't
# require manually restarting this whole app.

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

# How long to idle when a loop pass found nothing to do. Only ever an UPPER
# bound: the loop clamps it to the time remaining before the next packet is
# due, because this value is longer than the packet period above ~1750 pkt/s
# and would otherwise cap the send rate itself (measured: a nominal 0.5 ms
# sleep really takes ~570 us).
SEND_IDLE_SLEEP_S = 0.0005

# Config requests queued by the GUI, drained by the net thread. A Queue rather
# than a bare variable because the GUI thread writes it and the net thread
# reads it, and a request must not be lost if two arrive between passes.
config_out_q = queue.Queue(maxsize=16)

# Latest config read-back and metrics from the board, or None. Plain attribute
# assignment, which is atomic in CPython, so the GUI can poll them without a
# lock -- worst case it renders one refresh out of date.
last_config = None
last_metrics = None

# Coarse link state for the panel's status line, so "started but nothing is
# happening" can tell you WHY: idle (not started, or local mode),
# connecting (retrying), connected.
link_state = "idle"

# Seconds since the last inbound DATA packet, while streaming; 0 when not
# streaming or not expecting any. Read by the panel's status line.
#
# WHY THIS EXISTS. A healthy-looking send path is not evidence the far end is
# alive. The relay accepts every byte and silently discards it when it has no
# partner peer, so on 2026-09-01 the client printed "1700 pkts/s" with zero
# drops and zero send-stalls for several minutes while the board was wedged
# and not even answering ping. Every send-side counter was telling the truth
# and the conclusion was still wrong.
#
# The receive side knew: nothing had come back. It just had no way to say so.
rx_stale_s = 0.0


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


def _may_run():
    """True when this thread owns the current mode and the session is
    started. Both are checked in the same place because both mean the same
    thing to the socket: do not have one open.

    ANY channel on the board is enough to need the socket. The locally
    processed ones are done inline in this thread rather than by
    local_proc.local_thread, because both channels have to arrive at the plot
    in the same tuple -- handing half of it to another thread would mean
    ch1 and ch2 drifting apart on a shared time axis.
    """
    return runctl.is_running() and config.any_board()


def tcp_thread(plot_in_q, plot_out_q, stop_event):
    global link_state
    while not stop_event.is_set():
        # Nothing is connected -- not even attempted -- until Start is
        # pressed and this mode is selected. Waiting on the gate rather than
        # polling means a press takes effect immediately, and an idle app
        # makes no connection attempts to time out or log.
        if not _may_run():
            link_state = "idle"
            if not runctl.is_running():
                # Not started: block on the gate. wait_for_start(0.1) genuinely
                # sleeps here, since the flag is false, and returns the instant
                # Start is pressed.
                runctl.wait_for_start(0.1)
            else:
                # Started, but this thread does not own the mode (Local is
                # selected). Event.wait() returns immediately once the flag is
                # already true -- calling wait_for_start here would busy-loop,
                # not idle, and starve local_thread of GIL time. Use
                # stop_event.wait instead: it sleeps up to 0.1s and still
                # wakes early on Stop.
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
    # Reads config.SEND_RATE per chunk, so a rate change from the panel takes
    # effect on the very next packet rather than at the next reconnect.
    schedule = RateScheduler(lambda: config.SEND_RATE)

    # Per-channel filter memory for any channel marked "local", and the
    # inputs those channels still need after the round trip.
    #
    # WHY A QUEUE. A locally processed channel is filtered from what was
    # SENT, but its result has to leave here in the same tuple as the
    # channels that came BACK -- and the board's reply for a chunk arrives
    # some milliseconds after that chunk was generated. So the inputs are
    # parked here on the way out and collected when the matching reply
    # lands. The board echoes one reply per packet in order, and TCP keeps
    # that order, so plain FIFO is the whole matching rule.
    #
    # maxlen bounds it: if replies stop coming (board wedged, cable out) this
    # drops the oldest instead of growing without limit until the reconnect.
    local_states = [pipelines.new_state(), pipelines.new_state()]
    sent_inputs = collections.deque(maxlen=256)

    packets_sent = 0
    send_stalls = 0
    # Packets generated and sent, but dropped on the way to the PLOT because
    # its queue was full. Counted rather than silently passed: these are the
    # only samples in the whole path that actually go missing, and a "Log
    # buffer" dump taken while it is happening is short without saying so --
    # which matters because that dump is what SAT draws conclusions from.
    plot_dropped = 0
    last_rx = time.perf_counter()
    # Remainder of a packet the socket could not take in one go. A partial
    # packet must be finished before another is started -- the wire format is
    # a stream of [type][length][payload] frames with no resync marker, so a
    # half-written one would desynchronise the relay and the board.
    pending = None
    # Whether `pending` is a data packet, so finishing it counts toward the
    # throughput line and finishing a config packet does not.
    pending_is_data = False
    last_report = time.time()

    while not stop_event.is_set():
        # Stop, or a switch to local mode, ends the session and closes the
        # socket (tcp_thread's finally). Returning rather than idling with
        # the connection open is deliberate: a held-open socket that nobody
        # is draining is exactly what trips the relay's 2-second send
        # timeout, so "stopped" would slowly become "disconnected" anyway,
        # just less predictably.
        if not _may_run():
            print("[net] stopped, closing the connection")
            return

        now = time.perf_counter()

        # Work done on THIS pass -- a send, a receive, or a reassembly.
        # Declared here rather than just above the receive block: sending is
        # work too, and treating it as idle made the sleep at the bottom of
        # this loop a rate limiter. See that sleep for the numbers.
        did_work = False

        # Paused is not a stall -- see RateScheduler.hold().
        if not config.SEND_ENABLED:
            schedule.hold(now)

        # Receive watchdog. Only meaningful while we are actually streaming
        # AND expecting an echo -- with sending paused or receiving switched
        # off, silence is the correct behaviour, not a fault.
        if config.SEND_ENABLED and config.RECEIVE_ENABLED:
            rx_stale_s = now - last_rx
        else:
            rx_stale_s = 0.0
            last_rx = now

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
                # Still blocked. NOT progress -- see the sleep at the bottom:
                # counting a refused write as work makes this loop retry a
                # full socket at loop speed, which cannot help and costs a
                # core (measured on hardware: 74k stalls/s at 81% CPU).
                send_stalls += 1
            elif pending_is_data:
                # Only count a DATA packet here. A config packet that had to
                # be finished across two passes used to land in this branch
                # and be reported as stream throughput -- a handful a session,
                # but "pkts/s" should mean the thing the rate controls.
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
                    # Only parked when something will actually need them --
                    # with both channels on the board this is pure overhead
                    # on the hot path.
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
            # The dropped count is the interesting half: a nonzero value is
            # the direct signal that this loop stalled, and how badly.
            backlog_dropped = schedule.take_dropped()
            note = f" (dropped {backlog_dropped} late)" if backlog_dropped else ""
            # send_stalls counts passes where the socket would not take the
            # whole packet. A few are normal; a lot means the far end (relay,
            # link, or board) is the limit, not this loop.
            if send_stalls:
                note += f" ({send_stalls} send-stalls)"
            # The only samples in the path that actually go missing.
            if plot_dropped:
                note += f" ({plot_dropped} plot-drops -- a dump now is short)"
            # Sending can look flawless into a relay whose other peer is gone.
            # Say so, rather than reporting a perfect stream to nowhere.
            if rx_stale_s > config.RX_WATCHDOG_S:
                note += f"  [!] NOTHING RECEIVED for {rx_stale_s:.1f}s -- far end gone?"
            # Normalized by the window that actually elapsed, not assumed to
            # be exactly 1000 ms: the check runs once per loop pass, so the
            # window always overshoots by a varying amount. The firmware's
            # [S] line has always done this (see main.c); this one did not,
            # which is why a dead-on 1400 pkt/s setpoint kept alternating
            # 1400/1401 -- measurement noise, not the stream.
            print(f"Python sent: {packets_sent / elapsed:.0f} pkts/s{note}")
            packets_sent = 0
            send_stalls = 0
            plot_dropped = 0
            last_report = t

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
                    # Proof of life from the far end -- see rx_stale_s.
                    # Counted even for channels whose result is about to be
                    # thrown away: the point of the watchdog is whether the
                    # BOARD is still answering, not whether we use what it
                    # said.
                    last_rx = time.perf_counter()
                    out1, out2 = records["ch1"], records["ch2"]
                    if config.any_local():
                        # Substitute locally processed channels over the
                        # board's answer for the same chunk. If the queue is
                        # empty the reply has no matching input (a reconnect
                        # dropped it), so keep the board's samples rather
                        # than filtering the wrong chunk.
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

        # Yield only when there was nothing to do -- and never past the next
        # send deadline.
        #
        # Two separate traps here, both measured:
        #
        #   * An UNCONDITIONAL sleep put a hard ~1 kHz ceiling on loop passes,
        #     which is what turned the two receive caps above into a bandwidth
        #     limit instead of just a latency one. Hence `if not did_work`.
        #   * A FIXED 0.5 ms sleep is itself a rate limiter. It really takes
        #     ~570 us on this machine, so a loop that sleeps it whenever
        #     nothing arrived cannot pass more than ~1750 times a second --
        #     below the packet period above ~1750 pkt/s. Sending used not to
        #     count as work either, so with RECEIVE_ENABLED off (a checkbox on
        #     the panel) this capped the send rate outright, and reported the
        #     shortfall as "(dropped N late)" -- blaming a stall for what was
        #     this sleep.
        #
        # So: sleep at most until the next packet is due. Behind schedule,
        # that clamps to 0.0, which yields the GIL without throttling.
        if pending is not None:
            # The socket would not take the rest of a packet. Nothing this
            # loop does will change that -- only the far end draining will --
            # so retrying at loop speed is pure waste. Back off a fixed
            # amount, deliberately NOT clamped to the send deadline: when the
            # link is the limit the deadline is always in the past, so the
            # clamp below would come out 0 and spin.
            #
            # This is the one case where being behind schedule must NOT make
            # the loop run hotter. Measured on hardware at 2000 pkt/s over a
            # saturated link: 74k stalls/s and 81% of a core, achieving no
            # more throughput than backing off does.
            time.sleep(SEND_IDLE_SLEEP_S)
        elif not did_work:
            delay = SEND_IDLE_SLEEP_S
            if config.SEND_ENABLED:
                delay = min(delay, schedule.time_until())
            time.sleep(delay)
