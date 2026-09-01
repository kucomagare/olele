# The send schedule, shared by both workers.
#
# WHY THIS IS ITS OWN MODULE. net.tcp_thread and local_proc.local_thread run
# the same schedule on purpose -- "CHUNK_SIZE samples every 1/SEND_RATE
# seconds, both read live" -- so that a rate typed into the panel means the
# same thing in either mode and a capture taken in one is comparable with one
# taken in the other. That schedule used to be written out twice, and every
# bug in it was therefore two bugs:
#
#   * The idle branch busy-spun once Start had been pressed, because
#     Event.wait() returns immediately when the flag is already set. Found on
#     hardware 2026-09-01: it starved the active worker of GIL time and held
#     the stream to 4.8% of its configured rate. Fixed in two places.
#   * Pausing left the deadline behind, so the catch-up limiter measured the
#     whole pause as backlog and reported a 60 s pause at 1400 pkt/s as
#     "(dropped 84000 late)". Also fixed in two places.
#
# Neither was subtle once seen; both were invisible twice. The rules live here
# now so the next one is a single fix.
#
# The IDLE/gate handling is deliberately NOT here. It differs between the two
# workers in ways that matter -- one closes a socket, the other clears filter
# state -- and forcing them together would trade a real difference for a
# false symmetry.

import time

# How much send backlog the scheduler will replay after a stall before giving
# up on the rest. Expressed as a DURATION, not a packet count: the point is to
# tell routine jitter apart from a real stall, and that boundary lives in
# milliseconds, not packets. A packet count means the threshold shrinks as the
# rate rises -- at 1400 pkt/s the original 8 packets worked out to 5.7 ms,
# less than a single plot refresh (~8.8 ms measured), so ordinary frames were
# tripping a limiter meant for multi-second stalls and losing 7.6% of the
# stream to it.
#
# 50 ms comfortably absorbs a plot refresh, a GC pause or a scheduler slice
# while still abandoning anything pathological. The floor keeps it sane at low
# rates, where 50 ms can be less than one packet period.
CATCHUP_MAX_S = 0.05
CATCHUP_MIN_PKTS = 8


class RateScheduler:
    """When the next chunk is due, and what to do about a missed one.

    `rate_fn` is called for the rate rather than the rate being passed in
    once, so a SEND_RATE change from the control panel takes effect on the
    very next chunk instead of at the next reconnect.
    """

    def __init__(self, rate_fn):
        self._rate_fn = rate_fn
        self.next_due = time.perf_counter()
        # Chunks abandoned rather than replayed, since the last read of this
        # counter. The caller reports and clears it.
        self.dropped = 0
        self._held = False

    def reset(self, now=None):
        """Start the schedule from now -- a fresh run, or a new connection."""
        self.next_due = time.perf_counter() if now is None else now
        self.dropped = 0
        self._held = False

    def due(self, now):
        return now >= self.next_due

    def hold(self, now):
        """Paused: hold the deadline at `now`.

        Without this the deadline stands still while time passes, and the
        first advance() after resuming sees the entire pause as backlog. A
        pause is not a stall -- nothing was due -- so it must not be counted
        or replayed as one.
        """
        self.next_due = now
        self._held = True

    def advance(self, now):
        """Move the deadline on by one period, abandoning a hopeless backlog.

        Fixed-increment rather than `now + period`, so the long-run average
        rate stays honest across ordinary jitter. But after a real stall the
        deadline sits far in the past and a fixed increment alone makes the
        caller fire one chunk per loop pass until it catches up -- replaying
        the whole backlog at loop speed, far above the configured rate, for as
        long again as the stall lasted. Measured: a ~10 s stall at 1300 pkt/s
        produced ~30 s of 1600-2250 pkt/s before settling.

        So: keep a little catch-up (it absorbs jitter and holds the average
        honest), abandon the rest. Chunks due seconds ago cannot be sent on
        time retroactively, and replaying them only overruns the board.
        """
        period = 1.0 / self._rate_fn()

        if self._held:
            # First chunk after a pause. Start a fresh schedule instead of
            # measuring lateness against a deadline that was only being
            # dragged along by hold(): the gap between the last hold() and
            # now is however often the caller polled, not time the stream
            # was late by. Without this the poll interval leaks in as drops
            # -- 100 ms of it reads as 138 dropped packets at 1400 pkt/s.
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

        For sizing a sleep. Never sleep past this: a fixed idle sleep longer
        than the packet period is itself a rate limiter (a nominal 0.5 ms
        sleep really takes ~570 us, which caps the loop at ~1750 passes/s).
        """
        now = time.perf_counter() if now is None else now
        return max(0.0, self.next_due - now)

    def take_dropped(self):
        """Read and clear the abandoned-chunk counter."""
        n = self.dropped
        self.dropped = 0
        return n
