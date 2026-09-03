# The analysis window: a Tk panel of controls around a static 2x2 figure.
# Shaped like the streaming client's window (plot.py + control_panel.py) on
# purpose -- same TkAgg-window/sibling-widgets pattern -- but nothing here
# is live: every control recomputes from a file and redraws once, no
# blitting/animation/frame rate. That's why the spectrum lives here and not
# in the client. Presentation only -- the analysis itself is in sat.py.

import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, messagebox, ttk

import matplotlib
matplotlib.use("TkAgg")
import matplotlib.pyplot as plt
from matplotlib.ticker import MultipleLocator, ScalarFormatter
import numpy as np

import guiutil
import sat

FFT_SIZES = (0, 128, 256, 512, 1024, 2048, 4096, 8192)
VIEWS = ("capture", "response")
OVERLAYS = ("none", "gain", "spectrum", "both")
OVERLAY_CHANNELS = ("ch1", "ch2", "both")

# The capture overlays are drawn in greys, so nothing on the response axes
# can be mistaken for a pipeline: colour there means "a pipeline", and a
# recording is not one.
_GAIN_COLORS = {"ch1": "0.15", "ch2": "0.50"}
_SPECTRUM_COLOR = "0.45"

# One colour per pipeline, one line style per implementation, so a stack of
# curves on the response axes reads as "which filter" and "which arithmetic"
# at a glance rather than as eight unrelated lines.
_IMPL_STYLE = {"scipy": "-", "manual": "--"}


# Pipeline names only, built from the registry -- a pipeline added in
# pipelines.py appears here with no edit.
def _pipes():
    import pipelines
    return ("none",) + tuple(sorted(pipelines.PIPELINES))


def _impls(pipe):
    import pipelines
    return pipelines.implementations(pipe)


def _default_impl():
    import pipelines
    return pipelines.DEFAULT_IMPL


def _split_model(model):
    """A CLI "--model pipe[:impl]" back into the two dropdowns' values."""
    if not model:
        return "none", _default_impl()
    pipe, _, impl = model.partition(":")
    return pipe, (impl or _default_impl())


def _size_label(value):
    """0 is a real setting ("the whole capture"); shown as "0" it reads
    like a mistake."""
    return "capture" if not value else str(value)


def _size_value(label):
    try:
        return int(label)
    except ValueError:
        return 0


def _fmt(value):
    """A number for an entry field, or "" for absent -- blank is a real
    setting in the tunable fields ("take it from the capture")."""
    return "" if value is None else f"{float(value):g}"


def _curve_style(response):
    """(colour, linestyle) for one measured curve. Colour is fixed by the
    pipeline's position in the registry, so a given pipeline keeps the same
    colour whichever subset happens to be ticked."""
    import pipelines
    order = sorted(pipelines.PIPELINES)
    pipe = response["pipe"]
    idx = order.index(pipe) if pipe in order else len(order)
    return f"C{idx % 10}", _IMPL_STYLE.get(response["impl"], "-")


class _Tooltip:
    """Same minimal hover tooltip as the client's panel -- ttk has none."""

    def __init__(self, widget, text):
        self.widget, self.text, self.tip = widget, text, None
        widget.bind("<Enter>", self._show, add="+")
        widget.bind("<Leave>", self._hide, add="+")

    def _show(self, _e=None):
        if self.tip or not self.text:
            return
        x = self.widget.winfo_rootx() + 12
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        self.tip = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        tk.Label(tw, text=self.text, justify="left", background="#ffffe0",
                 relief="solid", borderwidth=1, wraplength=280,
                 font=("", 9)).pack(ipadx=4, ipady=2)

    def _hide(self, _e=None):
        if self.tip:
            self.tip.destroy()
            self.tip = None


