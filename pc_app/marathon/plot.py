# Live dual-channel matplotlib plot: two stacked subplots (ch1, ch2), each
# showing its in/out (sent/received) traces. Matplotlib-only concerns live
# here -- no networking, no packet parsing.
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

import csv
import time as _time
from pathlib import Path

import numpy as np
import matplotlib
matplotlib.use("TkAgg")
import matplotlib.pyplot as plt
from matplotlib.ticker import AutoMinorLocator, FuncFormatter, NullLocator

import config
import guiutil
import net
from packet_format import CH1_DTYPE, CH2_DTYPE
from control_panel import GUI_SECTIONS, SignalControlPanel, PlotControlPanel


def envelope(values, blocks=None):
    """Reduce a chunk to `blocks` min/max pairs (2*blocks points).

    Keeps the visible extremes -- a plain stride-based decimation would
    alias a triangle/sine down to whatever phase the stride happened to
    land on, and would hide short transients entirely. Returns the input
    unchanged when it is already short enough to be worth plotting raw.

    `blocks` defaults to config.PLOT_ENVELOPE_BLOCKS, read at CALL time. It
    used to be a default argument, which binds once at import and so pinned
    the value for the life of the process -- against the live-config rule
    the rest of the app follows, and a silent no-op the day the knob is put
    on the panel.
    """
    if blocks is None:
        blocks = config.PLOT_ENVELOPE_BLOCKS
    n = len(values)
    if blocks <= 0 or n == 0 or n <= 2 * blocks:
        return values

    per = n // blocks                      # samples per block, >= 2 here
    usable = per * blocks
    v = values[:usable].reshape(blocks, per)

    out = np.empty(2 * blocks, dtype=values.dtype)
    out[0::2] = v.min(axis=1)
    out[1::2] = v.max(axis=1)

    # Fold the remainder into the last block instead of dropping it. n is
    # rarely a multiple of `blocks`, and the leftover is the NEWEST end of
    # the window -- the part a live trace is actually being watched for. At
    # blocks=1 (the original PLOT_MODE="envelope" use) there is never a
    # remainder, which is why this never mattered until the display started
    # decimating to the axes' pixel width.
    tail = values[usable:]
    if tail.size:
        out[-2] = min(out[-2], tail.min())
        out[-1] = max(out[-1], tail.max())
    return out


