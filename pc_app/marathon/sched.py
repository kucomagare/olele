# Send schedule shared by both workers: CHUNK_SIZE samples every
# 1/SEND_RATE seconds, both read live. Used to be written twice; bugs found:
# idle busy-spin once started (Event.wait returns immediately when already
# set -- held stream to 4.8% of rate), and pausing leaving the deadline
# behind so catch-up counted the pause as backlog.
# IDLE/gate handling stays OUT of here -- differs between the two workers.

import time

# Backlog replay budget as a DURATION not a packet count -- a count shrinks
# as rate rises (at 1400 pkt/s, 8 pkts = 5.7ms, under one plot refresh),
# so ordinary frames tripped a limiter meant for real stalls (lost 7.6% of
# the stream). 50ms absorbs a refresh/GC pause; the floor keeps low rates sane.
CATCHUP_MAX_S = 0.05
CATCHUP_MIN_PKTS = 8


class RateScheduler:
    """When the next chunk is due, and what to do about a missed one.

    `rate_fn` is called live so a SEND_RATE change takes effect next chunk,
    not at the next reconnect.
    """

    def __init__(self, rate_fn):
        self._rate_fn = rate_fn
        self.next_due = time.perf_counter()
        self.dropped = 0   # chunks abandoned since last take_dropped()
        self._held = False

    def reset(self, now=None):
        """Start the schedule from now -- a fresh run, or a new connection."""
        self.next_due = time.perf_counter() if now is None else now
        self.dropped = 0
        self._held = False

    def due(self, now):
        return now >= self.next_due

    def hold(self, now):
        """Paused: hold the deadline at `now` so the pause isn't counted
        or replayed as backlog once resumed."""
        self.next_due = now
        self._held = True

    def advance(self, now):
        """Move the deadline on by one period, abandoning a hopeless backlog.

        Fixed-increment (not `now + period`) keeps the long-run rate honest
        across jitter. But after a real stall a fixed increment alone
        replays the whole backlog at loop speed (measured: a ~10s stall at
        1300 pkt/s produced ~30s of 1600-2250 pkt/s). So: a little catch-up,
        then abandon the rest.
        """
        period = 1.0 / self._rate_fn()

        if self._held:
            # First chunk after a pause: fresh schedule instead of measuring
            # lateness against a deadline dragged along by hold() -- that gap
            # is poll interval, not real lateness.
            self._held = False
            self.next_due = now + period
            return

        self.next_due += period
        limit = max(CATCHUP_MIN_PKTS * period, CATCHUP_MAX_S)
        behind = now - self.next_due
        if behind > limit:
            self.dropped += int(behind / period)
            self.next_due = now + period

    def time_until(self, now=None):
        """Seconds until the next chunk is due; 0.0 if it already is.

        For sizing a sleep -- never sleep past this: a fixed idle sleep
        longer than the packet period is itself a rate limiter.
        """
        now = time.perf_counter() if now is None else now
        return max(0.0, self.next_due - now)

    def take_dropped(self):
        """Read and clear the abandoned-chunk counter."""
        n = self.dropped
        self.dropped = 0
        return n
