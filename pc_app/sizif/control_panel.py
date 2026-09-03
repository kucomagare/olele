# Two Tkinter frames packed into the plot's TkAgg canvas window (see plot.py's
# DualPlot.__init__), editing config.py's knobs live instead of edit-and-restart.
# SignalControlPanel (right column) = ECG signal/streaming; PlotControlPanel
# (bottom bar) = display only. Writes land directly on config attributes, read
# live elsewhere -- no locking needed since plain attribute assignment is atomic.

import tkinter as tk
from tkinter import ttk

import config


class _Tooltip:
    """Minimal hover tooltip -- Tk/ttk has none built in."""

    def __init__(self, widget, text):
        self.widget = widget
        self.text = text
        self.tipwindow = None
        widget.bind("<Enter>", self._show, add="+")
        widget.bind("<Leave>", self._hide, add="+")

    def _show(self, _event=None):
        if self.tipwindow or not self.text:
            return
        x = self.widget.winfo_rootx() + 12
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        self.tipwindow = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        tk.Label(tw, text=self.text, justify="left", background="#ffffe0",
                 relief="solid", borderwidth=1, wraplength=260,
                 font=("", 9)).pack(ipadx=4, ipady=2)

    def _hide(self, _event=None):
        if self.tipwindow:
            self.tipwindow.destroy()
            self.tipwindow = None


def _add_entry(frame, row, label, var, on_commit, width=8, help_text=None):
    lbl = ttk.Label(frame, text=label)
    lbl.grid(row=row, column=0, sticky="w", pady=2)
    entry = ttk.Entry(frame, textvariable=var, width=width)
    entry.grid(row=row, column=1, sticky="e", pady=2)
    entry.bind("<Return>", lambda _e: on_commit())
    entry.bind("<FocusOut>", lambda _e: on_commit())
    if help_text:
        _Tooltip(lbl, help_text)
        _Tooltip(entry, help_text)
    return row + 1


def _parse_5tuple(text, fallback):
    """Parse "a,b,c,d,e" into 5 floats; falls back to last-good on any error."""
    try:
        parts = tuple(float(p.strip()) for p in text.split(","))
        if len(parts) != 5:
            raise ValueError
        return parts
    except ValueError:
        return fallback


def _format_5tuple(values):
    return ",".join(f"{v:g}" for v in values)


def _add_entry_horizontal(frame, col, label, var, on_commit):
    """Like _add_entry but laid left-to-right in one row (PlotControlPanel's bottom bar)."""
    ttk.Label(frame, text=label).grid(row=0, column=col, sticky="w", padx=(0, 4))
    entry = ttk.Entry(frame, textvariable=var, width=8)
    entry.grid(row=0, column=col + 1, sticky="w", padx=(0, 16))
    entry.bind("<Return>", lambda _e: on_commit())
    entry.bind("<FocusOut>", lambda _e: on_commit())
    return col + 2


# (name, enabled attr, level attr, beta, tooltip description)
_NOISE_ROWS = (
    ("Violet", "ECG_NOISE_VIOLET_ENABLED", "ECG_NOISE_VIOLET_LEVEL", -2,
     "emphasizes high frequencies (hiss-like)"),
    ("Blue", "ECG_NOISE_BLUE_ENABLED", "ECG_NOISE_BLUE_LEVEL", -1,
     "emphasizes high frequencies, less sharply than violet"),
    ("White", "ECG_NOISE_WHITE_ENABLED", "ECG_NOISE_WHITE_LEVEL", 0,
     "flat across all frequencies"),
    ("Pink", "ECG_NOISE_PINK_ENABLED", "ECG_NOISE_PINK_LEVEL", 1,
     "emphasizes low frequencies (rumble/drift-like)"),
    ("Brown", "ECG_NOISE_BROWN_ENABLED", "ECG_NOISE_BROWN_LEVEL", 2,
     "emphasizes low frequencies more strongly, closer to real baseline wander"),
)

# (display name, config attr for "enabled", "freq", "phase", "level")
_SINE_ROWS = (
    ("Sine 1", "ECG_SINE1_ENABLED", "ECG_SINE1_FREQ", "ECG_SINE1_PHASE", "ECG_SINE1_LEVEL"),
    ("Sine 2", "ECG_SINE2_ENABLED", "ECG_SINE2_FREQ", "ECG_SINE2_PHASE", "ECG_SINE2_LEVEL"),
)

