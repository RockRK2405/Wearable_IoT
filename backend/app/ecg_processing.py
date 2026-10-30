"""R-peak detection and RR-interval metrics (Pan-Tompkins-style, simplified).

This produces *screening metrics only*. It is not a clinically validated ECG analysis.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache

import numpy as np
from scipy.ndimage import uniform_filter1d
from scipy.signal import butter, find_peaks, sosfiltfilt


@dataclass
class EcgMetrics:
    n_beats: int = 0
    hr_bpm: float | None = None
    rr_ms: list[float] = field(default_factory=list)
    rr_mean_ms: float | None = None
    sdnn_ms: float | None = None
    rmssd_ms: float | None = None
    rr_cv: float | None = None
    max_rr_ms: float | None = None
    quality: float = 0.0
    r_peaks: np.ndarray = field(default_factory=lambda: np.array([], dtype=int))


@lru_cache(maxsize=8)
def _bandpass(fs: int):
    return butter(2, [5, 18], btype="band", fs=fs, output="sos")


@lru_cache(maxsize=8)
def _highpass(fs: int):
    return butter(2, 40, btype="highpass", fs=fs, output="sos")


def analyze(ecg_mv, fs: int, clip_fraction: float = 0.0) -> EcgMetrics:
    x = np.asarray(ecg_mv, dtype=float)
    if x.size < int(2 * fs) or not np.all(np.isfinite(x)):
        return EcgMetrics()
    x = x - np.mean(x)
    if np.std(x) < 0.01:  # flat line
        return EcgMetrics(quality=0.0)

    f = sosfiltfilt(_bandpass(fs), x)
    d = np.gradient(f)
    e = uniform_filter1d(d * d, size=max(3, int(0.10 * fs)))
    cand, _ = find_peaks(e, distance=max(1, int(0.25 * fs)))
    if cand.size == 0:
        return EcgMetrics(quality=0.1)
    h = e[cand]
    cand = cand[h >= 0.25 * np.percentile(h, 90)]

    lo, hi = int(0.05 * fs), int(0.08 * fs)
    refined = []
    for p in cand:
        a, b = max(0, p - lo), min(len(f), p + hi)
        refined.append(a + int(np.argmax(np.abs(f[a:b]))))
    refined = sorted(set(refined))
    peaks: list[int] = []
    for p in refined:  # drop double detections inside 250 ms, keep the larger
        if peaks and p - peaks[-1] < 0.25 * fs:
            if abs(f[p]) > abs(f[peaks[-1]]):
                peaks[-1] = p
        else:
            peaks.append(p)
    peaks_a = np.array(peaks, dtype=int)
    n = len(peaks_a)
    if n == 0:
        return EcgMetrics(quality=0.1)

    rr = np.diff(peaks_a) / fs * 1000.0
    trailing = (len(x) - 1 - peaks_a[-1]) / fs * 1000.0
    leading = peaks_a[0] / fs * 1000.0
    max_rr = float(max(rr.max() if rr.size else 0.0, trailing, leading))

    m = EcgMetrics(n_beats=n, rr_ms=[float(v) for v in rr], max_rr_ms=max_rr, r_peaks=peaks_a)
    if rr.size >= 2:
        m.rr_mean_ms = float(rr.mean())
        m.hr_bpm = 60000.0 / m.rr_mean_ms
    if rr.size >= 3:
        m.sdnn_ms = float(rr.std(ddof=1))
        m.rmssd_ms = float(np.sqrt(np.mean(np.diff(rr) ** 2)))
        m.rr_cv = m.sdnn_ms / m.rr_mean_ms

    # --- signal quality heuristic (0..1) ---
    q = 1.0
    amps = np.abs(f[peaks_a])
    if n >= 4 and amps.mean() > 0:
        q -= min(0.4, max(0.0, float(amps.std() / amps.mean()) - 0.35))
    ramp = float(np.median(np.abs(x[peaks_a]))) or 1e-6
    hp = sosfiltfilt(_highpass(fs), x)
    q -= min(0.7, float(np.std(hp) / ramp) * 2.5)
    q -= min(0.6, clip_fraction * 6)
    if rr.size:
        q *= 0.5 + 0.5 * float(np.mean((rr >= 250) & (rr <= 6000)))
    if n < 3:
        q = min(q, 0.3)
    m.quality = float(np.clip(q, 0.0, 1.0))
    return m
