# Runtime control panel: a Tkinter frame packed beside the plot's TkAgg
# canvas (same window, same Tk root -- see plot.py's DualPlot.__init__),
# letting SEND_RATE / CHUNK_SIZE / ECG_HEART_RATE / SEND_ENABLED /
# RECEIVE_ENABLED be changed while python_client.py is running instead of
# edited in config.py and restarted.
#
# The Pause/Resume button toggles config.SEND_ENABLED -- net.py's send loop
# checks it every cycle (net.py:78), so pausing stops new packets going out
# but leaves the connection and receive path alone.
#
# Writes land directly on the config module's attributes. net.py and
# signal_gen.py read those attributes live (not values frozen at import
# time), so a change here takes effect within one send cycle -- no
# threading/locking needed, plain attribute assignment is atomic and the
# net thread just reads whatever's current.

import tkinter as tk
from tkinter import ttk

import config


class ControlPanel:
    def __init__(self, parent):
        self.frame = ttk.Frame(parent, padding=8)

        self._send_rate = tk.StringVar(value=str(config.SEND_RATE))
        self._chunk_size = tk.StringVar(value=str(config.CHUNK_SIZE))
        self._heart_rate = tk.StringVar(value=str(config.ECG_HEART_RATE))
        self._receive_enabled = tk.BooleanVar(value=config.RECEIVE_ENABLED)
        self._rate_status = tk.StringVar()

        row = 0
        ttk.Label(self.frame, text="Controls", font=("", 10, "bold")).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=(0, 6))
        row += 1

        self._pause_button = ttk.Button(self.frame, text=self._pause_label(),
                                         command=self._toggle_pause)
        self._pause_button.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        row += 1

        row = self._add_entry(row, "Send rate (pkt/s)", self._send_rate, self._apply_send_rate)
        row = self._add_entry(row, "Chunk size (smp/pkt)", self._chunk_size, self._apply_chunk_size)
        row = self._add_entry(row, "Heart rate (bpm)", self._heart_rate, self._apply_heart_rate)

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

    def _add_entry(self, row, label, var, on_commit):
        ttk.Label(self.frame, text=label).grid(row=row, column=0, sticky="w", pady=2)
        entry = ttk.Entry(self.frame, textvariable=var, width=8)
        entry.grid(row=row, column=1, sticky="e", pady=2)
        entry.bind("<Return>", lambda _e: on_commit())
        entry.bind("<FocusOut>", lambda _e: on_commit())
        return row + 1

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

    def _pause_label(self):
        return "Resume" if not config.SEND_ENABLED else "Pause"

    def _toggle_pause(self):
        config.SEND_ENABLED = not config.SEND_ENABLED
        self._pause_button.config(text=self._pause_label())

    def _apply_receive_enabled(self):
        config.RECEIVE_ENABLED = self._receive_enabled.get()
