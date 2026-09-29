# Opt-in timing markers: OLELE_PROF=1 prints per-label ms once a second.
import os
import threading
import time
from contextlib import contextmanager

ENABLED = os.environ.get("OLELE_PROF") == "1"

_lock = threading.Lock()
_stats = {}                  # label -> [count, total_s, max_s]
_last_print = time.perf_counter()


def add(label, dt):
    with _lock:
        s = _stats.setdefault(label, [0, 0.0, 0.0])
        s[0] += 1
        s[1] += dt
        s[2] = max(s[2], dt)


@contextmanager
def span(label):
    if not ENABLED:
        yield
        return
    t0 = time.perf_counter()
    try:
        yield
    finally:
        add(label, time.perf_counter() - t0)


def report():
    """Call from one loop; prints and clears once per second."""
    global _last_print
    if not ENABLED:
        return
    now = time.perf_counter()
    if now - _last_print < 1.0:
        return
    _last_print = now
    with _lock:
        rows = sorted(_stats.items())
        _stats.clear()
    print("[prof] " + " | ".join(
        f"{k} n={n} avg={t / n * 1e3:.2f} max={m * 1e3:.2f}ms"
        for k, (n, t, m) in rows))
