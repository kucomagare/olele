# Live dual-channel matplotlib plot (TX vs RX, ch1/ch2). Matplotlib-only
# concerns live here -- no networking, no packet parsing.

import numpy as np
import matplotlib
matplotlib.use("TkAgg")
import matplotlib.pyplot as plt

from config import PLOT_MIN, PLOT_MAX
from packet_format import CH1_DTYPE, CH2_DTYPE


class DualPlot:
    def __init__(self, buffer_size):
        self.buffer_size = buffer_size

        plt.ion()
        self.fig, self.ax = plt.subplots(1, 1)
        plt.show(block=False)

        self.ch1_in  = np.zeros(buffer_size, dtype=CH1_DTYPE)
        self.ch2_in  = np.zeros(buffer_size, dtype=CH2_DTYPE)
        self.ch1_out = np.zeros(buffer_size, dtype=CH1_DTYPE)
        self.ch2_out = np.zeros(buffer_size, dtype=CH2_DTYPE)

        self.line_ch1_in,  = self.ax.plot(self.ch1_in,  color="blue",       label="ch1 in")
        self.line_ch2_in,  = self.ax.plot(self.ch2_in,  color="dodgerblue", label="ch2 in", linestyle="--")
        self.line_ch1_out, = self.ax.plot(self.ch1_out, color="red",        label="ch1 out")
        self.line_ch2_out, = self.ax.plot(self.ch2_out, color="orangered",  label="ch2 out", linestyle="--")

        self.ax.set_title("Streaming Input & Output (ch1/ch2)")
        self.ax.legend()
        self.ax.set_ylim(PLOT_MIN, PLOT_MAX)

    @staticmethod
    def _rolled(buf, values):
        n = len(values)
        if n == 0:
            return buf
        buf = np.roll(buf, -n)
        buf[-n:] = values
        return buf

    def update_input(self, ch1, ch2):
        self.ch1_in = self._rolled(self.ch1_in, ch1)
        self.ch2_in = self._rolled(self.ch2_in, ch2)
        self.line_ch1_in.set_ydata(self.ch1_in)
        self.line_ch2_in.set_ydata(self.ch2_in)

    def update_output(self, ch1, ch2):
        self.ch1_out = self._rolled(self.ch1_out, ch1)
        self.ch2_out = self._rolled(self.ch2_out, ch2)
        self.line_ch1_out.set_ydata(self.ch1_out)
        self.line_ch2_out.set_ydata(self.ch2_out)

    def refresh(self):
        self.fig.canvas.draw()
        self.fig.canvas.flush_events()
        plt.pause(0.001)
