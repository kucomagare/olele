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
FRAME_RATE = 24
# SEND_RATE (packets/s) x CHUNK_SIZE (samples/packet) is the offered load.
# Wire cost is 4 + 6*CHUNK_SIZE bytes per packet (ts+ch1+ch2, 2 bytes each).
#
# Measured on hardware 2026-08-17, both sides driven past their limit so
# these are true ceilings, not just what was configured:
#
#   CHUNK_SIZE=500  (3004 B/pkt) -> 146 pkt/s sustained = 0.44 MB/s
#   CHUNK_SIZE=1000 (6004 B/pkt) ->  57 pkt/s sustained = 0.34 MB/s
#
# Bigger chunks are WORSE, which is counterintuitive until you see why:
# the limit is bytes-in-flight against the board's TCP_SND_BUF (8192).
# At 3004 B two packets fit and pipeline; at 6004 B only one fits, so the
# board stalls for a full round trip between packets. Both land near
# ~6 KB in flight, hence the similar MB/s despite very different packet
# rates. CHUNK_SIZE=242 (1456 B, one Ethernet frame, 5 fit) is the next
# thing worth trying if you want more out of the current BSP.
#
# Exceeding the ceiling is no longer destructive -- the firmware detects a
# full RX ring and reconnects to resync rather than corrupting framing --
# but it costs a reconnect every ~110 ms and throughput collapses, so stay
# under it for real runs.
#
# The real unlock is raising TCP_SND_BUF/TCP_WND in the BSP (lwipopts.h is
# generated from lwip213_* CMake vars; hard ceiling 65535 each, since
# LWIP_WND_SCALE is off). That needs a platform rebuild, not a restart.
SEND_RATE = 100   # ~68% of the measured 146 pkt/s ceiling; runs clean

CHUNK_SIZE = 500  # <= 1364 is a hard limit while TCP_SND_BUF is 8192:
                  # 4 + 6*CHUNK_SIZE must fit, or comm_process()'s
                  # backpressure check can never pass.
TRIANGLE_PERIOD = 50

AMP_BASE = 2000
AMP_OSC  = 500
AMP_FREQ = 0.2

SEND_ENABLED = True
RECEIVE_ENABLED = True

PLOT_MIN = 0
PLOT_MAX = 5500

RECONNECT_DELAY = 1.0  # seconds between reconnect attempts after a dropped/failed connection
