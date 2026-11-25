# Copyright (C) 2026 Analog Devices, Inc.
#
# SPDX short identifier: ADIBSD

"""Quad ADA4356 — TDD-synchronized capture, signal quality, skew, CSV export.

A sinewave on J11 (distributed to all four ADC inputs) is used to measure the
signal quality of each channel and the phase offset of B/C/D relative to A.
Each run writes, under a run number N that never reuses an existing file:

  ada4356_quadN.png        TDD-synchronized overlay + stacked traces
  ada4356_quadN_thd.png    distortion spectra, one panel per channel
  ada4356_quadN_diag.png   residual autocorrelation
  ada4356_quadN_gate.png   one TDD gate pulse above the full capture
  ada4356_quadN.txt        copy of everything printed to the console
  ada4356_quadN_samples.csv, ada4356_quadN_metrics.csv   with --csv only

Every code-domain result is also reported in volts, and the front end of all
four channels is read back and printed before the analysis, so a channel at a
different gain than the one asked for is visible in the console and the log.

Front-end note: the ADA4356 is a photodiode front end.  Its quiescent code sits
near the top of the ADC range, so the generator needs a DC offset of roughly
half its amplitude or the sine clips against +8191 regardless of gain.  At
GSEL=00 (133k) the working point is about 1.0 Vpp with +0.5 V offset.

Positional arguments (all optional, in this order):
  uri       IIO context, default ip:192.168.2.1
  samples   record length, also the TDD gate length, default 65536
  tone      tone frequency on J11 in Hz, default 1000000
  gain      transimpedance on all four channels: 133k, 11k or 4k54; omit to
            leave it as the driver set it at probe.  Lowering it raises the DC
            pedestal toward the rail, so re-check the max column against 8191.
  filter    FREQ_SEL filter, named after the pin level: fsel1 (default, the
            narrower filter, worth 1.3 ENOB) or fsel0 (for characterising it)

Options (each defaults to the environment variable shown, so the old
environment-variable usage still works):
  --outdir DIR      ADA4356_OUTDIR    output folder, default
                    <system temp>/ada4356_quad_tddn_example_outputs
  --duration TIME   ADA4356_DURATION  time window the figures span ("500us");
                    the part past the end of the record is drawn as no data
  --periods P       ADA4356_PERIODS   tone periods in the time-domain panels of
                    ada4356_quadN.png, default 5; the gate figure always shows
                    the complete record
  --frame F         ADA4356_FRAME     TDD frame: unset = 1 s; "buffer" sizes the
                    frame and its ON window to the capture; or a cycle count
                    ("125000") or a time ("500us", "2ms", "1s")
  --align MODE      ADA4356_ALIGN     boot-align the channels on the tone before
                    the capture, using ada4356_quad_boot_align.py from the same
                    folder: off (default), shift (whole-sample software shift,
                    data not filtered), trim (AD9517 clock trim + whole shift;
                    the clock delays stay applied until reboot), interp (whole
                    shift + sub-sample FIR interpolation)
  --csv             ADA4356_CSV=1     write the sample and metrics CSVs (off by
                    default: one row per sample is the slowest step of a run)
  --no-show         ADA4356_NO_SHOW=1 do not pop the figures up on screen

Usage:
  python3 ada4356_quad_tddn_example.py ip:192.168.2.1
  python3 ada4356_quad_tddn_example.py ip:192.168.2.1 65536
  python3 ada4356_quad_tddn_example.py ip:192.168.2.1 65536 1000000
  python3 ada4356_quad_tddn_example.py ip:192.168.2.1 65536 100000 11k
  python3 ada4356_quad_tddn_example.py ip:192.168.2.1 65536 100000 11k fsel0
  python3 ada4356_quad_tddn_example.py ip:192.168.2.1 65536 100000 --align shift --csv
"""

import argparse
import csv
import glob
import os
import re
import sys
import tempfile
import time
import types

import matplotlib.pyplot as plt
import numpy as np

import adi

FRONT_END_CONTROLS = (
    ("gain_mode", "gain", ("133k", "11k", "4k54")),
    ("filter_mode", "filter", ("fsel0", "fsel1")),
)
TDD_CLOCK_HZ = 125_000_000
TDD_ON_BASE = 100
FULL_SCALE = 8192.0  # 14-bit signed, +-8192 codes = +-1 V
VREF_V = 1.0  # volts at full scale
LSB_V = VREF_V / FULL_SCALE  # volts per ADC code
N_HARMONICS = 9  # harmonics 2..9 are fitted and removed from the noise estimate
MIN_SAMPLES = 256  # below this the FFT and harmonic fit have nothing to work on
MAX_LAG = 64
STRUCTURAL_LAGS = (1, 2, 4, 8, 16)
SMOOTH_ACF1 = 0.5
ALIGN_MODES = ("off", "shift", "trim", "interp")
FRAME_FIT = ("buffer", "capture", "fit")
CSV_ON = ("1", "true", "yes", "on", "both", "all", "samples", "metrics")
NO_SHOW_ON = ("1", "true", "True")
LABELS = ["A", "B", "C", "D"]
COLORS = ["tab:blue", "tab:orange", "tab:green", "tab:red"]
BASENAME = "ada4356_quad"
OUTDIR_NAME = "ada4356_quad_tddn_example_outputs"


class _Tee:
    """Mirror everything printed to the console into the run log file."""

    def __init__(self, stream, fh):
        self._stream = stream
        self._fh = fh

    def write(self, text):
        self._stream.write(text)
        self._fh.write(text)
        return len(text)

    def flush(self):
        self._stream.flush()
        self._fh.flush()

    def isatty(self):
        return getattr(self._stream, "isatty", lambda: False)()


