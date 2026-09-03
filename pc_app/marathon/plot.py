# DualPlot: matplotlib TkAgg canvas for ch1/ch2 in/out traces. No networking
# or packet parsing here. Perf: blitting (redraw only line artists over a
# cached bg, not a full draw) + envelope decimation (chunk -> min/max pairs)
# keep CPU down at 400k samples/s (measured 2026-08-17).

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
    """Reduce a chunk to `blocks` min/max pairs -- keeps visible extremes,
    where plain stride decimation would alias/hide short transients.
    `blocks` is read from config at call time (not a default arg) so later
    panel edits take effect instead of binding once at import."""
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

    # Fold remainder into the last block (not dropped) -- it's the newest
    # end of the window, the part actually being watched.
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

        # Pack both control panels as siblings of the canvas inside TkAgg's
        # own window (no second Tk root/mainloop). PlotControlPanel spans
        # the bottom, SignalControlPanel is a right column, canvas+toolbar
        # fill what's left.
        #
        # Tk quirk: a side="top/bottom" slab reserves the FULL WIDTH and
        # side="left/right" the FULL HEIGHT regardless of fill/expand, so
        # packing our panels after the canvas/toolbar (which already claimed
        # the window) would stack them underneath instead of around. Fix:
        # pop canvas+toolbar out, pack ours first (bottom bar before right
        # column, or the column cuts the bar short), then restore them.
        tk_window = self.fig.canvas.manager.window
        # Default figure size leaves almost no room for the control column.
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

        # Capture buffers are PLOT_CAPTURE_FACTOR x the displayed window, so
        # the trigger has room to slide the window back to.
        self._cap_size = buffer_size * config.PLOT_CAPTURE_FACTOR
        self.ch1_in  = np.zeros(self._cap_size, dtype=CH1_DTYPE)
        self.ch2_in  = np.zeros(self._cap_size, dtype=CH2_DTYPE)
        self.ch1_out = np.zeros(self._cap_size, dtype=CH1_DTYPE)
        self.ch2_out = np.zeros(self._cap_size, dtype=CH2_DTYPE)

        # steps-mid: each sample is a flat segment, not a slope, so no
        # interpolated value is implied. Starting value only -- see
        # _apply_drawstyle(), which switches per frame by pixel density.
        self.line_ch1_in,  = self.ax1.plot(self.ch1_in[-buffer_size:],  color="blue", label="in",  animated=True, drawstyle="steps-mid")
        self.line_ch1_out, = self.ax1.plot(self.ch1_out[-buffer_size:], color="red",  label="out", animated=True, drawstyle="steps-mid")
        self.line_ch2_in,  = self.ax2.plot(self.ch2_in[-buffer_size:],  color="blue", label="in",  animated=True, drawstyle="steps-mid")
        self.line_ch2_out, = self.ax2.plot(self.ch2_out[-buffer_size:], color="red",  label="out", animated=True, drawstyle="steps-mid")
        self._lines_ax1 = (self.line_ch1_in, self.line_ch1_out)
        self._lines_ax2 = (self.line_ch2_in, self.line_ch2_out)

        # In-axes label, not a title, to avoid reopening the inter-plot gap
        # we close below. Static, so blitting keeps it.
        for ax, name in ((self.ax1, "Channel 1"), (self.ax2, "Channel 2")):
            ax.text(0.01, 0.94, name, transform=ax.transAxes, va="top",
                    fontsize=9, fontweight="bold", color="#444")

        for ax in (self.ax1, self.ax2):
            ax.legend(loc="upper right", fontsize=8, framealpha=0.8)
            ax.set_ylim(config.PLOT_MIN, config.PLOT_MAX)
            # Grid (major+minor) drawn once into the cached background.
            ax.set_axisbelow(True)
            self._style_grid(ax)
            # Engineering suffixes (2.1G) instead of matplotlib's shared
            # "1e9" offset text, which would sit in the gap we closed.
            ax.yaxis.set_major_formatter(FuncFormatter(self._format_count_tick))
            # x-data stays the plain buffer index; ticks reinterpret it as
            # time via ECG_SAMPLING_RATE, deliberately not SEND_RATE*CHUNK_SIZE
            # -- each slot is one ECG sample, and ECG time only matches
            # wall-clock playback at the 1x real-time default.
            ax.xaxis.set_major_formatter(FuncFormatter(self._format_time_tick))
        self.ax1.set_xlim(0, buffer_size - 1)
        self.ax2.set_xlabel("Time (s)")
        # Close the inter-axes gap -- they share an x-axis, read together.
        self.fig.tight_layout()
        self.fig.subplots_adjust(hspace=config.PLOT_HSPACE)

        # Cached per-axis, re-cached on any full draw so blits don't paint
        # a stale image.
        self._bg1 = None
        self._bg2 = None
        self.fig.canvas.mpl_connect("draw_event", self._on_draw)
        self.fig.canvas.draw()

        # View settings as of the last full draw -- see refresh(). Blitting
        # only redraws line artists, so changes here need a full draw().
        self._last_time_rate = config.ECG_SAMPLING_RATE
        self._last_ylim = (config.PLOT_MIN, config.PLOT_MAX)
        self._last_buffer_size = buffer_size
        self._last_grid = (config.PLOT_GRID, config.PLOT_GRID_MODE)
        # Set by invalidate_view() -- also resets x range since the toolbar
        # zooms both axes and fixing only y would strand the view zoomed.
        self._reset_xlim = False

        self._drawstyle = "steps-mid"   # matches the plot() calls above

        # True when buffers hold data set_ydata() hasn't seen yet.
        self._dirty = False

    @staticmethod
    def _format_time_tick(x, _pos):
        rate = config.ECG_SAMPLING_RATE
        return f"{x / rate:.2f}" if rate > 0 else ""

    def _apply_drawstyle(self):
        """Pick steps-mid or plain lines from the axes' current pixel width.

        steps-mid is only visible once a sample occupies >~1px; below that
        it's sub-pixel-identical to a plain line but costs ~2x vertices
        (measured 3.03ms vs 1.83ms/line, 2026-08-31). Re-checked every frame
        since PLOT_BUFFER/window size can change live; safe mid-session
        since lines are `animated`, so a change just applies next blit.
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
        """Reallocate the 4 rolling buffers to `new_size` (zero-padded left
        if growing), and update the lines' x-data to match."""
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

        Style kwargs passed only when enabling -- grid(False, color=...) is
        treated by matplotlib as a contradiction and turns the grid ON.
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
        """Force the next refresh() to re-apply every view setting, even
        unchanged ones -- needed because the toolbar (pan/zoom/home) can
        move the axes without config knowing, which would otherwise make an
        unchanged Apply a silent no-op (refresh() only touches axes whose
        config value actually differs from what it last drew)."""
        self._last_ylim = None
        self._last_time_rate = None
        self._last_grid = None
        self._reset_xlim = True

    def _on_draw(self, _event):
        self._bg1 = self.fig.canvas.copy_from_bbox(self.ax1.bbox)
        self._bg2 = self.fig.canvas.copy_from_bbox(self.ax2.bbox)

    @staticmethod
    def _rolled(buf, values):
        """Shift `buf` left and append, in place -- np.roll allocates fresh
        each call, too costly at packet rate."""
        n = len(values)
        if n == 0:
            return buf
        if n >= len(buf):
            buf[:] = values[-len(buf):]
            return buf
        buf[:-n] = buf[n:]
        buf[-n:] = values
        return buf

    # update_*() run at PACKET rate, refresh() at FRAME rate: buffers update
    # immediately (cheap numpy slice) so the window stays a correct,
    # contiguous stream regardless of redraw rate; only the expensive
    # set_ydata() push is deferred to sync(), once per frame. (An earlier
    # version deferred the buffer update too in "scope" mode -- at
    # FRAME_RATE < SEND_RATE that silently dropped ~half of every packet.)

    # Read live from config (not frozen at import) -- same reasoning as
    # envelope()'s blocks argument.
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
        """Capture-buffer index where the displayed window should start: the
        most recent upward crossing of the trigger level with a full window
        left after it (last, not first, since a repeating waveform's phase
        is the same at any crossing). Falls back to the newest window when
        triggering is off, the range is degenerate, or it never crosses."""
        newest = self._cap_size - self.buffer_size
        if not config.PLOT_TRIGGER:
            return newest

        lo, hi = config.PLOT_MIN, config.PLOT_MAX
        if hi <= lo:
            return newest
        level = lo + config.PLOT_TRIGGER_LEVEL * (hi - lo)

        # float64 avoids the buffers' big-endian wire dtype byte-order/
        # overflow surprises; not a hot path.
        candidates = ref[:newest + 1].astype(np.float64, copy=False)
        if candidates.size < 2:
            return newest
        crossings = np.flatnonzero((candidates[:-1] < level) & (candidates[1:] >= level))
        if crossings.size == 0:
            return newest
        return int(crossings[-1]) + 1

    def sync(self):
        """Push the buffers into the line artists, once per frame -- the
        expensive part update_*() avoid at packet rate."""
        if not self._dirty:
            return
        n = self.buffer_size
        # One offset per direction (in/out have separate triggers, delayed by
        # link RTT); ch1/ch2 share a time base, so reuse ch1's offset.
        off_in  = self._trigger_offset(self.ch1_in)
        off_out = self._trigger_offset(self.ch1_out)
        self.line_ch1_in.set_ydata(self.ch1_in[off_in:off_in + n])
        self.line_ch2_in.set_ydata(self.ch2_in[off_in:off_in + n])
        self.line_ch1_out.set_ydata(self.ch1_out[off_out:off_out + n])
        self.line_ch2_out.set_ydata(self.ch2_out[off_out:off_out + n])
        self._dirty = False

    def dump_buffers(self, out_dir=None):
        """Write the currently displayed window (post-trigger, exactly
        what's on screen -- not the whole capture buffer) of all 4 traces
        to a CSV, return the path. Runs on the Tk callback thread, same as
        update_*(), so no locking needed."""
        n = self.buffer_size
        off_in  = self._trigger_offset(self.ch1_in)
        off_out = self._trigger_offset(self.ch1_out)

        # Widen to uint64 or csv emits numpy scalar reprs, not plain ints.
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
        # One timestamp for both files so data and the settings that
        # produced it are obviously a pair.
        stamp = _time.strftime("%Y%m%d_%H%M%S")
        path = out_dir / f"plotdump_{stamp}.csv"
        cfg_path = out_dir / f"plot_config_data_{stamp}.txt"

        rate = config.ECG_SAMPLING_RATE or 1
        with open(path, "w", newline="") as f:
            # '#' lines: skippable by genfromtxt/read_csv, harmless to a
            # bare csv reader.
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
        """Snapshot every live setting next to the data dump. Reads
        config.py's public names reflectively (not a hand-maintained list
        that would rot); non-scalar values are skipped."""
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
            # isupper() alone lets _WIRE_MAX-style private helpers through.
            if name.startswith("_") or not name.isupper():
                continue
            value = getattr(config, name)
            if isinstance(value, (bool, int, float, str, type(None))):
                settings[name] = value
            elif isinstance(value, (tuple, list)) and all(
                    isinstance(v, (bool, int, float, str)) for v in value):
                settings[name] = value

        # Group to match the window's layout; anything GUI_SECTIONS doesn't
        # mention still gets written, under [ungrouped].
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

            # Board-side state -- reported by the board, not set here, but
            # just as much "what produced this data" as the config above.
            self._dump_board(f, width)


    @staticmethod
    def _dump_board(f, width):
        """Append the board's last config read-back and metrics (plain
        dicts from the net thread; None means nothing received yet)."""
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
        """Service the Tk event loop without redrawing. Split out of
        refresh() so clicks/typing run on their own faster schedule."""
        self.fig.canvas.flush_events()

    def refresh(self):
        self._apply_drawstyle()
        self.sync()
        # Driven from here (not its own timer), since this is already a
        # main-thread tick; self-skips when nothing new arrived.
        self.signal_control_panel.poll_board()
        # Re-derived every frame, not just on click, so a click that didn't
        # land never leaves a stale label the user would trust.
        self.signal_control_panel.poll_state()
        canvas = self.fig.canvas

        # Live-editable but baked into the cached blit background -- any
        # change needs one full canvas.draw() to show and re-cache.
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

        # flush_events() keeps the window responsive; plt.pause() did the
        # same plus a redundant redraw.
        canvas.flush_events()
