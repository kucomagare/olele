# Session run control: the Start/Stop gate shared by the GUI thread and
# the worker threads. NOT in config.py -- that holds knobs, this holds
# RUN STATE that threads block on (needs an Event, not an attribute). Own
# dependency-free module so net.py, local_proc.py and control_panel.py
# can all import it with no cycle.
#
# WHY A GATE AT ALL: without it the app connects and streams the instant
# it launches, racing the plot window to fix settings already wrong on
# the wire. With it, the window and panel come up first, nothing runs
# until a deliberate press -- making "run, capture, stop, change one
# thing, run again" a real workflow instead of a restart.

import threading

# Cleared at import. config.AUTOSTART (applied by python_client.py) is
# the opt-out for anyone who wants the old launch-and-go behaviour
# (unattended throughput runs, mostly).
_running = threading.Event()

# Set once the expensive one-time costs are paid: the neurokit2 ECG
# buffer (~0.9s) and the numba kernel compile (~0.3s). python_client.py
# does this in the background at launch so Start is instant instead of
# freezing the window for a second, which used to read as "the button
# did nothing" and invited a second press that stopped it again.
warm = threading.Event()


def start():
    _running.set()


def stop():
    _running.clear()


def is_running():
    return _running.is_set()


def toggle():
    """Flip the gate and return the new state -- what the button calls."""
    if _running.is_set():
        _running.clear()
    else:
        _running.set()
    return _running.is_set()


def wait_for_start(timeout=None):
    """Block until started (or `timeout` elapses); returns the state.

    Worker threads call this instead of polling with sleep() so Start
    takes effect immediately, and an idle app costs nothing while waiting.

    ONLY while it is not started. Event.wait() returns IMMEDIATELY once
    the flag is set, so after Start this doesn't block at all and a loop
    built on it spins at full speed -- not hypothetical: both workers
    used it for their "not my turn" idle branch, and once Start was
    pressed the one that didn't own the current mode burned a whole core
    holding the GIL, starving the one that did (measured on hardware
    2026-09-01: the stream ran at 4.8% of its configured rate). So: use
    this to wait FOR the start, and something else -- stop_event.wait is
    the usual answer -- to idle once started.
    """
    return _running.wait(timeout)