def parse_args():
    env = os.environ.get
    ap = argparse.ArgumentParser(
        description=__doc__.split("\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("uri", nargs="?", default="ip:192.168.2.1")
    ap.add_argument(
        "samples", nargs="?", type=int, default=65536, help="record = gate length"
    )
    ap.add_argument(
        "tone", nargs="?", type=float, default=1_000_000.0, help="tone on J11, Hz"
    )
    ap.add_argument("gain", nargs="?", default=None, help="133k, 11k or 4k54")
    ap.add_argument("filter", nargs="?", default=None, help="fsel0 or fsel1")
    ap.add_argument(
        "--outdir",
        default=env("ADA4356_OUTDIR", os.path.join(tempfile.gettempdir(), OUTDIR_NAME)),
        help="output folder (ADA4356_OUTDIR)",
    )
    ap.add_argument(
        "--duration",
        default=env("ADA4356_DURATION", ""),
        help="figure time window, e.g. 500us (ADA4356_DURATION)",
    )
    ap.add_argument(
        "--periods",
        default=env("ADA4356_PERIODS", "5"),
        help="tone periods in the time-domain panels (ADA4356_PERIODS)",
    )
    ap.add_argument(
        "--frame",
        default=env("ADA4356_FRAME", ""),
        help="TDD frame: buffer, cycles or a time (ADA4356_FRAME)",
    )
    ap.add_argument(
        "--align",
        default=env("ADA4356_ALIGN", "off"),
        help="off, shift, trim or interp (ADA4356_ALIGN)",
    )
    ap.add_argument(
        "--csv",
        action="store_true",
        default=env("ADA4356_CSV", "").strip().lower() in CSV_ON,
        help="write the sample and metrics CSVs (ADA4356_CSV=1)",
    )
    ap.add_argument(
        "--no-show",
        action="store_true",
        default=env("ADA4356_NO_SHOW", "") in NO_SHOW_ON,
        help="do not show the figures on screen (ADA4356_NO_SHOW=1)",
    )
    return ap.parse_args()


def next_run_index(outdir, basename=BASENAME):
    """Lowest unused run number, from the files already sitting in outdir.

    Scanning the directory rather than keeping a counter file means the
    numbering survives reboots, and copying the outputs elsewhere does not
    reset it.  Every output suffix is scanned so a run never reuses a number
    that any of its files already occupies.
    """
    used = set()
    pattern = re.compile(
        re.escape(basename) + r"(\d+)(?:_thd|_diag|_gate|_samples|_metrics)?\."
    )
    for path in glob.glob(os.path.join(outdir, basename + "*")):
        m = pattern.match(os.path.basename(path))
        if m:
            used.add(int(m.group(1)))
    idx = 1
    while idx in used:
        idx += 1
    return idx


def run_paths(outdir, run):
    stem = os.path.join(outdir, f"{BASENAME}{run}")
    return {
        "main": stem + ".png",
        "thd": stem + "_thd.png",
        "diag": stem + "_diag.png",
        "gate": stem + "_gate.png",
        "samples": stem + "_samples.csv",
        "metrics": stem + "_metrics.csv",
        "log": stem + ".txt",
    }


def parse_time_seconds(text, what):
    """Seconds from a string like '500us', '2.5 ms', '1s', '0.001'.

    A bare number is taken as seconds.  Returns None when text is empty.
    """
    text = (text or "").strip().lower()
    if not text:
        return None
    m = re.fullmatch(r"([0-9.]+)\s*(ns|us|ms|s|sec|seconds?)?", text)
    if not m:
        raise SystemExit(
            f"{what}: cannot parse {text!r}. Use a time like '500us', '2ms', "
            f"'1s', or a bare number of seconds like '0.001'."
        )
    scale = {
        None: 1.0,
        "s": 1.0,
        "sec": 1.0,
        "second": 1.0,
        "seconds": 1.0,
        "ms": 1e-3,
        "us": 1e-6,
        "ns": 1e-9,
    }[m.group(2)]
    seconds = float(m.group(1)) * scale
    if seconds <= 0:
        raise SystemExit(f"{what}: duration must be greater than zero.")
    return seconds


def duration_to_samples(seconds, rate_hz, what, quantum=8):
    """Samples needed to cover `seconds` at `rate_hz`, rounded up to `quantum`.

    The rounding is to the 8:1 SERDES word so a record never ends mid-word.
    """
    exact = seconds * rate_hz
    samples = int(-(-exact // quantum) * quantum)  # ceil to a whole word
    if samples < MIN_SAMPLES:
        raise SystemExit(
            f"{what}: {seconds * 1e6:.3f} us is only {exact:.2f} samples at "
            f"{rate_hz / 1e6:.3f} MHz, and the spectral analysis needs at "
            f"least {MIN_SAMPLES}; ask for "
            f"{MIN_SAMPLES / rate_hz * 1e6:.3f} us or more."
        )
    return samples


def apply_front_end(dev, gain_mode, filter_mode):
    """Set gain/filter on all four channels; None leaves the probe setting."""
    for attr, _what, allowed in FRONT_END_CONTROLS:
        requested = gain_mode if attr == "gain_mode" else filter_mode
        if requested is None:
            continue
        if requested not in allowed:
            raise SystemExit(
                f"{attr}: {requested!r} is not one of {', '.join(allowed)}"
            )
        if not hasattr(type(dev), attr):
            raise SystemExit(
                f"the installed adi/ada4356_quad.py has no '{attr}' property; "
                f"install the driver version that matches the OCT_08 boot files, "
                f"or omit the gain/filter arguments to leave the front end as the "
                f"driver set it at probe"
            )
        setattr(dev, attr, requested)


def view_window(window_s, fs, buffer_samples):
    """Samples the figures span; past the record end the gate is closed."""
    view_samples = buffer_samples
    if window_s is not None:
        view_samples = duration_to_samples(window_s, fs, "ADA4356_DURATION")
        print(
            f"Figure window      : {window_s * 1e6:.3f} us "
            f"({view_samples} samples) vs record "
            f"{buffer_samples / fs * 1e6:.3f} us ({buffer_samples} samples)"
        )
        if view_samples > buffer_samples:
            print(
                f"                     the last "
                f"{(view_samples - buffer_samples) / fs * 1e6:.3f} us of the "
                f"window has no data — the gate is closed there"
            )
        elif view_samples < buffer_samples:
            print(
                "                     window is shorter than the record, so the "
                "gate figure shows only its first part"
            )
    return view_samples


def parse_align_mode(text):
    align_mode = text.strip().lower() or "off"
    if align_mode not in ALIGN_MODES:
        raise SystemExit(
            f"ADA4356_ALIGN: {align_mode!r} is not one of {', '.join(ALIGN_MODES)}"
        )
    return align_mode


def tdd_frame(frame_text, buffer_samples, gate_on_cycles):
    """(frame_len, on_raw, off_raw) in TDD clock cycles.

    The gate is held asserted for the whole record, as the DMA only moves
    samples while it is asserted.  TDD and sample clock are both 125 MHz, so
    one buffer sample is one TDD cycle.
    """
    frame_env = frame_text.strip().lower()
    if frame_env in FRAME_FIT:
        # One frame is exactly one buffer: the record covers 100% of the frame.
        frame_len = buffer_samples + 2 * TDD_ON_BASE
        return frame_len, TDD_ON_BASE, TDD_ON_BASE + gate_on_cycles
    if not frame_env:
        # Default 1 s frame: plenty of time for the four DMAs to arm.
        frame_len = TDD_CLOCK_HZ
    else:
        m = re.fullmatch(r"([0-9.]+)\s*(cycles|c|us|ms|s)?", frame_env)
        if not m:
            raise SystemExit(
                f"ADA4356_FRAME: cannot parse {frame_env!r}. Use 'buffer', a cycle "
                f"count like '125000', or a time like '500us' / '2ms' / '1s'."
            )
        val, unit = float(m.group(1)), (m.group(2) or "cycles")
        mult = {
            "cycles": 1.0,
            "c": 1.0,
            "us": TDD_CLOCK_HZ / 1e6,
            "ms": TDD_CLOCK_HZ / 1e3,
            "s": float(TDD_CLOCK_HZ),
        }[unit]
        frame_len = int(round(val * mult))
        if frame_len <= 2 * TDD_ON_BASE:
            raise SystemExit(
                f"ADA4356_FRAME: frame of {frame_len} cycles is too short; "
                f"it must exceed {2 * TDD_ON_BASE} cycles of lead-in."
            )
    off_raw = min(frame_len - TDD_ON_BASE, TDD_ON_BASE + gate_on_cycles)
    if frame_env and buffer_samples > frame_len - 2 * TDD_ON_BASE:
        print(
            f"NOTE: buffer of {buffer_samples} samples is longer than the "
            f"{frame_len}-cycle frame — the capture will span several frames "
            f"and cross gate edges."
        )
    return frame_len, TDD_ON_BASE, off_raw


def setup_tdd(dev, frame_text, buffer_samples, gate_on_cycles):
    """Gate the four DMAs from TDD channels 1..4; returns (on_raw, off_raw)."""
    tdd = dev.tdd
    tdd.enable = False
    tdd.burst_count = 0
    frame_len, on_raw, off_raw = tdd_frame(frame_text, buffer_samples, gate_on_cycles)
    tdd.frame_length_raw = frame_len
    print(
        f"TDD frame          : {frame_len} cycles "
        f"({frame_len / TDD_CLOCK_HZ * 1e3:.3f} ms), "
        f"ON window {off_raw - on_raw} cycles "
        f"({(off_raw - on_raw) / TDD_CLOCK_HZ * 1e3:.3f} ms), "
        f"capture covers {100.0 * buffer_samples / frame_len:.1f}% of the frame"
    )
    tdd.channel[0].enable = False
    for ch_idx in range(1, 5):
        tdd.channel[ch_idx].on_raw = on_raw
        tdd.channel[ch_idx].off_raw = off_raw
        tdd.channel[ch_idx].enable = True
    tdd.enable = True
    try:
        tdd.sync_soft = True
    except Exception:
        pass
    return on_raw, off_raw


def boot_align(dev, align_mode, tone_hz, fs):
    """Run the stages of ada4356_quad_boot_align.py selected by align_mode."""
    import ada4356_quad_boot_align as ba

    if align_mode == "trim" and not hasattr(dev, "set_clock_delay"):
        raise SystemExit(
            "ADA4356_ALIGN=trim: the installed adi/ada4356_quad.py has no "
            "set_clock_delay(); install the driver from the quad_ada4356_py "
            "branch, or use ADA4356_ALIGN=shift"
        )
    align_args = types.SimpleNamespace(tone=tone_hz, captures=5, settle=1.0)
    ns_per_sample = 1e9 / fs
    print(f"\n=== Boot alignment ({align_mode}, tone {tone_hz / 1e3:.3f} kHz) ===")
    trim = {}
    try:
        if align_mode == "trim":
            dev.bypass_clock_delays()
            time.sleep(align_args.settle)
            before, _ = ba.averaged(dev, tone_hz, align_args.captures)
            print(
                "  uncorrected skew : "
                + "  ".join(
                    f"{lab}{b * ns_per_sample:+.2f}ns" for lab, b in zip("BCD", before)
                )
            )
            trim = ba.clock_trim(dev, before, align_args, fs)
            fmax = ba.max_fraction()
            print(
                "  AD9517 trim      : "
                + "  ".join(
                    f"{lab}:{ba.trim_label(fr, en, fmax, off='-')}"
                    for lab, (fr, en) in trim.items()
                )
            )
        cal = dev.calibrate_alignment(tone_hz, subsample=(align_mode == "interp"))
        print(
            "  whole-sample shift: "
            + "  ".join(f"{lab}{cal['removed_samples'][lab]:+d}samp" for lab in "ABCD")
        )
        if align_mode == "interp":
            print(
                "  sub-sample residual removed by interpolation: "
                + "  ".join(
                    f"{lab}{cal['residual_samples'][lab] * ns_per_sample:+.2f}ns"
                    for lab in "ABCD"
                )
            )
    except BaseException:
        if trim:
            dev.bypass_clock_delays()
            print("  aborted: clock delays restored to bypass")
        raise
    if trim:
        print("  clock delays left applied (a reboot restores bypass)")


def capture(dev):
    """All four DMAs armed concurrently, started on the same TDD pulse."""
    data = dev.rx()
    for ch in dev.channels:
        try:
            ch.rx_destroy_buffer()
        except OSError as e:
            print(
                f"NOTE: could not release {ch._ctrl.name} buffer ({e}); data already captured"
            )
    return data


def design_matrix(f0, n_harm, n_samples, fs):
    """[DC, cos(w), sin(w), cos(2w), sin(2w), ...] for harmonics within Nyquist."""
    n = np.arange(n_samples)
    cols = [np.ones(n_samples)]
    for h in range(1, n_harm + 1):
        if f0 * h >= fs / 2:
            break
        w = 2 * np.pi * f0 * h * n / fs
        cols.append(np.cos(w))
        cols.append(np.sin(w))
    return np.column_stack(cols)


def fit_at(x, f0, fs, n_harm=N_HARMONICS):
    """Least-squares fit of DC + fundamental + harmonics at a fixed f0.

    Amplitude is taken from the fit, never by summing FFT bins: with a window
    the leakage spans several bins and summing their power overstates the
    amplitude by ~30%.  Normal equations: ~10x faster than lstsq here.
    """
    A = design_matrix(f0, n_harm, x.size, fs)
    coef = np.linalg.solve(A.T @ A, A.T @ x)
    model = A @ coef
    resid = x - model
    dc = coef[0]
    amps = np.hypot(coef[1::2], coef[2::2])
    phase = np.degrees(np.arctan2(-coef[2], coef[1]))
    return dc, amps, phase, resid, model


def estimate_f0(x, f_req, fs):
    """Hierarchical peak search on |windowed DFT|, refined against fit residual.

    f0 needs to be right to ~1 Hz: over a 0.5 ms record a 200 Hz error slips
    the model by 40 degrees and inflates every noise number.  A 3-point FFT
    interpolation is not good enough.  Bounded to +-2 bins around the requested
    frequency on purpose: an unconstrained argmax locks onto spurious tones.
    """
    n = np.arange(x.size)
    xw = x * np.hanning(x.size)
    bin_hz = fs / x.size

    def mag(f):
        return np.abs(np.dot(xw, np.exp(-2j * np.pi * f * n / fs)))

    lo, hi = f_req - 2 * bin_hz, f_req + 2 * bin_hz
    for _ in range(3):
        grid = np.linspace(max(lo, 1.0), hi, 41)
        m = np.array([mag(f) for f in grid])
        i = int(np.argmax(m))
        step = grid[1] - grid[0]
        lo, hi = grid[i] - step, grid[i] + step
    if 0 < i < len(grid) - 1:
        y0, y1, y2 = m[i - 1], m[i], m[i + 1]
        coarse = grid[i] + 0.5 * (y0 - y2) / (y0 - 2 * y1 + y2) * step
    else:
        coarse = grid[i]
    fine = coarse + np.linspace(-step, step, 11)
    return fine[int(np.argmin([np.std(fit_at(x, f, fs)[3]) for f in fine]))]


def print_capture_info(fs, buffer_samples, tone_hz, f0, run):
    bins = f0 * buffer_samples / fs
    print(f"\nSampling frequency : {fs / 1e6:.3f} MHz")
    print(
        f"Buffer size        : {buffer_samples} samples "
        f"({buffer_samples / fs * 1e3:.3f} ms)"
    )
    print(f"Requested tone     : {tone_hz / 1e3:.3f} kHz")
    print(
        f"Fitted tone        : {f0 / 1e3:.6f} kHz  ({bins:.3f} bins, {bins:.2f} cycles)"
    )
    print(
        f"Code scaling       : 1 LSB = {LSB_V * 1e6:.3f} uV, full scale = +-{VREF_V:.3f} V"
    )
    print(f"Run number         : {run}")


def front_end_state(dev, attr, labels):
    """({'A': '11k', ...}, per_channel) from the dict or plain-string getter.

    Older drivers return one string for the whole device; it is widened to all
    four labels and flagged, so a per-channel claim is never made from it.
    """
    try:
        value = getattr(dev, attr)
    except Exception as exc:  # attribute missing on this boot image, etc.
        return {L: f"unreadable ({exc})" for L in labels}, False
    if isinstance(value, dict):
        return {L: str(value.get(L, "?")).strip() for L in labels}, True
    return {L: str(value).strip() for L in labels}, False


def report_front_end(dev):
    """Print the front end read back per channel; return the figure stamp.

    Cross-check against: iio_attr -u <uri> -c ada4356-a voltage0 gain_mode
    """
    fe = {}
    per_channel = True
    for attr, what, _allowed in FRONT_END_CONTROLS:
        fe[what], ok = front_end_state(dev, attr, LABELS)
        per_channel = per_channel and ok
    for i, label in enumerate(LABELS):
        head = "Front end          : " if i == 0 else "                     "
        print(head + f"{label} gain {fe['gain'][label]}  filter {fe['filter'][label]}")
    if not per_channel:
        print(
            "                     (device-wide read — this driver does not "
            "report the four channels separately)"
        )
    else:
        for what in ("gain", "filter"):
            vals = fe[what]
            odd = [L for L in LABELS if vals[L] != vals[LABELS[0]]]
            if odd:
                common = max(set(vals.values()), key=list(vals.values()).count)
                for L in LABELS:
                    if vals[L] != common:
                        print(
                            f"  *** channel {L} {what} {vals[L]} differs from the "
                            f"others ({common}) — amplitudes are not comparable"
                        )
    mismatch = per_channel and any(
        len(set(fe[w].values())) > 1 for w in ("gain", "filter")
    )
    box = "Front end settings\n" + "\n".join(
        f"{L}: gain {fe['gain'][L]:<5} {fe['filter'][L]}" for L in LABELS
    )
    if not per_channel:
        box += "\n(device-wide read)"
    if mismatch:
        box += "\n*** mismatch between channels"
    return box, mismatch


def stamp_front_end(fig, stamp):
    box, mismatch = stamp
    fig.text(
        0.995,
        0.995,
        box,
        ha="right",
        va="top",
        fontsize=8,
        family="monospace",
        color="red" if mismatch else "black",
        bbox=dict(
            boxstyle="round", fc="white", ec="red" if mismatch else "0.5", alpha=0.9
        ),
    )


def signal_quality(raw, data, fits, f0, fs):
    """Print the signal-quality table; return one metrics tuple per channel."""
    print(
        "\n=== Signal quality (least-squares fit, harmonics 2..%d removed) ==="
        % N_HARMONICS
    )
    print(
        f"{'Ch':>3}  {'ampl':>8}  {'dBFS':>7}  {'offset':>8}  {'noise':>7}  "
        f"{'SNR/FS':>7}  {'THD':>8}  {'SFDR':>7}  {'SINAD':>7}  {'ENOB':>6}  {'codes':>6}"
    )
    quality = []
    for i, (label, x, (dc, amps, phase, resid, model)) in enumerate(
        zip(LABELS, raw, fits)
    ):
        N = resid.size
        a1 = amps[0]
        harm = amps[1:]
        noise_rms = np.std(resid)
        # Largest non-harmonic spur, from the spectrum of the residual.  DC and
        # every fitted harmonic are masked: the fit leaves a little residue there.
        rspec = np.abs(np.fft.rfft(resid * np.hanning(N))) * 4.0 / N  # Hann gain 0.5
        rspec[:10] = 0.0
        for h in range(1, N_HARMONICS + 1):
            b = int(round(f0 * h * N / fs))
            if b < len(rspec):
                rspec[max(0, b - 5) : b + 6] = 0.0
        spur = rspec.max()
        spur_hz = np.fft.rfftfreq(N, 1.0 / fs)[int(np.argmax(rspec))]
        worst = max(spur, harm.max() if harm.size else 0.0)
        sfdr = 20 * np.log10(a1 / worst) if worst > 0 else np.inf
        thd = 10 * np.log10(np.sum(harm ** 2) / a1 ** 2) if harm.size else -np.inf
        snr_fs = 20 * np.log10((FULL_SCALE / np.sqrt(2)) / noise_rms)
        sinad = 10 * np.log10((a1 ** 2 / 2) / (noise_rms ** 2 + np.sum(harm ** 2) / 2))
        # ENOB referred to full scale, so a small signal is not penalised twice
        sinad_fs = sinad + 20 * np.log10(FULL_SCALE / a1)
        enob = (sinad_fs - 1.76) / 6.02
        distinct = len(np.unique(data[i]))
        quality.append(
            (
                a1,
                dc,
                noise_rms,
                spur,
                spur_hz,
                harm,
                sfdr,
                thd,
                sinad_fs,
                enob,
                snr_fs,
                distinct,
            )
        )
        print(
            f"{label:>3}  {a1:>8.1f}  {20 * np.log10(a1 / FULL_SCALE):>7.2f}  {dc:>8.1f}  "
            f"{noise_rms:>7.2f}  {snr_fs:>7.2f}  {thd:>8.2f}  {sfdr:>7.2f}  "
            f"{sinad_fs:>7.2f}  {enob:>6.2f}  {distinct:>6}"
        )
    print(
        "  ampl/offset/noise in ADC codes · dB values in dB · ENOB referred to full scale"
    )
    return quality


def print_quality_volts(quality):
    """Same amplitudes in volts; the dB columns are ratios and not repeated."""
    print("\n=== Signal quality in volts (1 LSB = %.3f uV) ===" % (LSB_V * 1e6))
    print(
        f"{'Ch':>3}  {'ampl (mV)':>10}  {'Vpp (mV)':>10}  {'offset (mV)':>12}  "
        f"{'noise (uVrms)':>14}  {'LSB-equiv':>10}"
    )
    for label, q in zip(LABELS, quality):
        a1, dc, noise_rms = q[0], q[1], q[2]
        print(
            f"{label:>3}  {a1 * LSB_V * 1e3:>10.3f}  {2 * a1 * LSB_V * 1e3:>10.3f}  "
            f"{dc * LSB_V * 1e3:>12.3f}  {noise_rms * LSB_V * 1e6:>14.2f}  "
            f"{noise_rms:>10.2f}"
        )
    print("  amplitude is the fitted sine peak; Vpp = 2 x amplitude")
    print("  offset is the fitted DC level referred to the ADC's bipolar zero")


def print_clipping(raw):
    clipped = [
        label
        for label, x in zip(LABELS, raw)
        if np.max(x) >= FULL_SCALE - 1 or np.min(x) <= -FULL_SCALE + 1
    ]
    if clipped:
        print(
            f"\n  *** CLIPPING on channel(s) {', '.join(clipped)} — every number above is invalid."
        )
        print("  *** Reduce the generator amplitude and/or adjust its DC offset.")


def print_harmonics(quality):
    print("\n=== Harmonic content (dBc relative to the fundamental) ===")
    hdr = "  ".join(f"{'H' + str(h):>7}" for h in range(2, N_HARMONICS + 1))
    print(f"{'Ch':>3}  {hdr}   worst spur")
    for label, q in zip(LABELS, quality):
        a1, spur, spur_hz, harm = q[0], q[3], q[4], q[5]
        cells = "  ".join(
            f"{20 * np.log10(h / a1):>7.1f}" if h > 0 else f"{'-':>7}" for h in harm
        )
        print(
            f"{label:>3}  {cells}   {20 * np.log10(spur / a1):>6.1f} dBc @ "
            f"{spur_hz / 1e6:.3f} MHz"
        )
    print("\n=== Worst spur and total harmonic content in volts ===")
    print(f"{'Ch':>3}  {'worst spur (uV)':>16}  {'at (MHz)':>9}  {'harm rms (uV)':>14}")
    for label, q in zip(LABELS, quality):
        spur, spur_hz, harm = q[3], q[4], q[5]
        harm_rms = np.sqrt(np.sum(harm ** 2) / 2) if harm.size else 0.0
        print(
            f"{label:>3}  {spur * LSB_V * 1e6:>16.2f}  {spur_hz / 1e6:>9.3f}  "
            f"{harm_rms * LSB_V * 1e6:>14.2f}"
        )


def autocorrelation(r, max_lag=MAX_LAG):
    """Normalised autocorrelation of the residual, via Wiener-Khinchin.

    O(N log N); np.correlate(mode="full") is O(N^2) and hangs at N=65536.
    After the tone and its harmonics are fitted out, is what remains white?
    """
    r = r - r.mean()
    nfft = 1 << int(np.ceil(np.log2(2 * r.size)))
    s = np.fft.rfft(r, nfft)
    acr = np.fft.irfft(np.abs(s) ** 2, nfft)[: max_lag + 1]
    return acr / acr[0]


def _fmt_codes(v):
    return f"{v:.2f}" if v == v else "—"


def _fmt_uv(v):
    return f"{v * LSB_V * 1e6:.2f}" if v == v else "—"


def residual_whiteness(fits, f0, fs):
    """Split each residual into a white and a correlated part.

    A residual with periodic structure reaches the FFT as a raised, featureless
    floor; the autocorrelation separates "this converter is noisy" from "this
    board has one narrowband intruder on it".  A component correlated at lag 1
    contributes ~acf[1] of the variance, so the white part is var*(1-acf[1]);
    its frequency follows from the first zero crossing (a quarter period).

    Returns (acfs, acf_summary, split, sig_bound): acf_summary holds (zero
    crossing lag, dominant Hz, correlated fraction), split (resid rms, white,
    correlated) in codes, per channel.
    """
    N = fits[0][3].size
    sig_bound = 3.0 / np.sqrt(N)
    acfs = [autocorrelation(f[3]) for f in fits]
    print("\n=== Residual autocorrelation — is the leftover noise white? ===")
    print(
        f"White noise gives |r| < {sig_bound:.4f} (3 sigma at N={N}) for every lag > 0"
    )
    print(
        f"\n{'Ch':>3}  "
        + "  ".join(f"{'lag' + str(L):>8}" for L in STRUCTURAL_LAGS)
        + "   other significant lags"
    )
    for label, acf in zip(LABELS, acfs):
        cells = "  ".join(f"{acf[L]:>8.4f}" for L in STRUCTURAL_LAGS)
        other = [
            L
            for L in range(1, MAX_LAG + 1)
            if abs(acf[L]) > sig_bound and L not in STRUCTURAL_LAGS
        ]
        tail = ", ".join(str(L) for L in other[:8]) + (" ..." if len(other) > 8 else "")
        print(f"{label:>3}  {cells}   {tail if other else '-'}")
    print(
        """
  lag 1 alone            -> band-limited noise; expected with FREQ_SEL=1, whose
                            filter is worth 1.3 ENOB and is meant to be on
  lag 4 and multiples    -> BUFR /4 divided-clock artefact
  lag 8 and multiples    -> 8:1 SERDES word boundary
  broad decaying tail    -> the residual is one narrowband thing, not noise;
                            the table below says what and how much"""
    )
    print(
        f"\n{'Ch':>3}  {'resid rms':>9}  {'white':>7}  {'correlated':>10}  "
        f"{'dominant':>10}  what the 'noise' column is actually measuring"
    )
    acf_summary = []
    split = []
    for label, acf, (dc, amps, phase, resid, model) in zip(LABELS, acfs, fits):
        tot = np.std(resid)
        worst_lag = int(np.argmax(np.abs(acf[1:]))) + 1
        dom = None
        zc = None
        if abs(acf[worst_lag]) < sig_bound:
            white, verdict = tot, "white — the noise column is real converter noise"
        elif acf[1] < SMOOTH_ACF1:
            # Structured but not smooth: the lag-1 split does not hold, do not guess.
            white, verdict = (
                float("nan"),
                f"structured at lag {worst_lag} (r={acf[worst_lag]:+.3f}) — "
                f"not smooth, so no split; see the lag table",
            )
        else:
            white = min(tot * np.sqrt(max(1.0 - acf[1], 0.0)), tot)
            neg = np.flatnonzero(acf[1:] < 0)
            if neg.size == 0:
                verdict = (
                    f"one slow component below {fs / (4 * MAX_LAG) / 1e6:.2f} MHz; "
                    "raise MAX_LAG to localise it"
                )
            else:
                z = neg[0] + 1
                zc = z - 1 + acf[z - 1] / (acf[z - 1] - acf[z])
                dom = fs / (4 * zc)
                verdict = (
                    "f0 is mis-fitted — fix estimate_f0, not a board fault"
                    if abs(dom - f0) < 0.2 * f0
                    else f"a spur near {dom / 1e6:.2f} MHz, not noise; true SNR/FS "
                    f"{20 * np.log10(FULL_SCALE / np.sqrt(2) / white):.1f} dB"
                )
        corr = (
            np.sqrt(max(tot ** 2 - white ** 2, 0.0)) if white == white else float("nan")
        )
        acf_summary.append(
            (zc, dom, (corr / tot) ** 2 if corr == corr else float("nan"))
        )
        split.append((tot, white, corr))
        print(
            f"{label:>3}  {tot:>9.2f}  {_fmt_codes(white):>7}  {_fmt_codes(corr):>10}  "
            f"{(f'{dom / 1e6:.3f} MHz' if dom else '—'):>10}  {verdict}"
        )
    print(
        "  The split assumes the correlated part is smooth at lag 1, which the"
        "\n  decaying ACF itself establishes. Cross-check 'dominant' against the"
        "\n  worst-spur column in the harmonic table — they should agree."
    )
    print(
        f"\n{'Ch':>3}  {'resid (uVrms)':>14}  {'white (uVrms)':>14}  {'correlated (uVrms)':>19}"
    )
    for label, (tot, white, corr) in zip(LABELS, split):
        print(
            f"{label:>3}  {_fmt_uv(tot):>14}  {_fmt_uv(white):>14}  {_fmt_uv(corr):>19}"
        )
    return acfs, acf_summary, split, sig_bound


def channel_skew(fits, f0, fs):
    """Phase of each channel vs A, in degrees, samples and ns.

    Cross-correlation is deliberately not used: on a clean sine it equals the
    fitted phase, and np.correlate is dragged toward zero lag by shared noise.
    """
    deg_per_sample = 360.0 * f0 / fs
    print("\n=== Inter-channel skew (vs channel A) ===")
    print(
        f"1 sample = {deg_per_sample:.4f} deg at this tone; "
        f"unambiguous range +-{180.0 / deg_per_sample:.0f} samples"
    )
    print(
        f"\n{'Ch':>3}  {'phase (deg)':>12}  {'offset (deg)':>13}  {'skew (samp)':>12}  {'skew (ns)':>11}"
    )
    skews = []
    for label, (dc, amps, phase, resid, model) in zip(LABELS, fits):
        offset_deg = (phase - fits[0][2] + 180) % 360 - 180
        skew_samples = offset_deg / 360.0 * (fs / f0)
        skews.append((phase, offset_deg, skew_samples))
        print(
            f"{label:>3}  {phase:>12.3f}  {offset_deg:>13.3f}  "
            f"{skew_samples:>12.4f}  {skew_samples / fs * 1e9:>11.2f}"
        )
    print("\n  To classify a non-zero skew, re-run at 10x the tone frequency:")
    print("    skew in SAMPLES stays constant  -> digital/SERDES delay")
    print("    skew in DEGREES stays constant  -> analog front-end pole mismatch")
    return skews


def raw_stats(data):
    print("\n=== Raw sample statistics, ADC codes ===")
    print(f"{'Ch':>3}  {'mean':>10}  {'std':>10}  {'min':>10}  {'max':>10}")
    stats = []
    for label, ch_data in zip(LABELS, data):
        s = (
            float(np.mean(ch_data)),
            float(np.std(ch_data)),
            int(np.min(ch_data)),
            int(np.max(ch_data)),
        )
        stats.append(s)
        print(f"{label:>3}  {s[0]:>10.1f}  {s[1]:>10.1f}  {s[2]:>10}  {s[3]:>10}")
    print("\n=== Raw sample statistics, volts ===")
    print(
        f"{'Ch':>3}  {'mean (mV)':>10}  {'std (mV)':>10}  {'min (mV)':>10}  "
        f"{'max (mV)':>10}  {'headroom':>9}"
    )
    for label, s in zip(LABELS, stats):
        headroom = min(FULL_SCALE - 1 - s[3], s[2] + FULL_SCALE)
        print(
            f"{label:>3}  {s[0] * LSB_V * 1e3:>10.3f}  {s[1] * LSB_V * 1e3:>10.3f}  "
            f"{s[2] * LSB_V * 1e3:>10.3f}  {s[3] * LSB_V * 1e3:>10.3f}  "
            f"{headroom * LSB_V * 1e3:>6.1f} mV"
        )
    print("  headroom is the distance from the nearer rail; <= 0 means clipping")
    return stats


def write_samples_csv(path, uri, fs, tone_hz, f0, data):
    """Raw codes next to volts, so the data can be re-analysed independently."""
    N = len(data[0])
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["# file", os.path.basename(path)])
        w.writerow(["# uri", uri])
        w.writerow(["# sampling_frequency_hz", f"{fs:.6f}"])
        w.writerow(["# buffer_samples", N])
        w.writerow(["# requested_tone_hz", f"{tone_hz:.6f}"])
        w.writerow(["# fitted_tone_hz", f"{f0:.6f}"])
        w.writerow(["# lsb_volts", f"{LSB_V:.12g}"])
        w.writerow(
            ["sample", "time_us"]
            + [f"{L}_codes" for L in LABELS]
            + [f"{L}_volts" for L in LABELS]
        )
        # One np.savetxt pass instead of a per-row csv.writer loop; same bytes.
        cols = [np.asarray(ch, dtype=np.float64) for ch in data]
        table = np.column_stack(
            [np.arange(N), np.arange(N) / fs * 1e6] + cols + [c * LSB_V for c in cols]
        )
        fmt = ["%d", "%.6f"] + ["%d"] * len(cols) + ["%.9f"] * len(cols)
        # newline matches csv.writer's \r\n above, so the file is not a mix.
        np.savetxt(fh, table, fmt=fmt, delimiter=",", newline="\r\n")
    print(f"\nCSV saved: {path}  ({N} rows)")


def write_metrics_csv(path, fs, f0, quality, acf_summary, split, skews, stats):
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["# file", os.path.basename(path)])
        w.writerow(["# sampling_frequency_hz", f"{fs:.6f}"])
        w.writerow(["# fitted_tone_hz", f"{f0:.6f}"])
        w.writerow(["# lsb_volts", f"{LSB_V:.12g}"])
        header = [
            "channel",
            "amplitude_codes",
            "amplitude_mV",
            "vpp_mV",
            "offset_codes",
            "offset_mV",
            "noise_codes",
            "noise_uVrms",
            "snr_fs_dB",
            "thd_dB",
            "sfdr_dB",
            "sinad_fs_dB",
            "enob_bits",
            "distinct_codes",
            "worst_spur_dBc",
            "worst_spur_hz",
            "worst_spur_uV",
        ]
        header += [f"h{h}_dBc" for h in range(2, N_HARMONICS + 1)]
        header += [
            "resid_rms_codes",
            "resid_white_codes",
            "resid_correlated_codes",
            "resid_rms_uVrms",
            "dominant_hz",
            "correlated_fraction",
            "phase_deg",
            "skew_deg",
            "skew_samples",
            "skew_ns",
            "mean_codes",
            "std_codes",
            "min_codes",
            "max_codes",
            "mean_mV",
            "std_mV",
            "min_mV",
            "max_mV",
        ]
        w.writerow(header)
        for i, label in enumerate(LABELS):
            a1, dc, noise_rms, spur, spur_hz, harm = quality[i][:6]
            sfdr, thd, sinad_fs, enob, snr_fs, distinct = quality[i][6:]
            zc, dom, frac = acf_summary[i]
            tot, white, corr = split[i]
            phase, skew_deg, skew_samp = skews[i]
            mean_c, std_c, min_c, max_c = stats[i]
            row = [
                label,
                f"{a1:.4f}",
                f"{a1 * LSB_V * 1e3:.6f}",
                f"{2 * a1 * LSB_V * 1e3:.6f}",
                f"{dc:.4f}",
                f"{dc * LSB_V * 1e3:.6f}",
                f"{noise_rms:.4f}",
                f"{noise_rms * LSB_V * 1e6:.4f}",
                f"{snr_fs:.4f}",
                f"{thd:.4f}",
                f"{sfdr:.4f}",
                f"{sinad_fs:.4f}",
                f"{enob:.4f}",
                distinct,
                f"{20 * np.log10(spur / a1):.4f}",
                f"{spur_hz:.4f}",
                f"{spur * LSB_V * 1e6:.4f}",
            ]
            row += [f"{20 * np.log10(h / a1):.4f}" if h > 0 else "" for h in harm]
            row += [""] * (N_HARMONICS - 1 - harm.size)
            row += [
                f"{tot:.4f}",
                f"{white:.4f}" if white == white else "",
                f"{corr:.4f}" if corr == corr else "",
                f"{tot * LSB_V * 1e6:.4f}",
                f"{dom:.4f}" if dom else "",
                f"{frac:.6f}" if frac == frac else "",
                f"{phase:.4f}",
                f"{skew_deg:.4f}",
                f"{skew_samp:.6f}",
                f"{skew_samp / fs * 1e9:.4f}",
                f"{mean_c:.4f}",
                f"{std_c:.4f}",
                min_c,
                max_c,
                f"{mean_c * LSB_V * 1e3:.6f}",
                f"{std_c * LSB_V * 1e3:.6f}",
                f"{min_c * LSB_V * 1e3:.6f}",
                f"{max_c * LSB_V * 1e3:.6f}",
            ]
            w.writerow(row)
    print(f"CSV saved: {path}  (4 rows)")


def plot_periods(periods_text, main_png, gate_png):
    """Tone periods for the time-domain panels; "all" falls back to 5."""
    periods = periods_text.strip().lower()
    if periods in ("all", "full", "0"):
        # The whole record is in the gate figure; keep this one readable.
        print(
            "\nNOTE: ADA4356_PERIODS=all ignored for "
            + os.path.basename(main_png)
            + " — it keeps 5 periods so the traces stay readable; "
            + os.path.basename(gate_png)
            + " shows the complete record."
        )
        return 5.0
    return float(periods)


def plot_overview(paths, run, ac, quality, f0, fs, tone_hz, periods, stamp):
    """Figure 1: all channels overlaid on top, stacked lanes below.

    TDD gates all four DMAs off the same pulse, so sample n is the same instant
    on every channel: a delay or amplitude error shows as traces that fail to
    lie on top of each other.  DC is removed; offsets are in the legend.
    """
    N = len(ac[0])
    # Sized from the requested tone, not the fitted f0: on short records the
    # fit can drift and shrink the window below the requested number of cycles.
    zoom_n = int(round(fs / tone_hz * periods))
    if zoom_n > N:
        print(
            f"\nNOTE: {periods:.3g} periods of {tone_hz / 1e3:.3f} kHz need "
            f"{zoom_n} samples but the record has {N}; "
            + os.path.basename(paths["main"])
            + " shows the whole record "
            f"({N * tone_hz / fs:.3g} periods)."
        )
        zoom_n = N
    zoom_n = max(zoom_n, 2)
    shown_periods = zoom_n * tone_hz / fs
    t_us = np.arange(zoom_n) / fs * 1e6
    fig = plt.figure(figsize=(15, 10))
    gs = fig.add_gridspec(2, 1, height_ratios=[1, 1.15], hspace=0.30)
    ax_s = fig.add_subplot(gs[0, 0])
    for a, label, color, q in zip(ac, LABELS, COLORS, quality):
        ax_s.plot(
            t_us,
            a[:zoom_n],
            color=color,
            linewidth=0.9,
            label=f"Ch {label} — {2 * q[0] * LSB_V * 1e3:.1f} mVpp, "
            f"DC {q[1] * LSB_V * 1e3:.1f} mV",
        )
    ax_s.set_xlabel("Time (µs)")
    ax_s.set_ylabel("ADC code (DC removed)")
    ax_s.set_title(
        f"TDD-synchronized capture — all channels overlaid, DC removed — "
        f"{f0 / 1e3:.3f} kHz — {shown_periods:.3g} periods "
        f"({zoom_n / fs * 1e6:.2f} µs, {zoom_n} samples) — run {run}"
    )
    ax_s.set_xlim(0, t_us[-1])
    ax_s.legend(fontsize=8, loc="upper right", ncol=2)
    ax_s.grid(True, alpha=0.3)
    ax_sv = ax_s.twinx()
    lo_c, hi_c = ax_s.get_ylim()
    ax_sv.set_ylim(lo_c * LSB_V * 1e3, hi_c * LSB_V * 1e3)
    ax_sv.set_ylabel("Amplitude (mV, DC removed)")
    # One lane per channel, lifted by a pitch set from the largest amplitude so
    # the lanes never bleed into each other.
    ax_t = fig.add_subplot(gs[1, 0], sharex=ax_s)
    span = max(np.ptp(a[:zoom_n]) for a in ac)
    pitch = span * 1.35 if span > 0 else 1.0
    centres = [(len(ac) - 1 - i) * pitch for i in range(len(ac))]
    for a, label, color, centre, q in zip(ac, LABELS, COLORS, centres, quality):
        ax_t.plot(t_us, a[:zoom_n] + centre, color=color, linewidth=0.9)
        ax_t.axhline(centre, color=color, linestyle=":", linewidth=0.6, alpha=0.5)
        ax_t.text(
            t_us[-1],
            centre + 0.30 * pitch,
            f"Ch {label} — {2 * q[0] * LSB_V * 1e3:.1f} mVpp, "
            f"DC {q[1] * LSB_V * 1e3:.1f} mV",
            ha="right",
            va="bottom",
            fontsize=9,
            color=color,
        )
    ax_t.set_yticks(centres)
    ax_t.set_yticklabels([f"Ch {L}" for L in LABELS])
    ax_t.set_ylim(centres[-1] - 0.75 * pitch, centres[0] + 0.75 * pitch)
    ax_t.set_xlabel("Time (µs)")
    ax_t.set_ylabel("Stacked channels (DC removed)")
    ax_t.set_title("Individual channel traces, stacked")
    ax_t.grid(True, axis="x", alpha=0.3)
    ax_v = ax_t.twinx()
    lane_lo, lane_hi = ax_t.get_ylim()
    ax_v.set_ylim(lane_lo * LSB_V * 1e3, lane_hi * LSB_V * 1e3)
    ax_v.set_ylabel(f"Stacked offset (mV) — lane pitch {pitch * LSB_V * 1e3:.1f} mV")
    stamp_front_end(fig, stamp)
    plt.savefig(paths["main"], dpi=150, bbox_inches="tight")
    print("\nPlot saved: " + paths["main"])


def plot_spectra(path, run, ac, quality, f0, fs, stamp):
    """Figure 2: distortion spectra, one subplot per channel."""
    N = len(ac[0])
    window = np.hanning(N)
    freq_mhz = np.fft.rfftfreq(N, d=1.0 / fs) / 1e6
    fig = plt.figure(figsize=(15, 9))
    gs = fig.add_gridspec(2, 2, hspace=0.35, wspace=0.22)
    fig.suptitle(
        f"Harmonic distortion spectra — {f0 / 1e3:.3f} kHz — run {run}", fontsize=12
    )
    for idx, (a, label, color) in enumerate(zip(ac, LABELS, COLORS)):
        ax = fig.add_subplot(gs[idx // 2, idx % 2])
        s = np.fft.rfft(a * window)
        psd = 20 * np.log10(np.abs(s) * 4 / N / FULL_SCALE + 1e-20)
        ax.plot(freq_mhz, psd, color=color, linewidth=0.6)
        a1, spur, spur_hz = quality[idx][0], quality[idx][3], quality[idx][4]
        sfdr, thd, enob = quality[idx][6], quality[idx][7], quality[idx][9]
        for h in range(2, N_HARMONICS + 1):
            fh = f0 * h
            if fh < fs / 2:
                ax.axvline(
                    fh / 1e6, color="gray", linestyle=":", linewidth=0.6, alpha=0.7
                )
        ax.plot(
            spur_hz / 1e6,
            20 * np.log10(spur / FULL_SCALE),
            "kv",
            markersize=5,
            label=f"worst spur {20 * np.log10(spur / a1):.0f} dBc "
            f"({spur * LSB_V * 1e6:.1f} µV)",
        )
        ax.set_title(
            f"Ch {label} — {20 * np.log10(a1 / FULL_SCALE):.1f} dBFS "
            f"({2 * a1 * LSB_V * 1e3:.1f} mVpp), "
            f"SFDR {sfdr:.1f} dB, THD {thd:.1f} dB, ENOB {enob:.2f}",
            fontsize=10,
        )
        ax.set_xlabel("Frequency (MHz)")
        ax.set_ylabel("dBFS")
        ax.set_ylim(-180, 5)
        ax.legend(loc="upper right", fontsize=8)
        ax.grid(True, alpha=0.3)
    stamp_front_end(fig, stamp)
    plt.savefig(path, dpi=150, bbox_inches="tight")
    print("Plot saved: " + path)


def plot_autocorrelation(path, run, acfs, acf_summary, sig_bound, stamp):
    """Figure 3: residual autocorrelation; the title states the finding."""
    fig, ax = plt.subplots(figsize=(11, 6.5))
    for acf, label, color, (zc, dom, frac) in zip(acfs, LABELS, COLORS, acf_summary):
        tag = f"Ch {label} — $r_1$={acf[1]:.3f}"
        if frac == frac:
            tag += f", {100 * frac:.0f}% correlated"
        ax.plot(
            np.arange(1, MAX_LAG + 1),
            acf[1:],
            color=color,
            linewidth=1.0,
            marker=".",
            markersize=3,
            label=tag,
        )
    ax.axhspan(
        -sig_bound,
        sig_bound,
        color="gray",
        alpha=0.35,
        label=f"white noise would live entirely inside this (±{sig_bound:.4f})",
    )
    for L in (4, 8, 16, 32):
        ax.axvline(L, color="gray", linestyle=":", linewidth=0.6)
    ax.annotate(
        "lags 4/8/16/32 = clock-divider and SERDES-word\n"
        "fault signatures; smooth here means the digital side is clean",
        xy=(0.015, 0.03),
        xycoords="axes fraction",
        fontsize=8,
        color="gray",
    )
    # Same tone on every channel: annotate the worst-affected one only.
    worst = max(
        range(len(acf_summary)),
        key=lambda i: acf_summary[i][2]
        if acf_summary[i][2] == acf_summary[i][2]
        else -1,
    )
    zc, dom, _frac = acf_summary[worst]
    if zc is not None and dom is not None:
        ax.axvline(zc, color="k", linestyle="--", linewidth=0.9)
        ax.axhline(0.0, color="k", linewidth=0.5)
        ax.annotate(
            f"zero crossing at lag {zc:.1f}\n= quarter period\n"
            f"$\\rightarrow$ {dom / 1e6:.2f} MHz",
            xy=(zc, 0.0),
            xytext=(zc + 0.08 * MAX_LAG, 0.45),
            fontsize=9,
            arrowprops=dict(arrowstyle="->", color="k", linewidth=0.8),
            bbox=dict(boxstyle="round", facecolor="wheat", alpha=0.75),
        )
        headline = (
            f"Residual autocorrelation — the leftover is a {dom / 1e6:.2f} MHz tone, "
            "not noise"
        )
    else:
        headline = (
            "Residual autocorrelation — leftover is white, the noise column is real"
        )
    ax.set_xlabel("Lag (samples)")
    ax.set_ylabel("Normalised autocorrelation")
    ax.set_title(f"{headline} — run {run}")
    ax.legend(fontsize=8, loc="upper right")
    ax.grid(True, alpha=0.3)
    stamp_front_end(fig, stamp)
    plt.savefig(path, dpi=150, bbox_inches="tight")
    print("Plot saved: " + path)


def plot_gate(path, run, ac, quality, fs, view_samples, on_raw, off_raw, stamp):
    """Figure 4: one gate pulse on top, the synchronized capture below.

    The gate trace is reconstructed from the register values, not measured:
    the ADC digitises the analog input, not the TDD gate line.  t = 0 is the
    rising edge and both panels share one x axis.
    """
    N = len(ac[0])
    gate_on_us = (off_raw - on_raw) / TDD_CLOCK_HZ * 1e6
    cap_us = N / fs * 1e6
    view_us = view_samples / fs * 1e6  # may exceed, or clip, the record
    span_us = view_us if view_us < cap_us else max(gate_on_us, cap_us, view_us)
    lead_us = 0.12 * span_us
    past_end = view_us > cap_us * 1.001
    fig = plt.figure(figsize=(15, 9))
    gs = fig.add_gridspec(2, 1, height_ratios=[1, 1.6], hspace=0.28)
    ax_g = fig.add_subplot(gs[0, 0])
    step_t = [-lead_us, 0.0, 0.0, gate_on_us, gate_on_us, span_us + lead_us]
    step_v = [0.0, 0.0, 1.0, 1.0, 0.0, 0.0]
    ax_g.plot(
        step_t, step_v, color="tab:purple", linewidth=2.0, label="TDD gate (configured)"
    )
    ax_g.axvspan(
        0.0, cap_us, color="tab:blue", alpha=0.12, label=f"capture span {cap_us:.1f} µs"
    )
    if past_end:
        # The DMA is gated off past the record and cannot produce samples.
        ax_g.axvspan(
            cap_us,
            view_us,
            color="0.6",
            alpha=0.25,
            hatch="//",
            label=f"no data — gate closed ({view_us - cap_us:.1f} µs)",
        )
    ax_g.set_xlim(-lead_us, span_us + lead_us)
    ax_g.set_ylim(-0.15, 1.35)
    ax_g.set_yticks([0, 1])
    ax_g.set_yticklabels(["OFF", "ON"])
    ax_g.set_ylabel("Gate state")
    ax_g.set_title(
        f"TDD gate , ON for {off_raw - on_raw} cycles "
        f"({gate_on_us:.3f} µs), run {run}"
    )
    ax_g.legend(fontsize=8, loc="center right")
    ax_g.grid(True, alpha=0.3)
    ax_gc = fig.add_subplot(gs[1, 0], sharex=ax_g)
    t_full_us = np.arange(N) / fs * 1e6
    for a, label, color, q in zip(ac, LABELS, COLORS, quality):
        ax_gc.plot(
            t_full_us,
            a,
            color=color,
            linewidth=0.6,
            label=f"Ch {label} — {2 * q[0] * LSB_V * 1e3:.1f} mVpp",
        )
    ax_gc.axvline(
        0.0, color="tab:purple", linestyle="--", linewidth=1.0, label="gate opens"
    )
    if past_end:
        ax_gc.axvline(
            cap_us,
            color="tab:purple",
            linestyle="--",
            linewidth=1.0,
            label="gate closes",
        )
        ax_gc.axvspan(
            cap_us,
            view_us,
            color="0.6",
            alpha=0.25,
            hatch="//",
            label="no data — gate closed",
        )
    ax_gc.set_xlabel("Time from the gate rising edge (µs)")
    ax_gc.set_ylabel("ADC code (DC removed)")
    ax_gc.set_title(
        f"TDD-synchronized capture — all {N} samples, DC removed"
        + (
            f" of a {view_us:.1f} µs window — no data after the gate closes"
            if past_end
            else ""
        )
    )
    ax_gc.legend(fontsize=8, loc="upper right", ncol=3)
    ax_gc.grid(True, alpha=0.3)
    stamp_front_end(fig, stamp)
    plt.savefig(path, dpi=150, bbox_inches="tight")
    print("Plot saved: " + path)


def show_figures(no_show):
    """Pop the figures up, last: show() blocks until the windows are closed."""
    if no_show:
        return
    if plt.get_backend().lower() == "agg":
        print("\nNon-interactive matplotlib backend (Agg) — open the PNGs manually.")
    else:
        print("\nShowing plots — close all four windows to exit.")
        plt.show()


def main():
    args = parse_args()
    window_s = parse_time_seconds(args.duration, "ADA4356_DURATION")
    os.makedirs(args.outdir, exist_ok=True)
    run = next_run_index(args.outdir)
    paths = run_paths(args.outdir, run)
    log_fh = open(paths["log"], "w", encoding="utf-8")
    sys.stdout = _Tee(sys.stdout, log_fh)
    sys.stderr = _Tee(sys.stderr, log_fh)
    dev = adi.ada4356_quad(uri=args.uri)
    # Front end first: the gain and filter pins drive analog switches, so they
    # settle before the buffer and the TDD core are touched.
    apply_front_end(dev, args.gain, args.filter)
    fs = dev.sampling_frequency
    view_samples = view_window(window_s, fs, args.samples)
    dev.rx_buffer_size = args.samples
    align_mode = parse_align_mode(args.align)
    gate_on_cycles = args.samples
    if align_mode != "off":
        # An aligned capture is ALIGN_GUARD samples longer so the shift can be
        # sliced out; the gate has to stay open for those samples too.
        from adi.ada4356_quad import ALIGN_GUARD

        gate_on_cycles += ALIGN_GUARD
    on_raw, off_raw = setup_tdd(dev, args.frame, args.samples, gate_on_cycles)
    time.sleep(0.3)
    if align_mode != "off":
        boot_align(dev, align_mode, args.tone, fs)
    data = capture(dev)
    raw = [ch.astype(np.float64) for ch in data]
    ac = [x - np.mean(x) for x in raw]
    f0 = estimate_f0(ac[0], args.tone, fs)
    # Every channel is fitted at the SAME f0 so the phases are comparable.
    fits = [fit_at(x, f0, fs) for x in raw]
    print_capture_info(fs, args.samples, args.tone, f0, run)
    stamp = report_front_end(dev)
    quality = signal_quality(raw, data, fits, f0, fs)
    print_quality_volts(quality)
    print_clipping(raw)
    print_harmonics(quality)
    acfs, acf_summary, split, sig_bound = residual_whiteness(fits, f0, fs)
    skews = channel_skew(fits, f0, fs)
    stats = raw_stats(data)
    if not args.csv:
        print(
            "\nCSV export skipped (set ADA4356_CSV=1 to write the sample "
            "and metrics CSVs; the sample file is the slow part of a run)."
        )
    else:
        write_samples_csv(paths["samples"], args.uri, fs, args.tone, f0, data)
        write_metrics_csv(
            paths["metrics"], fs, f0, quality, acf_summary, split, skews, stats
        )
    periods = plot_periods(args.periods, paths["main"], paths["gate"])
    plot_overview(paths, run, ac, quality, f0, fs, args.tone, periods, stamp)
    plot_spectra(paths["thd"], run, ac, quality, f0, fs, stamp)
    plot_autocorrelation(paths["diag"], run, acfs, acf_summary, sig_bound, stamp)
    plot_gate(paths["gate"], run, ac, quality, fs, view_samples, on_raw, off_raw, stamp)
    show_figures(args.no_show)
    print(f"\nConsole log saved: {paths['log']}")
    sys.stdout.flush()
    sys.stderr.flush()
    sys.stdout = sys.__stdout__
    sys.stderr = sys.__stderr__
    log_fh.close()


if __name__ == "__main__":
    main()
