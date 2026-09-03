# The analysis window: a Tk panel of controls around a static 2x2 figure.
#
# Deliberately shaped like the streaming client's window (plot.py +
# control_panel.py) so the two feel like the same tool: matplotlib's own
# TkAgg window, our widgets packed as siblings of its canvas, no second Tk
# root. What is different is that NOTHING here is live -- every control
# recomputes from a file and redraws once. There is no blitting, no animated
# artist and no frame rate, because there is no stream: that is the whole
# reason the spectrum lives here instead of in the client.
#
# The analysis itself is in sat.py. This file is presentation only; if a
# number looks wrong, it is wrong there.

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import matplotlib
matplotlib.use("TkAgg")
import matplotlib.pyplot as plt
import numpy as np

import guiutil
import sat

FFT_SIZES = (0, 128, 256, 512, 1024, 2048, 4096, 8192)
# Pipeline names only -- the implementation is its own dropdown, as in the
# client's Local tab. Built from the registry rather than written out, so a
# pipeline added in pipelines.py appears here with no edit.
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
    """0 is a real setting -- "the whole capture" -- but showing it as "0" in
    a dropdown of sample counts reads like a mistake."""
    return "capture" if not value else str(value)


def _size_value(label):
    try:
        return int(label)
    except ValueError:
        return 0


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
                 settle=200):
        self.log_dir = log_dir
        self.traces = None
        self.info = None

        self.fig, self.axes = plt.subplots(2, 2, figsize=(13, 7), squeeze=False)
        self.fig.canvas.manager.set_window_title("olele — buffer analysis")

        # Same Tk pack dance as the client's DualPlot: a side="bottom" slab
        # reserves the full width and a side="right" slab the full height, so
        # packing after the canvas has already claimed the window puts our
        # widgets in whatever sliver is left. Pop the canvas and toolbar out,
        # pack ours first, then restore theirs -- they land in the remaining
        # top-left area.
        window = self.fig.canvas.manager.window
        # Same size as the client's window, from the same config knobs.
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
        # Per channel, laid out the same way as the client's Local tab: a
        # pipeline dropdown and an implementation dropdown that greys out
        # when the pipeline is a fixed one. A capture has two channels that
        # may well have been produced by two different pipelines, so scoring
        # both against a single model could only ever be right for one.
        pipe0, impl0 = _split_model(model)
        pipe1, impl1 = _split_model(model_ch2 if model_ch2 is not None else model)
        self._pipe = [tk.StringVar(value=pipe0), tk.StringVar(value=pipe1)]
        self._impl = [tk.StringVar(value=impl0), tk.StringVar(value=impl1)]
        self._impl_combo = []
        self._shift = tk.StringVar(value="" if shift is None else str(shift))
        self._settle = tk.StringVar(value=str(settle))
        self._file = tk.StringVar()

        # What Defaults restores: the values this window opened with, which
        # with no CLI arguments are sat.py's own defaults. The dump file is
        # not in here -- that is what you are looking at, not a setting.
        self._initial = {id(v): v.get() for v in
                         (self._fft_size, self._fmax, self._db_min,
                          self._peak_fmin, self._shift, self._settle,
                          *self._pipe, *self._impl)}

        self._build_controls()
        self._build_report()

        self.reload_dumps(select=path)

    # ------------------------------------------------------------------
    # Widgets
    # ------------------------------------------------------------------
    def _sync_impl(self, ch):
        """Offer the implementation choice only where one exists.

        Same rule as the client's Local tab, deliberately: bypass and iir are
        single fixed things, so naming an implementation for them would claim
        a choice that was never made.
        """
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
        # Enter recomputes, as a shortcut for the Recompute button. No
        # <FocusOut>: recomputing merely because the cursor left a field is
        # the apply-on-every-change behaviour the button replaces, and here
        # it also redraws the whole figure.
        ent.bind("<Return>", lambda _e: self.refresh())
        if tip:
            _Tooltip(lbl, tip)
            _Tooltip(ent, tip)
        return row + 1

    def _build_controls(self):
        # Apply first and from the bottom, then a scrolling body for the
        # sections: packed the other way round, a controls column taller
        # than the window lost its last widget entirely -- Tk drops what no
        # longer fits rather than clipping it -- and the button that applies
        # everything simply was not drawn. See guiutil.ScrollFrame.
        recompute = ttk.Button(self.controls, text="Apply / Recompute",
                               command=self.refresh)
        recompute.pack(side="bottom", fill="x", pady=(6, 0))
        defaults = ttk.Button(self.controls, text="Defaults",
                              command=self.reset_defaults)
        defaults.pack(side="bottom", fill="x", pady=(6, 0))
        _Tooltip(defaults,
                 "Put every field back to what this window opened with "
                 "(sat.py's defaults, unless overridden on the command "
                 "line) and recompute. The loaded dump is kept.")
        _Tooltip(recompute,
                 "Apply every field above and redraw. Nothing typed or "
                 "selected here takes effect until this is pressed (Enter "
                 "in any field does the same). Choosing a different dump "
                 "loads it immediately -- that is picking what to look at, "
                 "not changing how it is analysed.")

        scroller = guiutil.ScrollFrame(self.controls, max_req_height=320,
                                       padding=0)
        scroller.outer.pack(side="top", fill="both", expand=True)
        c = scroller.body
        ttk.Label(c, text="Analysis", font=("", 10, "bold")).pack(anchor="w", pady=(0, 6))

        # --- file -----------------------------------------------------
        f = ttk.LabelFrame(c, text="Dump", padding=6)
        f.pack(fill="x", pady=(0, 8))
        self._file_box = ttk.Combobox(f, textvariable=self._file, width=24,
                                      state="readonly")
        self._file_box.grid(row=0, column=0, columnspan=2, sticky="ew", pady=2)
        self._file_box.bind("<<ComboboxSelected>>", lambda _e: self.load_selected())
        _Tooltip(self._file_box,
                 "Dumps written by the client's 'Log buffer' button, newest "
                 "first. Each one carries its own settings sidecar, which is "
                 "where the metadata below comes from.")
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
        f.columnconfigure(0, weight=1)
        f.columnconfigure(1, weight=1)

        # --- spectrum -------------------------------------------------
        s = ttk.LabelFrame(c, text="Spectrum", padding=6)
        s.pack(fill="x", pady=(0, 8))
        ttk.Label(s, text="Size").grid(row=0, column=0, sticky="w", pady=2)
        size_box = ttk.Combobox(s, textvariable=self._fft_size, width=8,
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
        row = self._entry(s, row, "F max (Hz)", self._fmax,
                          "Frequency axis limit. 0 = Nyquist.")
        row = self._entry(s, row, "dB min", self._db_min,
                          "Bottom of the magnitude axis, dBFS. 0 dB is a sine "
                          "spanning the full wire range.")
        row = self._entry(s, row, "Peak from (Hz)", self._peak_fmin,
                          "Ignore bins below this when locating the peak. "
                          "Raise it above the ECG band (say 40) to measure an "
                          "injected tone -- otherwise the ECG's harmonics win, "
                          "because they are genuinely stronger.")

        # --- model ----------------------------------------------------
        m = ttk.LabelFrame(c, text="Reference model", padding=6)
        m.pack(fill="x", pady=(0, 8))
        row = 0
        for ch in range(2):
            ttk.Label(m, text=f"Ch{ch + 1} pipe").grid(
                row=row, column=0, sticky="w", pady=2)
            pipe_box = ttk.Combobox(m, textvariable=self._pipe[ch], width=8,
                                    state="readonly", values=list(_pipes()))
            pipe_box.grid(row=row, column=1, sticky="e", pady=2)
            # Retargets the implementation dropdown only -- showing what
            # is available is not the same as recomputing against it.
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

            ttk.Label(m, text=f"Ch{ch + 1} impl").grid(
                row=row, column=0, sticky="w", pady=(0, 6))
            impl_box = ttk.Combobox(m, textvariable=self._impl[ch], width=8,
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
        row = self._entry(m, row, "Shift", self._shift,
                          "Blank = whatever the board's register actually "
                          "held, read from the sidecar. Set it to try a "
                          "different one against the same capture.")
        row = self._entry(m, row, "Settle", self._settle,
                          "Samples skipped before scoring. The model starts "
                          "from zeroed state and the board did not, so the "
                          "first samples disagree for a reason that says "
                          "nothing about the algorithm.")

    def reset_defaults(self):
        """Every analysis field back to its startup value, then redraw."""
        for var in (self._fft_size, self._fmax, self._db_min, self._peak_fmin,
                    self._shift, self._settle, *self._pipe, *self._impl):
            var.set(self._initial[id(var)])
        for ch in range(2):
            self._sync_impl(ch)
        self.refresh()

    def _build_report(self):
        # A Text rather than a Label so the numbers can be selected and
        # pasted into a note -- which is most of what they are for.
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
            # A malformed or truncated dump must not take the window down --
            # the whole point is to be able to open one and look at it.
            self.traces = self.info = None
            self._say(f"Could not read {path.name}:\n\n{exc}")
            return
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
        # Show what was actually used, so a blank field stops being a mystery
        # once a sidecar has answered it.
        self._shift.set(str(shift))

        full_scale = sat.wire_full_scale(self.traces, self.info)
        results, curves = [], {}
        for ch in ("ch1", "ch2"):
            if f"{ch}_in" not in self.traces and f"{ch}_out" not in self.traces:
                continue
            r, c = sat.analyse_channel(self.traces, ch, self.info["rate"],
                                            full_scale, size, peak_fmin)
            results.append(r)
            curves[ch] = c

        # Per channel: each is scored against its own selection, or skipped.
        models = []
        for i, ch in enumerate(("ch1", "ch2")):
            model = self._model_for(i)
            if model is None:
                continue
            models.append(sat.compare_model(self.traces, ch, model, shift,
                                            settle, self.info["rate"]))

        self._say(sat.format_report(self.info, results, models))
        self._draw(curves, models, fmax, db_min)

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

        for row in range(2):
            for col in range(2):
                self.axes[row][col].clear()
                self.axes[row][col].set_visible(row < len(channels))

        for row, ch in enumerate(channels):
            ax_t, ax_f = self.axes[row]
            for direction, color in (("in", "tab:blue"), ("out", "tab:red")):
                key = f"{ch}_{direction}"
                if key in self.traces:
                    ax_t.plot(t, self.traces[key], color=color, lw=0.9, label=direction)
            m = model_by_ch.get(ch)
            if m is not None:
                # Dashed over the recorded output: where they separate is the
                # point, and dashes let the solid line show through wherever
                # they agree.
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

    def run(self):
        plt.show()
