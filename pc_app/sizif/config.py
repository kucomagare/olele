# All tunable knobs for the PC-side client in one place -- edit here when
# iterating on send rate / chunk size / plot window / signal shape, etc.
# (kept separate from networking/plotting/signal-generation code so the
# values you actually hand-tune during testing aren't buried in the
# middle of a longer script).

BOARD_CONNECTED = True  # True: connect over the real board network (192.168.1.100)
                        # False: loop back to the C++ server on this machine, no board needed

HOST = "192.168.1.100" if BOARD_CONNECTED else "127.0.0.1"
PORT = 5001

PLOT_BUFFER = 1000
FRAME_RATE = 24   # Plot redraws/s. Costs real CPU: plot.py's refresh()
                  # does a full canvas.draw(), ~20-40 ms each on TkAgg, so
                  # 24 fps is ~60% of a core. Measured 2026-08-17: dropping
                  # this to 2 took python_client from 103% to 36% CPU. It
                  # did NOT change throughput (that was a firmware-side
                  # limit), so this is a UI-smoothness knob, not a
                  # performance one -- lower it only if you need the CPU.
                  # The proper fix is blitting in plot.py.
# SEND_RATE (packets/s) x CHUNK_SIZE (samples/packet) is the offered load.
# Wire cost is 4 + 6*CHUNK_SIZE bytes per packet (ts+ch1+ch2, 2 bytes each).
#
# MEASURED CEILING (hardware, 2026-08-17, CHUNK_SIZE=500):
#
#   ~825 pkt/s = 412,500 samples/s = 2.48 MB/s
#
# It plateaus flat at 822-827 while the PC offers more, so this is the
# board's real limit, not a configured rate. Per packet the board spends
# ~1190 us:
#
#   ~874 us (73%)  axi_process_sample(): 500 samples x 4 AXI-Lite
#                  transactions x ~437 ns each (~22 cycles @ 50 MHz)
#   ~316 us (27%)  byte-swapping 500 records, memcpy, ring ops, tcp_write
#
# So the ceiling is set by the per-sample AXI round trips. Getting past it
# means batching (AXI-Stream/DMA, or a peripheral that takes several
# samples per transaction) -- a block-design change, not tuning. TCP
# buffers are NOT the limit: TCP_SND_BUF/TCP_WND are already 65535 and the
# main loop idles at ~737k passes/s.
#
# WARNING -- overshooting the ceiling currently wedges the board. Ring
# overflow triggers a resync (framing stays intact, no corruption), but
# each reconnect logs ~68 bytes and comm_log_flush() blocks on a 115200
# baud UART: 5.9 ms of dead CPU per message. That stalls the loop, which
# causes more overflow, which logs more -- a livelock that drops the board
# to ~160 loops/s and 0 pkt/s until the sender backs off. Fix pending
# (bounded log budget in comm_log). Until then, stay under the ceiling.
#
# Historical note: figures here before this date (a "146 pkt/s / 0.44 MB/s
# ceiling", and a claim that bigger CHUNK_SIZE was worse) were measured
# with a broken stats clock and were wrong by 3x. Any conclusion drawn
# from them -- including the CHUNK_SIZE 500-vs-1000 comparison -- is void.
SEND_RATE = 700   # ~85% of the measured 825 pkt/s ceiling. Deliberately
                  # not closer: until the logging livelock above is fixed,
                  # briefly exceeding the ceiling wedges the board rather
                  # than just costing throughput, so the checked-in default
                  # keeps margin. 820 runs fine when watched.

CHUNK_SIZE = 500  # Upper bound is now MAX_PAYLOAD_SAMPLES (2000, firmware
                  # + relay), not TCP_SND_BUF: at 65535 the old
                  # (TCP_SND_BUF-4)/6 = 1364 limit no longer binds.
TRIANGLE_PERIOD = 50

AMP_BASE = 2000
AMP_OSC  = 500
AMP_FREQ = 0.2

SEND_ENABLED = True
RECEIVE_ENABLED = True

PLOT_MIN = 0
PLOT_MAX = 5500

RECONNECT_DELAY = 1.0  # seconds between reconnect attempts after a dropped/failed connection
