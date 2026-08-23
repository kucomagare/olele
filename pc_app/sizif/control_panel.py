# Runtime control panels: two Tkinter frames placed around the plot's
# TkAgg canvas in the same window (same Tk root -- see plot.py's
# DualPlot.__init__), letting config.py's knobs be changed while
# python_client.py is running instead of edited and restarted.
#
# Split by what each knob is actually about, laid out differently since
# one has few, short fields and the other has more:
#   SignalControlPanel (right-side column) -- the ECG signal itself and how
#     fast it's generated/streamed: SEND_RATE / CHUNK_SIZE / ECG_HEART_RATE
#     / ECG_SAMPLING_RATE / ECG_AMPLITUDE / SEND_ENABLED / RECEIVE_ENABLED.
#   PlotControlPanel (bottom bar, one row) -- purely how the plot displays
#     that signal: PLOT_MIN / PLOT_MAX / PLOT_BUFFER / FRAME_RATE. None of
#     these affect what's generated or sent on the wire, only what's drawn
#     and how -- laid out horizontally via _add_entry_horizontal() rather
#     than the vertical _add_entry() SignalControlPanel uses, since it
#     spans the full window width as a single line instead of a sidebar.
#
# The Pause/Resume button toggles config.SEND_ENABLED -- net.py's send loop
# checks it every cycle (net.py:78), so pausing stops new packets going out
# but leaves the connection and receive path alone.
#
# Writes land directly on the config module's attributes. net.py and
# signal_gen.py read those attributes live (not values frozen at import
# time), so a change here takes effect within one send cycle -- no
# threading/locking needed, plain attribute assignment is atomic and the
# net thread just reads whatever's current. PLOT_MIN/PLOT_MAX/PLOT_BUFFER/
# FRAME_RATE are consumed the same way by plot.py's DualPlot.refresh() /
# python_client.py's main loop, which need a full canvas redraw (or a
# recomputed frame period) to apply them -- see there for why.

import tkinter as tk
from tkinter import ttk

import config


def _add_entry(frame, row, label, var, on_commit):
    ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", pady=2)
    entry = ttk.Entry(frame, textvariable=var, width=8)
    entry.grid(row=row, column=1, sticky="e", pady=2)
    entry.bind("<Return>", lambda _e: on_commit())
    entry.bind("<FocusOut>", lambda _e: on_commit())
    return row + 1


def _add_entry_horizontal(frame, col, label, var, on_commit):
    """Same as _add_entry but laid out left-to-right in a single row
    (label, entry, label, entry, ...) instead of stacked rows -- used by
    PlotControlPanel, which sits in one line along the bottom of the
    window rather than a sidebar column."""
    ttk.Label(frame, text=label).grid(row=0, column=col, sticky="w", padx=(0, 4))
    entry = ttk.Entry(frame, textvariable=var, width=8)
    entry.grid(row=0, column=col + 1, sticky="w", padx=(0, 16))
    entry.bind("<Return>", lambda _e: on_commit())
    entry.bind("<FocusOut>", lambda _e: on_commit())
    return col + 2


