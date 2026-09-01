# Packet/sample structure -- loaded from shared/<variant>/packet_format.json,
# the single source of truth also used to generate packet_format.h for the
# firmware and the C++ relay (see shared/gen_packet_header.py). Change
# field widths/signedness there, not here. No socket/matplotlib
# dependency here on purpose -- this is pure wire-format logic, usable
# and testable independent of networking or plotting.

import json
import struct
import numpy as np
from pathlib import Path

# This file lives at <repo_root>/pc_app/<variant>/packet_format.py, so the
# variant name is just the parent directory's name -- no config needed, and a
# copied tree picks up its own wire format automatically.
_HERE = Path(__file__).resolve().parent
VARIANT = _HERE.name
PACKET_FORMAT_PATH = _HERE.parent.parent / "shared" / VARIANT / "packet_format.json"

try:
    with open(PACKET_FORMAT_PATH) as _f:
        PACKET_FORMAT = json.load(_f)
except FileNotFoundError:
    raise FileNotFoundError(
        f"packet_format.json not found at {PACKET_FORMAT_PATH} -- expected "
        f"at <repo_root>/shared/{VARIANT}/packet_format.json (variant taken "
        f"from this file's directory name). Has it moved, or is the variant "
        f"directory missing?"
    )

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

CONFIG_TYPE   = next(t for t, v in PACKET_TYPES.items() if v["name"] == "config")
CONFIG_DTYPE  = PACKET_DTYPES[CONFIG_TYPE]
METRICS_TYPE  = next(t for t, v in PACKET_TYPES.items() if v["name"] == "metrics")
METRICS_DTYPE = PACKET_DTYPES[METRICS_TYPE]

# Config packet ops. Mirrored in the firmware as CONFIG_OP_* -- the JSON's
# "config" description is the source of truth for what they mean.
CONFIG_OP_READ = 0
CONFIG_OP_WRITE = 1

# Board UART verbosity bits -- mirrors COMM_LOG_* in the firmware's comm_log.h.
LOG_STATS  = 0x01   # [S] per-second throughput line
LOG_ERROR  = 0x02   # [E] errors, resyncs, suppression counts
LOG_NOTICE = 0x04   # [N] connect/reconnect/lifecycle
LOG_CONFIG = 0x08   # [C] config read-backs
LOG_OTHER  = 0x10   # everything else, e.g. [CLK] and boot banners
LOG_ALL    = 0x1F
LOG_NONE   = 0x00


def build_config_packet(op, n_channels=0, shift=0, ctrl=0, log_mask=LOG_ALL):
    """Frame one config packet ready for the wire.

    status is sent as 0: it is read-only on the board and ignored inbound,
    so there is nothing meaningful to put there.
    """
    rec = np.zeros(1, dtype=CONFIG_DTYPE)
    rec[0]["op"] = op
    rec[0]["n_channels"] = n_channels
    rec[0]["shift"] = shift
    rec[0]["ctrl"] = ctrl
    rec[0]["log_mask"] = log_mask
    rec[0]["status"] = 0
    return struct.pack("!HH", CONFIG_TYPE, 1) + rec.tobytes()


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

        # No dtype lookup guard here: PACKET_RECORD_SIZE and PACKET_DTYPES are
        # built from the same PACKET_TYPES dict, so the record_size check above
        # has already rejected every type this could fail on.
        #
        # (type, records) rather than a bare array: there are three packet
        # types on this link now and the caller has to dispatch on it. Callers
        # that only care about data compare against DATA_TYPE.
        return type_r, np.frombuffer(body, dtype=PACKET_DTYPES[type_r])
