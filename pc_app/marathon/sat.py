#!/usr/bin/env python3
"""SAT -- Static Analysis Tool. Offline analysis of a logged buffer: spectra,
and the hardware against a model.

    ./sat.py                         # open the window on the newest dump
    ./sat.py FILE.csv                # ...on a particular one
    ./sat.py --list                  # what dumps exist
    ./sat.py --no-plot               # numbers only, for a terminal or a pipe
    ./sat.py --no-plot --model iir --peak-fmin 40

Every flag is also a control in the window (sat_gui.py); flags remain for
scripting and headless boxes.

Separate from the live client: a rolling-buffer FFT there would cost every
frame forever for a measurement that isn't actually live -- easier on a
file, where the same capture can be examined many ways.

Reads the pair the plot bar's "Log buffer" button writes:
    plotdump_<stamp>.csv            index, time_s, and the four traces
    plot_config_data_<stamp>.txt    every setting, board registers, metrics
The sidecar is the ground truth for the samples next to it.
"""

import argparse
import re
import sys
from pathlib import Path

import numpy as np

# Run from anywhere: sibling modules (spectrum, pipelines) live next to
# this file, not necessarily in the working directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import spectrum

TRACES = ("ch1_in", "ch1_out", "ch2_in", "ch2_out")
DEFAULT_LOGS = Path(__file__).resolve().parent / "build" / "logs"


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def find_dumps(log_dir):
    return sorted(Path(log_dir).glob("plotdump_*.csv"))


def sidecar_for(csv_path):
    """Settings snapshot written alongside a dump, by naming convention."""
    name = csv_path.name.replace("plotdump_", "plot_config_data_")
    return csv_path.with_name(name).with_suffix(".txt")


def read_sidecar(path):
    """Parse `key = value` lines into a dict, section heading as prefix
    (so board-register "shift" can't collide with a config knob)."""
    out = {}
    if not path.exists():
        return out
    section = ""
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            continue
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()
        out[key] = val
        out[f"{section}::{key}"] = val
    return out


