# Two Tkinter frames beside plot.py's TkAgg canvas (same Tk root):
# SignalControlPanel (sidebar tabs) owns the ECG signal, PlotControlPanel
# (bottom bar) owns only display. Writes land directly on config attributes,
# read live (not frozen at import) by net/signal_gen/plot -- atomic, no
# locking needed, effective within one cycle.

import subprocess
import sys
import tkinter as tk
from pathlib import Path
from tkinter import ttk

import numpy as np

import config
import guiutil
import pipelines
import net
import packet_format
import runctl
import signal_gen

# sat.py (Static Analysis Tool) lives next to this file regardless of the
# working directory the app was launched from.
SAT_SCRIPT = Path(__file__).resolve().parent / "sat.py"

# Wire width, for the ADC-bits tooltip that quotes it. Derived from the
# packet format (and so from shared/marathon/packet_format.json) rather
# than written into the text -- which is how the Amplitude field once came
# to advertise sizif's uint16 range long after marathon moved to 32-bit
# slots.
_WIRE_BITS = np.dtype(packet_format.CH1_DTYPE).itemsize * 8


# Apply-button captions. The marker is the only signal that a typed or
# ticked change has not reached config yet, which without it reads as a
# control that simply does not work.
_APPLY_CLEAN = "Apply changes"
_APPLY_DIRTY = "Apply changes  \u25cf"
_APPLY_BAR_CLEAN = "Apply"
_APPLY_BAR_DIRTY = "Apply \u25cf"


class _Tooltip:
    """Minimal hover tooltip -- Tk/ttk has no built-in one."""

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
    # Enter = shortcut for Apply. No <FocusOut> binding on purpose: that
    # would apply on every change, exactly what the Apply button replaces.
    entry.bind("<Return>", lambda _e: on_commit())
    if help_text:
        _Tooltip(lbl, help_text)
        _Tooltip(entry, help_text)
    return row + 1


def _parse_5tuple(text, fallback):
    """Parse "a,b,c,d,e" into 5 floats; falls back to the last-good value
    on any parse error or wrong count (reject-the-whole-thing policy)."""
    try:
        parts = tuple(float(p.strip()) for p in text.split(","))
        if len(parts) != 5:
            raise ValueError
        return parts
    except ValueError:
        return fallback


def _format_5tuple(values):
    return ",".join(f"{v:g}" for v in values)


def _fmt_limit(value):
    """4294967295, not 4294967295.0 -- these are sample counts, so no
    wasted trailing ".0" in a field where every character counts."""
    value = float(value)
    return str(int(value)) if value.is_integer() else f"{value:g}"


def _add_entry_horizontal(frame, col, label, var, on_commit, width=8,
                          help_text=None):
    """_add_entry, laid out left-to-right in one row -- used by
    PlotControlPanel's single bottom-bar line instead of a sidebar."""
    ttk.Label(frame, text=label).grid(row=0, column=col, sticky="w", padx=(0, 3))
    entry = ttk.Entry(frame, textvariable=var, width=width)
    entry.grid(row=0, column=col + 1, sticky="w", padx=(0, 8))
    entry.bind("<Return>", lambda _e: on_commit())
    if help_text:
        _Tooltip(entry, help_text)
    return col + 2


# Which config settings each part of the GUI owns, in GUI order -- used by
# DualPlot.dump_buffers() to group the settings snapshot. Presentation
# only: an unlisted knob still dumps, just under [ungrouped].
GUI_SECTIONS = (
    ("Plot bar", (
        "PLOT_MIN", "PLOT_MAX", "PLOT_BUFFER", "FRAME_RATE",
        "PLOT_TRIGGER", "PLOT_TRIGGER_LEVEL",
        "PLOT_SHOW_CH1", "PLOT_SHOW_CH2", "PLOT_LINE_WIDTH",
        "PLOT_GRID", "PLOT_GRID_MODE", "PLOT_HSPACE",
        "PLOT_STEPS_MIN_PX", "UI_POLL_RATE",
    )),
    ("Session (Start / Mode, above the tabs)", (
        "AUTOSTART", "CH_MODE",
    )),
    ("Signal / Local tab", (
        "CH_PIPE", "CH_IMPL", "LOCAL_SHIFT",
        "PIPE2_HP_HZ", "PIPE2_NOTCH_HZ", "PIPE2_NOTCH_Q", "PIPE2_LP_HZ",
    )),
    ("Signal / Basic tab", (
        "SEND_RATE", "CHUNK_SIZE", "ECG_HEART_RATE", "ECG_SAMPLING_RATE",
        "ECG_AMPLITUDE_MV", "ECG_ENABLED",
        "SEND_ENABLED", "RECEIVE_ENABLED",
    )),
    ("Signal / Waveform tab", (
        "ECG_METHOD", "ECG_HEART_RATE_STD", "ECG_LFHFRATIO",
        "ECG_TI", "ECG_AI", "ECG_BI", "ECG_RANDOM_SEED",
    )),
    ("Signal / Noise tab", (
        "ECG_NOISE",
    ) + tuple(f"ECG_NOISE_{colour}_CH{ch}_{field}"
              for colour in ("VIOLET", "BLUE", "WHITE", "PINK", "BROWN")
              for ch in (1, 2) for field in ("ENABLED", "LEVEL_MV")
              ) + tuple(f"ECG_SINE{n}_CH{ch}_{field}"
              for n in range(1, 5) for ch in (1, 2)
              for field in ("ENABLED", "FREQ", "PHASE", "LEVEL_MV"))),
    ("Signal / ADC tab", (
        "GAIN_CH1", "GAIN_CH2", "V_OFFSET_CH1", "V_OFFSET_CH2",
        "VREF_PLUS", "VREF_MINUS", "ADC_BITS",
    )),
)

# Which config names each Defaults button restores -- same ownership map,
# minus _RUN_CONTROLS: those are the RUN, not a setting. Defaulting CH_MODE
# mid-session silently kills a running local-mode trace (switches back to
# board with nothing answering); SEND_ENABLED is Pause; AUTOSTART only
# matters at launch. A reset must not decide whether the signal is flowing.
_RUN_CONTROLS = ("CH_MODE", "SEND_ENABLED", "AUTOSTART")
_PLOT_BAR = "Plot bar"
PLOT_SETTINGS = tuple(n for title, names in GUI_SECTIONS
                      if title == _PLOT_BAR for n in names
                      if n not in _RUN_CONTROLS)
SIGNAL_SETTINGS = tuple(n for title, names in GUI_SECTIONS
                        if title != _PLOT_BAR for n in names
                        if n not in _RUN_CONTROLS)


# (display name, config colour key, beta, what it sounds/looks like)
_NOISE_ROWS = (
    ("Violet", "VIOLET", -2, "emphasizes high frequencies (hiss-like)"),
    ("Blue", "BLUE", -1, "emphasizes high frequencies, less sharply than violet"),
    ("White", "WHITE", 0, "flat across all frequencies"),
    ("Pink", "PINK", 1, "emphasizes low frequencies (rumble/drift-like)"),
    ("Brown", "BROWN", 2,
     "emphasizes low frequencies more strongly, closer to real baseline wander"),
)

_METHODS = ["ecgsyn", "simple"]  # NOT "multileads" -- see config.py's
                                  # ECG_METHOD comment.