_METHODS = ["ecgsyn", "simple"]  # NOT "multileads" -- readonly combobox, exhaustive.


class SignalControlPanel:
    def __init__(self, parent):
        self.frame = ttk.Frame(parent, padding=8)

        ttk.Label(self.frame, text="Signal", font=("", 10, "bold")).pack(anchor="w", pady=(0, 6))

        self._pause_button = ttk.Button(self.frame, text=self._pause_label(),
                                         command=self._toggle_pause)
        self._pause_button.pack(fill="x", pady=(0, 6))

        notebook = ttk.Notebook(self.frame)
        notebook.pack(fill="both", expand=True)
        basic = ttk.Frame(notebook, padding=6)
        waveform = ttk.Frame(notebook, padding=6)
        noise = ttk.Frame(notebook, padding=6)
        notebook.add(basic, text="Basic")
        notebook.add(waveform, text="Waveform")
        notebook.add(noise, text="Noise")

        self._build_basic_tab(basic)
        self._build_waveform_tab(waveform)
        self._build_noise_tab(noise)

        ttk.Separator(self.frame, orient="horizontal").pack(fill="x", pady=6)
        self._rate_status = tk.StringVar()
        ttk.Label(self.frame, textvariable=self._rate_status, wraplength=200,
                  justify="left", foreground="#555").pack(anchor="w")
        self._update_rate_status()

    # -- Basic tab --
    def _build_basic_tab(self, frame):
        self._send_rate = tk.StringVar(value=str(config.SEND_RATE))
        self._chunk_size = tk.StringVar(value=str(config.CHUNK_SIZE))
        self._heart_rate = tk.StringVar(value=str(config.ECG_HEART_RATE))
        self._ecg_sample_rate = tk.StringVar(value=str(config.ECG_SAMPLING_RATE))
        self._amplitude = tk.StringVar(value=f"{config.ECG_AMPLITUDE * 100:g}")
        self._receive_enabled = tk.BooleanVar(value=config.RECEIVE_ENABLED)

        row = 0
        row = _add_entry(frame, row, "Send rate (pkt/s)", self._send_rate, self._apply_send_rate,
                          help_text="How many packets/second are sent. Together with Chunk "
                                     "size, this sets the effective streaming rate (samples/s) "
                                     "-- see the status box below to check it against the ECG's "
                                     "own sample rate.")
        row = _add_entry(frame, row, "Chunk size (smp/pkt)", self._chunk_size, self._apply_chunk_size,
                          help_text="Samples per packet. Together with Send rate, sets the "
                                     "effective streaming rate (samples/s).")
        row = _add_entry(frame, row, "Heart rate (bpm)", self._heart_rate, self._apply_heart_rate,
                          help_text="Mean simulated heart rate. Actual beat-to-beat timing can "
                                     "vary slightly for realism -- see Heart rate std on the "
                                     "Waveform tab.")
        row = _add_entry(frame, row, "ECG sample rate (Hz)", self._ecg_sample_rate,
                          self._apply_ecg_sample_rate,
                          help_text="Native rate the ECG waveform itself is generated at. This "
                                     "is what the plot's Time axis is calculated from -- not "
                                     "Send rate/Chunk size, which only control delivery speed.")
        row = _add_entry(frame, row, "Amplitude (% of uint16)", self._amplitude,
                          self._apply_amplitude,
                          help_text="How much of the wire format's uint16 range (0-65535) the "
                                     "signal's peak-to-peak occupies, centered at the midpoint. "
                                     "A pure wire/display scale -- it does not change the "
                                     "waveform's shape, only its size.")

        rx = ttk.Checkbutton(frame, text="Receive enabled", variable=self._receive_enabled,
                              command=self._apply_receive_enabled)
        rx.grid(row=row, column=0, columnspan=2, sticky="w", pady=2)
        _Tooltip(rx, "When off, incoming (echoed) packets from the board/relay are ignored -- "
                      "the plot's \"out\" trace stops updating. Sending continues unaffected.")

    # -- Waveform tab: nk.ecg_simulate()'s ECGSYN-model parameters --
    def _build_waveform_tab(self, frame):
        self._method = tk.StringVar(value=config.ECG_METHOD)
        self._heart_rate_std = tk.StringVar(value=f"{config.ECG_HEART_RATE_STD:g}")
        self._lfhfratio = tk.StringVar(value=f"{config.ECG_LFHFRATIO:g}")
        self._ti = tk.StringVar(value=_format_5tuple(config.ECG_TI))
        self._ai = tk.StringVar(value=_format_5tuple(config.ECG_AI))
        self._bi = tk.StringVar(value=_format_5tuple(config.ECG_BI))
        self._random_seed = tk.StringVar(value=str(config.ECG_RANDOM_SEED))

        row = 0
        method_lbl = ttk.Label(frame, text="Method")
        method_lbl.grid(row=row, column=0, sticky="w", pady=2)
        method_box = ttk.Combobox(frame, textvariable=self._method, values=_METHODS,
                                   state="readonly", width=8)
        method_box.grid(row=row, column=1, sticky="e", pady=2)
        method_box.bind("<<ComboboxSelected>>", self._apply_method)
        method_help = ("\"ecgsyn\" (default) -- full dynamical model, realistic morphology, "
                        "every field below has an effect. \"simple\" -- cheaper wavelet "
                        "approximation of one cardiac cycle; verified it silently ignores "
                        "Heart rate std, LF/HF ratio, and the ti/ai/bi fields below (no error, "
                        "just no visible effect).")
        _Tooltip(method_lbl, method_help)
        _Tooltip(method_box, method_help)
        row += 1

        row = _add_entry(frame, row, "Heart rate std (bpm)", self._heart_rate_std,
                          self._apply_heart_rate_std,
                          help_text="Beat-to-beat heart rate variability. 0 = perfectly regular "
                                     "rhythm; higher values add realistic jitter between beats. "
                                     "Only affects the \"ecgsyn\" method.")
        row = _add_entry(frame, row, "LF/HF ratio", self._lfhfratio, self._apply_lfhfratio,
                          help_text="Low/high-frequency ratio of the heart-rate-variability "
                                     "power spectrum -- shapes HOW the beat-to-beat variability "
                                     "above is distributed over time, not how much of it there "
                                     "is. Only visible when Heart rate std > 0, and only "
                                     "affects the \"ecgsyn\" method.")

        ttk.Label(frame, text="Only affect \"ecgsyn\" method:", foreground="#777",
                  font=("", 8)).grid(row=row, column=0, columnspan=2, sticky="w", pady=(6, 2))
        row += 1
        row = _add_entry(frame, row, "P,Q,R,S,T angles (ti)", self._ti, self._apply_ti, width=18,
                          help_text="Angular position (degrees) of each wave in the cardiac "
                                     "cycle, comma-separated for P,Q,R,S,T in that order. "
                                     "Shifts the timing/spacing between waves. Default: "
                                     "-70,-15,0,15,100.")
        row = _add_entry(frame, row, "P,Q,R,S,T heights (ai)", self._ai, self._apply_ai, width=18,
                          help_text="Relative height of each wave (P,Q,R,S,T). Changing the "
                                     "RATIOS between them reshapes the waveform (e.g. a taller "
                                     "T wave) -- but scaling all five by the same factor has no "
                                     "visible effect, since the overall signal gets renormalized "
                                     "regardless (use Amplitude on the Basic tab for that "
                                     "instead). Default: 1.2,-5,30,-7.5,0.75.")
        row = _add_entry(frame, row, "P,Q,R,S,T widths (bi)", self._bi, self._apply_bi, width=18,
                          help_text="Width (spread) of each wave -- larger values widen that "
                                     "wave's bump, e.g. a wide value for T broadens the T wave "
                                     "noticeably. Default: 0.25,0.1,0.1,0.1,0.4.")
        row = _add_entry(frame, row, "Random seed", self._random_seed, self._apply_random_seed,
                          help_text="Seed for the random generator. The same seed always "
                                     "regenerates the identical waveform. Channel 2 always uses "
                                     "seed+1, so the two channels differ from each other but "
                                     "both stay reproducible.")

    # -- Noise tab: built-in ecg_simulate noise + separate colored noise layers --
    def _build_noise_tab(self, frame):
        self._ecg_noise = tk.StringVar(value=f"{config.ECG_NOISE:g}")

        row = 0
        row = _add_entry(frame, row, "Built-in noise", self._ecg_noise, self._apply_ecg_noise,
                          help_text="Amplitude of the small random noise the model itself adds "
                                     "while generating the waveform (Laplace-distributed). "
                                     "Baked into the model at generation time -- separate from "
                                     "the colored noise layers below, which are distinct signals "
                                     "added afterward. 0 = perfectly clean signal.")

        ttk.Separator(frame, orient="horizontal").grid(
            row=row, column=0, columnspan=3, sticky="ew", pady=6)
        row += 1
        ttk.Label(frame, text="Colored noise -- any combination can be active:",
                  foreground="#777", font=("", 8), wraplength=180, justify="left").grid(
            row=row, column=0, columnspan=3, sticky="w", pady=(0, 4))
        row += 1

        # One row per color, independently toggleable, summed in signal_gen.py's _simulate_raw().
        self._noise_vars = {}
        for name, enabled_attr, level_attr, beta, character in _NOISE_ROWS:
            enabled_var = tk.BooleanVar(value=getattr(config, enabled_attr))
            level_var = tk.StringVar(value=f"{getattr(config, level_attr) * 100:g}")
            self._noise_vars[enabled_attr] = (enabled_var, level_var)

            cb = ttk.Checkbutton(frame, text=name, variable=enabled_var,
                                  command=lambda a=enabled_attr, v=enabled_var:
                                  self._apply_noise_enabled(a, v))
            cb.grid(row=row, column=0, sticky="w", pady=2)
            _Tooltip(cb, f"beta={beta} -- {character}. Independent of the other colors; "
                          f"check any combination to layer them together.")

            entry = ttk.Entry(frame, textvariable=level_var, width=6)
            entry.grid(row=row, column=1, sticky="e", pady=2)
            commit = (lambda a=level_attr, v=level_var: self._apply_noise_level(a, v))
            entry.bind("<Return>", lambda _e, c=commit: c())
            entry.bind("<FocusOut>", lambda _e, c=commit: c())
            level_help = (f"{name} noise's strength, as a percentage of the ECG signal's OWN "
                           f"peak-to-peak swing -- so a given percentage means the same thing "
                           f"regardless of heart rate or Amplitude settings, and regardless of "
                           f"how many other colors are also active. Only has any effect while "
                           f"{name} is checked.")
            _Tooltip(entry, level_help)

            ttk.Label(frame, text="%").grid(row=row, column=2, sticky="w")
            row += 1

        ttk.Separator(frame, orient="horizontal").grid(
            row=row, column=0, columnspan=3, sticky="ew", pady=6)
        row += 1
        ttk.Label(frame, text="Sine interference -- e.g. mains hum, up to 2 at once:",
                  foreground="#777", font=("", 8), wraplength=180, justify="left").grid(
            row=row, column=0, columnspan=3, sticky="w", pady=(0, 4))
        row += 1

        # Each sine added identically to both channels (unlike noise rows) --
        # see signal_gen.py's _sine_contribution().
        self._sine_vars = {}
        for name, enabled_attr, freq_attr, phase_attr, level_attr in _SINE_ROWS:
            enabled_var = tk.BooleanVar(value=getattr(config, enabled_attr))
            freq_var = tk.StringVar(value=f"{getattr(config, freq_attr):g}")
            phase_var = tk.StringVar(value=f"{getattr(config, phase_attr):g}")
            level_var = tk.StringVar(value=f"{getattr(config, level_attr) * 100:g}")
            self._sine_vars[enabled_attr] = (enabled_var, freq_var, phase_var, level_var)

            cb = ttk.Checkbutton(frame, text=f"{name} enabled", variable=enabled_var,
                                  command=lambda a=enabled_attr, v=enabled_var:
                                  self._apply_sine_enabled(a, v))
            cb.grid(row=row, column=0, columnspan=3, sticky="w", pady=(4, 0))
            _Tooltip(cb, f"Adds a pure sine wave to BOTH channels identically (e.g. to "
                          f"simulate powerline hum) -- independent of the other sine generator "
                          f"and the colored noise above; any combination can be active.")
            row += 1

            row = _add_entry(frame, row, "  Frequency (Hz)", freq_var,
                              lambda a=freq_attr, v=freq_var: self._apply_sine_freq(a, v),
                              help_text=f"{name}'s frequency, evaluated at the ECG's own sample "
                                         f"rate -- exact regardless of Send rate/Chunk size "
                                         f"playback speed. Clamped below Nyquist (half the "
                                         f"current ECG sample rate) to avoid aliasing.")
            row = _add_entry(frame, row, "  Phase (deg)", phase_var,
                              lambda a=phase_attr, v=phase_var: self._apply_sine_phase(a, v),
                              help_text=f"{name}'s starting phase offset in degrees.")
            row = _add_entry(frame, row, "  Amplitude (%)", level_var,
                              lambda a=level_attr, v=level_var: self._apply_sine_level(a, v),
                              help_text=f"{name}'s strength, as a percentage of the ECG "
                                         f"signal's OWN peak-to-peak swing -- same convention "
                                         f"as the colored-noise levels above. Only has any "
                                         f"effect while \"{name} enabled\" is checked.")

    def _apply_sine_enabled(self, attr, var):
        setattr(config, attr, var.get())

    def _apply_sine_freq(self, attr, var):
        try:
            value = float(var.get())
            if value <= 0:
                raise ValueError
        except ValueError:
            value = getattr(config, attr)
        nyquist = config.ECG_SAMPLING_RATE / 2.0
        value = max(0.01, min(value, nyquist))
        var.set(f"{value:g}")
        setattr(config, attr, value)

    def _apply_sine_phase(self, attr, var):
        try:
            value = float(var.get())
        except ValueError:
            value = getattr(config, attr)
        var.set(f"{value:g}")
        setattr(config, attr, value)

    def _apply_sine_level(self, attr, var):
        try:
            value = float(var.get())
        except ValueError:
            value = getattr(config, attr) * 100
        value = max(0.0, min(value, 200.0))
        var.set(f"{value:g}")
        setattr(config, attr, value / 100.0)

    def _apply_noise_enabled(self, attr, var):
        setattr(config, attr, var.get())

    def _apply_noise_level(self, attr, var):
        try:
            value = float(var.get())
        except ValueError:
            value = getattr(config, attr) * 100
        value = max(0.0, min(value, 200.0))  # up to 2x the ECG's own ptp
        var.set(f"{value:g}")
        setattr(config, attr, value / 100.0)

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
        value = max(50, min(value, 2000))  # 50 Hz floor keeps QRS shape recognizable.
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

    # -- Waveform tab apply methods --
    def _apply_method(self, _event=None):
        config.ECG_METHOD = self._method.get()

    def _apply_heart_rate_std(self):
        try:
            value = float(self._heart_rate_std.get())
        except ValueError:
            value = config.ECG_HEART_RATE_STD
        value = max(0.0, min(value, 30.0))
        self._heart_rate_std.set(f"{value:g}")
        config.ECG_HEART_RATE_STD = value

    def _apply_lfhfratio(self):
        try:
            value = float(self._lfhfratio.get())
            if value <= 0:
                raise ValueError
        except ValueError:
            value = config.ECG_LFHFRATIO
        value = max(0.01, min(value, 20.0))
        self._lfhfratio.set(f"{value:g}")
        config.ECG_LFHFRATIO = value

    def _apply_ti(self):
        value = _parse_5tuple(self._ti.get(), config.ECG_TI)
        self._ti.set(_format_5tuple(value))
        config.ECG_TI = value

    def _apply_ai(self):
        value = _parse_5tuple(self._ai.get(), config.ECG_AI)
        self._ai.set(_format_5tuple(value))
        config.ECG_AI = value

    def _apply_bi(self):
        value = _parse_5tuple(self._bi.get(), config.ECG_BI)
        self._bi.set(_format_5tuple(value))
        config.ECG_BI = value

    def _apply_random_seed(self):
        try:
            value = int(self._random_seed.get())
        except ValueError:
            value = config.ECG_RANDOM_SEED
        value = max(0, value)
        self._random_seed.set(str(value))
        config.ECG_RANDOM_SEED = value

    # -- Noise tab apply methods --
    def _apply_ecg_noise(self):
        try:
            value = float(self._ecg_noise.get())
        except ValueError:
            value = config.ECG_NOISE
        value = max(0.0, min(value, 1.0))
        self._ecg_noise.set(f"{value:g}")
        config.ECG_NOISE = value

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
            # Reject the whole pair rather than guess which field was wrong.
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
        value = max(10, min(value, 100_000))  # sanity bounds, not a hard limit
        self._plot_buffer.set(str(value))
        config.PLOT_BUFFER = value

    def _apply_frame_rate(self):
        try:
            value = int(self._frame_rate.get())
        except ValueError:
            value = config.FRAME_RATE
        value = max(1, min(value, 60))  # each frame costs a real canvas draw/blit.
        self._frame_rate.set(str(value))
        config.FRAME_RATE = value
