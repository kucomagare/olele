# Shared window setup for the two Tk windows -- the live client (plot.py)
# and SAT (sat_gui.py). Its own module so sat_gui doesn't have to import
# plot/control_panel (which would drag net, numba and the live worker
# into a tool that needs none of it).

import tkinter as tk
from tkinter import ttk

import config


class ScrollFrame:
    """A frame that scrolls vertically when given less room than it
    wants, instead of losing whatever didn't fit.

    Tk's packer hands out space in packing order and never maps what's
    left over -- a sidebar taller than the window doesn't clip its last
    widget, it drops it entirely, silently. That's what made the Apply
    buttons look broken: they were below the fold and never drawn.

    Build into `.body`; place `.outer`. Requested height is capped at
    `max_req_height` so a tall body can't push its neighbours out of the
    window; actual height is whatever the geometry manager gives it.
    """

    _WHEEL = ("<MouseWheel>", "<Button-4>", "<Button-5>")

    def __init__(self, parent, max_req_height=300, padding=6):
        self.max_req_height = max_req_height
        self.outer = ttk.Frame(parent)
        self._canvas = tk.Canvas(self.outer, highlightthickness=0,
                                 borderwidth=0)
        bar = ttk.Scrollbar(self.outer, orient="vertical",
                            command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        self._canvas.pack(side="left", fill="both", expand=True)

        self.body = ttk.Frame(self._canvas, padding=padding)
        self._window = self._canvas.create_window((0, 0), window=self.body,
                                                  anchor="nw")
        self.body.bind("<Configure>", self._on_body)
        self._canvas.bind("<Configure>", self._on_canvas)
        # Wheel grabbed only while the pointer is over this frame, so the
        # figure canvas keeps its own scroll behaviour everywhere else.
        self._canvas.bind("<Enter>", self._grab_wheel)
        self._canvas.bind("<Leave>", self._release_wheel)

    def _on_body(self, _event=None):
        self._canvas.configure(
            scrollregion=self._canvas.bbox("all"),
            width=self.body.winfo_reqwidth(),
            height=min(self.body.winfo_reqheight(), self.max_req_height))

    def _on_canvas(self, event):
        # Keep the body spanning the visible width, so right-aligned
        # entries sit where they would with no canvas in between.
        self._canvas.itemconfigure(
            self._window, width=max(event.width, self.body.winfo_reqwidth()))

    def _grab_wheel(self, _event=None):
        for seq in self._WHEEL:
            self._canvas.bind_all(seq, self._on_wheel)

    def _release_wheel(self, _event=None):
        for seq in self._WHEEL:
            self._canvas.unbind_all(seq)

    def _on_wheel(self, event):
        if event.num == 4:
            step = -1
        elif event.num == 5:
            step = 1
        else:
            step = -1 if event.delta > 0 else 1
        self._canvas.yview_scroll(step, "units")


def size_window(window, width=None, height=None):
    """Set `window`'s starting size from config.WINDOW_W / WINDOW_H --
    fixed, read straight from config, no screen measuring. Starting size
    only; Tk leaves the window resizable afterward."""
    w = config.WINDOW_W if width is None else width
    h = config.WINDOW_H if height is None else height
    window.geometry(f"{int(w)}x{int(h)}")