# Controls batch behind an Apply button rather than writing config on every
# keystroke/click, so a multi-field change (e.g. SEND_RATE + CHUNK_SIZE)
# can't be caught half-applied. ACTIONs (Start/Stop, Pause, Log buffer,
# SAT, Board tab's own hardware-register Apply) stay immediate.
class SignalControlPanel:
    def __init__(self, parent):
        self.frame = ttk.Frame(parent, padding=8)
        # Commit callables, in creation order. apply_all() runs them all.
        self._commits = []
        # Editable input variables, for the pending-change marker.
        self._watched = []
        # config -> widget readers, for the Defaults button.
        self._reloads = []

        ttk.Label(self.frame, text="Signal", font=("", 10, "bold")).pack(anchor="w", pady=(0, 6))

        # Start/Stop is the SESSION (socket, generation, wire, filter
        # state); Pause is SEND_ENABLED only (connection/receive stay up).
        self._start_button = ttk.Button(self.frame, text=self._start_label(),
                                        command=self._toggle_run)
        self._start_button.pack(fill="x", pady=(0, 4))
        _Tooltip(self._start_button,
                 "Start or stop the run. Nothing is generated, connected or "
                 "sent until this is pressed -- set everything up first, then "
                 "start. Stopping closes the connection (board mode) and "
                 "clears the filter state (local mode).")

        # Mode picker: both worker threads are always alive and idle unless
        # they own the mode, so switching is just this attribute write --
        # see python_client.py. One row per channel (independent).
        self._mode = []
        for ch in range(2):
            mode_row = ttk.Frame(self.frame)
            mode_row.pack(fill="x", pady=(0, 2 if ch == 0 else 6))
            ttk.Label(mode_row, text=f"Ch{ch + 1}").pack(side="left", padx=(0, 6))
            var = self._cvar(tk.StringVar, lambda c=ch: config.CH_MODE[c])
            self._mode.append(var)
            self._commits.append(self._apply_mode)
            self._watch(var)
            for label, value, tip in (
                ("Board", "board",
                 "The real path: TCP to the relay, which forwards to the "
                 "board; the board filters this channel in fabric and echoes "
                 "it back."),
                ("Local", "local",
                 "This channel is processed here instead, by its Local-tab "
                 "pipeline. It is still SENT (both channels share one frame "
                 "and the board cannot be given just one), and the board "
                 "still filters it -- that answer is simply discarded and "
                 "replaced. For developing a pipeline before it is RTL, and "
                 "for working with no hardware present."),
            ):
                rb = ttk.Radiobutton(mode_row, text=label, value=value,
                                     variable=var,
                                     )
                rb.pack(side="left")
                _Tooltip(rb, tip)

        self._pause_button = ttk.Button(self.frame, text=self._pause_label(),
                                         command=self._toggle_pause)
        self._pause_button.pack(fill="x", pady=(0, 2))

        self._status_var = tk.StringVar(value="stopped")
        ttk.Label(self.frame, textvariable=self._status_var, foreground="#06c",
                  wraplength=200, justify="left").pack(anchor="w", pady=(0, 6))

        # Tabbed since most sessions only touch Basic; Waveform/Noise hold
        # the less-common nk.ecg_simulate/signal_noise kwargs.
        # Identity of the last dict rendered, so poll_board() can skip
        # frames where nothing new arrived (metrics land at 1 Hz vs. poll's
        # FRAME_RATE).
        self._last_metrics_seen = None
        self._last_config_seen = None

        notebook = ttk.Notebook(self.frame)
        tabs = {}
        for name in ("Basic", "Waveform", "Noise", "ADC", "Board", "Local"):
            tab = guiutil.ScrollFrame(notebook)
            notebook.add(tab.outer, text=name)
            tabs[name] = tab.body

        # Packed bottom-up, before the notebook: the notebook's expand=True
        # otherwise swallows the whole cavity and these widgets never map
        # (Tk's packer drops overflow silently, doesn't clip it).
        self._rate_status = tk.StringVar()
        ttk.Label(self.frame, textvariable=self._rate_status, wraplength=200,
                  justify="left", foreground="#555").pack(side="bottom",
                                                          anchor="w")
        ttk.Separator(self.frame, orient="horizontal").pack(side="bottom",
                                                            fill="x", pady=6)
        # Directly under the tabs so it's visible from any tab.
        buttons = ttk.Frame(self.frame)
        buttons.pack(side="bottom", fill="x", pady=(6, 0))
        self._apply_button = ttk.Button(buttons, text=_APPLY_CLEAN,
                                        command=self.apply_all)
        apply_btn = self._apply_button
        apply_btn.pack(side="left", fill="x", expand=True)
        defaults_btn = ttk.Button(buttons, text="Defaults",
                                  command=self.reset_defaults)
        defaults_btn.pack(side="left", padx=(6, 0))

        notebook.pack(fill="both", expand=True)

        self._build_basic_tab(tabs["Basic"])
        self._build_waveform_tab(tabs["Waveform"])
        self._build_noise_tab(tabs["Noise"])
        self._build_adc_tab(tabs["ADC"])
        self._build_board_tab(tabs["Board"])
        self._build_local_tab(tabs["Local"])

        _Tooltip(apply_btn,
                 "Apply every field in every tab at once. Nothing typed or "
                 "ticked here takes effect until this is pressed (Enter in "
                 "any field does the same). Start/Stop, Pause, Log buffer "
                 "and SAT act immediately -- they are actions, not "
                 "settings, and the Board tab keeps its own Apply because "
                 "that one writes hardware registers.")
        _Tooltip(defaults_btn,
                 "Put every field in every tab back to config.py's startup "
                 "value, and apply it now -- no second press. Does NOT touch "
                 "the run: Start/Stop, Pause and the Board/Local mode stay "
                 "as they are, and the Board tab keeps its registers (they "
                 "have their own Apply).")

        # Watch every control so the Apply button can flag pending edits.
        self._watch_vars()

        self._update_rate_status()

    # ------------------------------------------------------------------
    # Board tab: live metrics pushed by the board once a second, and the
    # TDM filter's runtime registers.
    # ------------------------------------------------------------------
    def _build_board_tab(self, frame):
        row = 0
        ttk.Label(frame, text="Metrics (1 Hz from board)",
                  font=("", 9, "bold")).grid(row=row, column=0, columnspan=2,
                                              sticky="w", pady=(0, 4))
        row += 1

        self._metric_vars = {}
        for key, label in _METRIC_ROWS:
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", padx=(0, 6))
            var = tk.StringVar(value="--")
            ttk.Label(frame, textvariable=var, width=12, anchor="e",
                      relief="sunken", padding=2).grid(row=row, column=1, sticky="w", pady=1)
            self._metric_vars[key] = var
            row += 1

        ttk.Separator(frame, orient="horizontal").grid(
            row=row, column=0, columnspan=2, sticky="ew", pady=6)
        row += 1

        ttk.Label(frame, text="TDM filter", font=("", 9, "bold")).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=(0, 4))
        row += 1

        self._filter_nchan = tk.StringVar(value="2")
        self._filter_shift = tk.StringVar(value="4")
        self._filter_swap = tk.BooleanVar(value=True)
        self._filter_clear = tk.BooleanVar(value=False)

        row = self._entry(frame, row, "N channels", self._filter_nchan, self._noop,
                          help_text="Slots per frame the filter expects, written to the "
                                     "filter's reg0. Nothing is sent until you press Apply.")
        row = self._entry(frame, row, "Shift (0=bypass)", self._filter_shift, self._noop,
                          help_text="IIR cutoff: alpha = 1/2**SHIFT, so bigger means more "
                                     "smoothing and more lag. 0 is exactly a bypass "
                                     "(y = y + (x-y) = x), which is the quickest way to check "
                                     "the datapath is transparent.")
        sw = ttk.Checkbutton(frame, text="Byte swap in fabric", variable=self._filter_swap)
        sw.grid(row=row, column=0, columnspan=2, sticky="w")
        _Tooltip(sw, "ctrl bit 0. The fabric swaps wire byte order so the CPU never has to -- "
                      "turning this off will produce garbage unless something else swaps.")
        row += 1
        cl = ttk.Checkbutton(frame, text="Clear filter state", variable=self._filter_clear)
        cl.grid(row=row, column=0, columnspan=2, sticky="w")
        _Tooltip(cl, "ctrl bit 1. Holds every channel's accumulator at zero, so the output "
                      "passes through unfiltered while set.")
        row += 1

        ttk.Separator(frame, orient="horizontal").grid(
            row=row, column=0, columnspan=2, sticky="ew", pady=6)
        row += 1
        ttk.Label(frame, text="UART logging", font=("", 9, "bold")).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=(0, 2))
        row += 1

        self._log_vars = {}
        for label, bitname, tip in _LOG_ROWS:
            var = tk.BooleanVar(value=True)
            cb = ttk.Checkbutton(frame, text=label, variable=var)
            cb.grid(row=row, column=0, columnspan=2, sticky="w")
            _Tooltip(cb, tip)
            self._log_vars[bitname] = var
            row += 1

        buttons = ttk.Frame(frame)
        buttons.grid(row=row, column=0, columnspan=2, sticky="w", pady=(4, 2))
        ttk.Button(buttons, text="Apply", command=self._apply_filter_config).pack(side="left")
        ttk.Button(buttons, text="Read", command=self._read_filter_config).pack(side="left", padx=4)
        row += 1

        self._filter_status = tk.StringVar(value="not read yet")
        ttk.Label(frame, textvariable=self._filter_status, wraplength=200,
                  justify="left", foreground="#555").grid(
            row=row, column=0, columnspan=2, sticky="w")

    @staticmethod
    def _noop():
        """Entry commit for fields only sent on Apply -- not per keystroke,
        since a half-typed value must never hit a live fabric register."""

    def _apply_filter_config(self):
        try:
            nchan = int(self._filter_nchan.get())
            shift = int(self._filter_shift.get())
        except ValueError:
            self._filter_status.set("n channels / shift must be integers")
            return
        ctrl = (0x1 if self._filter_swap.get() else 0) | \
               (0x2 if self._filter_clear.get() else 0)
        if net.request_config(net.CONFIG_OP_WRITE, nchan, shift, ctrl,
                              self._log_mask()):
            self._filter_status.set("apply sent, awaiting read-back...")
        else:
            self._filter_status.set("not sent -- link down?")

    def _log_mask(self):
        """Current UART mask from the checkboxes. Sent on every Apply
        (one WRITE carries the whole config), not just when a box changes."""
        mask = 0
        for bitname, var in self._log_vars.items():
            if var.get():
                mask |= getattr(packet_format, bitname)
        return mask

    def _read_filter_config(self):
        if net.request_config(net.CONFIG_OP_READ):
            self._filter_status.set("read sent...")
        else:
            self._filter_status.set("not sent -- link down?")

    def poll_board(self):
        """Refresh the Board tab from the net thread's last data. Called at
        FRAME_RATE against 1 Hz metrics, so checks identity first and skips
        frames where nothing new landed."""
        m = net.last_metrics
        if m is not None and m is not self._last_metrics_seen:
            self._last_metrics_seen = m
            # Derived (max-min), not transmitted -- can't disagree with
            # the two numbers it comes from.
            jitter = max(0, m.get("lat_max_us", 0) - m.get("lat_min_us", 0))
            for key, _ in _METRIC_ROWS:
                value = jitter if key == "lat_jitter" else m.get(key, 0)
                self._metric_vars[key].set(_format_metric(key, value))

        c = net.last_config
        if c is not None and c is not self._last_config_seen:
            self._last_config_seen = c
            self._filter_nchan.set(str(c["n_channels"]))
            self._filter_shift.set(str(c["shift"]))
            self._filter_swap.set(bool(c["ctrl"] & 0x1))
            self._filter_clear.set(bool(c["ctrl"] & 0x2))
            mask = c.get("log_mask", 0)
            for bitname, var in self._log_vars.items():
                var.set(bool(mask & getattr(packet_format, bitname)))
            self._filter_status.set(
                f"read back: n={c['n_channels']} shift={c['shift']} "
                f"ctrl=0x{c['ctrl']:x} log=0x{c.get('log_mask', 0):02x} "
                f"status=0x{c['status']:08x}")

    def _entry(self, frame, row, label, var, on_commit, width=8, help_text=None):
        """_add_entry, with the commit deferred to Apply instead of run now."""
        self._commits.append(on_commit)
        self._watch(var)
        return _add_entry(frame, row, label, var, self.apply_all, width, help_text)

    def _cvar(self, cls, source):
        """Config-backed Tk variable: seeded from source(), re-seeded by
        reset_defaults() -- one registration keeps both from drifting."""
        var = cls(value=source())
        self._reloads.append(lambda: var.set(source()))
        return var

    def _watch(self, *variables):
        """Track as editable inputs for the pending-change marker.
        Explicit list, not every Tk var on the panel: some are read-only
        displays (status line, board metrics) that must not count."""
        for var in variables:
            self._watched.append(var)
        return variables[0] if len(variables) == 1 else variables

    def _watch_vars(self):
        for var in self._watched:
            var.trace_add("write", lambda *_: self._mark_dirty())
        self._dirty = False

    def _mark_dirty(self):
        if getattr(self, "_dirty", False):
            return
        self._dirty = True
        if getattr(self, "_apply_button", None) is not None:
            self._apply_button.config(text=_APPLY_DIRTY)

    def apply_all(self):
        """Commit every pending field; each commit clamps its own value and
        writes it back, so Apply is also how you learn 2048 became 2000."""
        for commit in self._commits:
            try:
                commit()
            except Exception as exc:                  # noqa: BLE001
                # One bad field must not block the rest from applying.
                print(f"[panel] {getattr(commit, '__name__', commit)} "
                      f"failed: {exc}")
        # Commits re-trip the dirty trace by writing back -- clear after.
        self._dirty = False
        self._apply_button.config(text=_APPLY_CLEAN)
        self.poll_state()

    def reset_defaults(self):
        """Every setting this panel owns back to config.py's value, applied
        immediately. Leaves _RUN_CONTROLS and the Board tab (its own
        hardware-register Apply) untouched."""
        config.restore_defaults(SIGNAL_SETTINGS)
        for reload_var in self._reloads:
            reload_var()
        for ch in range(2):
            self._sync_impl(ch)
        self._dirty = False
        self._apply_button.config(text=_APPLY_CLEAN)
        self.poll_state()
        print("[panel] signal settings reset to defaults")

    # Local tab: in-process algorithm + params. Separate from the Board
    # tab (which writes hardware registers) even though "iir" models the
    # same filter, so it's never ambiguous which one just happened.
    def _build_local_tab(self, frame):
        row = 0
        ttk.Label(frame, text="Processing pipeline",
                  font=("", 9, "bold")).grid(row=row, column=0, columnspan=2,
                                              sticky="w", pady=(0, 4))
        row += 1

        # Two independent dropdown pairs, one per channel, built from
        # pipelines.PIPELINES -- a new pipeline appears with no edit here.
        self._pipe = []
        self._impl = []
        self._impl_combo = []
        for ch in range(2):
            ttk.Label(frame, text=f"Ch{ch + 1} pipe").grid(
                row=row, column=0, sticky="w", pady=2)
            pipe_var = self._cvar(tk.StringVar, lambda c=ch: config.CH_PIPE[c])
            self._pipe.append(pipe_var)
            pipe_combo = ttk.Combobox(frame, textvariable=pipe_var,
                                      values=sorted(pipelines.PIPELINES),
                                      width=8, state="readonly")
            pipe_combo.grid(row=row, column=1, sticky="e", pady=2)
            # Retargets the impl dropdown at once (display only, not a
            # config write -- that still waits for Apply).
            pipe_combo.bind(
                "<<ComboboxSelected>>",
                lambda _e, c=ch: self._sync_impl(c))
            self._commits.append(lambda c=ch: self._apply_pipe(c))
            self._watch(pipe_var)
            _Tooltip(pipe_combo,
                     "Which pipeline processes this channel.\n"
                     "bypass: passthrough, as a control case.\n"
                     "iir: bit-accurate model of axi_tdm_filter.vhd -- the "
                     "filter the board actually runs, including its "
                     "truncation bias and dead zone.\n"
                     "pipe1...: your pipelines. Add them in "
                     "pipelines.PIPELINES and they appear here.")
            row += 1

            ttk.Label(frame, text=f"Ch{ch + 1} impl").grid(
                row=row, column=0, sticky="w", pady=(0, 6))
            impl_var = self._cvar(tk.StringVar, lambda c=ch: config.CH_IMPL[c])
            self._impl.append(impl_var)
            impl_combo = ttk.Combobox(frame, textvariable=impl_var,
                                      width=8, state="readonly")
            impl_combo.grid(row=row, column=1, sticky="e", pady=(0, 6))
            # No binding -- _apply_pipe commits pipe and impl together.
            self._impl_combo.append(impl_combo)
            self._watch(impl_var)
            self._sync_impl(ch)
            _Tooltip(impl_combo,
                     "scipy: float64 via scipy.signal -- what the filter "
                     "SHOULD do. The place to decide cutoffs and response "
                     "shape.\n"
                     "manual: hand-written integer arithmetic -- what it WILL "
                     "do once it is RTL. This is the version that gets "
                     "translated to VHDL.\n"
                     "The difference between them is the quantisation error, "
                     "which is the reason both are kept.")
            row += 1

        self._local_shift = self._cvar(tk.StringVar, lambda: str(config.LOCAL_SHIFT))
        row = self._entry(frame, row, "Shift", self._local_shift,
                         self._apply_local_shift,
                         help_text="alpha = 1/2**shift, used by the iir "
                                   "pipeline only. 0 is an exact bypass, in "
                                   "the model for the same reason as in the "
                                   "fabric: y = y + (x - y) = x. This is the "
                                   "local counterpart of the board's shift "
                                   "register, kept separate because there is "
                                   "no hardware to write to here.")

        ttk.Separator(frame, orient="horizontal").grid(
            row=row, column=0, columnspan=2, sticky="ew", pady=6)
        row += 1
        ttk.Label(frame, text="pipe2 corners", font=("", 9, "bold")).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=(0, 2))
        row += 1

        # One set for both channels (filter design, not per-channel). 0
        # skips a stage, so does any corner at or above Nyquist.
        self._pipe2_hp = self._cvar(tk.StringVar,
                                    lambda: f"{config.PIPE2_HP_HZ:g}")
        self._pipe2_notch = self._cvar(tk.StringVar,
                                       lambda: f"{config.PIPE2_NOTCH_HZ:g}")
        self._pipe2_q = self._cvar(tk.StringVar,
                                   lambda: f"{config.PIPE2_NOTCH_Q:g}")
        self._pipe2_lp = self._cvar(tk.StringVar,
                                    lambda: f"{config.PIPE2_LP_HZ:g}")
        row = self._entry(frame, row, "  High-pass (Hz)", self._pipe2_hp,
                          lambda: self._apply_pipe2_freq("PIPE2_HP_HZ",
                                                         self._pipe2_hp),
                          help_text="Baseline-wander corner. The ST segment "
                                    "is nearly DC, so raising this tilts ST "
                                    "and can invent depression/elevation "
                                    "that is not there -- 0.05 Hz is the AHA "
                                    "diagnostic limit, 0.5 Hz monitoring. "
                                    "0 skips the stage.")
        row = self._entry(frame, row, "  Notch (Hz)", self._pipe2_notch,
                          lambda: self._apply_pipe2_freq("PIPE2_NOTCH_HZ",
                                                         self._pipe2_notch),
                          help_text="Mains frequency to reject: 50 in most of "
                                    "the world, 60 in North America. 0 skips "
                                    "the stage.")
        row = self._entry(frame, row, "  Notch Q", self._pipe2_q,
                          self._apply_pipe2_q,
                          help_text="How narrow the notch is: width in Hz is "
                                    "roughly notch/Q, so Q=30 at 50 Hz is "
                                    "~1.7 Hz wide. Wider takes a bite out of "
                                    "the QRS; narrower rings for longer after "
                                    "each beat.")
        row = self._entry(frame, row, "  Low-pass (Hz)", self._pipe2_lp,
                          lambda: self._apply_pipe2_freq("PIPE2_LP_HZ",
                                                         self._pipe2_lp),
                          help_text="Upper edge of the band: 150 Hz is the "
                                    "standard adult diagnostic bandwidth. "
                                    "Lower and QRS amplitude and notching "
                                    "start to go. 0 skips the stage.")

        ttk.Separator(frame, orient="horizontal").grid(
            row=row, column=0, columnspan=2, sticky="ew", pady=6)
        row += 1
        ttk.Label(frame, wraplength=190, justify="left", foreground="#555",
                  text="Applies to channels set to Local above. Those are "
                       "still sent (both share one frame), but the board's "
                       "answer for them is discarded and replaced by the "
                       "pipeline's. Changing pipe or impl resets that "
                       "channel's filter state. See pipelines.py."
                  ).grid(row=row, column=0, columnspan=2, sticky="w")

    def _apply_pipe2_freq(self, attr, var):
        """A pipe2 corner in Hz: 0 (skip) or below Nyquist, clamped against
        the LIVE ECG sample rate so it follows the Basic tab, not startup."""
        try:
            value = float(var.get())
            if value < 0:
                raise ValueError
        except ValueError:
            value = getattr(config, attr)
        nyquist = config.ECG_SAMPLING_RATE / 2.0
        if value >= nyquist:
            # The pipelines skip any stage at or above Nyquist, so a clamp
            # TO Nyquist would silently turn the stage off. Stay just below.
            clamped = float(f"{nyquist * 0.99:.4g}")
            print(f"[local] {attr} {value:g} Hz is at/above Nyquist "
                  f"({nyquist:g} Hz) -- using {clamped:g} Hz")
            value = clamped
        var.set(f"{value:g}")
        setattr(config, attr, value)

    def _apply_pipe2_q(self):
        try:
            value = float(self._pipe2_q.get())
            if value <= 0:
                raise ValueError
        except ValueError:
            value = config.PIPE2_NOTCH_Q
        value = min(max(value, 0.1), 1000.0)
        self._pipe2_q.set(f"{value:g}")
        config.PIPE2_NOTCH_Q = value

    def _sync_impl(self, ch):
        """Point the impl dropdown at what this pipeline offers. bypass/iir
        are fixed (no scipy/manual choice), so the dropdown greys out."""
        combo = self._impl_combo[ch]
        # The variable, not config: runs before Apply, so config still
        # holds the previous pipeline.
        choices = pipelines.implementations(self._pipe[ch].get())
        if choices:
            combo["values"] = choices
            combo["state"] = "readonly"
            if self._impl[ch].get() not in choices:
                self._impl[ch].set(choices[0])
        else:
            combo["values"] = ()
            self._impl[ch].set("--")
            combo["state"] = "disabled"

    def _apply_pipe(self, ch):
        """Commit pipeline + impl together -- writing pipeline alone would
        briefly pair it with the previous pipeline's implementation."""
        name = self._pipe[ch].get()
        if name in pipelines.PIPELINES:
            config.CH_PIPE[ch] = name
        impl = self._impl[ch].get()
        if impl in pipelines.implementations(config.CH_PIPE[ch]):
            config.CH_IMPL[ch] = impl

    def _apply_local_shift(self):
        try:
            value = int(self._local_shift.get())
        except ValueError:
            value = config.LOCAL_SHIFT
        value = max(0, min(value, config.LOCAL_SHIFT_MAX))
        self._local_shift.set(str(value))
        config.LOCAL_SHIFT = value

    def _start_label(self):
        return "Stop" if runctl.is_running() else "Start"

    def _toggle_run(self):
        runctl.toggle()
        self.poll_state()

    def _apply_mode(self):
        # Per channel, not a list replace -- a worker thread reading
        # mid-update sees one changed element, never a half-built list.
        for ch, var in enumerate(self._mode):
            config.CH_MODE[ch] = var.get()

    def _status_text(self):
        """One line for what the app is doing -- warming up / paused /
        board unreachable were previously visible only on the console."""
        if not runctl.is_running():
            return "stopped" if runctl.warm.is_set() else "stopped (warming up)"
        if not runctl.warm.is_set():
            return "starting (warming up)"
        paused = "" if config.SEND_ENABLED else ", paused"
        if config.all_local():
            return (f"running: local, "
                    f"ch1 {pipelines.label(config.CH_PIPE[0], config.CH_IMPL[0])}, "
                    f"ch2 {pipelines.label(config.CH_PIPE[1], config.CH_IMPL[1])}"
                    f"{paused}")
        # "connected" only means the relay took the connection -- it
        # forwards blindly, so a dead board can look healthy from the send
        # side (it did, for minutes, on 2026-09-01). Receive silence is
        # the one signal that tells them apart, so it outranks link_state.
        if net.rx_stale_s > config.RX_WATCHDOG_S:
            return (f"running: board, connected but NOTHING RECEIVED for "
                    f"{net.rx_stale_s:.0f}s -- board wedged or unplugged?{paused}")
        # Name locally-processed channels explicitly -- mixed mode looks
        # identical to all-board from the link's point of view.
        local = ", ".join(
            f"ch{i + 1} {pipelines.label(config.CH_PIPE[i], config.CH_IMPL[i])}"
            for i in range(2) if config.CH_MODE[i] == "local")
        mixed = f", local: {local}" if local else ""
        return f"running: board, {net.link_state}{mixed}{paused}"

    def poll_state(self):
        """Re-derive run/pause labels and the status line every frame;
        each widget touched only when its text actually changes."""
        for widget, text in ((self._start_button, self._start_label()),
                             (self._pause_button, self._pause_label())):
            if widget.cget("text") != text:
                widget.config(text=text)
        status = self._status_text()
        if self._status_var.get() != status:
            self._status_var.set(status)

    def _build_basic_tab(self, frame):
        V, S, B = self._cvar, tk.StringVar, tk.BooleanVar
        self._send_rate = V(S, lambda: str(config.SEND_RATE))
        self._chunk_size = V(S, lambda: str(config.CHUNK_SIZE))
        self._heart_rate = V(S, lambda: str(config.ECG_HEART_RATE))
        self._ecg_sample_rate = V(S, lambda: str(config.ECG_SAMPLING_RATE))
        self._amplitude = V(S, lambda: f"{config.ECG_AMPLITUDE_MV:g}")
        self._receive_enabled = V(B, lambda: config.RECEIVE_ENABLED)
        self._ecg_enabled = V(B, lambda: config.ECG_ENABLED)

        row = 0
        row = self._entry(frame, row, "Send rate (pkt/s)", self._send_rate, self._apply_send_rate,
                          help_text="How many packets/second are sent. Together with Chunk "
                                     "size, this sets the effective streaming rate (samples/s) "
                                     "-- see the status box below to check it against the ECG's "
                                     "own sample rate.")
        row = self._entry(frame, row, "Chunk size (smp/pkt)", self._chunk_size, self._apply_chunk_size,
                          help_text="Samples per packet. Together with Send rate, sets the "
                                     "effective streaming rate (samples/s). Rounded down to a "
                                     "multiple of 8: one packet is one DMA buffer, which must "
                                     "be a whole number of frames and a multiple of 32 bytes.")
        row = self._entry(frame, row, "Heart rate (bpm)", self._heart_rate, self._apply_heart_rate,
                          help_text="Mean simulated heart rate. Actual beat-to-beat timing can "
                                     "vary slightly for realism -- see Heart rate std on the "
                                     "Waveform tab.")
        row = self._entry(frame, row, "ECG sample rate (Hz)", self._ecg_sample_rate,
                          self._apply_ecg_sample_rate,
                          help_text="Native rate the ECG waveform itself is generated at. This "
                                     "is what the plot's Time axis is calculated from -- not "
                                     "Send rate/Chunk size, which only control delivery speed.")
        row = self._entry(frame, row, "Amplitude (mV pp)", self._amplitude,
                          self._apply_amplitude,
                          help_text="Peak-to-peak amplitude of the CLEAN ECG waveform, "
                                     "in mV (~1-2 mV is a typical limb-lead R-wave). "
                                     "Combines with the Noise tab's levels (also mV) "
                                     "and then the ADC tab's per-channel Gain/Offset "
                                     "and shared VREF/ADC bits before becoming a wire "
                                     "code -- see the ADC tab.")

        ecg_on = ttk.Checkbutton(frame, text="ECG enabled", variable=self._ecg_enabled)
        self._commits.append(self._apply_ecg_enabled)
        self._watch(self._ecg_enabled)
        ecg_on.grid(row=row, column=0, columnspan=2, sticky="w", pady=2)
        _Tooltip(ecg_on, "Off removes the heartbeat and leaves only the noise and sine "
                          "generators from the Noise tab, at exactly the levels they already "
                          "had -- their levels are absolute mV now, independent of the ECG, so "
                          "nothing jumps when you toggle this.")
        row += 1

        rx = ttk.Checkbutton(frame, text="Receive enabled",
                              variable=self._receive_enabled)
        self._commits.append(self._apply_receive_enabled)
        self._watch(self._receive_enabled)
        rx.grid(row=row, column=0, columnspan=2, sticky="w", pady=2)
        _Tooltip(rx, "When off, incoming (echoed) packets from the board/relay are ignored -- "
                      "the plot's \"out\" trace stops updating. Sending continues unaffected.")

    # Waveform tab: nk.ecg_simulate()'s ECGSYN-model parameters.
    def _build_waveform_tab(self, frame):
        V, S = self._cvar, tk.StringVar
        self._method = V(S, lambda: config.ECG_METHOD)
        self._heart_rate_std = V(S, lambda: f"{config.ECG_HEART_RATE_STD:g}")
        self._lfhfratio = V(S, lambda: f"{config.ECG_LFHFRATIO:g}")
        self._ti = V(S, lambda: _format_5tuple(config.ECG_TI))
        self._ai = V(S, lambda: _format_5tuple(config.ECG_AI))
        self._bi = V(S, lambda: _format_5tuple(config.ECG_BI))
        self._random_seed = V(S, lambda: str(config.ECG_RANDOM_SEED))

        row = 0
        method_lbl = ttk.Label(frame, text="Method")
        method_lbl.grid(row=row, column=0, sticky="w", pady=2)
        method_box = ttk.Combobox(frame, textvariable=self._method, values=_METHODS,
                                   state="readonly", width=8)
        method_box.grid(row=row, column=1, sticky="e", pady=2)
        self._commits.append(self._apply_method)
        self._watch(self._method)

        method_help = ("\"ecgsyn\" (default) -- full dynamical model, realistic morphology, "
                        "every field below has an effect. \"simple\" -- cheaper wavelet "
                        "approximation of one cardiac cycle; verified it silently ignores "
                        "Heart rate std, LF/HF ratio, and the ti/ai/bi fields below (no error, "
                        "just no visible effect).")
        _Tooltip(method_lbl, method_help)
        _Tooltip(method_box, method_help)
        row += 1

        row = self._entry(frame, row, "Heart rate std (bpm)", self._heart_rate_std,
                          self._apply_heart_rate_std,
                          help_text="Beat-to-beat heart rate variability. 0 = perfectly regular "
                                     "rhythm; higher values add realistic jitter between beats. "
                                     "Only affects the \"ecgsyn\" method.")
        row = self._entry(frame, row, "LF/HF ratio", self._lfhfratio, self._apply_lfhfratio,
                          help_text="Low/high-frequency ratio of the heart-rate-variability "
                                     "power spectrum -- shapes HOW the beat-to-beat variability "
                                     "above is distributed over time, not how much of it there "
                                     "is. Only visible when Heart rate std > 0, and only "
                                     "affects the \"ecgsyn\" method.")

        ttk.Label(frame, text="Only affect \"ecgsyn\" method:", foreground="#777",
                  font=("", 8)).grid(row=row, column=0, columnspan=2, sticky="w", pady=(6, 2))
        row += 1
        row = self._entry(frame, row, "P,Q,R,S,T angles (ti)", self._ti, self._apply_ti, width=18,
                          help_text="Angular position (degrees) of each wave in the cardiac "
                                     "cycle, comma-separated for P,Q,R,S,T in that order. "
                                     "Shifts the timing/spacing between waves. Default: "
                                     "-70,-15,0,15,100.")
        row = self._entry(frame, row, "P,Q,R,S,T heights (ai)", self._ai, self._apply_ai, width=18,
                          help_text="Relative height of each wave (P,Q,R,S,T). Changing the "
                                     "RATIOS between them reshapes the waveform (e.g. a taller "
                                     "T wave) -- but scaling all five by the same factor has no "
                                     "visible effect, since the overall signal gets renormalized "
                                     "regardless (use Amplitude on the Basic tab for that "
                                     "instead). Default: 1.2,-5,30,-7.5,0.75.")
        row = self._entry(frame, row, "P,Q,R,S,T widths (bi)", self._bi, self._apply_bi, width=18,
                          help_text="Width (spread) of each wave -- larger values widen that "
                                     "wave's bump, e.g. a wide value for T broadens the T wave "
                                     "noticeably. Default: 0.25,0.1,0.1,0.1,0.4.")
        row = self._entry(frame, row, "Random seed", self._random_seed, self._apply_random_seed,
                          help_text="Seed for the random generator. The same seed always "
                                     "regenerates the identical waveform. Channel 2 always uses "
                                     "seed+1, so the two channels differ from each other but "
                                     "both stay reproducible.")

    # Noise tab: nk.ecg_simulate()'s own noise param + a separate
    # nk.signal_noise()-based colored noise added on top.
    def _build_noise_tab(self, frame):
        self._ecg_noise = self._cvar(tk.StringVar, lambda: f"{config.ECG_NOISE:g}")

        row = 0
        row = self._entry(frame, row, "Built-in noise", self._ecg_noise, self._apply_ecg_noise,
                          help_text="Amplitude of the small random noise the model itself adds "
                                     "while generating the waveform (Laplace-distributed). "
                                     "Baked into the model at generation time -- separate from "
                                     "the colored noise layers below, which are distinct signals "
                                     "added afterward. 0 = perfectly clean signal.")

        ttk.Separator(frame, orient="horizontal").grid(
            row=row, column=0, columnspan=5, sticky="ew", pady=6)
        row += 1
        ttk.Label(frame, text="Colored noise -- per channel, any combination:",
                  foreground="#777", font=("", 8), wraplength=180, justify="left").grid(
            row=row, column=0, columnspan=5, sticky="w", pady=(0, 4))
        row += 1

        # One row per colour, both channels: independent, sum together --
        # see signal_gen's _simulate_raw().
        for col, head in enumerate(("", "Ch1", "mV", "Ch2", "mV")):
            ttk.Label(frame, text=head, foreground="#777",
                      font=("", 8)).grid(row=row, column=col, sticky="w")
        row += 1

        self._noise_vars = {}
        for name, colour, beta, character in _NOISE_ROWS:
            ttk.Label(frame, text=name).grid(row=row, column=0, sticky="w",
                                             padx=(0, 4), pady=1)
            for ch in (1, 2):
                enabled_attr, level_attr = signal_gen.noise_attrs(colour, ch)
                enabled_var = self._cvar(
                    tk.BooleanVar, lambda a=enabled_attr: getattr(config, a))
                level_var = self._cvar(
                    tk.StringVar,
                    lambda a=level_attr: f"{getattr(config, a):g}")
                self._noise_vars[(colour, ch)] = (enabled_var, level_var)

                cb = ttk.Checkbutton(frame, variable=enabled_var)
                cb.grid(row=row, column=1 + (ch - 1) * 2, sticky="w", pady=1)
                self._commits.append(lambda a=enabled_attr, v=enabled_var:
                                     self._apply_noise_enabled(a, v))
                self._watch(enabled_var)
                _Tooltip(cb, f"{name} noise on channel {ch}. beta={beta} -- "
                             f"{character}. Independent of the other colours "
                             f"and of the same colour on the other channel; "
                             f"any combination layers together, and the two "
                             f"channels are decorrelated even when set "
                             f"identically.")

                self._cell(frame, row, 2 + (ch - 1) * 2, level_var,
                           lambda a=level_attr, v=level_var:
                           self._apply_noise_level(a, v),
                           f"{name} noise on ch{ch}, peak-to-peak in mV -- an "
                           f"absolute physical quantity now, picked up at the "
                           f"electrode alongside the ECG (so it's amplified by "
                           f"the same ADC-tab Gain). Only applies while its "
                           f"box is ticked.")
            row += 1

        ttk.Separator(frame, orient="horizontal").grid(
            row=row, column=0, columnspan=5, sticky="ew", pady=6)
        row += 1
        ttk.Label(frame, text="Sine interference -- 4 generators, each set per channel:",
                  foreground="#777", font=("", 8), wraplength=180, justify="left").grid(
            row=row, column=0, columnspan=5, sticky="w", pady=(0, 4))
        row += 1

        # One block per generator: name, header row, one compact row per
        # channel (on/freq/phase/level) -- 12 rows instead of 32 stacked.
        self._sine_vars = {}
        for n in range(1, signal_gen.SINE_COUNT + 1):
            ttk.Label(frame, text=f"Sine {n}", font=("", 9, "bold")).grid(
                row=row, column=0, columnspan=5, sticky="w", pady=(6, 0))
            row += 1
            for col, head in enumerate(("", "Hz", "deg", "mV")):
                ttk.Label(frame, text=head, foreground="#777",
                          font=("", 8)).grid(row=row, column=col, sticky="w")
            row += 1

            for ch in (1, 2):
                enabled_attr, freq_attr, phase_attr, level_attr = \
                    signal_gen.sine_attrs(n, ch)
                V = self._cvar
                enabled_var = V(tk.BooleanVar,
                                lambda a=enabled_attr: getattr(config, a))
                freq_var = V(tk.StringVar,
                             lambda a=freq_attr: f"{getattr(config, a):g}")
                phase_var = V(tk.StringVar,
                              lambda a=phase_attr: f"{getattr(config, a):g}")
                level_var = V(tk.StringVar,
                              lambda a=level_attr: f"{getattr(config, a):g}")
                self._sine_vars[(n, ch)] = (enabled_var, freq_var,
                                            phase_var, level_var)

                cb = ttk.Checkbutton(frame, text=f"Ch{ch}", variable=enabled_var)
                cb.grid(row=row, column=0, sticky="w", pady=1)
                self._commits.append(lambda a=enabled_attr, v=enabled_var:
                                     self._apply_sine_enabled(a, v))
                self._watch(enabled_var)
                _Tooltip(cb, f"Add sine {n} to channel {ch}. Every generator is "
                             f"configured per channel: the same interference "
                             f"reaches two leads with a different amplitude and "
                             f"phase, and that difference is what a two-channel "
                             f"rejection scheme has to work with. Set both "
                             f"channels identically for common-mode.")

                self._cell(frame, row, 1, freq_var,
                           lambda a=freq_attr, v=freq_var:
                           self._apply_sine_freq(a, v),
                           f"Frequency in Hz, evaluated at the ECG's own sample "
                           f"rate -- exact regardless of Send rate/Chunk size. "
                           f"Clamped below Nyquist (half the ECG sample rate).")
                self._cell(frame, row, 2, phase_var,
                           lambda a=phase_attr, v=phase_var:
                           self._apply_sine_phase(a, v),
                           f"Starting phase in degrees. A difference between "
                           f"Ch1 and Ch2 here is what makes the interference "
                           f"non-common-mode.")
                self._cell(frame, row, 3, level_var,
                           lambda a=level_attr, v=level_var:
                           self._apply_sine_level(a, v),
                           f"Peak-to-peak amplitude in mV, absolute -- equal "
                           f"numbers on both channels mean equal amplitude "
                           f"with no shared reference needed. Only applies "
                           f"while Ch{ch} is ticked.")
                row += 1

    # ADC tab: per-channel gain/offset, then the shared ADC's VREF/bits.
    def _build_adc_tab(self, frame):
        V, S = self._cvar, tk.StringVar
        self._gain_ch1 = V(S, lambda: f"{config.GAIN_CH1:g}")
        self._gain_ch2 = V(S, lambda: f"{config.GAIN_CH2:g}")
        self._v_offset_ch1 = V(S, lambda: f"{config.V_OFFSET_CH1:g}")
        self._v_offset_ch2 = V(S, lambda: f"{config.V_OFFSET_CH2:g}")
        self._vref_plus = V(S, lambda: f"{config.VREF_PLUS:g}")
        self._vref_minus = V(S, lambda: f"{config.VREF_MINUS:g}")
        self._adc_bits = V(S, lambda: str(config.ADC_BITS))

        row = 0
        ttk.Label(frame, text="Per-channel analog front end", font=("", 9, "bold")
                  ).grid(row=row, column=0, columnspan=2, sticky="w", pady=(0, 4))
        row += 1
        row = self._entry(frame, row, "Gain Ch1 (V/V)", self._gain_ch1,
                          self._apply_gain_ch1,
                          help_text="Analog gain applied to channel 1's physical "
                                     "signal (ECG + noise + sine, already summed in "
                                     "mV) before the ADC -- models an "
                                     "instrumentation-amplifier stage. Noise/sine are "
                                     "picked up at the electrode too, so they're "
                                     "amplified along with the ECG.")
        row = self._entry(frame, row, "Gain Ch2 (V/V)", self._gain_ch2,
                          self._apply_gain_ch2,
                          help_text="Same as Gain Ch1, independent per channel.")
        row = self._entry(frame, row, "Offset Ch1 (V)", self._v_offset_ch1,
                          self._apply_v_offset_ch1,
                          help_text="DC bias added AFTER Gain Ch1, volts -- lifts "
                                     "the amplified (naturally bipolar) signal into "
                                     "the shared ADC's [VREF-, VREF+] input window.")
        row = self._entry(frame, row, "Offset Ch2 (V)", self._v_offset_ch2,
                          self._apply_v_offset_ch2,
                          help_text="Same as Offset Ch1, independent per channel.")

        ttk.Separator(frame, orient="horizontal").grid(
            row=row, column=0, columnspan=2, sticky="ew", pady=6)
        row += 1
        ttk.Label(frame, text="Shared ADC", font=("", 9, "bold")
                  ).grid(row=row, column=0, columnspan=2, sticky="w", pady=(0, 4))
        row += 1
        row = self._entry(frame, row, "VREF+ (V)", self._vref_plus,
                          self._apply_vref_plus,
                          help_text="The ADC's positive reference -- the highest "
                                     "voltage it can digitize. One shared ADC for "
                                     "both channels (matches marathon's real "
                                     "hardware, a single physical ADC multiplexed "
                                     "via TDM). Anything above this clips, exactly "
                                     "like real hardware -- also the plot's Y-axis "
                                     "upper bound.")
        row = self._entry(frame, row, "VREF- (V)", self._vref_minus,
                          self._apply_vref_minus,
                          help_text="The ADC's negative reference. Need not be 0 -- "
                                     "some real ADCs reference below ground. Must "
                                     "stay below VREF+.")
        row = self._entry(frame, row, "ADC bits", self._adc_bits,
                          self._apply_adc_bits,
                          help_text=f"ADC resolution. Real ECG AFEs top out around "
                                     f"24-bit (TI ADS1298); the wire slot stays "
                                     f"{_WIRE_BITS}-bit regardless, right-aligned -- "
                                     f"the upper bits are zero, so a lower value here "
                                     f"also gives a numerically smaller sample, not "
                                     f"just a less precise one. Sweep this down live "
                                     f"to see quantisation noise appear on the plot.")

    def _apply_gain_ch1(self):
        try:
            value = float(self._gain_ch1.get())
        except ValueError:
            value = config.GAIN_CH1
        value = max(0.0, value)
        self._gain_ch1.set(f"{value:g}")
        config.GAIN_CH1 = value

    def _apply_gain_ch2(self):
        try:
            value = float(self._gain_ch2.get())
        except ValueError:
            value = config.GAIN_CH2
        value = max(0.0, value)
        self._gain_ch2.set(f"{value:g}")
        config.GAIN_CH2 = value

    def _apply_v_offset_ch1(self):
        try:
            value = float(self._v_offset_ch1.get())
        except ValueError:
            value = config.V_OFFSET_CH1
        self._v_offset_ch1.set(f"{value:g}")
        config.V_OFFSET_CH1 = value

    def _apply_v_offset_ch2(self):
        try:
            value = float(self._v_offset_ch2.get())
        except ValueError:
            value = config.V_OFFSET_CH2
        self._v_offset_ch2.set(f"{value:g}")
        config.V_OFFSET_CH2 = value

    def _apply_vref_plus(self):
        try:
            value = float(self._vref_plus.get())
        except ValueError:
            value = config.VREF_PLUS
        value = max(value, config.VREF_MINUS + 1e-6)
        self._vref_plus.set(f"{value:g}")
        config.VREF_PLUS = value

    def _apply_vref_minus(self):
        try:
            value = float(self._vref_minus.get())
        except ValueError:
            value = config.VREF_MINUS
        value = min(value, config.VREF_PLUS - 1e-6)
        self._vref_minus.set(f"{value:g}")
        config.VREF_MINUS = value

    def _apply_adc_bits(self):
        try:
            value = int(self._adc_bits.get())
        except ValueError:
            value = config.ADC_BITS
        value = max(config.ADC_BITS_MIN, min(value, config.ADC_BITS_MAX))
        self._adc_bits.set(str(value))
        config.ADC_BITS = value

    def _cell(self, frame, row, col, var, on_commit, help_text=None):
        """Bare entry at a grid position (no own label, for tables with
        column headers). Commit deferred to Apply, like _entry."""
        entry = ttk.Entry(frame, textvariable=var, width=6)
        entry.grid(row=row, column=col, sticky="w", padx=(0, 4), pady=1)
        entry.bind("<Return>", lambda _e: self.apply_all())
        self._commits.append(on_commit)
        self._watch(var)
        if help_text:
            _Tooltip(entry, help_text)
        return entry

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
            value = getattr(config, attr)
        value = max(0.0, min(value, 50.0))  # mV; ceiling just catches typos
        var.set(f"{value:g}")
        setattr(config, attr, value)

    def _apply_noise_enabled(self, attr, var):
        setattr(config, attr, var.get())

    def _apply_noise_level(self, attr, var):
        try:
            value = float(var.get())
        except ValueError:
            value = getattr(config, attr)
        value = max(0.0, min(value, 50.0))  # mV; ceiling just catches typos
        var.set(f"{value:g}")
        setattr(config, attr, value)

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
        # Snap to a DMA-safe group (config.CHUNK_SIZE_GRANULARITY): one
        # packet is one DMA buffer, must be a multiple of 32 bytes.
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
            value = config.ECG_AMPLITUDE_MV
        value = max(0.0, value)
        self._amplitude.set(f"{value:g}")
        config.ECG_AMPLITUDE_MV = value

    # Waveform tab apply methods
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

    # Noise tab apply methods
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
        self.poll_state()

    def _apply_ecg_enabled(self):
        config.ECG_ENABLED = bool(self._ecg_enabled.get())

    def _apply_receive_enabled(self):
        config.RECEIVE_ENABLED = self._receive_enabled.get()


