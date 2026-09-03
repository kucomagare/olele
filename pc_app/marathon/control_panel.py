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
#                   {VIOLET,BLUE,WHITE,PINK,BROWN}_CH{1,2}_ENABLED/_LEVEL --
#                   each colour picks the channels it affects), and four sine-wave
#                   interference generators configured per channel
#                   (ECG_SINE{1..4}_CH{1,2}_ENABLED/_FREQ/_PHASE/_LEVEL,
#                   e.g. for mains hum) -- see signal_gen.py's
#                   _simulate_raw()/_sine_contribution().
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

# Wire sample range, for the labels that quote it. Derived from the packet
# format (and so from shared/marathon/packet_format.json) rather than written
# into the text -- which is how the Amplitude field came to advertise sizif's
# uint16 range long after marathon moved to 32-bit slots.
_WIRE_MAX = int(np.iinfo(packet_format.CH1_DTYPE).max)
_WIRE_BITS = np.dtype(packet_format.CH1_DTYPE).itemsize * 8


# Apply-button captions. The marker is the only signal that a typed or
# ticked change has not reached config yet, which without it reads as a
# control that simply does not work.
_APPLY_CLEAN = "Apply changes"
_APPLY_DIRTY = "Apply changes  \u25cf"
_APPLY_BAR_CLEAN = "Apply"
_APPLY_BAR_DIRTY = "Apply \u25cf"


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
    # Enter applies everything, as a keyboard shortcut for the Apply button.
    # There is deliberately no <FocusOut> binding: committing as soon as the
    # cursor leaves a field IS applying on every change, which is the thing
    # the Apply button exists to replace.
    entry.bind("<Return>", lambda _e: on_commit())
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


def _fmt_limit(value):
    """Format a y-limit for its entry box: 4294967295, not 4294967295.0.

    The limits are parsed with float() (so 2.1e9 is typeable) but they are
    sample counts, and a trailing ".0" is two wasted characters in a field
    where every character is already spoken for."""
    value = float(value)
    return str(int(value)) if value.is_integer() else f"{value:g}"


def _add_entry_horizontal(frame, col, label, var, on_commit, width=8,
                          help_text=None):
    """Same as _add_entry but laid out left-to-right in a single row
    (label, entry, label, entry, ...) instead of stacked rows -- used by
    PlotControlPanel, which sits in one line along the bottom of the
    window rather than a sidebar column.

    `width` because the y-limits are raw sample counts: full scale is
    4294967295, ten digits, and in an 8-character box that reads
    "42949672" -- a number the user then reasonably believes is the
    current maximum. Fields have to show what they hold."""
    ttk.Label(frame, text=label).grid(row=0, column=col, sticky="w", padx=(0, 3))
    entry = ttk.Entry(frame, textvariable=var, width=width)
    entry.grid(row=0, column=col + 1, sticky="w", padx=(0, 8))
    entry.bind("<Return>", lambda _e: on_commit())
    if help_text:
        _Tooltip(entry, help_text)
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
        "ECG_AMPLITUDE", "ECG_OFFSET", "ECG_ENABLED",
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
              for ch in (1, 2) for field in ("ENABLED", "LEVEL")
              ) + tuple(f"ECG_SINE{n}_CH{ch}_{field}"
              for n in range(1, 5) for ch in (1, 2)
              for field in ("ENABLED", "FREQ", "PHASE", "LEVEL"))),
)

# Which config names each Defaults button restores -- the same ownership map,
# so a knob added to GUI_SECTIONS is reset by the panel that shows it.
#
# Except these: they are the RUN, not settings. Defaulting CH_MODE moves the
# data source out from under a running session (local mode back to board,
# with nothing answering, so the processed trace just stops); SEND_ENABLED is
# the Pause button and AUTOSTART only means anything at launch. A reset must
# not decide where the signal comes from or whether it is flowing.
_RUN_CONTROLS = ("CH_MODE", "SEND_ENABLED", "AUTOSTART")
_PLOT_BAR = "Plot bar"
PLOT_SETTINGS = tuple(n for title, names in GUI_SECTIONS
                      if title == _PLOT_BAR for n in names
                      if n not in _RUN_CONTROLS)
SIGNAL_SETTINGS = tuple(n for title, names in GUI_SECTIONS
                        if title != _PLOT_BAR for n in names
                        if n not in _RUN_CONTROLS)


