# Frequency-domain view of the plot buffers: pure math, no matplotlib and no
# config, so it can be exercised from a REPL or a test independent of the GUI.
#
# WHY THIS EXISTS. The rig can already inject a known sine (signal_gen.py's
# ECG_SINE1/2, e.g. 50 Hz mains hum) and low-pass it in fabric
# (axi_tdm_filter.vhd, alpha = 1/2**SHIFT). "Does the filter actually
# attenuate that tone, and by how much" is not answerable from a scope trace
# -- the trace shows the sum of everything. One rfft of the in window and one
# of the out window answers it directly, and turns the sine generators from a
# way to make the plot look messy into a measurement instrument: inject a
# tone, read the attenuation, step the frequency, and you have plotted the
# filter's response curve using only parts that already exist.
#
# UNITS. Magnitudes are dBFS -- decibels relative to a full-scale sine of the
# wire dtype. 0 dB is the largest sine the format can carry, so every value
# is negative and the numbers mean the same thing regardless of
# ECG_AMPLITUDE. What matters for a filter measurement is the DIFFERENCE
# between the in and out curves at one frequency, and a shared reference is
# what makes that difference readable straight off the screen.

import numpy as np

# Hann windows keyed by length. Recomputing one is cheap (a few
# microseconds), but this runs four times per frame and the lengths repeat
# forever, so it is free to just keep them.
_WINDOWS = {}

# Floor for the log: 20*log10(0) is -inf, which breaks autoscaling and leaves
# gaps in the line. Anything this far down is numerically zero anyway.
DB_FLOOR = -200.0


def _hann(n):
    w = _WINDOWS.get(n)
    if w is None:
        # periodic=False (symmetric) -- the textbook choice for spectrum
        # analysis of a finite record, as opposed to the periodic variant
        # used for overlap-add synthesis.
        w = np.hanning(n)
        _WINDOWS[n] = w
    return w


def spectrum(samples, sample_rate, full_scale):
    """Return (freqs_hz, magnitude_dbfs) for one window of samples.

    `samples` is a wire-dtype array (big-endian unsigned); `full_scale` is
    that dtype's maximum. 0 dB is a sine spanning the whole of it.

    Returns two empty arrays when there is not enough data to transform,
    which the caller can hand straight to set_data() -- an empty line simply
    draws nothing, no special case needed.
    """
    n = len(samples)
    if n < 8 or sample_rate <= 0:
        empty = np.zeros(0)
        return empty, empty

    # float64 up front: the buffers are big-endian wire dtypes and the mean
    # subtraction below must not wrap.
    x = samples.astype(np.float64)

    # Remove DC. The wire format centres the signal on half full-scale, so
    # without this the DC bin sits ~120 dB above everything else and
    # flattens the entire display against the top of the axis. It is also
    # the honest thing to do: that offset is a format artefact
    # (ECG_OFFSET rides on top of it), not signal content.
    x = x - x.mean()

    w = _hann(n)
    mag = np.abs(np.fft.rfft(x * w))

    # Scale to the amplitude of the sine that produced each bin: x2 because
    # a real sine splits its energy between the positive and negative
    # frequency, and /sum(w) to undo the window's coherent gain. Without
    # both, an injected sine at level L reads as some other number and the
    # axis stops meaning anything absolute.
    mag *= 2.0 / w.sum()

    # Reference is HALF the dtype range, not the whole of it: a sine that
    # spans the format end to end has amplitude full_scale/2, and that is
    # what 0 dBFS has to mean. Referencing the full range instead puts
    # every reading exactly 6.02 dB low.
    db = 20.0 * np.log10(np.maximum(mag, 1e-30) / (full_scale / 2.0))
    np.maximum(db, DB_FLOOR, out=db)

    freqs = np.fft.rfftfreq(n, d=1.0 / sample_rate)
    return freqs, db


def peak(freqs, db, fmin=1.0):
    """Index of the strongest bin at or above `fmin` Hz, or None.

    `fmin` skips the lowest bins: baseline wander and the residue of the
    mean subtraction live there and would otherwise win every time,
    reporting "the peak is at 0.5 Hz" no matter what tone was injected.
    """
    if freqs.size == 0:
        return None
    usable = np.flatnonzero(freqs >= fmin)
    if usable.size == 0:
        return None
    return int(usable[0] + np.argmax(db[usable]))