# Board UART log categories: (label, packet_format bit, tooltip).
_LOG_ROWS = (
    ("[S] stats",  "LOG_STATS",  "The once-a-second throughput line. The chatty one -- "
                                  "mute it while watching for something else."),
    ("[E] errors", "LOG_ERROR",  "Errors, resyncs and the suppressed-message counter. "
                                  "Leave this on unless you have a reason not to: it is "
                                  "how the board tells you it is in trouble."),
    ("[N] notice", "LOG_NOTICE", "Connect/reconnect and lifecycle messages."),
    ("[C] config", "LOG_CONFIG", "Config packet read-backs, including the one that turned "
                                  "this off."),
    ("other",      "LOG_OTHER",  "Everything untagged -- boot banner, [CLK], and anything "
                                  "new that has not been given a category yet."),
)

# (packet field, label), kept in wire order so the boxes read like [S].
_METRIC_ROWS = (
    ("rx_pps",    "RX packets/s"),
    ("tx_pps",    "TX packets/s"),
    ("rx_sps",    "Samples/s"),
    ("rx_bps",    "Throughput"),
    ("loop_ps",   "Main loop/s"),
    ("ring_used", "Ring used"),
    ("ring_peak", "Ring peak"),
    ("resyncs",   "Resyncs"),
    ("lat_min_us",  "Latency min"),
    ("lat_mean_us", "Latency mean"),
    ("lat_max_us",  "Latency max"),
    ("lat_jitter",  "Jitter (max-min)"),
    ("window_ms", "Stats window"),
    ("uptime_s",  "Uptime"),
)


