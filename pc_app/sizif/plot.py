# Live dual-channel matplotlib plot (ch1/ch2 in/out traces). Matplotlib-only.
#
# Perf (measured 2026-08-17 @ 400k smp/s): full canvas.draw() was ~30ms/frame
# (107% CPU) -- blitting only the 4 line artists over a cached background is
# far cheaper. Envelope decimation (min/max per chunk) fixes O(CHUNK_SIZE)->O(1)
# per-packet cost and the aliasing a raw 1000-pt buffer showed at that rate.

import numpy as np
import matplotlib
matplotlib.use("TkAgg")
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter

import config
from config import PLOT_ENVELOPE_BLOCKS, PLOT_MODE
from packet_format import CH1_DTYPE, CH2_DTYPE
from control_panel import SignalControlPanel, PlotControlPanel

_SCOPE = (PLOT_MODE == "scope")


def envelope(values, blocks=PLOT_ENVELOPE_BLOCKS):
    """Reduce to `blocks` min/max pairs -- keeps visible extremes, unlike
    stride decimation which aliases/hides transients."""
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
        self.fig, (self.ax1, self.ax2) = plt.subplots(2, 1, sharex=True)
        plt.show(block=False)

        # Tk pack quirk (verified): a side="top" slab (canvas/toolbar) claims
        # full width regardless of fill/expand, squeezing panels packed after
        # it. Fix: pop canvas+toolbar out, pack our panels first, restore them.
        tk_window = self.fig.canvas.manager.window
        existing = [(child, child.pack_info()) for child in tk_window.pack_slaves()]
        for child, _ in existing:
            child.pack_forget()

        self.plot_control_panel = PlotControlPanel(tk_window)
        self.plot_control_panel.frame.pack(side="bottom", fill="x")
        self.signal_control_panel = SignalControlPanel(tk_window)
        self.signal_control_panel.frame.pack(side="right", fill="y")

        for child, info in existing:
            info["in_"] = info.pop("in")
            child.pack(**info)

        self.ch1_in  = np.zeros(buffer_size, dtype=CH1_DTYPE)
        self.ch2_in  = np.zeros(buffer_size, dtype=CH2_DTYPE)
        self.ch1_out = np.zeros(buffer_size, dtype=CH1_DTYPE)
        self.ch2_out = np.zeros(buffer_size, dtype=CH2_DTYPE)

        # steps-mid: flat segment per sample, not a slope into the next --
        # avoids implying values never actually measured/sent.
        self.line_ch1_in,  = self.ax1.plot(self.ch1_in,  color="blue", label="in",  animated=True, drawstyle="steps-mid")
        self.line_ch1_out, = self.ax1.plot(self.ch1_out, color="red",  label="out", animated=True, drawstyle="steps-mid")
        self.line_ch2_in,  = self.ax2.plot(self.ch2_in,  color="blue", label="in",  animated=True, drawstyle="steps-mid")
        self.line_ch2_out, = self.ax2.plot(self.ch2_out, color="red",  label="out", animated=True, drawstyle="steps-mid")
        self._lines_ax1 = (self.line_ch1_in, self.line_ch1_out)
        self._lines_ax2 = (self.line_ch2_in, self.line_ch2_out)

        self.ax1.set_title("Channel 1")
        self.ax2.set_title("Channel 2")
        for ax in (self.ax1, self.ax2):
            ax.legend()
            ax.set_ylim(config.PLOT_MIN, config.PLOT_MAX)
            # x-data is buffer index; tick labels use ECG_SAMPLING_RATE, NOT
            # SEND_RATE*CHUNK_SIZE (wall-clock speed) -- the latter was only
            # correct by coincidence at 1x real-time, wrong otherwise.
            ax.xaxis.set_major_formatter(FuncFormatter(self._format_time_tick))
        self.ax1.set_xlim(0, buffer_size - 1)
        self.ax2.set_xlabel("Time (s)")
        self.fig.tight_layout()

        # Cached per-axis background, re-cached on any full draw or blits go stale.
        self._bg1 = None
        self._bg2 = None
        self.fig.canvas.mpl_connect("draw_event", self._on_draw)
        self.fig.canvas.draw()

        # Last-drawn values for the 3 settings needing a full canvas.draw() to
        # apply (blitting only redraws line artists, not axes/limits) -- see refresh().
        self._last_time_rate = config.ECG_SAMPLING_RATE
        self._last_ylim = (config.PLOT_MIN, config.PLOT_MAX)
        self._last_buffer_size = buffer_size

        self._dirty = False  # buffers hold data set_ydata() hasn't seen yet

    @staticmethod
    def _format_time_tick(x, _pos):
        rate = config.ECG_SAMPLING_RATE
        return f"{x / rate:.2f}" if rate > 0 else ""

    def _resize_buffers(self, new_size):
        """Reallocate the 4 rolling buffers, keeping recent samples (zero-padded
        if growing); also updates lines' x-data (Line2D needs matching lengths)."""
        def resized(old):
            new = np.zeros(new_size, dtype=old.dtype)
            keep = min(new_size, len(old))
            if keep:
                new[-keep:] = old[-keep:]
            return new

        self.ch1_in  = resized(self.ch1_in)
        self.ch2_in  = resized(self.ch2_in)
        self.ch1_out = resized(self.ch1_out)
        self.ch2_out = resized(self.ch2_out)
        self.buffer_size = new_size

        x = np.arange(new_size)
        self.line_ch1_in.set_data(x, self.ch1_in)
        self.line_ch1_out.set_data(x, self.ch1_out)
        self.line_ch2_in.set_data(x, self.ch2_in)
        self.line_ch2_out.set_data(x, self.ch2_out)
        self.ax1.set_xlim(0, new_size - 1)

    def _on_draw(self, _event):
        self._bg1 = self.fig.canvas.copy_from_bbox(self.ax1.bbox)
        self._bg2 = self.fig.canvas.copy_from_bbox(self.ax2.bbox)

    @staticmethod
    def _rolled(buf, values):
        """Shift left and append in place -- np.roll allocates fresh, too costly at packet rate."""
        n = len(values)
        if n == 0:
            return buf
        if n >= len(buf):
            buf[:] = values[-len(buf):]
            return buf
        buf[:-n] = buf[n:]
        buf[-n:] = values
        return buf

    # update_*() run at packet rate (cheap slice assignment); only set_ydata()
    # is deferred to sync(), once per frame. An earlier version deferred the
    # buffer update itself, causing discontinuities when FRAME_RATE < SEND_RATE
    # -- don't reintroduce that.

    def update_input(self, ch1, ch2):
        values1 = ch1 if _SCOPE else envelope(ch1)
        values2 = ch2 if _SCOPE else envelope(ch2)
        self._rolled(self.ch1_in, values1)
        self._rolled(self.ch2_in, values2)
        self._dirty = True

    def update_output(self, ch1, ch2):
        values1 = ch1 if _SCOPE else envelope(ch1)
        values2 = ch2 if _SCOPE else envelope(ch2)
        self._rolled(self.ch1_out, values1)
        self._rolled(self.ch2_out, values2)
        self._dirty = True

    def sync(self):
        """Push buffers into line artists once per frame (the expensive part)."""
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

        # These 3 settings need a full canvas.draw(); blitting alone won't pick them up.
        needs_full_draw = False

        time_rate = config.ECG_SAMPLING_RATE
        if time_rate != self._last_time_rate:
            self._last_time_rate = time_rate
            needs_full_draw = True

        new_ylim = (config.PLOT_MIN, config.PLOT_MAX)
        if new_ylim != self._last_ylim and new_ylim[1] > new_ylim[0]:
            self._last_ylim = new_ylim
            self.ax1.set_ylim(*new_ylim)
            self.ax2.set_ylim(*new_ylim)
            needs_full_draw = True

        new_buffer_size = config.PLOT_BUFFER
        if new_buffer_size != self._last_buffer_size and new_buffer_size > 0:
            self._last_buffer_size = new_buffer_size
            self._resize_buffers(new_buffer_size)
            needs_full_draw = True

        if needs_full_draw:
            canvas.draw()
            canvas.flush_events()
            return

        if self._bg1 is None or self._bg2 is None:
            canvas.draw()
            return

        canvas.restore_region(self._bg1)
        for line in self._lines_ax1:
            self.ax1.draw_artist(line)
        canvas.blit(self.ax1.bbox)

        canvas.restore_region(self._bg2)
        for line in self._lines_ax2:
            self.ax2.draw_artist(line)
        canvas.blit(self.ax2.bbox)

        # flush_events() keeps window responsive; plt.pause() adds a redundant redraw (CPU sink).
        canvas.flush_events()