class SATWindow:
    def __init__(self, log_dir, path=None, fft_size=0, fmax=0.0, db_min=-120.0,
                 peak_fmin=1.0, model=None, model_ch2=None, shift=None,
                 settle=200, view="capture", curves=(),
                 response_size=sat.DEFAULT_RESPONSE_SIZE,
                 response_points=sat.DEFAULT_RESPONSE_POINTS,
                 response_drive=sat.DEFAULT_RESPONSE_DRIVE,
                 response_averages=sat.DEFAULT_RESPONSE_AVERAGES,
                 response_fmin=0.0, response_fmax=0.0, overrides=None,
                 overlay="none", overlay_ch="both"):
        self.log_dir = log_dir
        self.traces = None
        self.info = None

        self.fig = plt.figure(figsize=(13, 7))
        # The two views want different grids, so the axes are built on
        # demand (_layout) rather than fixed here.
        self._shape = None
        self._twin = None           # the dBFS overlay's right-hand axis
        self.axes = self._layout((2, 2))
        self.fig.canvas.manager.set_window_title("olele — buffer analysis")

        # Same Tk pack dance as the client's DualPlot (see plot.py) -- pop
        # canvas/toolbar out, pack ours first, restore theirs.
        window = self.fig.canvas.manager.window
        guiutil.size_window(window)
        existing = [(c, c.pack_info()) for c in window.pack_slaves()]
        for child, _ in existing:
            child.pack_forget()

        self.report_frame = ttk.Frame(window, padding=(8, 4))
        self.report_frame.pack(side="bottom", fill="x")
        self.controls = ttk.Frame(window, padding=8)
        self.controls.pack(side="right", fill="y")

        for child, info in existing:
            info["in_"] = info.pop("in")
            child.pack(**info)

        self._fft_size = tk.StringVar(value=_size_label(fft_size))
        self._fmax = tk.StringVar(value=f"{fmax:g}")
        self._db_min = tk.StringVar(value=f"{db_min:g}")
        self._peak_fmin = tk.StringVar(value=f"{peak_fmin:g}")
        # Per channel: a capture's two channels may have run different
        # pipelines, so a single model could only ever be right for one.
        pipe0, impl0 = _split_model(model)
        pipe1, impl1 = _split_model(model_ch2 if model_ch2 is not None else model)
        self._pipe = [tk.StringVar(value=pipe0), tk.StringVar(value=pipe1)]
        self._impl = [tk.StringVar(value=impl0), tk.StringVar(value=impl1)]
        self._impl_combo = []
        self._shift = tk.StringVar(value="" if shift is None else str(shift))
        self._settle = tk.StringVar(value=str(settle))
        self._file = tk.StringVar()

        # --- response view ---------------------------------------------
        self._view = tk.StringVar(value=view if view in VIEWS else "capture")
        # One flag per pipe:impl, so any subset can be ticked and the ticked
        # ones are drawn over each other. Built from the registry, so a
        # pipeline added in pipelines.py gets a checkbox with no edit here.
        self._curves = {name: tk.BooleanVar(value=name in tuple(curves))
                        for name in sat.model_choices()}
        if not any(v.get() for v in self._curves.values()):
            # Nothing asked for: start on one design pipeline's manual/scipy
            # pair, which is the comparison this view exists for.
            design = [n for n in self._curves if ":" in n]
            newest = design[-1].split(":")[0] if design else None
            for name in design:
                if name.startswith(f"{newest}:"):
                    self._curves[name].set(True)
        self._resp_size = tk.StringVar(value=str(response_size))
        # What was picked from the dropdown, as opposed to what F min pushed
        # it up to. Kept apart so raising F min again drops back to the
        # chosen value instead of leaving the measurement stuck at the
        # longest period it was ever asked for.
        self._size_floor = int(response_size)
        self._resp_points = tk.StringVar(value=str(response_points))
        self._resp_drive = tk.StringVar(value=f"{response_drive * 100:g}")
        self._resp_averages = tk.StringVar(value=str(response_averages))
        self._resp_fmin = tk.StringVar(value=f"{response_fmin:g}")
        self._resp_fmax = tk.StringVar(value=f"{response_fmax:g}")
        self._resp_rate = tk.StringVar()
        # A pipeline's own knobs, filled in from the loaded capture (or the
        # pipeline's own defaults) rather than left blank -- a corner you
        # cannot see is a corner you cannot sensibly change, and the point
        # of these is to nudge them and watch what moves.
        self._tunable = {key: tk.StringVar() for _s, key, _l in sat.TUNABLES}
        # Applied on top of the first capture's values, then forgotten, so a
        # --set from the command line is where you start and not a mode you
        # are stuck in.
        self._pending_tunables = dict(overrides or {})
        self._resp_floor = tk.BooleanVar(value=True)
        self._resp_ideal = tk.BooleanVar(value=False)
        self._overlay = tk.StringVar(
            value=overlay if overlay in OVERLAYS else "none")
        self._overlay_ch = tk.StringVar(
            value=overlay_ch if overlay_ch in OVERLAY_CHANNELS else "both")

        # What Defaults restores -- the dump file isn't in here, it's what
        # you're looking at, not a setting.
        self._initial = {id(v): v.get() for v in
                         (self._fft_size, self._fmax, self._db_min,
                          self._peak_fmin, self._shift, self._settle,
                          self._view, self._resp_size, self._resp_points,
                          self._resp_drive, self._resp_averages,
                          self._resp_fmin, self._resp_fmax, self._resp_floor,
                          self._resp_ideal, self._overlay, self._overlay_ch,
                          *self._pipe, *self._impl, *self._curves.values(),
                          *self._tunable.values())}

        self._build_controls()
        self._build_report()

        self.reload_dumps(select=path)

    # ------------------------------------------------------------------
    # Widgets
    # ------------------------------------------------------------------
    def _layout(self, shape):
        """The axes grid for the current view, rebuilt only when the shape
        changes -- capture wants 2x2 (channel x time/spectrum), response
        wants 2x1 (amplitude over phase). Cheap either way: this figure is
        static, nothing here is animated."""
        # The dBFS overlay hangs a twinx off the magnitude axis. ax.clear()
        # does not remove it (it is a separate axes in the figure), so
        # without this it would survive every redraw and stack up.
        if self._twin is not None:
            self._twin.remove()
            self._twin = None
        if shape != self._shape:
            self.fig.clear()
            self.axes = self.fig.subplots(*shape, squeeze=False)
            self._shape = shape
        for row in self.axes:
            for ax in row:
                ax.clear()
                ax.set_visible(True)
        return self.axes

    def _sync_impl(self, ch):
        """Offer the implementation choice only where one exists -- bypass
        and iir are fixed, no implementation choice to claim."""
        combo = self._impl_combo[ch]
        choices = _impls(self._pipe[ch].get())
        if choices:
            combo["values"] = choices
            combo["state"] = "readonly"
            if self._impl[ch].get() not in choices:
                self._impl[ch].set(choices[0])
        else:
            combo["values"] = ()
            self._impl[ch].set("--")
            combo["state"] = "disabled"

    def _model_for(self, ch):
        """The "pipe" or "pipe:impl" string for one channel, or None."""
        pipe = self._pipe[ch].get()
        if pipe == "none" or pipe not in _pipes():
            return None
        impls = _impls(pipe)
        return f"{pipe}:{self._impl[ch].get()}" if impls else pipe

    def _entry(self, parent, row, label, var, tip=None, width=10):
        lbl = ttk.Label(parent, text=label)
        lbl.grid(row=row, column=0, sticky="w", pady=2)
        ent = ttk.Entry(parent, textvariable=var, width=width)
        ent.grid(row=row, column=1, sticky="e", pady=2)
        # Enter = Recompute shortcut. No <FocusOut> -- that would be the
        # apply-on-every-change behaviour the button exists to replace.
        ent.bind("<Return>", lambda _e: self.refresh())
        if tip:
            _Tooltip(lbl, tip)
            _Tooltip(ent, tip)
        return row + 1

    def _build_controls(self):
        # Apply/Defaults packed first from the bottom, then the notebook --
        # reversed, Tk drops the bottom button entirely rather than
        # clipping it once the column exceeds the window. See guiutil.ScrollFrame.
        recompute = ttk.Button(self.controls, text="Apply / Recompute",
                               command=self.refresh)
        recompute.pack(side="bottom", fill="x", pady=(6, 0))
        defaults = ttk.Button(self.controls, text="Defaults",
                              command=self.reset_defaults)
        defaults.pack(side="bottom", fill="x", pady=(6, 0))
        _Tooltip(defaults,
                 "Put every field back to what this window opened with "
                 "(sat.py's defaults, unless overridden on the command "
                 "line) and recompute. The loaded dump is kept, and the "
                 "filter corners go back to the ones it recorded.")
        _Tooltip(recompute,
                 "Apply every field in every tab and redraw. Nothing typed "
                 "or selected takes effect until this is pressed (Enter in "
                 "any field does the same). Choosing a different dump loads "
                 "it immediately -- that is picking what to look at, not "
                 "changing how it is analysed.")

        # View sits above the notebook rather than in it: it decides which
        # tabs exist, so it cannot live inside one of them.
        head = ttk.Frame(self.controls, padding=(0, 0, 0, 6))
        head.pack(side="top", fill="x")
        ttk.Label(head, text="View", font=("", 10, "bold")).grid(
            row=0, column=0, sticky="w")
        view_box = ttk.Combobox(head, textvariable=self._view, width=10,
                                state="readonly", values=list(VIEWS))
        view_box.grid(row=0, column=1, sticky="e")
        view_box.bind("<<ComboboxSelected>>", lambda _e: self._switch_view())
        _Tooltip(view_box,
                 "capture: the recorded buffer -- time traces and their "
                 "spectra, plus the model comparison.\n\n"
                 "response: the pipelines themselves -- amplitude and phase "
                 "against frequency, measured by driving each one.\n\n"
                 "The tabs below change with it: each view gets the controls "
                 "that act on it and no others. Settings in the hidden tabs "
                 "keep their values, so switching back finds them unchanged.")
        head.columnconfigure(0, weight=1)

        scroller = guiutil.ScrollFrame(self.controls, max_req_height=360,
                                       padding=0)
        scroller.outer.pack(side="top", fill="both", expand=True)
        # ttk sizes a notebook to its widest PAGE and never consults its tab
        # strip, so a view with one tab more than the pages are wide gets the
        # last tab silently cut in half. Nothing exposes the strip's width,
        # so it is measured from the font here and reserved as a floor.
        style = ttk.Style()
        style.configure("SAT.TNotebook.Tab", padding=(6, 3))
        self._tabs = ttk.Notebook(scroller.body, style="SAT.TNotebook")
        self._tabs.pack(fill="both", expand=True)

        # Every tab is built once and stays built; _switch_view only adds
        # and forgets them. Rebuilding per view would drop widget state and
        # flicker, and the panels are cheap to keep.
        self._tab_dump = self._build_dump_tab()
        self._tab_capture_plot = self._build_capture_plot_tab()
        self._tab_model = self._build_model_tab()
        self._tab_curves = self._build_curves_tab()
        self._tab_measure = self._build_measure_tab()
        self._tab_response_plot = self._build_response_plot_tab()
        self._switch_view(redraw=False)
        # Once geometry has settled, so the pages report real widths.
        self.controls.after_idle(self._reserve_tab_strip)

    def _reserve_tab_strip(self):
        """Widen the notebook, if needed, so the busiest view's tab strip
        fits.

        Measured rather than guessed: label font and tab padding both come
        from the live theme, so this holds wherever it runs rather than only
        where it was tuned. Every page is sized, not just the current view's
        -- otherwise the column would change width on each view switch.
        """
        style = ttk.Style()
        font = tkfont.Font(font=style.lookup("TNotebook.Tab", "font")
                           or "TkDefaultFont")
        padding = style.lookup("SAT.TNotebook.Tab", "padding")
        try:
            per_tab = 2 * int(str(padding).split()[0])
        except (TypeError, ValueError, IndexError):
            per_tab = 12

        was = self._view.get()
        strip = 0
        for view in VIEWS:
            self._view.set(view)
            strip = max(strip, sum(font.measure(label) + per_tab
                                   for _page, label in self._view_tabs()))
        self._view.set(was)
        pages = max(page.winfo_reqwidth() for page in
                    (self._tab_dump, self._tab_capture_plot, self._tab_model,
                     self._tab_curves, self._tab_measure,
                     self._tab_response_plot))
        # The allowance is deliberately generous: the font measurement runs
        # a few px under what ttk actually lays out, and being short clips a
        # tab whereas being long only widens the column slightly. Pages
        # usually win anyway; the strip matters for the busiest view.
        self._tabs.configure(width=max(pages, strip + 16))

    def _tab(self):
        """A fresh page for the notebook. Not added to it here -- which
        pages are shown is _switch_view's business."""
        frame = ttk.Frame(self._tabs, padding=8)
        frame.columnconfigure(0, weight=1)
        return frame

    # --- tabs ---------------------------------------------------------
    def _build_dump_tab(self):
        """Which capture is loaded. Shown in BOTH views, deliberately: the
        response view reads the dump's sample rate, its filter corners and
        (for the overlays) its samples, so being unable to change it there
        would mean leaving the view to do it. As a tab it costs no space
        until you want it, which was the actual problem with it."""
        f = self._tab()
        self._file_box = ttk.Combobox(f, textvariable=self._file, width=24,
                                      state="readonly")
        self._file_box.grid(row=0, column=0, columnspan=2, sticky="ew", pady=2)
        self._file_box.bind("<<ComboboxSelected>>", lambda _e: self.load_selected())
        _Tooltip(self._file_box,
                 "Dumps written by the client's 'Log buffer' button, newest "
                 "first. Each one carries its own settings sidecar, which is "
                 "where the sample rate, the board's shift register and the "
                 "filter corners come from -- so it feeds the response view "
                 "too, not just the capture view.")
        ttk.Button(f, text="Rescan", command=self.reload_dumps).grid(
            row=1, column=0, sticky="ew", pady=(4, 0), padx=(0, 2))
        ttk.Button(f, text="Open…", command=self.open_file).grid(
            row=1, column=1, sticky="ew", pady=(4, 0), padx=(2, 0))
        del_btn = ttk.Button(f, text="Delete all logs",
                             command=self._delete_all_dumps)
        del_btn.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        _Tooltip(del_btn,
                 "Deletes every plotdump_*.csv in this folder and its "
                 "settings sidecar -- everything the client's 'Log buffer' "
                 "button has produced. Asks for confirmation first. Cannot "
                 "be undone.")
        f.columnconfigure(1, weight=1)
        return f

    def _build_capture_plot_tab(self):
        """What the capture view's spectra are computed over and shown on."""
        f = self._tab()
        ttk.Label(f, text="Size").grid(row=0, column=0, sticky="w", pady=2)
        size_box = ttk.Combobox(f, textvariable=self._fft_size, width=8,
                                state="readonly",
                                values=[_size_label(v) for v in FFT_SIZES])
        size_box.grid(row=0, column=1, sticky="e", pady=2)
        _Tooltip(size_box,
                 "How many of the newest samples are transformed. \"capture\" "
                 "uses the whole dump. Resolution is rate/N and is shown in "
                 "the report. Truncates, never zero-pads: padding would "
                 "interpolate between real bins and pull the apparent noise "
                 "floor down.")
        row = 1
        row = self._entry(f, row, "F max (Hz)", self._fmax,
                          "Frequency axis limit for the spectra. 0 = Nyquist. "
                          "The response view has its own F min / F max, since "
                          "there the limits choose what gets measured and not "
                          "just what is shown.")
        row = self._entry(f, row, "dB min", self._db_min,
                          "Bottom of the magnitude axis, dBFS. 0 dB is a sine "
                          "spanning the full wire range. Shared with the "
                          "response view's Plot tab, where the same number is "
                          "the bottom of the gain axis.")
        row = self._entry(f, row, "Peak from (Hz)", self._peak_fmin,
                          "Ignore bins below this when locating the peak. "
                          "Raise it above the ECG band (say 40) to measure an "
                          "injected tone -- otherwise the ECG's harmonics win, "
                          "because they are genuinely stronger.")
        return f

    def _build_model_tab(self):
        """The capture view's 'did the hardware compute what I think'
        comparison, per channel."""
        f = self._tab()
        row = 0
        for ch in range(2):
            ttk.Label(f, text=f"Ch{ch + 1} pipe").grid(
                row=row, column=0, sticky="w", pady=2)
            pipe_box = ttk.Combobox(f, textvariable=self._pipe[ch], width=8,
                                    state="readonly", values=list(_pipes()))
            pipe_box.grid(row=row, column=1, sticky="e", pady=2)
            # Retargets the impl dropdown only, doesn't recompute.
            pipe_box.bind("<<ComboboxSelected>>",
                          lambda _e, c=ch: self._sync_impl(c))
            _Tooltip(pipe_box,
                     "Run this pipeline on the recorded INPUT for this "
                     "channel and score it against the recorded OUTPUT -- "
                     "'did the hardware compute what I think it computed'. "
                     "iir is a bit-accurate model of axi_tdm_filter.vhd. "
                     "none skips this channel. Drawn as a green dashed line "
                     "over the output; where they separate is the answer.")
            row += 1

            ttk.Label(f, text=f"Ch{ch + 1} impl").grid(
                row=row, column=0, sticky="w", pady=(0, 6))
            impl_box = ttk.Combobox(f, textvariable=self._impl[ch], width=8,
                                    state="readonly")
            impl_box.grid(row=row, column=1, sticky="e", pady=(0, 6))
            self._impl_combo.append(impl_box)
            _Tooltip(impl_box,
                     "scipy: the float64 design. manual: the hand-written "
                     "integer version that becomes RTL. Scoring a capture "
                     "against each in turn is how you see what quantisation "
                     "cost. Greyed out for bypass and iir, which are single "
                     "fixed things with no implementation to choose.")
            self._sync_impl(ch)
            row += 1
        row = self._entry(f, row, "Shift", self._shift,
                          "Blank = whatever the board's register actually "
                          "held, read from the sidecar. Set it to try a "
                          "different one against the same capture.")
        row = self._entry(f, row, "Settle", self._settle,
                          "Samples skipped before scoring. The model starts "
                          "from zeroed state and the board did not, so the "
                          "first samples disagree for a reason that says "
                          "nothing about the algorithm.")
        return f

    def _build_curves_tab(self):
        """Which pipelines the response view draws, and the corners they
        run with -- 'which filters, and configured how'."""
        f = self._tab()
        ttk.Label(f, text="Draw these, overlaid:").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 2))
        # A row per pipeline, so manual and scipy sit side by side -- they
        # are read as a pair, and the difference between them is the reason
        # both exist. Stacking all six in one column also made this the one
        # tab tall enough to need scrolling.
        groups = {}
        for name in self._curves:
            groups.setdefault(name.split(":")[0], []).append(name)
        single = [g[0] for g in groups.values() if len(g) == 1]
        rows = ([single[i:i + 2] for i in range(0, len(single), 2)]
                + [g for g in groups.values() if len(g) > 1])

        row = 1
        for names in rows:
            for col, name in enumerate(names):
                box = ttk.Checkbutton(f, text=name, variable=self._curves[name])
                box.grid(row=row, column=col, sticky="w")
                _Tooltip(box,
                         f"Measure {name} and draw it on the amplitude and "
                         f"phase axes. Tick as many as you like -- they share "
                         f"the axes, one colour per pipeline and a dashed line "
                         f"for the integer (manual) arithmetic, so the distance "
                         f"between a solid and a dashed line of the same "
                         f"colour is what fixed point costs.")
            row += 1
        btns = ttk.Frame(f)
        btns.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(4, 6))
        ttk.Button(btns, text="All", width=6,
                   command=lambda: self._set_curves(True)).pack(
                       side="left", expand=True, fill="x")
        ttk.Button(btns, text="None", width=6,
                   command=lambda: self._set_curves(False)).pack(
                       side="left", expand=True, fill="x")
        row += 1

        ttk.Separator(f, orient="horizontal").grid(
            row=row, column=0, columnspan=2, sticky="ew", pady=(4, 6))
        row += 1
        tunable_tip = (
            "The pipeline's own corner, for the response measurement only. "
            "Filled in from the loaded capture (or the pipeline's own "
            "default) and refilled whenever a dump is loaded, so it shows "
            "the filter that actually ran. Type a number to sweep it and "
            "watch what moves.\n\n"
            "It deliberately does NOT reach the capture view's Model "
            "comparison. Scoring a recording against a filter that never ran "
            "on it would not mean anything; asking what a filter DOES at a "
            "different corner is a fair question, and that is this view.")
        for _sidecar, key, label in sat.TUNABLES:
            row = self._entry(f, row, label, self._tunable[key], tunable_tip)
        from_capture = ttk.Button(f, text="Corners from capture",
                                  command=self._reset_tunables)
        from_capture.grid(row=row, column=0, columnspan=2, sticky="ew",
                          pady=(4, 0))
        _Tooltip(from_capture,
                 "Put the corners back to what the loaded dump's sidecar "
                 "recorded, and recompute.")
        return f

    def _build_measure_tab(self):
        """How hard the response view looks -- excitation length, how many
        tones, how hard it drives, how many realisations."""
        f = self._tab()
        ttk.Label(f, text="Size").grid(row=0, column=0, sticky="w", pady=2)
        resp_size = ttk.Combobox(f, textvariable=self._resp_size, width=9,
                                 state="readonly",
                                 values=[str(v) for v in sat.RESPONSE_SIZES])
        resp_size.grid(row=0, column=1, sticky="e", pady=2)
        resp_size.bind("<<ComboboxSelected>>", lambda _e: self._pin_size())
        _Tooltip(resp_size,
                 "Length of one excitation period, in samples. This is what "
                 "sets how low the plot can reach: the lowest frequency that "
                 "exists at all is one bin, rate/Size — 0.125 Hz at 16384 "
                 "and 2048 Hz, 0.001 Hz at the top of this list.\n\n"
                 "You do not normally have to set it. Lowering F min (Plot "
                 "tab) raises this on its own, to whatever that frequency "
                 "needs, and shows you the value it used — the cost is real "
                 "(about 1.6 s and 200 MB per curve at the largest) so it is "
                 "not hidden. Set it directly only to buy resolution you "
                 "have not asked for by another route.")
        row = 1
        row = self._entry(f, row, "Tones", self._resp_points,
                          "How many tones the excitation carries, spread "
                          "logarithmically. They are snapped to whole FFT "
                          "bins and duplicates dropped, so the low end thins "
                          "out on its own -- the report says how many "
                          "actually landed. Extra tones are always clustered "
                          "on the corner frequencies from the Curves tab, so "
                          "a narrow notch gets measured rather than stepped "
                          "over.")
        row = self._entry(f, row, "Drive (% FS)", self._resp_drive,
                          "Peak excitation as a percentage of full scale. "
                          "Not cosmetic: an integer pipeline's response "
                          "depends on level, and a drive small enough to sit "
                          "in the truncation dead zone will measure as no "
                          "filter at all. Too high and it clips.")
        row = self._entry(f, row, "Averages", self._resp_averages,
                          "Realisations averaged, each with fresh random "
                          "phases. More is a cleaner curve and a lower floor, "
                          "at proportional cost.")
        row = self._entry(f, row, "Rate (Hz)", self._resp_rate,
                          "Sample rate to run the pipelines at. Blank = the "
                          "capture's, which is the honest default.\n\n"
                          "This is the ONLY way past the high end of the "
                          "axis: it stops at Nyquist, half the rate, and no "
                          "other setting moves that because a sampled signal "
                          "does not carry anything above it. Raise this to "
                          "ask a different question -- what would this "
                          "filter do at 8192 Hz. The corners stay where they "
                          "are in Hz, so the digital filter really is a "
                          "different one, which is the interesting part.")
        return f

    def _build_response_plot_tab(self):
        """The response view's axes and what else gets drawn on them."""
        f = self._tab()
        band_tip = ("The band the response is measured over. These bound the "
                    "excitation, not just the axis, so outside them nothing "
                    "is asked and nothing is drawn. Narrowing spends the same "
                    "number of tones over less frequency, which is how you "
                    "get resolution where you want it: 40-60 Hz with 200 "
                    "tones resolves the notch's shape properly.\n\n"
                    "Both ends stop at a hard limit, and they are different "
                    "limits with different answers. Below, it is one bin — "
                    "rate/Size — so lowering F min simply raises Size (Measure "
                    "tab) for you until it fits; watch it follow. Above, it "
                    "is Nyquist, half the Rate, and nothing reaches past it: "
                    "raise RATE on the Measure tab instead, which asks what "
                    "the filter does at a faster sample rate.\n\n"
                    "Asking for more than either is not an error and is not "
                    "silently ignored: the axis stops where the measurement "
                    "does, so there is never blank space that reads as "
                    "missing data, and the report names the wall you hit. "
                    "0 means the limit itself.")
        row = 0
        row = self._entry(f, row, "F min (Hz)", self._resp_fmin, band_tip)
        row = self._entry(f, row, "F max (Hz)", self._resp_fmax, band_tip)
        row = self._entry(f, row, "dB min", self._db_min,
                          "Bottom of the amplitude axis, in dB of gain (0 dB "
                          "is unity). Shared with the capture view's Plot "
                          "tab, where the same number is dBFS.")

        ttk.Separator(f, orient="horizontal").grid(
            row=row, column=0, columnspan=2, sticky="ew", pady=(8, 4))
        row += 1
        floor_box = ttk.Checkbutton(f, text="Noise + distortion floor",
                                    variable=self._resp_floor)
        floor_box.grid(row=row, column=0, columnspan=2, sticky="w")
        _Tooltip(floor_box,
                 "Dotted: what came out on the bins where nothing was put in. "
                 "Nothing put energy there, so it is the pipeline's own "
                 "truncation noise and distortion, referred to the drive "
                 "level. Flat and low for scipy; for manual it is the real "
                 "bottom of the plot -- attenuation below this line is not "
                 "attenuation you get. Only drawn when some of it is above "
                 "dB min.")
        row += 1
        ideal_box = ttk.Checkbutton(f, text="Design curve (from sos)",
                                    variable=self._resp_ideal)
        ideal_box.grid(row=row, column=0, columnspan=2, sticky="w")
        _Tooltip(ideal_box,
                 "Thin black: the transfer function computed straight from "
                 "the designed coefficients, for the pipelines that keep an "
                 "sos (the scipy ones). It is drawn as a check on the "
                 "measurement itself -- if it does not sit on top of the "
                 "measured scipy curve, distrust the measurement, not the "
                 "filter.")
        row += 1

        ttk.Separator(f, orient="horizontal").grid(
            row=row, column=0, columnspan=2, sticky="ew", pady=(8, 4))
        row += 1
        ttk.Label(f, text="Overlay capture").grid(row=row, column=0,
                                                  sticky="w", pady=2)
        overlay_box = ttk.Combobox(f, textvariable=self._overlay, width=9,
                                   state="readonly", values=list(OVERLAYS))
        overlay_box.grid(row=row, column=1, sticky="e", pady=2)
        _Tooltip(overlay_box,
                 "Put the loaded capture (Dump tab) on the response axes. "
                 "Two different things, because they are two different "
                 "quantities:\n\n"
                 "gain — the recording's OWN in/out ratio, band by band. "
                 "That is a gain in dB, the same quantity the response "
                 "curves are, so it goes on the same axis and compares "
                 "directly: what the run actually delivered against what the "
                 "model says it should. Drawn in black/grey with markers, "
                 "and it stops where the capture stops being able to "
                 "answer.\n\n"
                 "spectrum — the input's dBFS level, on a second axis on the "
                 "right. That is a LEVEL, not a gain, and the two are not "
                 "comparable as numbers. It answers a different question: is "
                 "the signal actually where the filter is? Does the 50 Hz "
                 "notch sit on a real mains peak in this recording. The "
                 "right axis is given the same dB span as the left, so "
                 "slopes on it mean what they look like.")
        row += 1
        ttk.Label(f, text="Overlay from").grid(row=row, column=0, sticky="w",
                                               pady=2)
        overlay_ch_box = ttk.Combobox(f, textvariable=self._overlay_ch,
                                      width=9, state="readonly",
                                      values=list(OVERLAY_CHANNELS))
        overlay_ch_box.grid(row=row, column=1, sticky="e", pady=2)
        _Tooltip(overlay_ch_box,
                 "Which channel the overlay is taken from. The two channels "
                 "can have run different pipelines, so 'both' is two "
                 "different measurements, not one measured twice.")
        return f

    def _set_curves(self, value):
        for var in self._curves.values():
            var.set(value)

    def _view_tabs(self):
        """(page, label) for the current view, in tab order.

        Dump is in both: the response view reads the loaded capture's sample
        rate, its filter corners and -- for the overlays -- its samples, so
        dropping it there would mean leaving the view to change dumps. Plot
        is in both too, meaning the same thing each time: what the axes span
        and what gets drawn on them.
        """
        if self._view.get() == "response":
            return ((self._tab_dump, "Dump"),
                    (self._tab_curves, "Curves"),
                    (self._tab_measure, "Measure"),
                    (self._tab_response_plot, "Plot"))
        return ((self._tab_dump, "Dump"),
                (self._tab_capture_plot, "Plot"),
                (self._tab_model, "Model"))

    def _switch_view(self, redraw=True):
        """Rebuild the tab strip for the current view. Pages are forgotten,
        not destroyed, so every setting in a hidden tab keeps its value and
        switching back finds it unchanged."""
        wanted = self._view_tabs()
        labels = [label for _page, label in wanted]
        # Only when the strip actually differs: _switch_view also runs on
        # Defaults and at startup, and forgetting/re-adding every page would
        # throw away which tab you were on for no reason.
        if [self._tabs.tab(t, "text") for t in self._tabs.tabs()] != labels:
            for page in list(self._tabs.tabs()):
                self._tabs.forget(self._tabs.nametowidget(page))
            for page, label in wanted:
                self._tabs.add(page, text=label)
        if redraw:
            self.refresh()

    def _selected_curves(self):
        return [name for name, var in self._curves.items() if var.get()]

    def _pin_size(self):
        """Picking a Size from the dropdown makes it the floor -- the value
        F min is allowed to raise but not to fall below."""
        try:
            self._size_floor = int(self._resp_size.get())
        except ValueError:
            pass

    def _fill_tunables(self):
        """Show the corners the loaded capture ran with (falling back to the
        pipeline's own defaults). Called on every load, so the fields track
        whichever dump is open instead of quietly keeping the last one's."""
        values = sat.resolve_tunables((self.info or {}).get("meta"))
        values.update(self._pending_tunables)
        self._pending_tunables = {}
        for key, var in self._tunable.items():
            var.set(_fmt(values.get(key)))

    def _reset_tunables(self):
        self._fill_tunables()
        self.refresh()

    def _overrides(self):
        """The corner fields as {params key: float}. A blank field is left
        out entirely, so model_params falls through to the capture's own
        value rather than to a zero."""
        out = {}
        for key, var in self._tunable.items():
            text = var.get().strip()
            if not text:
                continue
            try:
                out[key] = float(text)
            except ValueError:
                var.set("")           # unreadable: say so by clearing it
        return out

    def reset_defaults(self):
        """Every analysis field back to its startup value, then redraw."""
        for var in (self._fft_size, self._fmax, self._db_min, self._peak_fmin,
                    self._shift, self._settle, self._view, self._resp_size,
                    self._resp_points, self._resp_drive, self._resp_averages,
                    self._resp_fmin, self._resp_fmax, self._resp_floor,
                    self._resp_ideal, self._overlay, self._overlay_ch,
                    *self._pipe, *self._impl, *self._curves.values(),
                    *self._tunable.values()):
            var.set(self._initial[id(var)])
        # The corners' "default" is the loaded capture's, not whatever the
        # window happened to open with.
        self._fill_tunables()
        self._pin_size()
        for ch in range(2):
            self._sync_impl(ch)
        self._switch_view(redraw=False)
        self.refresh()

    def _build_report(self):
        # Text, not Label, so the numbers can be selected/copied.
        self.report = tk.Text(self.report_frame, height=11, wrap="none",
                              font=("monospace", 9), background="#f7f7f7",
                              relief="flat")
        bar = ttk.Scrollbar(self.report_frame, orient="vertical",
                            command=self.report.yview)
        self.report.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        self.report.pack(side="left", fill="both", expand=True)

    # ------------------------------------------------------------------
    # Data
    # ------------------------------------------------------------------
    def reload_dumps(self, select=None):
        dumps = sat.find_dumps(self.log_dir)
        self._dumps = {p.name: p for p in reversed(dumps)}   # newest first
        self._file_box["values"] = list(self._dumps)
        if select is not None:
            self._dumps.setdefault(select.name, select)
            if select.name not in self._file_box["values"]:
                self._file_box["values"] = [select.name] + list(self._file_box["values"])
            self._file.set(select.name)
        elif self._dumps and not self._file.get():
            self._file.set(next(iter(self._dumps)))
        if self._file.get():
            self.load_selected()
        else:
            self._say(f"No dumps in {self.log_dir}.\n\n"
                      f"Press 'Log buffer' in the streaming client to make "
                      f"one, or use Open… to pick a file elsewhere.")

    def _delete_all_dumps(self):
        dumps = sat.find_dumps(self.log_dir)
        if not dumps:
            return
        if not messagebox.askyesno(
                "Delete all logs",
                f"Delete all {len(dumps)} logged buffer(s) in "
                f"{self.log_dir}?\n\nEach plotdump_*.csv and its settings "
                f"sidecar will be removed. This cannot be undone."):
            return
        for csv_path in dumps:
            sidecar = sat.sidecar_for(csv_path)
            for path in (csv_path, sidecar):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    print(f"[sat] could not delete {path.name}: {exc}")
        self.traces = self.info = None
        self._file.set("")
        self._dumps = {}
        self.reload_dumps()

    def open_file(self):
        from pathlib import Path
        name = filedialog.askopenfilename(
            title="Open a buffer dump", initialdir=str(self.log_dir),
            filetypes=[("Buffer dumps", "plotdump_*.csv"), ("CSV", "*.csv"),
                       ("All files", "*")])
        if name:
            self.reload_dumps(select=Path(name))

    def load_selected(self):
        path = self._dumps.get(self._file.get())
        if path is None:
            return
        try:
            self.traces, self.info = sat.load_dump(path)
        except Exception as exc:                          # noqa: BLE001
            # A malformed/truncated dump must not take the window down.
            self.traces = self.info = None
            self._say(f"Could not read {path.name}:\n\n{exc}")
            return
        self._fill_tunables()
        self.refresh()

    # ------------------------------------------------------------------
    # Recompute + redraw
    # ------------------------------------------------------------------
    def _number(self, var, fallback, cast=float):
        try:
            return cast(var.get())
        except (TypeError, ValueError):
            var.set(f"{fallback:g}" if isinstance(fallback, float) else str(fallback))
            return fallback

    def refresh(self):
        if self.traces is None:
            return
        size = _size_value(self._fft_size.get())
        fmax = max(0.0, self._number(self._fmax, 0.0))
        db_min = min(-6.0, self._number(self._db_min, -120.0))
        peak_fmin = max(0.0, self._number(self._peak_fmin, 1.0))
        settle = max(0, self._number(self._settle, 200, int))
        self._fmax.set(f"{fmax:g}")
        self._db_min.set(f"{db_min:g}")

        shift_text = self._shift.get().strip()
        if shift_text:
            try:
                shift = int(shift_text)
            except ValueError:
                shift = None
        else:
            shift = None
        if shift is None:
            shift = int(self.info["shift"]) if self.info.get("shift") is not None else 4
        # Show what was actually used -- resolves a blank field's value.
        self._shift.set(str(shift))

        if self._view.get() == "response":
            # Blank Rate keeps tracking the capture, so it is not written
            # back the way a resolved Shift is.
            text = self._resp_rate.get().strip()
            try:
                rate = float(text) if text else 0.0
            except ValueError:
                self._resp_rate.set("")
                rate = 0.0
            if rate <= 0:
                rate = self.info["rate"]
            # Overrides only here -- see model_params. The comparison below
            # keeps the capture's own settings.
            self._refresh_response(
                sat.model_params(shift, rate, self.info.get("meta"),
                                 self._overrides()),
                db_min, rate)
            return

        full_scale = sat.wire_full_scale(self.traces, self.info)
        results, curves = [], {}
        for ch in ("ch1", "ch2"):
            if f"{ch}_in" not in self.traces and f"{ch}_out" not in self.traces:
                continue
            r, c = sat.analyse_channel(self.traces, ch, self.info["rate"],
                                            full_scale, size, peak_fmin)
            results.append(r)
            curves[ch] = c

        # Each channel scored against its own selection, or skipped.
        models = []
        for i, ch in enumerate(("ch1", "ch2")):
            model = self._model_for(i)
            if model is None:
                continue
            models.append(sat.compare_model(self.traces, ch, model, shift,
                                            settle, self.info["rate"],
                                            self.info.get("meta")))

        self._say(sat.format_report(self.info, results, models))
        self._draw(curves, models, fmax, db_min)

    def _refresh_response(self, params, db_min, rate):
        names = self._selected_curves()
        overlay = self._overlay.get()
        if not names and overlay == "none":
            self._layout((2, 1))
            self.fig.canvas.draw_idle()
            self._say("No pipelines ticked.\n\nTick one or more in 'Response "
                      "curves' -- they are drawn over each other on the same "
                      "axes, which is what the view is for. Or set 'Overlay "
                      "capture' to see the loaded recording on its own.")
            return

        size = self._size_floor
        points = max(2, self._number(self._resp_points,
                                     sat.DEFAULT_RESPONSE_POINTS, int))
        drive = min(0.95, max(0.001, self._number(
            self._resp_drive, sat.DEFAULT_RESPONSE_DRIVE * 100.0) / 100.0))
        averages = max(1, self._number(self._resp_averages,
                                       sat.DEFAULT_RESPONSE_AVERAGES, int))
        fmin = max(0.0, self._number(self._resp_fmin, 0.0))
        fmax = max(0.0, self._number(self._resp_fmax, 0.0))
        # An inverted band would silently measure nothing; say so by
        # dropping the upper bound rather than drawing an empty plot.
        if 0.0 < fmax <= fmin:
            fmax = 0.0
        # F min names a frequency; Size is what actually delivers one, since
        # the lowest that exists is one bin (rate/Size). Asking for a lower
        # F min is really asking for a longer period, so grow it to fit
        # rather than quietly measuring nothing down there. Written back to
        # the Size box, because the cost is real and should be visible.
        size = min(max(size, sat.size_for_fmin(rate, fmin)),
                   sat.RESPONSE_SIZE_MAX)
        self._resp_size.set(str(size))
        self._resp_points.set(str(points))
        self._resp_drive.set(f"{drive * 100:g}")
        self._resp_averages.set(str(averages))
        self._resp_fmin.set(f"{fmin:g}")
        self._resp_fmax.set(f"{fmax:g}")

        # Every curve is several passes of `size` samples through a real
        # pipeline, so this is the one control in the window that can take a
        # visible moment. Say so before starting rather than looking hung.
        self._say(f"Measuring {len(names)} pipeline(s) at {rate:g} Hz, "
                  f"{averages} x {(sat.RESPONSE_SETTLE_PERIODS + 1) * size} samples each…")
        self.report.update_idletasks()

        responses = []
        for name in names:
            try:
                responses.append(sat.measure_response(
                    name, params, rate, size=size, points=points, drive=drive,
                    averages=averages, fmin=fmin, fmax=fmax))
            except Exception as exc:                      # noqa: BLE001
                # A pipeline is free to raise (see pipelines.py) -- one bad
                # one must not cost the others their curves.
                print(f"[sat] {name}: {exc}")
                responses.append(None)

        responses = [r for r in responses if r]

        channels = (OVERLAY_CHANNELS[:2] if self._overlay_ch.get() == "both"
                    else (self._overlay_ch.get(),))
        gains, spectra = [], []
        if overlay in ("gain", "both"):
            # Onto the response's own x positions, so the measured and the
            # modelled are read off one grid instead of two that nearly
            # agree. With nothing ticked there is no grid to share, so build
            # the one the curves would have used.
            grid = (responses[0]["freqs"] if responses
                    else sat.response_bins(size, rate, points, fmin, fmax)
                    * (rate / size))
            gains = [g for g in (sat.capture_gain(self.traces, ch, rate, grid)
                                 for ch in channels) if g]
        if overlay in ("spectrum", "both"):
            full_scale = sat.wire_full_scale(self.traces, self.info)
            spectra = [s for s in
                       (sat.capture_spectrum(self.traces, ch, rate, full_scale)
                        for ch in channels) if s]

        self._say(sat.format_report(self.info, [], [], responses, gains))
        self._draw_response(responses, fmax, db_min, fmin, gains, spectra)

    def _say(self, text):
        self.report.configure(state="normal")
        self.report.delete("1.0", "end")
        self.report.insert("1.0", text)
        self.report.configure(state="disabled")

    def _draw(self, curves_by_ch, models, fmax, db_min):
        rate = self.info["rate"]
        channels = [ch for ch in ("ch1", "ch2")
                    if f"{ch}_in" in self.traces or f"{ch}_out" in self.traces]
        model_by_ch = {m["channel"]: m for m in models if m}
        t = np.arange(self.info["samples"]) / rate

        self._layout((2, 2))
        for row in range(2):
            for col in range(2):
                self.axes[row][col].set_visible(row < len(channels))

        for row, ch in enumerate(channels):
            ax_t, ax_f = self.axes[row]
            for direction, color in (("in", "tab:blue"), ("out", "tab:red")):
                key = f"{ch}_{direction}"
                if key in self.traces:
                    ax_t.plot(t, self.traces[key], color=color, lw=0.9, label=direction)
            m = model_by_ch.get(ch)
            if m is not None:
                # Dashed over the recorded output -- separation is the point.
                ax_t.plot(t, m["modelled"], color="tab:green", lw=0.9, ls="--",
                          label=f"model ({m['algorithm']})")
            ax_t.set_title(f"{ch} — time")
            ax_t.set_xlabel("Time (s)")
            ax_t.legend(fontsize=8)
            ax_t.grid(alpha=0.3)

            for direction, color in (("in", "tab:blue"), ("out", "tab:red")):
                if direction in curves_by_ch.get(ch, {}):
                    f, db = curves_by_ch[ch][direction]
                    ax_f.plot(f, db, color=color, lw=0.8, label=direction)
            ax_f.set_title(f"{ch} — spectrum")
            ax_f.set_xlabel("Frequency (Hz)")
            ax_f.set_ylabel("dBFS")
            ax_f.set_xlim(0, fmax if fmax else rate / 2)
            ax_f.set_ylim(db_min, 6)
            ax_f.legend(fontsize=8)
            ax_f.grid(alpha=0.3)

        self.fig.tight_layout()
        self.fig.canvas.draw_idle()

    def _draw_response(self, responses, fmax, db_min, fmin, gains=(), spectra=()):
        """Every ticked pipeline on one pair of axes: amplitude over phase,
        log frequency. Overlaid deliberately -- the reason to look at two of
        these at once is the distance between them."""
        axes = self._layout((2, 1))
        ax_mag, ax_ph = axes[0][0], axes[1][0]
        if not responses and not gains and not spectra:
            self.fig.canvas.draw_idle()
            return

        # What the curves were actually measured at, which is the Rate field
        # when it is set -- not the capture's, or the title would name a
        # sample rate nothing on the plot was run at.
        rate = responses[0]["fs"] if responses else self.info["rate"]
        lows = ([float(r["freqs"][0]) for r in responses]
                + [float(g["freqs"][0]) for g in gains])
        highs = ([float(r["freqs"][-1]) for r in responses]
                 + [float(g["freqs"][-1]) for g in gains])
        # The dBFS overlay on its own has no measured band to bound the axis
        # with -- it starts at DC, which a log axis cannot show at all.
        measured_lo = min(lows) if lows else rate / 1024.0
        measured_hi = max(highs) if highs else rate / 2.0
        # Never stretch past what was measured. Below one bin (rate/Size)
        # and above Nyquist there is nothing to draw, and an axis running
        # into either is empty space that reads as missing data rather than
        # as a limit. The report says so when a field asked for more.
        lo = max(fmin, measured_lo) if fmin > 0 else measured_lo
        hi = min(fmax, measured_hi) if fmax > 0 else measured_hi
        if hi <= lo:                       # a band with nothing in it
            lo, hi = measured_lo, measured_hi
        ideal = self._resp_ideal.get()

        # Behind everything: a level on its own axis, so it can never be
        # read off the gain scale by accident. Drawn first and z-ordered
        # under, since it is context for the curves rather than one of them.
        if spectra:
            self._twin = ax_mag.twinx()
            # Lifting the curve axes above the twin also hides its own
            # background, so the twin's content shows through -- both halves
            # of the usual matplotlib twinx dance, and both are needed.
            ax_mag.set_zorder(self._twin.get_zorder() + 1)
            ax_mag.patch.set_visible(False)
            for s in spectra:
                self._twin.fill_between(s["freqs"], db_min - 200.0, s["db"],
                                        color=_SPECTRUM_COLOR, alpha=0.07,
                                        lw=0)
                self._twin.plot(s["freqs"], s["db"], color=_SPECTRUM_COLOR,
                                lw=0.7, alpha=0.75,
                                label=f"{s['channel']} {s['direction']} (dBFS)")

        # Labelled once each, not once per curve: every design overlay is the
        # same black line and every floor the same dotted one, and a legend
        # that repeats them crowds out the curves it exists to name.
        named = set()

        def once(key, label):
            if key in named:
                return None
            named.add(key)
            return label

        for r in responses:
            color, ls = _curve_style(r)
            ax_mag.plot(r["freqs"], r["mag_db"], color=color, ls=ls, lw=1.2,
                        label=r["algorithm"])
            ax_ph.plot(r["freqs"], r["phase_deg"], color=color, ls=ls, lw=1.2,
                       label=r["algorithm"])
            # Only when some of it is actually on the axis: a floor 70 dB
            # below the bottom is good news, but as a legend entry pointing
            # at no visible line it just reads as a missing curve.
            if (self._resp_floor.get() and r["floor_db"].size
                    and float(np.max(r["floor_db"])) > db_min):
                ax_mag.plot(r["floor_freqs"], r["floor_db"], color=color,
                            ls=":", lw=0.9, alpha=0.7,
                            label=once("floor", "noise + distortion floor"))
            if ideal and r["sos"] is not None:
                from scipy import signal
                # Straight from the designed coefficients: a check on the
                # measurement, not on the filter. Black and thin so it reads
                # as an overlay rather than as another curve.
                w, h = signal.sosfreqz(r["sos"], worN=r["freqs"], fs=r["fs"])
                mag = 20.0 * np.log10(np.maximum(np.abs(h), 1e-30))
                ax_mag.plot(w, mag, color="k", lw=0.7, alpha=0.7,
                            label=once("design", "design (from sos)"))
                ax_ph.plot(w, np.degrees(np.unwrap(np.angle(h))), color="k",
                           lw=0.7, alpha=0.7)

        # The recording's own in->out, in the same dB as the curves above it.
        # Markers, not a smooth line: each point is one band of a five-second
        # record, and drawing it as a continuous curve would claim a
        # resolution the capture does not have.
        for g in gains:
            color = _GAIN_COLORS.get(g["channel"], "0.3")
            ax_mag.plot(g["freqs"], g["mag_db"], color=color, lw=1.1,
                        marker=".", ms=3.5, alpha=0.9,
                        label=f"{g['channel']} capture in→out")
            ax_ph.plot(g["freqs"], g["phase_deg"], color=color, lw=1.1,
                       marker=".", ms=3.5, alpha=0.9,
                       label=f"{g['channel']} capture in→out")

        tops = ([float(r["ref_db"]) for r in responses]
                + [float(np.max(g["mag_db"])) for g in gains])
        top = max(6.0, (max(tops) + 6.0) if tops else 6.0)
        for ax in (ax_mag, ax_ph):
            ax.set_xscale("log")
            ax.set_xlim(lo, hi)
            # Log x: minor gridlines are the decade's 2/3/4..., and without
            # them a Bode plot is unreadable between the decades.
            ax.grid(which="both", alpha=0.25)
            ax.grid(which="major", alpha=0.45)
            # Narrowed to less than a decade -- zoomed onto a notch, say --
            # the log formatter labels the ticks "5 x 10^1", which is a poor
            # way to write 50. Plain numbers below a decade.
            if hi < lo * 10.0:
                ax.xaxis.set_major_formatter(ScalarFormatter())
                ax.xaxis.set_minor_formatter(ScalarFormatter())
        ax_mag.set_ylim(db_min, top)
        ax_mag.set_ylabel("Amplitude (dB)")
        title = f"pipeline response — measured at {rate:g} Hz"
        if responses:
            title += f", {responses[0]['drive'] * 100:.0f}% FS drive"
            if abs(rate - self.info["rate"]) > 0.5:
                title += f"  (capture was {self.info['rate']:g} Hz)"
        ax_mag.set_title(title)

        handles, labels = ax_mag.get_legend_handles_labels()
        if self._twin is not None:
            # Same dB span as the left axis, anchored on the signal's own
            # peak. Different quantity, same scale: a 20 dB rolloff in the
            # signal then looks like a 20 dB rolloff in the filter, and the
            # gridlines line up instead of drawing a second set.
            peak = max(float(np.max(s["db"])) for s in spectra)
            self._twin.set_ylim(peak + 6.0 - (top - db_min), peak + 6.0)
            self._twin.set_ylabel("Capture input level (dBFS)", color="0.35")
            self._twin.tick_params(axis="y", colors="0.35", labelsize=8)
            self._twin.set_xscale("log")
            self._twin.set_xlim(lo, hi)
            h2, l2 = self._twin.get_legend_handles_labels()
            handles, labels = handles + h2, labels + l2
        ax_mag.legend(handles, labels, fontsize=8, ncol=2, framealpha=0.85)

        ax_ph.set_ylabel("Phase (deg)")
        ax_ph.set_xlabel("Frequency (Hz)")
        span = [float(np.ptp(r["phase_deg"])) for r in responses]
        if span and max(span) > 180.0:
            ax_ph.yaxis.set_major_locator(MultipleLocator(90))
        ax_ph.axhline(0.0, color="0.5", lw=0.6)

        self.fig.tight_layout()
        self.fig.canvas.draw_idle()

    def run(self):
        plt.show()
