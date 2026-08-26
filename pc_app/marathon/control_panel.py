# Runtime control panels: two Tkinter frames placed around the plot's
# TkAgg canvas in the same window (same Tk root -- see plot.py's
# DualPlot.__init__), letting config.py's knobs be changed while
# python_client.py is running instead of edited and restarted.
#
# Split by what each knob is actually about, laid out differently since
# one has few, short fields and the other has more:
#   SignalControlPanel (right-side column) -- the ECG signal itself and how
#     fast it's generated/streamed, in three tabs:
#       Basic    -- SEND_RATE / CHUNK_SIZE / ECG_HEART_RATE /
#                   ECG_SAMPLING_RATE / ECG_AMPLITUDE / RECEIVE_ENABLED,
#                   plus the Pause/Resume button above the tabs.
#       Waveform -- nk.ecg_simulate()'s ECGSYN-model kwargs: ECG_METHOD,
#                   ECG_HEART_RATE_STD, ECG_LFHFRATIO, ECG_TI/AI/BI (each a
#                   P,Q,R,S,T 5-tuple), ECG_RANDOM_SEED.
#       Noise    -- ECG_NOISE (nk.ecg_simulate()'s own built-in noise), five
#                   independent colored-noise layers (ECG_NOISE_
#                   {VIOLET,BLUE,WHITE,PINK,BROWN}_ENABLED/_LEVEL, any
#                   combination active at once), and two sine-wave
#                   interference generators (ECG_SINE{1,2}_ENABLED/_FREQ/
#                   _PHASE/_LEVEL, e.g. for mains hum) -- see
#                   signal_gen.py's _simulate_raw()/_sine_contribution().
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


class _Tooltip:
    """Minimal hover tooltip -- Tk/ttk has no built-in one. A small
    borderless Toplevel appears near the widget on <Enter> and is
    destroyed on <Leave>; there's deliberately no delay/fade logic, this
    just needs to answer "what does this field do" on hover."""

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
    """Parse a comma-separated "a,b,c,d,e" entry (used for the ECGSYN
    model's ti/ai/bi wave parameters, each a 5-tuple for P,Q,R,S,T) back
    into a tuple of 5 floats. Falls back to the last-good value on any
    parse error or wrong count, same "reject the whole thing rather than
    guess" policy as the other validated fields."""
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


# Which config settings each part of the GUI owns, in GUI order. Used by
# DualPlot.dump_buffers() to group the settings snapshot the same way the
# window is laid out, so it is obvious at a glance that a whole tab was
# captured -- a flat alphabetical list makes a missing tab invisible.
#
# This is presentation only. The snapshot itself is built reflectively from
# config, and anything not listed here still gets written, under [ungrouped].
# So forgetting to add a new knob here costs you the grouping, never the value.
GUI_SECTIONS = (
    ("Plot bar", (
        "PLOT_MIN", "PLOT_MAX", "PLOT_BUFFER", "FRAME_RATE",
        "PLOT_TRIGGER", "PLOT_TRIGGER_LEVEL",
    )),
    ("Signal / Basic tab", (
        "SEND_RATE", "CHUNK_SIZE", "ECG_HEART_RATE", "ECG_SAMPLING_RATE",
        "ECG_AMPLITUDE", "SEND_ENABLED", "RECEIVE_ENABLED",
    )),
    ("Signal / Waveform tab", (
        "ECG_METHOD", "ECG_HEART_RATE_STD", "ECG_LFHFRATIO",
        "ECG_TI", "ECG_AI", "ECG_BI", "ECG_RANDOM_SEED",
    )),
    ("Signal / Noise tab", (
        "ECG_NOISE",
        "ECG_NOISE_VIOLET_ENABLED", "ECG_NOISE_VIOLET_LEVEL",
        "ECG_NOISE_BLUE_ENABLED",   "ECG_NOISE_BLUE_LEVEL",
        "ECG_NOISE_WHITE_ENABLED",  "ECG_NOISE_WHITE_LEVEL",
        "ECG_NOISE_PINK_ENABLED",   "ECG_NOISE_PINK_LEVEL",
        "ECG_NOISE_BROWN_ENABLED",  "ECG_NOISE_BROWN_LEVEL",
        "ECG_SINE1_ENABLED", "ECG_SINE1_FREQ", "ECG_SINE1_PHASE", "ECG_SINE1_LEVEL",
        "ECG_SINE2_ENABLED", "ECG_SINE2_FREQ", "ECG_SINE2_PHASE", "ECG_SINE2_LEVEL",
    )),
)


# (display name, config attr for "enabled", config attr for "level", beta,
#  short description of that color's character for its tooltip)
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

_METHODS = ["ecgsyn", "simple"]  # NOT "multileads" -- see config.py's
                                  # ECG_METHOD comment. A readonly combobox
                                  # (state="readonly" below) means the user
                                  # can only ever pick from this list.