def load_dump(csv_path):
    """Return (traces dict, metadata dict). Metadata from the sidecar
    first, CSV `#` header lines second, so a lost sidecar still analyses."""
    meta = read_sidecar(sidecar_for(csv_path))

    # Parsed by hand, not genfromtxt: its comment handling blanks the `#`
    # lines before names=True finds the header, so it reads column names
    # off the wrong line and rejects every data row.
    header_meta, columns, rows = {}, None, []
    with open(csv_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            if line.startswith("#"):
                for k, v in re.findall(r"(\w+)=([^\s]+)", line):
                    header_meta.setdefault(k, v)
                continue
            fields = line.split(",")
            if columns is None:
                columns = fields
                continue
            rows.append(fields)

    # ValueError, not SystemExit: the GUI's Exception handler would let a
    # BaseException-derived SystemExit through and take the window down.
    if columns is None or not rows:
        raise ValueError(f"{csv_path}: no data rows")

    table = np.array(rows, dtype=np.float64)
    traces = {name: table[:, i] for i, name in enumerate(columns)
              if name in TRACES}
    if not traces:
        raise ValueError(f"{csv_path}: no recognised trace columns "
                         f"(expected {', '.join(TRACES)})")

    def number(*keys, default=None):
        for k in keys:
            for src in (meta, header_meta):
                if k in src:
                    try:
                        return float(src[k])
                    except ValueError:
                        pass
        return default

    info = {
        "path": csv_path,
        "samples": len(next(iter(traces.values()))),
        "rate": number("ECG_SAMPLING_RATE", "ecg_rate", default=2048.0),
        "shift": number("board filter registers (read back from fabric)::shift",
                        default=None),
        "heart_rate": number("ECG_HEART_RATE", "hr"),
        "amplitude": number("ECG_AMPLITUDE", "amplitude"),
        "send_rate": number("SEND_RATE", "send_rate"),
        "chunk": number("CHUNK_SIZE", "chunk"),
        "trigger": meta.get("PLOT_TRIGGER", header_meta.get("trigger")),
        # 4 generators x 2 channels; a pre-per-channel dump has none of
        # these keys and the list comes out empty.
        "sines": [(n, ch, meta.get(f"ECG_SINE{n}_CH{ch}_ENABLED"),
                   meta.get(f"ECG_SINE{n}_CH{ch}_FREQ"),
                   meta.get(f"ECG_SINE{n}_CH{ch}_PHASE"),
                   meta.get(f"ECG_SINE{n}_CH{ch}_LEVEL"))
                  for n in range(1, 5) for ch in (1, 2)],
        "meta": meta,
    }
    return traces, info


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

def wire_full_scale(traces, info=None):
    """Full-scale value of the wire format the dump was captured in.
    Prefers the sidecar's `wire_dtype`; inferring from sample values is only
    right if the capture happens to hit the top of its range (a quiet
    32-bit capture under 65535 would misread as 16-bit), so that's the
    fallback only."""
    if info:
        dtype = (info.get("meta", {}) or {}).get("wire_dtype")
        if dtype:
            try:
                return float(np.iinfo(np.dtype(dtype)).max)
            except (TypeError, ValueError):
                pass                       # unrecognised: fall through
    peak = max(float(np.max(v)) for v in traces.values())
    return 2.0 ** 32 - 1 if peak > 65535 else 65535.0


def analyse_channel(traces, ch, rate, full_scale, size, peak_fmin):
    """Spectra of one channel's in/out, plus the peak comparison between
    them. Returns (result dict, {label: (freqs, db)})."""
    curves, result = {}, {"channel": ch}
    win = {}
    for direction in ("in", "out"):
        key = f"{ch}_{direction}"
        if key not in traces:
            continue
        samples = traces[key][-size:] if size else traces[key]
        win[direction] = samples
        curves[direction] = spectrum.spectrum(samples, rate, full_scale)
    if "in" not in curves and "out" not in curves:
        return None, curves

    result["n"] = len(next(iter(win.values())))
    result["resolution"] = rate / result["n"]

    # Locate on the input when present, read both there -- a working filter
    # moves the output peak, so independently-located peaks would compare
    # two different frequencies.
    ref = "in" if "in" in curves else "out"
    f, db = curves[ref]
    i = spectrum.peak(f, db, peak_fmin)
    if i is None:
        return result, curves
    result["peak_hz"] = float(f[i])
    for direction, (_f, _db) in curves.items():
        if i < _db.size:
            result[f"{direction}_dbfs"] = float(_db[i])
    if "in_dbfs" in result and "out_dbfs" in result:
        result["delta_db"] = result["out_dbfs"] - result["in_dbfs"]
    return result, curves


def model_choices():
    """Every pipeline, and every pipeline:implementation pair, built from
    pipelines.py so a new pipeline is scorable immediately. Import deferred
    -- the filters pull in numba, not worth it without --model."""
    import pipelines
    out = []
    for pipe in sorted(pipelines.PIPELINES):
        impls = pipelines.implementations(pipe)
        if impls:
            out.extend(f"{pipe}:{impl}" for impl in impls)
        else:
            # bypass/iir have nothing to quantise -- no fake :impl choice.
            out.append(pipe)
    return out


_PIPE2_KEYS = ("PIPE2_HP_HZ", "PIPE2_NOTCH_HZ", "PIPE2_NOTCH_Q",
               "PIPE2_LP_HZ")


def _model_params(shift, fs, meta=None):
    """What a pipeline is handed for an offline run. Corner frequencies come
    from the capture's own sidecar when present (like fs) -- scoring against
    this machine's live config would compare against a filter the recording
    never saw. Missing keys (older dumps) fall back to pipeline defaults."""
    params = {"shift": shift, "fs": float(fs)}
    for key in _PIPE2_KEYS:
        value = (meta or {}).get(key)
        if value is None:
            continue
        try:
            params[f"pipe2_{key[len('PIPE2_'):].lower()}"] = float(value)
        except (TypeError, ValueError):
            pass
    return params


def compare_model(traces, ch, algorithm, shift, settle, fs, meta=None):
    """Run a pipelines.py algorithm on the recorded input, score it against
    the recorded output -- did the hardware compute what I think it did.
    `settle` samples are skipped: the model starts from zeroed state, the
    board didn't, so early samples disagree for a reason unrelated to the
    algorithm."""
    import pipelines
    src, ref = f"{ch}_in", f"{ch}_out"
    if src not in traces or ref not in traces:
        return None

    dtype = ">u4"
    x = traces[src].astype(np.uint32).astype(dtype)
    # "pipe" or "pipe:impl" -- same resolution the live app uses, so a
    # capture scored here matches the same live selection.
    pipe, _, impl = algorithm.partition(":")
    fn = pipelines.resolve(pipe, impl or pipelines.DEFAULT_IMPL)
    if fn is None:
        return None
    # fs from the capture's own sidecar, not this machine's live config --
    # a dump analysed months later filters at the rate it was recorded.
    modelled = fn(x, pipelines.new_state(), _model_params(shift, fs, meta))
    modelled = modelled.astype(np.float64)

    a = modelled[settle:]
    b = traces[ref][settle:]
    n = min(len(a), len(b))
    if n < 2:
        return None
    a, b = a[:n], b[:n]

    err = a - b
    span = max(float(np.ptp(b)), 1.0)
    corr = float(np.corrcoef(a, b)[0, 1]) if np.std(a) and np.std(b) else float("nan")
    return {
        "channel": ch, "algorithm": algorithm, "shift": shift, "compared": n,
        "max_abs": float(np.max(np.abs(err))),
        "mean_abs": float(np.mean(np.abs(err))),
        "mean_err": float(np.mean(err)),      # signed: the truncation bias
        "pct_of_span": float(np.max(np.abs(err))) / span * 100.0,
        "corr": corr,
        "modelled": modelled,
    }


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def format_report(info, results, models):
    """The report as text -- returns rather than prints so the GUI can
    reuse it verbatim in its panel."""
    out = []
    say = out.append

    p = info["path"]
    say(f"{p.name}   {info['samples']} samples @ {info['rate']:g} Hz")
    bits = []
    for label, key, fmt in (("heart rate", "heart_rate", "{:.0f} bpm"),
                            ("amplitude", "amplitude", "{:.2f}"),
                            ("send rate", "send_rate", "{:.0f} pkt/s"),
                            ("chunk", "chunk", "{:.0f}")):
        if info.get(key) is not None:
            bits.append(f"{label} {fmt.format(info[key])}")
    if info.get("shift") is not None:
        bits.append(f"board shift {info['shift']:.0f}")
    if info.get("trigger") is not None:
        bits.append(f"trigger {info['trigger']}")
    if bits:
        say("  " + ", ".join(bits))
    for n, ch, en, freq, phase, level in info["sines"]:
        if en and en.lower() == "true":
            say(f"  sine {n} ch{ch}: {freq} Hz at level {level}, "
                f"phase {phase} deg")

    say("")
    say("  spectrum")
    for r in results:
        if not r or "peak_hz" not in r:
            say(f"    {r['channel'] if r else '?':8} no peak found")
            continue
        line = (f"    {r['channel']:8} N={r['n']:<6} {r['resolution']:.2f} Hz/bin"
                f"   peak {r['peak_hz']:8.2f} Hz")
        if "in_dbfs" in r:
            line += f"   in {r['in_dbfs']:7.2f}"
        if "out_dbfs" in r:
            line += f"   out {r['out_dbfs']:7.2f}"
        if "delta_db" in r:
            line += f"   delta {r['delta_db']:7.2f} dB"
        say(line)

    if any(models):
        say("")
        say("  model vs recorded output")
        for m in models:
            if not m:
                continue
            say(f"    {m['channel']:8} {m['algorithm']} shift={m['shift']}"
                f"   n={m['compared']}")
            say(f"             max |err| {m['max_abs']:.1f} counts "
                f"({m['pct_of_span']:.4f}% of span),"
                f" mean |err| {m['mean_abs']:.1f}")
            say(f"             mean signed err {m['mean_err']:+.1f} counts"
                f"   correlation {m['corr']:.6f}")
    return "\n".join(out)


def print_report(info, results, models):
    print()
    print(format_report(info, results, models))
    print()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", nargs="?", help="dump CSV (default: the newest)")
    ap.add_argument("--log-dir", default=str(DEFAULT_LOGS))
    ap.add_argument("--list", action="store_true", help="list dumps and exit")
    ap.add_argument("--fft-size", type=int, default=0,
                    help="samples to transform, from the newest end "
                         "(0 = the whole capture). Truncates, never zero-pads")
    ap.add_argument("--fmax", type=float, default=0.0,
                    help="frequency axis limit, Hz (0 = Nyquist)")
    ap.add_argument("--db-min", type=float, default=-120.0)
    ap.add_argument("--peak-fmin", type=float, default=1.0,
                    help="ignore bins below this when locating the peak")
    ap.add_argument("--model", choices=model_choices(),
                    help="also run this pipeline on the recorded "
                         "input and score it against the recorded output. "
                         "bypass and iir take no implementation; design "
                         "pipelines are named pipe:impl (e.g. pipe1:scipy). "
                         "Applies to both channels unless overridden below")
    ap.add_argument("--model-ch1", choices=model_choices(),
                    help="score ch1 against this instead of --model. The two "
                         "channels can have been produced by different "
                         "pipelines, so they can be scored separately")
    ap.add_argument("--model-ch2", choices=model_choices(),
                    help="score ch2 against this instead of --model")
    ap.add_argument("--shift", type=int,
                    help="shift for --model (default: the board's, from the sidecar)")
    ap.add_argument("--settle", type=int, default=200,
                    help="samples to skip before scoring the model, since it "
                         "starts from zeroed state and the board did not")
    ap.add_argument("--no-plot", action="store_true",
                    help="print the report and exit instead of opening the "
                         "window -- for a terminal, a pipe, or a headless box")
    args = ap.parse_args(argv)

    dumps = find_dumps(args.log_dir)
    if args.list:
        if not dumps:
            print(f"no dumps in {args.log_dir}")
        for d in dumps:
            print(d)
        return 0
    if args.file:
        path = Path(args.file)
    elif dumps:
        path = dumps[-1]
    else:
        print(f"no dumps in {args.log_dir} -- press 'Log buffer' in the client "
              f"first, or pass a file", file=sys.stderr)
        return 1
    if not path.exists():
        print(f"{path}: not found", file=sys.stderr)
        return 1

    if not args.no_plot:
        # The window is the normal path -- every flag below is also a
        # control in it. CLI stays for scripting/headless boxes.
        import sat_gui
        sat_gui.SATWindow(
            log_dir=Path(args.log_dir), path=path, fft_size=args.fft_size,
            fmax=args.fmax, db_min=args.db_min, peak_fmin=args.peak_fmin,
            model=args.model_ch1 or args.model,
            model_ch2=args.model_ch2 or args.model,
            shift=args.shift, settle=args.settle).run()
        return 0

    try:
        traces, info = load_dump(path)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    full_scale = wire_full_scale(traces, info)

    results, curves_by_ch = [], {}
    for ch in ("ch1", "ch2"):
        if f"{ch}_in" not in traces and f"{ch}_out" not in traces:
            continue
        r, curves = analyse_channel(traces, ch, info["rate"], full_scale,
                                     args.fft_size, args.peak_fmin)
        results.append(r)
        curves_by_ch[ch] = curves

    # Per channel -- each channel may have run a different pipeline.
    selection = {"ch1": args.model_ch1 or args.model,
                 "ch2": args.model_ch2 or args.model}
    models = []
    if any(selection.values()):
        shift = args.shift
        if shift is None:
            shift = int(info["shift"]) if info.get("shift") is not None else 4
        for ch in ("ch1", "ch2"):
            if selection[ch]:
                models.append(compare_model(traces, ch, selection[ch], shift,
                                            args.settle, info["rate"],
                                            info.get("meta")))

    print_report(info, results, models)
    return 0


if __name__ == "__main__":
    sys.exit(main())
