# Frequency-domain view of the plot buffers: pure math, no matplotlib/
# config -- exercisable from a REPL independent of the GUI.
#
# WHY: "does the filter actually attenuate this tone, and by how much" is
# not answerable from a scope trace. One rfft of the in window and one of
# the out window answers it directly, and turns the sine generators
# (signal_gen.py) from noise into a measurement tool: inject a tone, read
# the attenuation, step the frequency, plot the response curve.
#
# UNITS: magnitudes are dBFS -- relative to a full-scale sine of the wire
# dtype, so readings are comparable regardless of ECG_AMPLITUDE.

import numpy as np

# Hann windows keyed by length -- cheap to recompute, but this runs 4x
# per frame and lengths repeat, so it's free to just keep them.
_WINDOWS = {}

# Floor for the log: 20*log10(0) is -inf, which breaks autoscaling and
# leaves gaps in the line. Anything this far down is numerically zero anyway.
DB_FLOOR = -200.0


def _hann(n):
    w = _WINDOWS.get(n)
    if w is None:
        # periodic=False (symmetric) -- the textbook choice for spectrum
        # analysis of a finite record, vs. the periodic variant used for overlap-add.
        w = np.hanning(n)
        _WINDOWS[n] = w
    return w


def spectrum(samples, sample_rate, full_scale):
    """Return (freqs_hz, magnitude_dbfs) for one window of samples.

    `samples` is a wire-dtype array (big-endian unsigned); `full_scale`
    is that dtype's maximum. 0 dB is a sine spanning the whole of it.

    Returns two empty arrays when there is not enough data to transform,
    which the caller can hand straight to set_data() -- an empty line
    simply draws nothing, no special case needed.
    """
    n = len(samples)
    if n < 8 or sample_rate <= 0:
        empty = np.zeros(0)
        return empty, empty

    # float64 up front: the buffers are big-endian wire dtypes and the
    # mean subtraction below must not wrap.
    x = samples.astype(np.float64)

    # Remove DC -- the wire format centres the signal on half full-scale,
    # so without this the DC bin sits ~120dB above everything else and
    # flattens the display. It's also a format artefact, not signal content.
    x = x - x.mean()

    w = _hann(n)
    mag = np.abs(np.fft.rfft(x * w))

    # x2: a real sine splits its energy between the positive and negative
    # frequency. /sum(w): undoes the window's coherent gain. Without
    # both, an injected sine at level L reads as some other number and
    # the axis stops meaning anything absolute.
    mag *= 2.0 / w.sum()

    # Reference is HALF the dtype range, not the whole of it: a sine
    # spanning the format end to end has amplitude full_scale/2, and that
    # is what 0 dBFS has to mean. Referencing the full range would put
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