class DualPlot:
    def __init__(self, buffer_size):
        self.buffer_size = buffer_size

        plt.ion()
        self.fig, (self.ax1, self.ax2) = plt.subplots(2, 1, sharex=True)
        plt.show(block=False)

        # Pack both control panels as siblings of the canvas widget inside
        # the same Tk window TkAgg already created for the figure -- no
        # second Tk root/mainloop, just two more widgets in the manager's
        # window. Layout: PlotControlPanel spans the full width as one row
        # along the bottom; SignalControlPanel is a column on the right;
        # the canvas+toolbar fill the remaining top-left area.
        #
        # Tk pack quirk that matters here: a side="top"/"bottom" slab (the
        # canvas/toolbar, packed by plt.show() above) always reserves the
        # FULL WIDTH of the cavity regardless of fill/expand, and a
        # side="left"/"right" slab always reserves the FULL HEIGHT --
        # packing our panels *after* the canvas/toolbar already claimed
        # the whole window doesn't place them around it, it places them in
        # whatever sliver is left (verified empirically: they ended up
        # stacked underneath, not surrounding it). Fix: pop the canvas and
        # toolbar out, pack our panels first in the order that matters --
        # PlotControlPanel (bottom, full width) *before*
        # SignalControlPanel (right) so the bottom bar isn't cut short by
        # the right column already having claimed part of the width --
        # then restore canvas+toolbar with their original pack config; they
        # land in the remaining top-left area.
        tk_window = self.fig.canvas.manager.window
        # Before packing anything: matplotlib's default figure size is small
        # enough that the control column leaves almost no room for the
        # traces. Fixed size from config.WINDOW_W/H, still resizable after.
        guiutil.size_window(tk_window)
        existing = [(child, child.pack_info()) for child in tk_window.pack_slaves()]
        for child, _ in existing:
            child.pack_forget()

        self.plot_control_panel = PlotControlPanel(tk_window, plot=self)
        self.plot_control_panel.frame.pack(side="bottom", fill="x")
        self.signal_control_panel = SignalControlPanel(tk_window)
        self.signal_control_panel.frame.pack(side="right", fill="y")

        for child, info in existing:
            info["in_"] = info.pop("in")
            child.pack(**info)

        # Capture buffers are PLOT_CAPTURE_FACTOR x the DISPLAYED window, so
        # the trigger has somewhere to slide the window back to. Everything
        # else -- buffer_size, the x-axis, PLOT_BUFFER's meaning -- still
        # refers to the displayed length only.
        self._cap_size = buffer_size * config.PLOT_CAPTURE_FACTOR
        self.ch1_in  = np.zeros(self._cap_size, dtype=CH1_DTYPE)
        self.ch2_in  = np.zeros(self._cap_size, dtype=CH2_DTYPE)
        self.ch1_out = np.zeros(self._cap_size, dtype=CH1_DTYPE)
        self.ch2_out = np.zeros(self._cap_size, dtype=CH2_DTYPE)

        # drawstyle="steps-mid": each sample renders as a flat segment
        # centered on its x position instead of a straight line sloping
        # into the next sample -- no interpolation is happening either
        # way (see prior discussion), this just makes each individual
        # sample's value visually distinct rather than implying a value in
        # between two samples that was never actually measured/sent.
        #
        # Set here as the starting value only: _apply_drawstyle() below
        # switches it per frame based on how many pixels each sample gets,
        # because steps-mid is invisible -- and not free -- once the window
        # holds more samples than the axes has pixels.
        self.line_ch1_in,  = self.ax1.plot(self.ch1_in[-buffer_size:],  color="blue", label="in",  animated=True, drawstyle="steps-mid")
        self.line_ch1_out, = self.ax1.plot(self.ch1_out[-buffer_size:], color="red",  label="out", animated=True, drawstyle="steps-mid")
        self.line_ch2_in,  = self.ax2.plot(self.ch2_in[-buffer_size:],  color="blue", label="in",  animated=True, drawstyle="steps-mid")
        self.line_ch2_out, = self.ax2.plot(self.ch2_out[-buffer_size:], color="red",  label="out", animated=True, drawstyle="steps-mid")
        self._lines_ax1 = (self.line_ch1_in, self.line_ch1_out)
        self._lines_ax2 = (self.line_ch2_in, self.line_ch2_out)

        # Channel name inside the axes, not a title above it: a title lives in
        # the gap between the two plots, and the gap is what we are trying to
        # get rid of. Part of the static background, so blitting keeps it.
        for ax, name in ((self.ax1, "Channel 1"), (self.ax2, "Channel 2")):
            ax.text(0.01, 0.94, name, transform=ax.transAxes, va="top",
                    fontsize=9, fontweight="bold", color="#444")

        for ax in (self.ax1, self.ax2):
            ax.legend(loc="upper right", fontsize=8, framealpha=0.8)
            ax.set_ylim(config.PLOT_MIN, config.PLOT_MAX)
            # Grid behind the traces, drawn once into the cached background.
            # Minor divisions as well as major: five y gridlines across the
            # wire's whole range is too coarse to line two traces up against.
            ax.set_axisbelow(True)
            self._style_grid(ax)
            # Engineering suffixes (2.1G) instead of matplotlib's shared
            # "1e9" offset text, which is drawn just above the axes -- i.e.
            # exactly in the gap the two plots no longer have.
            ax.yaxis.set_major_formatter(FuncFormatter(self._format_count_tick))
            # x-data stays the plain buffer index (0..buffer_size-1, oldest
            # to newest) -- only the tick labels are reinterpreted as time,
            # via ECG_SAMPLING_RATE (live-editable from the panel). Avoids
            # touching the rolling-buffer/blit code below, which only ever
            # calls set_ydata().
            #
            # Deliberately NOT SEND_RATE*CHUNK_SIZE: each buffer slot is one
            # sample pulled straight from the raw ECG buffer (signal_gen.py),
            # so consecutive slots are always 1/ECG_SAMPLING_RATE seconds
            # apart *in ECG time* -- SEND_RATE*CHUNK_SIZE only sets wall-clock
            # playback speed (how fast that ECG time is delivered), which only
            # equals ECG_SAMPLING_RATE at the 1x real-time default. Using it
            # here gave a correct-looking axis only by coincidence at that
            # default and a wrong one (e.g. wrong R-R spacing for the
            # configured heart rate) at any other SEND_RATE/CHUNK_SIZE.
            ax.xaxis.set_major_formatter(FuncFormatter(self._format_time_tick))
        self.ax1.set_xlim(0, buffer_size - 1)
        self.ax2.set_xlabel("Time (s)")
        # tight_layout for the outer margins, then close the gap between the
        # two axes: they share an x-axis and are meant to be read against each
        # other, so the less that separates them the better.
        self.fig.tight_layout()
        self.fig.subplots_adjust(hspace=config.PLOT_HSPACE)

        # Cache each axis's static background separately (their bboxes
        # differ), then re-cache whenever matplotlib does a full draw of
        # its own (resize, toolbar zoom, first show) -- otherwise blits
        # would paint onto a stale image.
        self._bg1 = None
        self._bg2 = None
        self.fig.canvas.mpl_connect("draw_event", self._on_draw)
        self.fig.canvas.draw()

        # ECG sampling rate (x-axis tick labels), y-limits, and buffer size
        # as of the last full draw -- see refresh(). All three are live-
        # editable from the panel but only take visible effect on a full
        # canvas.draw(), since blitting only redraws line artists, not
        # axes/ticks/limits baked into the cached background.
        self._last_time_rate = config.ECG_SAMPLING_RATE
        self._last_ylim = (config.PLOT_MIN, config.PLOT_MAX)
        self._last_buffer_size = buffer_size
        self._last_grid = (config.PLOT_GRID, config.PLOT_GRID_MODE)
        # Set by invalidate_view(): also put the x range back, since the
        # toolbar zooms both axes and only fixing y would leave the window
        # showing part of the buffer with no way back but the toolbar.
        self._reset_xlim = False

        self._drawstyle = "steps-mid"   # matches the plot() calls above

        # True when the buffers hold data set_ydata() hasn't been given
        # yet -- see sync().
        self._dirty = False

    @staticmethod
    def _format_time_tick(x, _pos):
        rate = config.ECG_SAMPLING_RATE
        return f"{x / rate:.2f}" if rate > 0 else ""

    def _apply_drawstyle(self):
        """Pick steps-mid or plain lines from the axes' current pixel width.

        steps-mid exists to stop the plot implying an interpolated value
        between two samples that was never measured -- worth having, but it
        can only be SEEN while a sample occupies more than a pixel or so.
        Above that density the steps are sub-pixel and the two styles render
        identically, while steps-mid still doubles the vertex count: measured
        3.03 ms per 2000-point line against 1.83 ms, i.e. ~4.8 ms of every
        frame for the four traces (2026-08-31, ~800 px wide). At
        PLOT_BUFFER=2000 a frame goes 13.3 ms -> 7.9 ms.

        Re-evaluated every frame rather than only at resize, because both
        inputs move: PLOT_BUFFER is live-editable and the window is
        resizable. It is one bbox read and a comparison.

        Safe to change mid-session: the traces are `animated`, so they are
        not part of the cached blit background and a style change needs no
        full redraw -- it simply applies on the next blit.
        """
        min_px = config.PLOT_STEPS_MIN_PX
        if min_px <= 0:
            style = "steps-mid"
        else:
            per_sample = self.ax1.bbox.width / max(self.buffer_size, 1)
            style = "steps-mid" if per_sample >= min_px else "default"
        if style == self._drawstyle:
            return
        self._drawstyle = style
        for line in self._lines_ax1 + self._lines_ax2:
            line.set_drawstyle(style)

    def _resize_buffers(self, new_size):
        """Reallocate the four rolling buffers to `new_size`, keeping the
        most recent samples from the old ones (zero-padded on the left if
        growing). Must also update the lines' x-data -- Line2D requires
        matching x/y lengths, and the old x-data (0..old_size-1) was fixed
        at plot() time in __init__."""
        cap_size = new_size * config.PLOT_CAPTURE_FACTOR

        def resized(old):
            new = np.zeros(cap_size, dtype=old.dtype)
            keep = min(cap_size, len(old))
            if keep:
                new[-keep:] = old[-keep:]
            return new

        self.ch1_in  = resized(self.ch1_in)
        self.ch2_in  = resized(self.ch2_in)
        self.ch1_out = resized(self.ch1_out)
        self.ch2_out = resized(self.ch2_out)
        self.buffer_size = new_size
        self._cap_size = cap_size

        x = np.arange(new_size)
        self.line_ch1_in.set_data(x, self.ch1_in[-new_size:])
        self.line_ch1_out.set_data(x, self.ch1_out[-new_size:])
        self.line_ch2_in.set_data(x, self.ch2_in[-new_size:])
        self.line_ch2_out.set_data(x, self.ch2_out[-new_size:])
        self.ax1.set_xlim(0, new_size - 1)

    # Dark enough to actually see: the first attempt at #b0b0b0/alpha 0.7
    # rendered as 215/255 grey, present in the pixels and invisible on screen.
    _GRID_MAJOR = dict(color="#8c8c8c", linewidth=0.7)
    _GRID_MINOR = dict(color="#bdbdbd", linewidth=0.5)

    @staticmethod
    def _style_grid(ax):
        """Apply PLOT_GRID / PLOT_GRID_MODE to one axes.

        Styles are passed ONLY when enabling: matplotlib treats
        grid(False, color=...) as a contradiction, warns, and turns the grid
        on anyway -- which made the checkbox a one-way switch.
        """
        if not config.PLOT_GRID:
            for which in ("major", "minor"):
                ax.grid(False, which=which)
            for axis in (ax.xaxis, ax.yaxis):
                axis.set_minor_locator(NullLocator())
            return

        ax.grid(True, which="major", **DualPlot._GRID_MAJOR)
        fine = config.PLOT_GRID_MODE == "fine"
        for axis in (ax.xaxis, ax.yaxis):
            axis.set_minor_locator(
                AutoMinorLocator(config.PLOT_GRID_FINE_DIVISIONS) if fine
                else NullLocator())
        if fine:
            ax.grid(True, which="minor", **DualPlot._GRID_MINOR)
        else:
            ax.grid(False, which="minor")

    @staticmethod
    def _format_count_tick(y, _pos):
        """Sample counts as 0 / 1.1G / 2.1G rather than 0..4 over a "1e9"."""
        for scale, suffix in ((1e9, "G"), (1e6, "M"), (1e3, "k")):
            if abs(y) >= scale:
                return f"{y / scale:.3g}{suffix}"
        return f"{y:.0f}"

    def invalidate_view(self):
        """Make the next refresh() re-apply every view setting from config,
        whether or not the values changed.

        refresh() normally only touches the axes when a config value differs
        from what it last drew -- cheap, and right for a value that only ever
        changes through the panel. It is wrong for the Apply button: the
        axes can also be moved by matplotlib's own toolbar (pan, zoom, home),
        which config knows nothing about. Without this, pressing Apply after
        a toolbar zoom did nothing at all -- the fields already matched
        config, so refresh() concluded there was nothing to do and left the
        view zoomed. Apply has to mean "put the view where these fields say",
        not "notice that these fields changed".
        """
        self._last_ylim = None
        self._last_time_rate = None
        self._last_grid = None
        self._reset_xlim = True

    def _on_draw(self, _event):
        self._bg1 = self.fig.canvas.copy_from_bbox(self.ax1.bbox)
        self._bg2 = self.fig.canvas.copy_from_bbox(self.ax2.bbox)

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

    # update_*() are called at PACKET rate, refresh() at FRAME rate. The
    # buffers themselves (self.ch1_in etc.) are updated immediately, every
    # packet, here -- that's cheap (a numpy slice assignment, negligible
    # even at hundreds of packets/s) and it's what keeps the rolling window
    # a correct, contiguous stream of every sample regardless of how often
    # the display redraws. Only set_ydata() (pushing the buffer into the
    # line artist -- the actually expensive part) is deferred to sync(),
    # which runs once per frame.
    #
    # An earlier version of this deferred the *buffer update itself* in
    # "scope" mode, keeping only the newest chunk and dropping any others
    # received between two frames -- at FRAME_RATE below SEND_RATE (e.g.
    # the 24 fps default against 50 pkt/s), that silently discarded roughly
    # half of every packet, and because the survivor was still appended as
    # if it were the immediately-next contiguous block, it left a real gap
    # in the signal -- not just fewer points shown, an actual discontinuity
    # where dropped samples should have been. That's what made the plot
    # look "corrupted" at low frame rates and fine at high ones: at higher
    # FRAME_RATE fewer packets got dropped per frame, purely by accident of
    # timing, not because anything was actually more correct.

    # config.PLOT_MODE is read here rather than frozen into a module-level
    # flag at import, for the same reason as envelope()'s blocks argument:
    # every other knob in this app is live-editable, and one that silently
    # is not is worse than one that is missing.
    @staticmethod
    def _scope():
        return config.PLOT_MODE == "scope"

    def update_input(self, ch1, ch2):
        scope = self._scope()
        values1 = ch1 if scope else envelope(ch1)
        values2 = ch2 if scope else envelope(ch2)
        self._rolled(self.ch1_in, values1)
        self._rolled(self.ch2_in, values2)
        self._dirty = True

    def update_output(self, ch1, ch2):
        scope = self._scope()
        values1 = ch1 if scope else envelope(ch1)
        values2 = ch2 if scope else envelope(ch2)
        self._rolled(self.ch1_out, values1)
        self._rolled(self.ch2_out, values2)
        self._dirty = True

    def _trigger_offset(self, ref):
        """Index in the capture buffer where the displayed window should
        start: the most recent upward crossing of the trigger level that
        still leaves a full window after it.

        Picking the LAST valid crossing rather than the first keeps the
        display as fresh as possible; because the crossings of a repeating
        waveform are one period apart, whichever one is chosen puts the
        trace at the same phase, which is the whole point.

        Falls back to the newest window -- i.e. exactly the old untriggered
        behaviour -- when triggering is off, the view range is degenerate,
        or the signal simply never crosses the level (a flat or
        below-threshold trace, which free-runs rather than blanking).
        """
        newest = self._cap_size - self.buffer_size
        if not config.PLOT_TRIGGER:
            return newest

        lo, hi = config.PLOT_MIN, config.PLOT_MAX
        if hi <= lo:
            return newest
        level = lo + config.PLOT_TRIGGER_LEVEL * (hi - lo)

        # Only positions <= newest can start a full window. float64 because
        # the buffers are big-endian wire dtypes and this keeps the
        # comparison free of byte-order and overflow surprises; it is a few
        # thousand elements twice a frame, not a hot path.
        candidates = ref[:newest + 1].astype(np.float64, copy=False)
        if candidates.size < 2:
            return newest
        crossings = np.flatnonzero((candidates[:-1] < level) & (candidates[1:] >= level))
        if crossings.size == 0:
            return newest
        return int(crossings[-1]) + 1

    def sync(self):
        """Push the (already up to date) buffers into the line artists.
        Once per frame -- this is the expensive part update_input/
        update_output avoid doing at packet rate."""
        if not self._dirty:
            return
        n = self.buffer_size
        # One offset per direction, taken from that direction's ch1 and
        # applied to both its channels: in/out are separated by the link's
        # round-trip delay so they need their own triggers, but ch1/ch2
        # share a time base and must stay aligned with each other.
        off_in  = self._trigger_offset(self.ch1_in)
        off_out = self._trigger_offset(self.ch1_out)
        self.line_ch1_in.set_ydata(self.ch1_in[off_in:off_in + n])
        self.line_ch2_in.set_ydata(self.ch2_in[off_in:off_in + n])
        self.line_ch1_out.set_ydata(self.ch1_out[off_out:off_out + n])
        self.line_ch2_out.set_ydata(self.ch2_out[off_out:off_out + n])
        self._dirty = False

    def dump_buffers(self, out_dir=None):
        """Write the currently displayed window of all four traces to a CSV
        and return the path.

        Dumps the DISPLAYED window (post-trigger), not the whole capture
        buffer, so the file contains exactly what is on screen -- the point
        is to be able to inspect a trace that looks wrong, which means the
        samples the eye is actually looking at.

        Called from a Tk button callback, which runs on the same thread as
        update_input/update_output (python_client.py's main loop pumps the Tk
        event loop via canvas.flush_events()). So the buffers cannot be
        half-updated underneath this -- no locking needed. It would need
        locking if the net thread ever wrote them directly.
        """
        n = self.buffer_size
        off_in  = self._trigger_offset(self.ch1_in)
        off_out = self._trigger_offset(self.ch1_out)

        # Widen to uint64 before writing: the buffers are big-endian wire
        # dtypes, and csv would otherwise emit numpy scalar reprs rather than
        # plain integers.
        cols = {
            "ch1_in":  self.ch1_in[off_in:off_in + n].astype(np.uint64),
            "ch2_in":  self.ch2_in[off_in:off_in + n].astype(np.uint64),
            "ch1_out": self.ch1_out[off_out:off_out + n].astype(np.uint64),
            "ch2_out": self.ch2_out[off_out:off_out + n].astype(np.uint64),
        }

        if out_dir is None:
            out_dir = Path(__file__).resolve().parent / "build" / "logs"
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        # One timestamp for both files so the data and the settings that
        # produced it are obviously a pair.
        stamp = _time.strftime("%Y%m%d_%H%M%S")
        path = out_dir / f"plotdump_{stamp}.csv"
        cfg_path = out_dir / f"plot_config_data_{stamp}.txt"

        rate = config.ECG_SAMPLING_RATE or 1
        with open(path, "w", newline="") as f:
            # Metadata as leading comment lines: a dump is worthless later
            # without the settings that produced it. numpy.genfromtxt and
            # pandas.read_csv both skip '#' when told to; a bare csv reader
            # does not, hence the header row below them.
            f.write(f"# samples={n} trigger={config.PLOT_TRIGGER} "
                    f"level={config.PLOT_TRIGGER_LEVEL}\n")
            f.write(f"# send_rate={config.SEND_RATE} chunk={config.CHUNK_SIZE} "
                    f"effective_sps={config.SEND_RATE * config.CHUNK_SIZE}\n")
            f.write(f"# ecg_rate={config.ECG_SAMPLING_RATE} "
                    f"amplitude={config.ECG_AMPLITUDE} hr={config.ECG_HEART_RATE}\n")
            f.write(f"# plot_min={config.PLOT_MIN} plot_max={config.PLOT_MAX} "
                    f"dtype={self.ch1_in.dtype}\n")
            w = csv.writer(f)
            w.writerow(["index", "time_s", *cols.keys()])
            for i in range(n):
                w.writerow([i, f"{i / rate:.6f}", *(int(c[i]) for c in cols.values())])

        self._dump_config(cfg_path, stamp, path.name, n, off_in, off_out)

        print(f"[plot] dumped {n} samples x {len(cols)} traces -> {path}")
        print(f"[plot] settings -> {cfg_path}")
        return path

    def _dump_config(self, cfg_path, stamp, data_name, n, off_in, off_out):
        """Snapshot every live setting next to the data dump.

        Reads config.py's public names reflectively rather than listing them:
        the whole point of the file is to still be complete a month from now,
        and a hand-maintained list silently rots every time a knob is added.
        Values that are not plain scalars are skipped -- they are numpy
        buffers and imported modules, not settings.
        """
        derived = [
            ("effective_samples_per_s", config.SEND_RATE * config.CHUNK_SIZE),
            ("displayed_samples",       n),
            ("capture_samples",         self._cap_size),
            ("wire_dtype",              str(self.ch1_in.dtype)),
            ("trigger_offset_in",       off_in),
            ("trigger_offset_out",      off_out),
        ]
        settings = {}
        for name in sorted(dir(config)):
            # isupper() alone is not enough: underscores have no case, so
            # private helpers like _WIRE_MAX pass it.
            if name.startswith("_") or not name.isupper():
                continue
            value = getattr(config, name)
            if isinstance(value, (bool, int, float, str, type(None))):
                settings[name] = value
            elif isinstance(value, (tuple, list)) and all(
                    isinstance(v, (bool, int, float, str)) for v in value):
                settings[name] = value

        # Group to match the window's own layout, so a whole missing tab is
        # obvious. GUI_SECTIONS is presentation only -- anything it does not
        # mention still gets written, under [ungrouped]. A knob added to the
        # GUI without being listed there loses its grouping, never its value.
        grouped, seen = [], set()
        for section, names in GUI_SECTIONS:
            rows = [(n, settings[n]) for n in names if n in settings]
            seen.update(n for n, _ in rows)
            if rows:
                grouped.append((section, rows))
        rest = [(n, v) for n, v in settings.items() if n not in seen]
        if rest:
            grouped.append(("ungrouped (not owned by any GUI section)", rest))

        width = max([len(k) for k, _ in derived]
                    + [len(k) for _, rows in grouped for k, _ in rows] or [0])
        with open(cfg_path, "w") as f:
            f.write(f"# olele plot settings snapshot -- {stamp}\n")
            f.write(f"# pairs with {data_name}\n")
            f.write(f"# {len(settings)} settings, grouped as the window lays them out\n\n")
            f.write("[derived]\n")
            for k, v in derived:
                f.write(f"{k:<{width}} = {v}\n")
            for section, rows in grouped:
                f.write(f"\n[{section}]\n")
                for k, v in rows:
                    f.write(f"{k:<{width}} = {v}\n")

            # Board-side state. Not in config.py -- it is reported BY the
            # board, not set from here -- but it is just as much a part of
            # "what produced this data": the filter registers decide what the
            # out traces are, and the metrics say whether the board was
            # keeping up while the window was captured.
            self._dump_board(f, width)


    @staticmethod
    def _dump_board(f, width):
        """Append the board's last config read-back and metrics.

        Both come from net as plain dicts assigned by the net thread, so a
        None here means "nothing received yet" -- either the link is down or,
        for the filter, nobody has pressed Read. Said explicitly rather than
        left as an empty section, because a blank section reads like a value
        of zero.
        """
        cfg = net.last_config
        f.write("\n[board filter registers (read back from fabric)]\n")
        if cfg is None:
            f.write("# none received -- press Read on the Board tab\n")
        else:
            for k in ("n_channels", "shift", "ctrl", "status"):
                v = cfg.get(k, 0)
                extra = ""
                if k == "ctrl":
                    extra = f"  # swap={bool(v & 0x1)} clear={bool(v & 0x2)}"
                elif k == "shift":
                    extra = "  # alpha = 1/2**shift; 0 = bypass"
                elif k == "status":
                    f.write(f"{k:<{width}} = 0x{v:08x}\n")
                    continue
                f.write(f"{k:<{width}} = {v}{extra}\n")

        met = net.last_metrics
        f.write("\n[board metrics (last 1 Hz report)]\n")
        if met is None:
            f.write("# none received -- board not connected?\n")
        else:
            for k, v in met.items():
                f.write(f"{k:<{width}} = {v}\n")

    def pump_events(self):
        """Service the Tk event loop without redrawing anything.

        Split out of refresh() so button clicks and typing are handled on
        their own (much faster) schedule -- see python_client.py. 0.08 ms.
        """
        self.fig.canvas.flush_events()

    def refresh(self):
        self._apply_drawstyle()
        self.sync()
        # Board tab is driven from here rather than its own timer: it needs a
        # main-thread tick and this is already one. It self-skips when nothing
        # new has arrived, so calling it at FRAME_RATE for 1 Hz data is free.
        self.signal_control_panel.poll_board()
        # Button labels and the status line are re-derived from the actual
        # state every frame rather than only written when a button is
        # pressed. If the two ever disagree -- a click that did not land, a
        # state changed from somewhere else -- the label is what the user
        # trusts, and a label that lies is what makes someone press again and
        # undo what they just did.
        self.signal_control_panel.poll_state()
        canvas = self.fig.canvas

        # ECG_SAMPLING_RATE (x-axis tick labels), PLOT_MIN/PLOT_MAX
        # (y-limits) and PLOT_BUFFER (rolling buffer size) are all live-
        # editable from the panel, but blitting only redraws line artists --
        # axes/ticks/limits are baked into the cached background, so any of
        # these actually changing needs one full canvas.draw() to show up
        # and get re-cached (see _on_draw).
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

        grid = (config.PLOT_GRID, config.PLOT_GRID_MODE)
        if grid != self._last_grid:
            self._last_grid = grid
            self._style_grid(self.ax1)
            self._style_grid(self.ax2)
            needs_full_draw = True

        if self._reset_xlim:
            self._reset_xlim = False
            self.ax1.set_xlim(0, self.buffer_size - 1)
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

        # flush_events() pumps the GUI event loop (keeps the window
        # responsive). The old plt.pause(0.001) that used to be here did
        # the same thing plus a redundant redraw, and is a known CPU sink.
        canvas.flush_events()