# (display name, config attr for "enabled", config attr for "level", beta,
#  short description of that color's character for its tooltip)
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
                                  # ECG_METHOD comment. A readonly combobox
                                  # (state="readonly" below) means the user
                                  # can only ever pick from this list.


# Why the panels batch instead of applying per field
# -------------------------------------------------
# Every control used to write config the moment it changed -- on Enter, on
# leaving a field, on every checkbox click. That makes a multi-field change
# into a sequence of half-applied states: setting SEND_RATE and CHUNK_SIZE
# together meant the stream ran at the new rate with the old chunk size for
# however long it took to reach the second field.
#
# Now every control writes only its own Tk variable, and an Apply button
# commits them all in one pass. Enter in any field does the same thing, so
# the keyboard still works. What stays immediate is anything that is an
# ACTION rather than a setting -- Start/Stop, Pause, Log buffer, SAT, and
# the Board tab's own Apply (which writes hardware registers and has always
# been explicit).
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

        # Start/Stop and Pause/Resume are deliberately two different
        # controls, because they are two different things:
        #   Start/Stop -- the SESSION. Stopped means no socket open, no
        #                 samples generated, nothing on the wire, and (in
        #                 local mode) a filter that will begin from cleared
        #                 state next time. The app launches here.
        #   Pause      -- SEND_ENABLED only. The connection stays up and the
        #                 receive path keeps running; this just stops feeding
        #                 it. Useful for freezing the trace mid-run.
        self._start_button = ttk.Button(self.frame, text=self._start_label(),
                                        command=self._toggle_run)
        self._start_button.pack(fill="x", pady=(0, 4))
        _Tooltip(self._start_button,
                 "Start or stop the run. Nothing is generated, connected or "
                 "sent until this is pressed -- set everything up first, then "
                 "start. Stopping closes the connection (board mode) and "
                 "clears the filter state (local mode).")

        # Mode picker. Both worker threads are always alive and each idles
        # unless it owns the mode, so switching is just this attribute
        # write -- see python_client.py.
        # One row per channel: the two are independent, so ch1 can run on the
        # board while ch2 runs a pipeline being developed here.
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

        # Tabbed rather than one long stacked column -- Basic covers the
        # fields every session needs; Waveform/Noise are the nk.ecg_simulate
        # kwargs and nk.signal_noise() injection from the parameter survey,
        # most of which most sessions won't touch.
        # Identity of the last dict rendered, so poll_board() can skip the
        # frames where nothing new arrived (metrics land at 1 Hz, poll runs at
        # FRAME_RATE).
        self._last_metrics_seen = None
        self._last_config_seen = None

        notebook = ttk.Notebook(self.frame)
        tabs = {}
        for name in ("Basic", "Waveform", "Noise", "Board", "Local"):
            tab = guiutil.ScrollFrame(notebook)
            notebook.add(tab.outer, text=name)
            tabs[name] = tab.body

        # Everything below the tabs is packed FIRST, from the bottom up, so
        # it reserves its space before the notebook (which takes what is
        # left). Packed the other way round, the notebook's expand=True
        # swallowed the whole cavity and the Apply button below it was
        # never mapped -- the button was not broken, it was not on screen.
        self._rate_status = tk.StringVar()
        ttk.Label(self.frame, textvariable=self._rate_status, wraplength=200,
                  justify="left", foreground="#555").pack(side="bottom",
                                                          anchor="w")
        ttk.Separator(self.frame, orient="horizontal").pack(side="bottom",
                                                            fill="x", pady=6)
        # Directly under the tabs, so it is visible whichever tab is open --
        # a change made on Basic and one made on Noise are applied by the
        # same press.
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

        # Watch every control for edits, so the button can say there is
        # something waiting. Without this a ticked checkbox looks broken:
        # nothing happens until Apply, and nothing said so.
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
        """Entry commit handler for fields that are only sent on Apply.

        self._entry() binds Return/FocusOut to a callback, and these two fields
        deliberately do not push on every keystroke -- writing a half-typed
        channel count into live fabric registers is not something to do by
        accident."""

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
        """Current UART mask from the checkboxes.

        Sent on every Apply, not only when a box is clicked: one WRITE carries
        the whole config, so a mask assembled from anything other than what is
        on screen would quietly overwrite the board's verbosity while you were
        changing the filter."""
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
        """Refresh the Board tab from whatever the net thread last received.

        Called from DualPlot.refresh(), i.e. at FRAME_RATE, against data that
        arrives at 1 Hz -- so it compares object identity first and does
        nothing at all on the frames where nothing new landed."""
        m = net.last_metrics
        if m is not None and m is not self._last_metrics_seen:
            self._last_metrics_seen = m
            # Jitter is derived, not transmitted: it is exactly max-min and
            # sending it would be a third number that could disagree with the
            # two it comes from.
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

    # ------------------------------------------------------------------
    # Basic tab: the fields from before this session's parameter survey.
    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    def _entry(self, frame, row, label, var, on_commit, width=8, help_text=None):
        """_add_entry, but the commit is deferred to Apply instead of run now."""
        self._commits.append(on_commit)
        self._watch(var)
        return _add_entry(frame, row, label, var, self.apply_all, width, help_text)

    def _cvar(self, cls, source):
        """A config-backed Tk variable: seeded from source() now, and
        re-seeded from it by reset_defaults(). Registering the reader here
        rather than writing a second config->widget pass keeps the two from
        drifting when a field is added."""
        var = cls(value=source())
        self._reloads.append(lambda: var.set(source()))
        return var

    def _watch(self, *variables):
        """Track these as editable inputs, for the pending-change marker.

        Registered explicitly rather than by walking every Tk variable on
        the panel: several of them are read-only DISPLAYS -- the status
        line, and the board metrics that arrive once a second -- and
        watching those would leave the button permanently claiming there
        was something to apply.
        """
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
        """Commit every pending field, then refresh what the panel shows.

        Each commit validates and clamps its own value and writes the
        corrected text back into its variable, so pressing Apply is also how
        you find out that 2048 became 2000.
        """
        for commit in self._commits:
            try:
                commit()
            except Exception as exc:                  # noqa: BLE001
                # One bad field must not stop the rest from applying, or a
                # typo in a box you are not looking at silently swallows the
                # change you actually came to make.
                print(f"[panel] {getattr(commit, '__name__', commit)} "
                      f"failed: {exc}")
        # Commits write clamped values back into their variables, which trips
        # the dirty trace again -- so clear the flag after, not before.
        self._dirty = False
        self._apply_button.config(text=_APPLY_CLEAN)
        self.poll_state()

    def reset_defaults(self):
        """Put every setting this panel owns back to config.py's value.

        Applied on the press, not staged: "reset" that needs a second button
        to take effect is not a reset. Left alone: the run controls
        (_RUN_CONTROLS) and the Board tab, whose fields are hardware
        registers with their own Apply.
        """
        config.restore_defaults(SIGNAL_SETTINGS)
        for reload_var in self._reloads:
            reload_var()
        for ch in range(2):
            self._sync_impl(ch)
        self._dirty = False
        self._apply_button.config(text=_APPLY_CLEAN)
        self.poll_state()
        print("[panel] signal settings reset to defaults")

    # Local tab: the in-process algorithm and its parameters. Deliberately
    # separate from the Board tab even though "iir" models the same filter
    # -- the Board tab WRITES HARDWARE REGISTERS and reads back what the
    # fabric actually holds, this one sets Python variables. Merging them
    # would make it ambiguous which of those just happened.
    # ------------------------------------------------------------------
    def _build_local_tab(self, frame):
        row = 0
        ttk.Label(frame, text="Processing pipeline",
                  font=("", 9, "bold")).grid(row=row, column=0, columnspan=2,
                                              sticky="w", pady=(0, 4))
        row += 1

        # Two independent pairs of dropdowns, one per channel. Both are built
        # from pipelines.PIPELINES, so adding a pipeline there makes it
        # appear here with no edit to this file.
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
            # Retargets the implementation dropdown at once -- that is
            # showing you what is available, not applying anything. The
            # config write waits for Apply like everything else.
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
            # No binding: the variable is the pending value, and
            # _apply_pipe commits pipe and impl together.
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

        # One set for both channels, like Shift: these are the filter's
        # design, not a per-channel property. 0 skips a stage; so does any
        # corner at or above Nyquist.
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
        """A pipe2 corner in Hz: 0 (skip the stage) or below Nyquist.

        Clamped against the LIVE ECG sample rate, so the ceiling follows the
        Basic tab rather than a number fixed at startup.
        """
        try:
            value = float(var.get())
            if value < 0:
                raise ValueError
        except ValueError:
            value = getattr(config, attr)
        nyquist = config.ECG_SAMPLING_RATE / 2.0
        if value > 0:
            value = min(value, nyquist)
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
        """Point the implementation dropdown at what this pipeline offers.

        bypass and iir are single fixed things rather than a design with a
        float and a fixed-point version, so for those the dropdown is greyed
        out and shows a dash instead of naming an implementation that was
        never chosen. Both channels get the same treatment for the same
        pipeline -- the only reason they can look different is that they are
        running different pipelines.
        """
        combo = self._impl_combo[ch]
        # The variable, not config: this runs before Apply, so config still
        # holds the previous pipeline and would offer its implementations.
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
        """Commit one channel's pipeline and implementation together.

        Together because they are one selection: writing the pipeline
        without its implementation leaves a moment where pipe1 is paired
        with whatever the previous pipeline's implementation was.
        """
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
        # Written per channel rather than replacing the list, so a worker
        # thread reading it mid-update sees one changed element, never a
        # half-built list.
        for ch, var in enumerate(self._mode):
            config.CH_MODE[ch] = var.get()

    def _status_text(self):
        """One line saying what the app is actually doing right now.

        Exists because "I pressed Start and nothing happened" has several
        very different causes -- still warming up, running fine but paused,
        or in board mode failing to reach the board -- and none of them were
        visible anywhere except the console.
        """
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
        # "connected" only means the RELAY took the connection. The relay
        # accepts and discards everything when its other peer is gone, so a
        # dead board looks exactly like a healthy one from the send side --
        # it did, for minutes, on 2026-09-01. Silence on the receive path is
        # the one signal that tells them apart, so it outranks link_state.
        if net.rx_stale_s > config.RX_WATCHDOG_S:
            return (f"running: board, connected but NOTHING RECEIVED for "
                    f"{net.rx_stale_s:.0f}s -- board wedged or unplugged?{paused}")
        # Name the locally processed channels explicitly. Mixed mode looks
        # identical to all-board from the link's point of view, and "why is
        # ch2 not what the board sent" is exactly the question this line
        # exists to answer.
        local = ", ".join(
            f"ch{i + 1} {pipelines.label(config.CH_PIPE[i], config.CH_IMPL[i])}"
            for i in range(2) if config.CH_MODE[i] == "local")
        mixed = f", local: {local}" if local else ""
        return f"running: board, {net.link_state}{mixed}{paused}"

    def poll_state(self):
        """Re-derive the run/pause labels and the status line from the real
        state. Called every frame; each widget is only touched when its text
        actually changes, so this costs nothing in the steady state."""
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
        self._amplitude = V(S, lambda: f"{config.ECG_AMPLITUDE * 100:g}")
        self._receive_enabled = V(B, lambda: config.RECEIVE_ENABLED)
        self._offset = V(S, lambda: f"{config.ECG_OFFSET * 100:g}")
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
        # Range read from the wire dtype rather than written into the text:
        # this label said "uint16 (0-65535)" long after marathon moved to
        # 32-bit TDM slots, so the panel was quoting sizif's numbers. Taken
        # from the same packet_format.json the firmware header is generated
        # from, it cannot go stale again.
        row = self._entry(frame, row, "Amplitude (% of FS)", self._amplitude,
                          self._apply_amplitude,
                          help_text=f"How much of the wire format's range "
                                     f"({_WIRE_BITS}-bit, 0-{_WIRE_MAX}) the signal's "
                                     f"peak-to-peak occupies, centered at the midpoint. "
                                     f"A pure wire/display scale -- it does not change "
                                     f"the waveform's shape, only its size.")

        row = self._entry(frame, row, "Offset (% of FS)", self._offset,
                          self._apply_offset,
                          help_text="DC shift of the whole signal, as a percentage of the wire "
                                     "format's full scale. 0 centres it; +25 puts it at "
                                     "three-quarter scale. Independent of Amplitude -- this moves "
                                     "the band, that sizes it. Push it far enough and the signal "
                                     "clips at the range ends.")

        ecg_on = ttk.Checkbutton(frame, text="ECG enabled", variable=self._ecg_enabled)
        self._commits.append(self._apply_ecg_enabled)
        self._watch(self._ecg_enabled)
        ecg_on.grid(row=row, column=0, columnspan=2, sticky="w", pady=2)
        _Tooltip(ecg_on, "Off removes the heartbeat and leaves only the noise and sine "
                          "generators from the Noise tab, at exactly the levels they already "
                          "had -- their amplitudes stay referenced to the ECG's own swing, so "
                          "nothing jumps when you toggle this.")
        row += 1

        rx = ttk.Checkbutton(frame, text="Receive enabled",
                              variable=self._receive_enabled)
        self._commits.append(self._apply_receive_enabled)
        self._watch(self._receive_enabled)
        rx.grid(row=row, column=0, columnspan=2, sticky="w", pady=2)
        _Tooltip(rx, "When off, incoming (echoed) packets from the board/relay are ignored -- "
                      "the plot's \"out\" trace stops updating. Sending continues unaffected.")

    # ------------------------------------------------------------------
    # Waveform tab: nk.ecg_simulate()'s ECGSYN-model parameters.
    # ------------------------------------------------------------------
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

    # ------------------------------------------------------------------
    # Noise tab: nk.ecg_simulate()'s own noise param + a separate
    # nk.signal_noise()-based colored noise added on top.
    # ------------------------------------------------------------------
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

        # One row per colour, both channels on it: Ch1 on/level, Ch2 on/level.
        # Each is independent and they sum -- see signal_gen's _simulate_raw().
        for col, head in enumerate(("", "Ch1", "%", "Ch2", "%")):
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
                    lambda a=level_attr: f"{getattr(config, a) * 100:g}")
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
                           f"{name} noise on ch{ch}, as a percentage of the "
                           f"ECG's OWN peak-to-peak swing -- so a given "
                           f"percentage means the same thing regardless of "
                           f"heart rate, Amplitude, or how many other layers "
                           f"are active. Only applies while its box is "
                           f"ticked.")
            row += 1

        ttk.Separator(frame, orient="horizontal").grid(
            row=row, column=0, columnspan=5, sticky="ew", pady=6)
        row += 1
        ttk.Label(frame, text="Sine interference -- 4 generators, each set per channel:",
                  foreground="#777", font=("", 8), wraplength=180, justify="left").grid(
            row=row, column=0, columnspan=5, sticky="w", pady=(0, 4))
        row += 1

        # One block per generator: a name, a header row, then one compact
        # row per channel (on / freq / phase / level). Stacking four labelled
        # fields per channel would be 32 rows of sidebar; this is 12.
        self._sine_vars = {}
        for n in range(1, signal_gen.SINE_COUNT + 1):
            ttk.Label(frame, text=f"Sine {n}", font=("", 9, "bold")).grid(
                row=row, column=0, columnspan=5, sticky="w", pady=(6, 0))
            row += 1
            for col, head in enumerate(("", "Hz", "deg", "%")):
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
                              lambda a=level_attr: f"{getattr(config, a) * 100:g}")
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
                           f"Amplitude as a percentage of the clean ECG's own "
                           f"peak-to-peak (same convention as the noise levels "
                           f"above), referenced to ch1 for both channels so "
                           f"equal numbers mean equal amplitudes. Only applies "
                           f"while Ch{ch} is ticked.")
                row += 1

    def _cell(self, frame, row, col, var, on_commit, help_text=None):
        """A bare entry at a grid position -- no label of its own, for tables
        with column headers. Commit deferred to Apply, like _entry."""
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
        self.poll_state()

    def _apply_offset(self):
        try:
            value = float(self._offset.get()) / 100.0
        except ValueError:
            value = config.ECG_OFFSET
        value = max(config.ECG_OFFSET_MIN, min(config.ECG_OFFSET_MAX, value))
        self._offset.set(f"{value * 100:g}")
        config.ECG_OFFSET = value

    def _apply_ecg_enabled(self):
        config.ECG_ENABLED = bool(self._ecg_enabled.get())

    def _apply_receive_enabled(self):
        config.RECEIVE_ENABLED = self._receive_enabled.get()