class SignalControlPanel:
    def __init__(self, parent):
        self.frame = ttk.Frame(parent, padding=8)

        ttk.Label(self.frame, text="Signal", font=("", 10, "bold")).pack(anchor="w", pady=(0, 6))

        self._pause_button = ttk.Button(self.frame, text=self._pause_label(),
                                         command=self._toggle_pause)
        self._pause_button.pack(fill="x", pady=(0, 6))

        # Tabbed rather than one long stacked column -- Basic covers the
        # fields every session needs; Waveform/Noise are the nk.ecg_simulate
        # kwargs and nk.signal_noise() injection from the parameter survey,
        # most of which most sessions won't touch.
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

    # ------------------------------------------------------------------
    # Basic tab: the fields from before this session's parameter survey.
    # ------------------------------------------------------------------
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
                                     "effective streaming rate (samples/s). Rounded down to a "
                                     "multiple of 8: one packet is one DMA buffer, which must "
                                     "be a whole number of frames and a multiple of 32 bytes.")
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

    # ------------------------------------------------------------------
    # Waveform tab: nk.ecg_simulate()'s ECGSYN-model parameters.
    # ------------------------------------------------------------------
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

    # ------------------------------------------------------------------
    # Noise tab: nk.ecg_simulate()'s own noise param + a separate
    # nk.signal_noise()-based colored noise added on top.
    # ------------------------------------------------------------------
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

        # One row per color: checkbox + level entry, each independently
        # toggleable and summed together in signal_gen.py's _simulate_raw()
        # -- this replaced a single enabled/color/level combo that only
        # allowed one color active at a time.
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

        # Each sine gets its own checkbox + 3 stacked fields (freq, phase,
        # amplitude). Unlike the colored-noise rows, both generators can
        # differ in every parameter but are added IDENTICALLY to both
        # channels -- see signal_gen.py's _sine_contribution()/comment on
        # why (real interference like mains hum affects every channel the
        # same way, unlike the deliberately-decorrelated colored noise).
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
        value = max(0.0, min(value, 200.0))  # allow up to 2x the ECG's own
                                              # ptp per layer for a
                                              # genuinely noise-dominated
                                              # signal if wanted
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
        # Snap down to a whole number of DMA-safe groups. On marathon a
        # packet is one DMA buffer, which has to be a multiple of 32 bytes
        # AND a whole number of frames -- see CHUNK_SIZE_GRANULARITY in
        # config.py. Silently rounding beats accepting a value that would
        # leave the tested regime with no visible symptom; the entry box is
        # rewritten below so the user sees what actually took effect.
        gran = getattr(config, "CHUNK_SIZE_GRANULARITY", 1)
        if gran > 1:
            value = max(gran, (value // gran) * gran)
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
        # Bounds live in config.py next to the value they bound -- see the
        # comment there for why, and for the measurement behind the ceiling.
        value = max(config.ECG_SAMPLING_RATE_MIN,
                    min(value, config.ECG_SAMPLING_RATE_MAX))
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

    # ------------------------------------------------------------------
    # Waveform tab apply methods
    # ------------------------------------------------------------------
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

    # ------------------------------------------------------------------
    # Noise tab apply methods
    # ------------------------------------------------------------------
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
    def __init__(self, parent, plot=None):
        self.frame = ttk.Frame(parent, padding=8)
        # The DualPlot that owns the buffers, for the "Log buffer" button.
        # Optional so the panel stays constructible on its own; the button
        # just disables itself when there is nothing to dump.
        self._plot = plot

        self._plot_min = tk.StringVar(value=str(config.PLOT_MIN))
        self._plot_max = tk.StringVar(value=str(config.PLOT_MAX))
        self._plot_buffer = tk.StringVar(value=str(config.PLOT_BUFFER))
        self._frame_rate = tk.StringVar(value=str(config.FRAME_RATE))
        self._trigger_on = tk.BooleanVar(value=bool(config.PLOT_TRIGGER))
        self._trigger_level = tk.StringVar(value=str(config.PLOT_TRIGGER_LEVEL))

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

        # Scope trigger. Without it the window shows whatever phase happened
        # to be newest at frame time, which above a few thousand samples/s
        # means a different phase every frame -- see the PLOT_TRIGGER comment
        # in config.py. Untick to get the old free-running behaviour back.
        ttk.Checkbutton(self.frame, text="Trigger", variable=self._trigger_on,
                        command=self._apply_trigger).grid(
            row=0, column=col, sticky="w", padx=(0, 8))
        col += 1
        col = _add_entry_horizontal(self.frame, col, "Level (0-1)", self._trigger_level,
                                     self._apply_trigger)

        # Writes the on-screen window of all four traces to a CSV under
        # build/logs/. For inspecting a trace that looks wrong -- far more
        # useful than describing it, since the file carries the settings that
        # produced it alongside the samples.
        self._dump_button = ttk.Button(self.frame, text="Log buffer",
                                       command=self._dump_buffers)
        self._dump_button.grid(row=0, column=col, sticky="w", padx=(8, 4))
        if self._plot is None:
            self._dump_button.state(["disabled"])
        col += 1
        self._dump_status = ttk.Label(self.frame, text="")
        self._dump_status.grid(row=0, column=col, sticky="w")
        col += 1

    def _dump_buffers(self):
        if self._plot is None:
            return
        try:
            path = self._plot.dump_buffers()
        except Exception as exc:                      # noqa: BLE001
            # Never let a dump failure take down the plot: this is a
            # diagnostic, and losing the live view to save a file would be a
            # bad trade. Report it in the panel and in the log, carry on.
            self._dump_status.configure(text="dump failed")
            print(f"[plot] buffer dump failed: {exc}")
            return
        self._dump_status.configure(text=path.name)

    def _apply_trigger(self):
        config.PLOT_TRIGGER = bool(self._trigger_on.get())
        try:
            level = float(self._trigger_level.get())
        except ValueError:
            level = config.PLOT_TRIGGER_LEVEL
        # Clamped inside the view range rather than to [0, 1] exactly: a
        # level sitting on either limit can never be crossed, so it would
        # silently free-run instead of triggering.
        level = min(max(level, 0.01), 0.99)
        self._trigger_level.set(f"{level:g}")
        config.PLOT_TRIGGER_LEVEL = level

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
