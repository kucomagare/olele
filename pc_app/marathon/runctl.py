# Session run control: the Start/Stop gate shared by the GUI thread and the
# worker threads.
#
# Deliberately NOT in config.py. That file holds knobs -- values you set and
# read. This holds RUN STATE, which is a different thing: worker threads
# block on it, and blocking needs an Event, not an attribute. Keeping it in
# its own dependency-free module also lets net.py, local_proc.py and
# control_panel.py all import it with no cycle.
#
# WHY A GATE AT ALL. Without it the app connects and starts streaming the
# instant it launches, so every session begins by racing the plot window to
# fix settings that were already wrong on the wire. With it, the window and
# the panel come up first, nothing is generated, connected or sent, and the
# run starts on a deliberate press -- which also makes "start a run, capture
# it, stop, change one thing, run again" a real workflow instead of a
# restart.

import threading

# Cleared at import. config.AUTOSTART, applied by python_client.py at
# startup, is the opt-out for anyone who wants the old launch-and-go
# behaviour (unattended throughput runs, mostly).
_running = threading.Event()


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

    Worker threads call this instead of polling with sleep() so that
    pressing Start takes effect immediately rather than on the next poll
    tick -- and so an idle app costs nothing while it waits.
    """
    return _running.wait(timeout)