# Board metrics, in display order: (packet field, label). Kept in wire order
# rather than alphabetical so the boxes read like the [S] console line.
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
    """Human units for the metric boxes. Raw counts are unreadable at these
    magnitudes -- 16130000 means nothing at a glance, 16.13 MB/s does."""
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
        # The DualPlot that owns the buffers, for the "Log buffer" button.
        # Optional so the panel stays constructible on its own; the button
        # just disables itself when there is nothing to dump.
        self._plot = plot
        # Same deferred-commit model as SignalControlPanel -- see the comment
        # above that class.
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

        # Two groups, not one long grid row: the buttons are packed to the
        # RIGHT edge and the fields fill what is left. In one grid, a window
        # narrower than the whole row (~1210px with these fonts) clipped
        # whatever sat in the last columns -- which was the Apply button, so
        # it came out half-drawn or missing entirely while every field was
        # still visible. Now a narrow window eats into the fields instead,
        # and the buttons are always fully there.
        buttons = ttk.Frame(self.frame)
        buttons.pack(side="right", padx=(16, 0))
        fields = ttk.Frame(self.frame)
        fields.pack(side="left", fill="x", expand=True)

        col = 0
        ttk.Label(fields, text="Plot", font=("", 10, "bold")).grid(
            row=0, column=col, sticky="w", padx=(0, 16))
        col += 1

        # Wider than the rest: these hold up to 10 digits (full scale is
        # 4294967295) and an 8-character box shows the wrong number.
        ylim_help = (f"Y-axis range, in RAW SAMPLE COUNTS -- not millivolts "
                     f"and not a percentage. Full scale is 0.."
                     f"{config.WIRE_FULL_SCALE}, and the ECG sits around "
                     f"mid-scale ({config.WIRE_FULL_SCALE // 2}), so a "
                     f"small range like 0..100 is valid but puts the trace "
                     f"far off screen. Max must be greater than min or the "
                     f"pair is rejected.")
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

        # Scope trigger. Without it the window shows whatever phase happened
        # to be newest at frame time, which above a few thousand samples/s
        # means a different phase every frame -- see the PLOT_TRIGGER comment
        # in config.py. Untick to get the old free-running behaviour back.
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

        # Writes the on-screen window of all four traces to a CSV under
        # build/logs/. For inspecting a trace that looks wrong -- far more
        # useful than describing it, since the file carries the settings that
        # produced it alongside the samples.
        self._dump_button = ttk.Button(buttons, text="Log buffer",
                                       command=self._dump_buffers)
        self._dump_button.pack(side="left", padx=(0, 4))
        if self._plot is None:
            self._dump_button.state(["disabled"])
        # No filename label beside the button: it grew and shrank the bar
        # every time a dump was written, which moved every control to its
        # right. The names are listed in SAT's file picker, which is where
        # you go to open one anyway -- failures still print to the console.
        _Tooltip(self._dump_button,
                 "Write the on-screen window of all four traces to "
                 "build/logs/, with a sidecar recording every setting that "
                 "produced them. Open it with the SAT button.")

        # Launches sat.py as its own process rather than importing it
        # in-process: it opens its own Tk root and event loop, and this way a
        # long transform or a crash in there can never take the live stream
        # down with it. sys.executable so it runs under whichever interpreter
        # is already running this app (the venv, with numpy etc. installed),
        # regardless of what "python3" resolves to on PATH.
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
                    self._grid_on, self._grid_mode):
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
        """_add_entry_horizontal, with the commit deferred to Apply."""
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
        """Commit every pending field in the plot bar.

        Some of these (buffer length, y-limits) make plot.py reallocate and
        redraw, which is another reason not to do it per keystroke.
        """
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
        # Re-assert the view even when nothing in config changed: the axes
        # may have been moved by the matplotlib toolbar since the last
        # press, and "Apply" has to put them back where these fields say.
        # See DualPlot.invalidate_view().
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
            # Never let a dump failure take down the plot: this is a
            # diagnostic, and losing the live view to save a file would be a
            # bad trade. Report it in the panel and in the log, carry on.
            print(f"[plot] buffer dump failed: {exc}")
            return
        print(f"[plot] logged buffer to {path.name}")

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
            # Reject the whole pair on a bad edit rather than guessing which
            # field was wrong -- both snap back to config's last-good values.
            # Say so: silently replacing what someone just typed, with no
            # message anywhere, is indistinguishable from the button being
            # broken.
            print(f"[plot] Y min/Y max rejected "
                  f"({self._plot_min.get()!r}, {self._plot_max.get()!r}) -- "
                  f"need two numbers with max > min; "
                  f"full scale is 0..{config.WIRE_FULL_SCALE}")
            self._plot_min.set(_fmt_limit(config.PLOT_MIN))
            self._plot_max.set(_fmt_limit(config.PLOT_MAX))
            return
        # Stored as int when the value is integral: these are sample
        # counts, and they end up in the log sidecar's settings snapshot.
        config.PLOT_MIN = int(lo) if lo.is_integer() else lo
        config.PLOT_MAX = int(hi) if hi.is_integer() else hi

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
