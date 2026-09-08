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

import inspect

import numpy as np
import scipy.signal.windows as _sp_windows

# Windows keyed by (name, length) -- cheap to recompute, but this runs
# several times per frame and (name, length) pairs repeat, so it's free
# to just keep them.
_WINDOWS = {}

# Floor for the log: 20*log10(0) is -inf, which breaks autoscaling and
# leaves gaps in the line. Anything this far down is numerically zero anyway.
DB_FLOOR = -200.0


def _discover_window_names():
    """Every scipy.signal.windows function callable as just fn(length) --
    no shape parameter (beta, alpha, std, attenuation, ...) with no
    default. Those (kaiser, gaussian, chebwin, general_hamming,
    general_cosine, general_gaussian, dpss) need a second control this
    dropdown doesn't have, so they're left out rather than half-wired.
    boxcar is left out too -- it's the all-ones window, already offered
    as the explicit "none" choice.
    """
    names = []
    for name in sorted(_sp_windows.__all__):
        if name in ("get_window", "boxcar"):
            continue
        fn = getattr(_sp_windows, name)
        try:
            params = inspect.signature(fn).parameters
        except (TypeError, ValueError):
            continue
        extra_required = [p for p in params.values()
                          if p.default is inspect.Parameter.empty
                          and p.name not in ("M", "sym")]
        if not extra_required:
            names.append(name)
    return tuple(names)


# "none" first -- it's the no-op/rectangular choice and belongs at the
# top of the dropdown, not alphabetized in among the scipy names.
WINDOW_CHOICES = ("none",) + _discover_window_names()


def _window(name, n):
    key = (name, n)
    w = _WINDOWS.get(key)
    if w is None:
        # sym=True (every listed function's default) -- the textbook
        # choice for spectrum analysis of a finite record, vs. the
        # periodic variant used for overlap-add.
        w = np.ones(n) if name == "none" else getattr(_sp_windows, name)(n)
        _WINDOWS[key] = w
    return w


def spectrum(samples, sample_rate, full_scale, window="hann"):
    """Return (freqs_hz, magnitude_dbfs) for one window of samples.

    `samples` is a wire-dtype array (big-endian unsigned); `full_scale`
    is that dtype's maximum. 0 dB is a sine spanning the whole of it.
    `window` is a name from WINDOW_CHOICES.

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

    w = _window(window, n)
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


def window_starts(n_total, size, hop):
    """Start indices for successive `size`-sample windows across
    `n_total` samples, hopping by `hop`, starting at 0. A trailing
    partial window that would run past the end is dropped -- same rule
    a single size-only window already follows, just repeated. Falls back
    to a single window at 0 whenever there is nothing to hop over.
    """
    if size <= 0 or size >= n_total or hop <= 0:
        return [0]
    return list(range(0, n_total - size + 1, hop))


def spectra(samples, sample_rate, full_scale, size=0, hop=0, window="hann"):
    """One or more (start_index, freqs, db) spectra of `samples`.

    size=0 -- the whole array is one window; hop is irrelevant.
    hop=0 -- size is a single window taken from the *tail* (the
    newest samples) -- the plain "Size" field's original meaning.
    hop>0 -- successive size-sample windows starting at 0 and hopping
    by hop, covering the array forward (see window_starts). This is
    "Shift" is already the board's fixed-point bit-shift register
    elsewhere in this app, so this parameter is deliberately never
    named that here.
    """
    n_total = len(samples)
    if size <= 0 or size >= n_total:
        freqs, db = spectrum(samples, sample_rate, full_scale, window)
        return [(0, freqs, db)]
    if hop <= 0:
        start = n_total - size
        freqs, db = spectrum(samples[start:], sample_rate, full_scale, window)
        return [(start, freqs, db)]
    return [(s, *spectrum(samples[s:s + size], sample_rate, full_scale, window))
            for s in window_starts(n_total, size, hop)]


def average_spectrum(curves):
    """Combine several (freqs, db) curves of matching length into one, by
    averaging power -- Welch's method. Averaging the dB values directly
    is a common mistake: it's a mean in the log domain, biased low
    relative to the true average power, and doesn't correspond to any
    physical average of the underlying signal.
    """
    freqs = curves[0][0]
    rel_mag = np.array([10.0 ** (db / 20.0) for _, db in curves])
    power = np.mean(rel_mag ** 2, axis=0)
    db = 20.0 * np.log10(np.maximum(np.sqrt(power), 1e-30))
    np.maximum(db, DB_FLOOR, out=db)
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
