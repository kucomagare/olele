# Live dual-channel matplotlib plot (TX vs RX, ch1/ch2). Matplotlib-only
# concerns live here -- no networking, no packet parsing.
#
# Two things here exist for performance, both measured on 2026-08-17 with
# SEND_RATE=800 / CHUNK_SIZE=500 (400k samples/s):
#
#   1. BLITTING. refresh() used to call fig.canvas.draw(), a full re-render
#      of axes, ticks, legend and every line -- ~30 ms of CPU each, 24x a
#      second. python_client sat at 107% CPU with 93.6% of it in this
#      thread and only 12.1% in the network thread: the plot cost ~8x the
#      actual job. Blitting redraws only the four line artists over a
#      cached background.
#
#   2. ENVELOPE DECIMATION. At 400k samples/s a 1000-point buffer turned
#      over 400 times per second -- it displayed 2.5 ms of signal and was
#      pure aliasing. Each received chunk is now reduced to min/max pairs,
#      so the window covers a useful span AND the per-packet work drops
#      from O(CHUNK_SIZE) to O(1).

import numpy as np
import matplotlib
matplotlib.use("TkAgg")
import matplotlib.pyplot as plt

from config import PLOT_MIN, PLOT_MAX, PLOT_ENVELOPE_BLOCKS, PLOT_MODE
from packet_format import CH1_DTYPE, CH2_DTYPE

_SCOPE = (PLOT_MODE == "scope")


def envelope(values, blocks=PLOT_ENVELOPE_BLOCKS):
    """Reduce a chunk to `blocks` min/max pairs (2*blocks points).

    Keeps the visible extremes -- a plain stride-based decimation would
    alias a triangle/sine down to whatever phase the stride happened to
    land on, and would hide short transients entirely. Returns the input
    unchanged when it is already short enough to be worth plotting raw.
    """
    n = len(values)
    if blocks <= 0 or n == 0 or n <= 2 * blocks:
        return values

    usable = (n // blocks) * blocks
    v = values[:usable].reshape(blocks, -1)

    out = np.empty(2 * blocks, dtype=values.dtype)
    out[0::2] = v.min(axis=1)
    out[1::2] = v.max(axis=1)
    return out


class DualPlot:
    def __init__(self, buffer_size):
        self.buffer_size = buffer_size

        plt.ion()
        self.fig, self.ax = plt.subplots(1, 1)
        plt.show(block=False)

        self.ch1_in  = np.zeros(buffer_size, dtype=CH1_DTYPE)
        self.ch2_in  = np.zeros(buffer_size, dtype=CH2_DTYPE)
        self.ch1_out = np.zeros(buffer_size, dtype=CH1_DTYPE)
        self.ch2_out = np.zeros(buffer_size, dtype=CH2_DTYPE)

        self.line_ch1_in,  = self.ax.plot(self.ch1_in,  color="blue",       label="ch1 in",  animated=True)
        self.line_ch2_in,  = self.ax.plot(self.ch2_in,  color="dodgerblue", label="ch2 in",  animated=True, linestyle="--")
        self.line_ch1_out, = self.ax.plot(self.ch1_out, color="red",        label="ch1 out", animated=True)
        self.line_ch2_out, = self.ax.plot(self.ch2_out, color="orangered",  label="ch2 out", animated=True, linestyle="--")
        self._lines = (self.line_ch1_in, self.line_ch2_in,
                       self.line_ch1_out, self.line_ch2_out)

        self.ax.set_title("Streaming Input & Output (ch1/ch2)")
        self.ax.legend()
        self.ax.set_ylim(PLOT_MIN, PLOT_MAX)

        # Cache the static background once, then re-cache whenever
        # matplotlib does a full draw of its own (resize, toolbar zoom,
        # first show) -- otherwise blits would paint onto a stale image.
        self._bg = None
        self.fig.canvas.mpl_connect("draw_event", self._on_draw)
        self.fig.canvas.draw()

        # Deferred-update state (see update_input/sync).
        self._pending_in = None
        self._pending_out = None
        self._dirty = False

    def _on_draw(self, _event):
        self._bg = self.fig.canvas.copy_from_bbox(self.ax.bbox)

    @staticmethod
    def _rolled(buf, values):
        """Shift `buf` left by len(values) and append, in place.

        np.roll allocates a fresh array on every call; this runs at packet
        rate, so it is done as an in-place slice move instead.
        """
        n = len(values)
        if n == 0:
            return buf
        if n >= len(buf):
            buf[:] = values[-len(buf):]
            return buf
        buf[:-n] = buf[n:]
        buf[-n:] = values
        return buf

    # update_*() are called at PACKET rate (800/s), refresh() at FRAME rate
    # (24/s). So these do the cheapest possible thing and leave set_ydata()
    # to sync(), which runs once per frame -- pushing 1000-point arrays into
    # the artists 800 times a second was 33x more work than the display
    # could ever show, and was the bulk of the remaining plot-thread CPU
    # after blitting landed.
    #
    # In "scope" mode that means just keeping a reference to the newest
    # chunk; intermediate packets are intentionally dropped, since only the
    # most recent PLOT_BUFFER samples are ever displayed.

    def update_input(self, ch1, ch2):
        if _SCOPE:
            self._pending_in = (ch1, ch2)
        else:
            self._rolled(self.ch1_in, envelope(ch1))
            self._rolled(self.ch2_in, envelope(ch2))
            self._dirty = True

    def update_output(self, ch1, ch2):
        if _SCOPE:
            self._pending_out = (ch1, ch2)
        else:
            self._rolled(self.ch1_out, envelope(ch1))
            self._rolled(self.ch2_out, envelope(ch2))
            self._dirty = True

    def sync(self):
        """Push buffered data into the line artists. Once per frame."""
        if _SCOPE:
            if self._pending_in is not None:
                ch1, ch2 = self._pending_in
                self._rolled(self.ch1_in, ch1)
                self._rolled(self.ch2_in, ch2)
                self._pending_in = None
                self._dirty = True
            if self._pending_out is not None:
                ch1, ch2 = self._pending_out
                self._rolled(self.ch1_out, ch1)
                self._rolled(self.ch2_out, ch2)
                self._pending_out = None
                self._dirty = True

        if not self._dirty:
            return
        self.line_ch1_in.set_ydata(self.ch1_in)
        self.line_ch2_in.set_ydata(self.ch2_in)
        self.line_ch1_out.set_ydata(self.ch1_out)
        self.line_ch2_out.set_ydata(self.ch2_out)
        self._dirty = False

    def refresh(self):
        self.sync()
        canvas = self.fig.canvas
        if self._bg is None:
            canvas.draw()
            return
        canvas.restore_region(self._bg)
        for line in self._lines:
            self.ax.draw_artist(line)
        canvas.blit(self.ax.bbox)
        # flush_events() pumps the GUI event loop (keeps the window
        # responsive). The old plt.pause(0.001) that used to be here did
        # the same thing plus a redundant redraw, and is a known CPU sink.
        canvas.flush_events()