class SignalControlPanel:
    def __init__(self, parent):
        self.frame = ttk.Frame(parent, padding=8)

        self._send_rate = tk.StringVar(value=str(config.SEND_RATE))
        self._chunk_size = tk.StringVar(value=str(config.CHUNK_SIZE))
        self._heart_rate = tk.StringVar(value=str(config.ECG_HEART_RATE))
        self._ecg_sample_rate = tk.StringVar(value=str(config.ECG_SAMPLING_RATE))
        self._amplitude = tk.StringVar(value=f"{config.ECG_AMPLITUDE * 100:g}")
        self._receive_enabled = tk.BooleanVar(value=config.RECEIVE_ENABLED)
        self._rate_status = tk.StringVar()

        row = 0
        ttk.Label(self.frame, text="Signal", font=("", 10, "bold")).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=(0, 6))
        row += 1

        self._pause_button = ttk.Button(self.frame, text=self._pause_label(),
                                         command=self._toggle_pause)
        self._pause_button.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        row += 1

        row = _add_entry(self.frame, row, "Send rate (pkt/s)", self._send_rate, self._apply_send_rate)
        row = _add_entry(self.frame, row, "Chunk size (smp/pkt)", self._chunk_size, self._apply_chunk_size)
        row = _add_entry(self.frame, row, "Heart rate (bpm)", self._heart_rate, self._apply_heart_rate)
        row = _add_entry(self.frame, row, "ECG sample rate (Hz)", self._ecg_sample_rate,
                          self._apply_ecg_sample_rate)
        row = _add_entry(self.frame, row, "Amplitude (% of uint16)", self._amplitude,
                          self._apply_amplitude)

        ttk.Checkbutton(self.frame, text="Receive enabled", variable=self._receive_enabled,
                         command=self._apply_receive_enabled).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=2)
        row += 1

        ttk.Separator(self.frame, orient="horizontal").grid(
            row=row, column=0, columnspan=2, sticky="ew", pady=6)
        row += 1
        ttk.Label(self.frame, textvariable=self._rate_status, wraplength=160,
                  justify="left", foreground="#555").grid(
            row=row, column=0, columnspan=2, sticky="w")

        self._update_rate_status()

    def _update_rate_status(self):
        effective = config.SEND_RATE * config.CHUNK_SIZE
        native = config.ECG_SAMPLING_RATE
        speed = effective / native if native else 0.0
        self._rate_status.set(
            f"Effective: {effective:g} smp/s\nECG native: {native:g} smp/s\n"
            f"Playback: {speed:.2f}x real time"
        )

    def _apply_send_rate(self):
        try:
            value = float(self._send_rate.get())
            if value <= 0:
                raise ValueError
        except ValueError:
            self._send_rate.set(str(config.SEND_RATE))
            return
        config.SEND_RATE = value
        self._update_rate_status()

    def _apply_chunk_size(self):
        try:
            value = int(self._chunk_size.get())
        except ValueError:
            value = config.CHUNK_SIZE
        value = max(1, min(value, config.MAX_CHUNK_SIZE))
        self._chunk_size.set(str(value))
        config.CHUNK_SIZE = value
        self._update_rate_status()

    def _apply_heart_rate(self):
        try:
            value = int(self._heart_rate.get())
        except ValueError:
            value = config.ECG_HEART_RATE
        value = max(30, min(value, 220))
        self._heart_rate.set(str(value))
        config.ECG_HEART_RATE = value

    def _apply_ecg_sample_rate(self):
        try:
            value = int(self._ecg_sample_rate.get())
        except ValueError:
            value = config.ECG_SAMPLING_RATE
        value = max(50, min(value, 2000))  # 50 Hz floor keeps QRS shape
                                            # recognizable; 2000 Hz ceiling
                                            # is a sanity cap, well above
                                            # any real ECG hardware rate.
        self._ecg_sample_rate.set(str(value))
        config.ECG_SAMPLING_RATE = value
        self._update_rate_status()

    def _apply_amplitude(self):
        try:
            value = float(self._amplitude.get())
        except ValueError:
            value = config.ECG_AMPLITUDE * 100
        value = max(0.0, min(value, 100.0))
        self._amplitude.set(f"{value:g}")
        config.ECG_AMPLITUDE = value / 100.0

    def _pause_label(self):
        return "Resume" if not config.SEND_ENABLED else "Pause"

    def _toggle_pause(self):
        config.SEND_ENABLED = not config.SEND_ENABLED
        self._pause_button.config(text=self._pause_label())

    def _apply_receive_enabled(self):
        config.RECEIVE_ENABLED = self._receive_enabled.get()


class PlotControlPanel:
    def __init__(self, parent):
        self.frame = ttk.Frame(parent, padding=8)

        self._plot_min = tk.StringVar(value=str(config.PLOT_MIN))
        self._plot_max = tk.StringVar(value=str(config.PLOT_MAX))
        self._plot_buffer = tk.StringVar(value=str(config.PLOT_BUFFER))
        self._frame_rate = tk.StringVar(value=str(config.FRAME_RATE))

        col = 0
        ttk.Label(self.frame, text="Plot", font=("", 10, "bold")).grid(
            row=0, column=col, sticky="w", padx=(0, 16))
        col += 1

        col = _add_entry_horizontal(self.frame, col, "Y min", self._plot_min, self._apply_plot_ylim)
        col = _add_entry_horizontal(self.frame, col, "Y max", self._plot_max, self._apply_plot_ylim)
        col = _add_entry_horizontal(self.frame, col, "Buffer (smp)", self._plot_buffer,
                                     self._apply_plot_buffer)
        col = _add_entry_horizontal(self.frame, col, "Frame rate (fps)", self._frame_rate,
                                     self._apply_frame_rate)

    def _apply_plot_ylim(self):
        try:
            lo = float(self._plot_min.get())
            hi = float(self._plot_max.get())
            if hi <= lo:
                raise ValueError
        except ValueError:
            # Reject the whole pair on a bad edit rather than guessing which
            # field was wrong -- both snap back to config's last-good values.
            self._plot_min.set(str(config.PLOT_MIN))
            self._plot_max.set(str(config.PLOT_MAX))
            return
        config.PLOT_MIN = lo
        config.PLOT_MAX = hi

    def _apply_plot_buffer(self):
        try:
            value = int(self._plot_buffer.get())
        except ValueError:
            value = config.PLOT_BUFFER
        value = max(10, min(value, 100_000))  # sanity bounds, not a
                                               # protocol/hardware limit
        self._plot_buffer.set(str(value))
        config.PLOT_BUFFER = value

    def _apply_frame_rate(self):
        try:
            value = int(self._frame_rate.get())
        except ValueError:
            value = config.FRAME_RATE
        value = max(1, min(value, 60))  # plot.py's refresh() does a real
                                         # canvas draw/blit each frame --
                                         # 60 fps sanity cap keeps this from
                                         # becoming a self-inflicted CPU
                                         # hog (see config.py's FRAME_RATE
                                         # comment on measured cost).
        self._frame_rate.set(str(value))
        config.FRAME_RATE = value
