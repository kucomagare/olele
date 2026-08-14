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
SEND_RATE = 10

CHUNK_SIZE = 5  # was 2000 before ts+ch1+ch2 tripled bytes/sample (2 -> 6);
                  # 500*6+4=3004 bytes fits lwIP's TCP_SND_BUF=8192 with
                  # margin. Provisional for testing the new packet logic --
                  # revisit once throughput tuning resumes (see README).
TRIANGLE_PERIOD = 50

AMP_BASE = 2000
AMP_OSC  = 500
AMP_FREQ = 0.2

SEND_ENABLED = True
RECEIVE_ENABLED = True

PLOT_MIN = 0
PLOT_MAX = 5500

RECONNECT_DELAY = 1.0  # seconds between reconnect attempts after a dropped/failed connection
