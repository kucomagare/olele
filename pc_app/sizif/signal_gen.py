# Synthetic test-signal generation and packet building. No socket I/O
# here on purpose -- net.py owns the connection and calls into this
# module just to get bytes to send.

import numpy as np

from config import CHUNK_SIZE, TRIANGLE_PERIOD, AMP_BASE, AMP_OSC, AMP_FREQ
from packet_format import DATA_TYPE, DATA_DTYPE, TS_MODULUS, CH1_DTYPE, CH2_DTYPE

import struct


def generate_triangle_chunk(counter, now, dtype=np.uint16, amp_scale=1.0):
    amp = amp_scale * (AMP_BASE + AMP_OSC * np.sin(2 * np.pi * AMP_FREQ * now))
    t = (np.arange(CHUNK_SIZE) + counter) % TRIANGLE_PERIOD
    tri = amp * (2 * np.abs(t - TRIANGLE_PERIOD/2) / TRIANGLE_PERIOD)
    return tri.astype(dtype)


def build_data_packet(ts_start, ch1, ch2):
    n = len(ch1)
    rec = np.zeros(n, dtype=DATA_DTYPE)
    rec["ts"]  = (np.arange(n, dtype=np.int64) + ts_start) % TS_MODULUS
    rec["ch1"] = ch1
    rec["ch2"] = ch2
    header = struct.pack("!HH", DATA_TYPE, n)
    return header + rec.tobytes()


def generate_signal_packet(counter, now):
    """Returns (packet_bytes, ch1, ch2) for one send cycle. ch2 uses a
    smaller amplitude than ch1 (same generator/shape otherwise) purely so
    the two are visually distinguishable on the plot for now --
    placeholder until each channel gets real, distinct signal
    generation."""
    ch1 = generate_triangle_chunk(counter, now, CH1_DTYPE, amp_scale=1.0)
    ch2 = generate_triangle_chunk(counter, now, CH2_DTYPE, amp_scale=0.5)
    packet = build_data_packet(counter, ch1, ch2)
    return packet, ch1, ch2