def _format_metric(key, value):
    """Human units for the metric boxes -- 16130000 vs. 16.13 MB/s."""
    if key.startswith("lat_"):
        return f"{value} us"
    if key == "rx_bps":
        return f"{value / 1e6:.2f} MB/s"
    if key == "uptime_s":
        h, rem = divmod(value, 3600)
        return f"{h}:{rem // 60:02d}:{rem % 60:02d}"
    if key == "window_ms":
        return f"{value} ms"
    if key in ("ring_used", "ring_peak"):
        return f"{value / 1024:.1f} KB"
    if key in ("rx_sps", "loop_ps") and value >= 1000:
        return f"{value / 1000:.1f}k"
    return str(value)


class PlotControlPanel:
    def __init__(self, parent, plot=None):
        self.frame = ttk.Frame(parent, padding=8)
        # DualPlot that owns the buffers, for "Log buffer". Optional so the
        # panel stays constructible standalone; button disables if absent.
        self._plot = plot
        # Same deferred-commit model as SignalControlPanel.
        self._commits = []
        self._reloads = []

        V, S, B = self._cvar, tk.StringVar, tk.BooleanVar
        self._plot_min = V(S, lambda: _fmt_limit(config.PLOT_MIN))
        self._plot_max = V(S, lambda: _fmt_limit(config.PLOT_MAX))
        self._plot_buffer = V(S, lambda: str(config.PLOT_BUFFER))
        self._frame_rate = V(S, lambda: str(config.FRAME_RATE))
        self._trigger_on = V(B, lambda: bool(config.PLOT_TRIGGER))
        self._trigger_level = V(S, lambda: str(config.PLOT_TRIGGER_LEVEL))
        self._grid_on = V(B, lambda: bool(config.PLOT_GRID))
        self._grid_mode = V(S, lambda: config.PLOT_GRID_MODE)
        self._line_width = V(S, lambda: f"{config.PLOT_LINE_WIDTH:g}")
        self._show = [V(S, lambda: config.PLOT_SHOW_CH1),
                      V(S, lambda: config.PLOT_SHOW_CH2)]

        # Two groups (buttons right, fields fill the rest), not one grid
        # row: in one grid, a narrow window used to clip the Apply button
        # (last column) while every field stayed visible. Now a narrow
        # window eats into the fields instead, buttons always fully there.
        buttons = ttk.Frame(self.frame)
        buttons.pack(side="right", padx=(16, 0))
        fields = ttk.Frame(self.frame)
        fields.pack(side="left", fill="x", expand=True)

        col = 0
        ttk.Label(fields, text="Plot", font=("", 10, "bold")).grid(
            row=0, column=col, sticky="w", padx=(0, 16))
        col += 1

        ylim_help = (f"Y-axis range, in VOLTS -- the ADC's own input voltage "
                     f"(see the Signal panel's ADC tab), not raw wire codes "
                     f"and not millivolts. No wire code can ever represent a "
                     f"voltage outside [VREF-, VREF+] "
                     f"({config.VREF_MINUS:g}..{config.VREF_PLUS:g} by "
                     f"default), so that's the full range; narrow it to "
                     f"zoom. Max must be greater than min or the pair is "
                     f"rejected.")
        col = self._entry_h(fields, col, "Y min", self._plot_min,
                            self._apply_plot_ylim, width=11,
                            help_text=ylim_help)
        col = self._entry_h(fields, col, "Y max", self._plot_max,
                            self._apply_plot_ylim, width=11,
                            help_text=ylim_help)
        col = self._entry_h(fields, col, "Buffer", self._plot_buffer,
                                     self._apply_plot_buffer)
        col = self._entry_h(fields, col, "FPS", self._frame_rate,
                                     self._apply_frame_rate)

        # Scope trigger -- see config.py's PLOT_TRIGGER comment. Untick for
        # the old free-running behaviour.
        ttk.Checkbutton(fields, text="Trigger",
                        variable=self._trigger_on).grid(
            row=0, column=col, sticky="w", padx=(0, 8))
        col += 1
        grid_cb = ttk.Checkbutton(fields, text="Grid", variable=self._grid_on)
        grid_cb.grid(row=0, column=col, sticky="w", padx=(0, 4))
        col += 1
        grid_combo = ttk.Combobox(fields, textvariable=self._grid_mode,
                                  values=list(config.PLOT_GRID_MODES),
                                  width=6, state="readonly")
        grid_combo.grid(row=0, column=col, sticky="w", padx=(0, 10))
        col += 1
        self._commits.append(self._apply_grid)
        _Tooltip(grid_cb, "Gridlines on both channel plots, for reading one "
                          "against the other.")
        _Tooltip(grid_combo,
                 "normal: a line at each axis tick. fine: each of those "
                 "subdivided into 5, the way ECG paper puts five small "
                 "squares in every large one -- for reading an interval off "
                 "the screen, not just lining the channels up. Only applies "
                 "while Grid is ticked.")
        col = self._entry_h(fields, col, "Level", self._trigger_level,
                                     self._apply_trigger)

        for n, var in enumerate(self._show, start=1):
            ttk.Label(fields, text=f"Ch{n}").grid(row=0, column=col,
                                                  sticky="w", padx=(0, 3))
            box = ttk.Combobox(fields, textvariable=var, width=5,
                               values=list(config.PLOT_SHOW_CHOICES),
                               state="readonly")
            box.grid(row=0, column=col + 1, sticky="w", padx=(0, 8))
            _Tooltip(box, f"Which of channel {n}'s traces to draw: in (blue), "
                          f"out (red) or both.")
            col += 2
        self._commits.append(self._apply_show)
        col = self._entry_h(fields, col, "Width", self._line_width,
                            self._apply_line_width, width=5,
                            help_text="Trace thickness in points. 0.8 is "
                                      "thin, 1.5 is matplotlib's default.")

        # Writes the on-screen window of all four traces to a CSV under
        # build/logs/, with the settings that produced them.
        self._dump_button = ttk.Button(buttons, text="Log buffer",
                                       command=self._dump_buffers)
        self._dump_button.pack(side="left", padx=(0, 4))
        if self._plot is None:
            self._dump_button.state(["disabled"])
        # No filename label beside the button -- it used to grow/shrink the
        # bar on every dump. Names are listed in SAT's file picker instead.
        _Tooltip(self._dump_button,
                 "Write the on-screen window of all four traces to "
                 "build/logs/, with a sidecar recording every setting that "
                 "produced them. Open it with the SAT button.")

        # Own process, not imported in-process: its own Tk root/event loop
        # means a crash there can't take the live stream down. sys.executable
        # so it runs under this app's own interpreter (the venv), not
        # whatever "python3" resolves to on PATH.
        self._sat_button = ttk.Button(buttons, text="SAT",
                                      command=self._open_sat)
        self._sat_button.pack(side="left", padx=(0, 4))

        self._commits.append(self._apply_trigger)
        self._apply_button = ttk.Button(buttons, text=_APPLY_BAR_CLEAN,
                                        command=self.apply_all)
        apply_btn = self._apply_button
        apply_btn.pack(side="left", padx=(12, 0))
        defaults_btn = ttk.Button(buttons, text="Defaults",
                                  command=self.reset_defaults)
        defaults_btn.pack(side="left", padx=(4, 0))
        _Tooltip(defaults_btn,
                 "Put the plot fields back to config.py's startup values and "
                 "apply them now.")
        # Same pending-change marker as the signal panel.
        for var in (self._plot_min, self._plot_max, self._plot_buffer,
                    self._frame_rate, self._trigger_on, self._trigger_level,
                    self._grid_on, self._grid_mode, self._line_width,
                    *self._show):
            var.trace_add("write", lambda *_: self._mark_dirty())
        self._dirty = False
        _Tooltip(apply_btn,
                 "Apply the plot fields above. Nothing here takes effect "
                 "until this is pressed (Enter in any field does the same). "
                 "Also puts the view back where these fields say, which is "
                 "how you undo a toolbar pan or zoom.")
        _Tooltip(self._sat_button,
                 "SAT -- Static Analysis Tool. Opens sat.py in its own "
                 "window, on the newest logged buffer: spectra, peak "
                 "readout, and the board-vs-model comparison. A new click "
                 "opens another instance.")

    def _cvar(self, cls, source):
        """Config-backed variable -- see SignalControlPanel._cvar."""
        var = cls(value=source())
        self._reloads.append(lambda: var.set(source()))
        return var

    def _entry_h(self, frame, col, label, var, on_commit, width=8,
                 help_text=None):
        """_add_entry_horizontal with the commit deferred to Apply."""
        self._commits.append(on_commit)
        return _add_entry_horizontal(frame, col, label, var, self.apply_all,
                                     width=width, help_text=help_text)

    def _mark_dirty(self):
        if getattr(self, "_dirty", False):
            return
        self._dirty = True
        if getattr(self, "_apply_button", None) is not None:
            self._apply_button.config(text=_APPLY_BAR_DIRTY)

    def apply_all(self):
        """Commit every pending field in the plot bar. Some (buffer length,
        y-limits) make plot.py reallocate and redraw -- another reason not
        to do this per keystroke."""
        seen = set()
        for commit in self._commits:
            # _apply_plot_ylim is registered by both Y min and Y max; running
            # it twice would just redo the same work.
            if commit in seen:
                continue
            seen.add(commit)
            try:
                commit()
            except Exception as exc:                  # noqa: BLE001
                print(f"[plot] {getattr(commit, '__name__', commit)} "
                      f"failed: {exc}")
        # Re-assert the view even if config didn't change -- the toolbar
        # may have moved the axes since. See DualPlot.invalidate_view().
        if self._plot is not None:
            self._plot.invalidate_view()
        self._dirty = False
        self._apply_button.config(text=_APPLY_BAR_CLEAN)

    def reset_defaults(self):
        """Plot settings back to config.py's values, applied on the press."""
        config.restore_defaults(PLOT_SETTINGS)
        for reload_var in self._reloads:
            reload_var()
        if self._plot is not None:
            self._plot.invalidate_view()
        self._dirty = False
        self._apply_button.config(text=_APPLY_BAR_CLEAN)
        print("[plot] plot settings reset to defaults")

    def _open_sat(self):
        try:
            subprocess.Popen([sys.executable, str(SAT_SCRIPT)],
                              cwd=str(SAT_SCRIPT.parent))
        except OSError as exc:
            print(f"[plot] could not launch sat.py: {exc}")

    def _dump_buffers(self):
        if self._plot is None:
            return
        try:
            path = self._plot.dump_buffers()
        except Exception as exc:                      # noqa: BLE001
            # A diagnostic feature must never take the live view down.
            print(f"[plot] buffer dump failed: {exc}")
            return
        print(f"[plot] logged buffer to {path.name}")

    def _apply_trigger(self):
        config.PLOT_TRIGGER = bool(self._trigger_on.get())
        try:
            level = float(self._trigger_level.get())
        except ValueError:
            level = config.PLOT_TRIGGER_LEVEL
        # Clamp strictly inside the rails -- a level ON a rail never crosses.
        eps = (config.VREF_PLUS - config.VREF_MINUS) * 0.01
        level = min(max(level, config.VREF_MINUS + eps), config.VREF_PLUS - eps)
        self._trigger_level.set(f"{level:g}")
        config.PLOT_TRIGGER_LEVEL = level

    def _apply_line_width(self):
        try:
            value = float(self._line_width.get())
        except ValueError:
            value = config.PLOT_LINE_WIDTH
        value = min(max(value, 0.1), 5.0)
        self._line_width.set(f"{value:g}")
        config.PLOT_LINE_WIDTH = value

    def _apply_show(self):
        for n, var in enumerate(self._show, start=1):
            mode = var.get()
            if mode not in config.PLOT_SHOW_CHOICES:
                mode = getattr(config, f"PLOT_SHOW_CH{n}")
                var.set(mode)
            setattr(config, f"PLOT_SHOW_CH{n}", mode)

    def _apply_grid(self):
        config.PLOT_GRID = bool(self._grid_on.get())
        mode = self._grid_mode.get()
        if mode not in config.PLOT_GRID_MODES:
            mode = config.PLOT_GRID_MODE
            self._grid_mode.set(mode)
        config.PLOT_GRID_MODE = mode

    def _apply_plot_ylim(self):
        try:
            lo = float(self._plot_min.get())
            hi = float(self._plot_max.get())
            if hi <= lo:
                raise ValueError
        except ValueError:
            # Reject the whole pair rather than guess which field was
            # wrong; say so, or it reads as the button being broken.
            print(f"[plot] Y min/Y max rejected "
                  f"({self._plot_min.get()!r}, {self._plot_max.get()!r}) -- "
                  f"need two numbers with max > min, volts; full ADC range "
                  f"is {config.VREF_MINUS:g}..{config.VREF_PLUS:g}")
            self._plot_min.set(_fmt_limit(config.PLOT_MIN))
            self._plot_max.set(_fmt_limit(config.PLOT_MAX))
            return
        # Stored as int when integral -- purely cosmetic (no trailing
        # ".0" for e.g. VREF_MINUS=0) in the log sidecar's settings snapshot.
        config.PLOT_MIN = int(lo) if lo.is_integer() else lo
        config.PLOT_MAX = int(hi) if hi.is_integer() else hi

    def _apply_plot_buffer(self):
        try:
            value = int(self._plot_buffer.get())
        except ValueError:
            value = config.PLOT_BUFFER
        value = max(10, min(value, 100_000))  # sanity bounds, not a limit
        self._plot_buffer.set(str(value))
        config.PLOT_BUFFER = value

    def _apply_frame_rate(self):
        try:
            value = int(self._frame_rate.get())
        except ValueError:
            value = config.FRAME_RATE
        value = max(1, min(value, 240))  # ~5 ms/frame, so 240 is a full core;
                                          # match SEND_RATE (64 default).
        self._frame_rate.set(str(value))
        config.FRAME_RATE = value
