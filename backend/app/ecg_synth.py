"""Synthetic ECG generation (sum of Gaussians per beat). Used ONLY by the simulator
and the dataset generator - never for real-sensor mode."""
from __future__ import annotations

import numpy as np

# name: (centre offset from R peak [s], amplitude [mV], sigma [s])
_WAVES = {
    "P": (-0.17, 0.10, 0.025),
    "Q": (-0.030, -0.10, 0.010),
    "R": (0.0, 1.0, 0.013),
    "S": (0.032, -0.22, 0.011),
    "T": (0.26, 0.28, 0.050),
}


def beat_template(t_rel: np.ndarray, rr_prev_s: float, amp: float = 0.8) -> np.ndarray:
    scale = float(np.sqrt(np.clip(rr_prev_s, 0.35, 1.5)))
    out = np.zeros_like(t_rel)
    for name, (c, a, s) in _WAVES.items():
        if name == "T":
            c = c * scale
        out += a * amp * np.exp(-0.5 * ((t_rel - c) / s) ** 2)
    return out


def render(t: np.ndarray, beats: list[tuple[float, float]], amp: float = 0.8) -> np.ndarray:
    """Render ECG (mV) at sample times t (sorted). beats = [(r_time_s, rr_prev_s)]."""
    y = np.zeros_like(t)
    for b, rr in beats:
        i0 = int(np.searchsorted(t, b - 0.4))
        i1 = int(np.searchsorted(t, b + 0.6))
        if i1 > i0:
            y[i0:i1] += beat_template(t[i0:i1] - b, rr, amp)
    return y


def next_rr(rng: np.random.Generator, hr: float, cv: float, t: float) -> float:
    mean = 60.0 / max(hr, 20.0)
    if cv >= 0.15:  # irregularly irregular
        v = mean * (1.0 + cv * rng.standard_normal())
    else:  # sinus rhythm with respiratory modulation
        v = mean * (1.0 + 0.03 * np.sin(2 * np.pi * 0.25 * t) + cv * rng.standard_normal())
    return float(np.clip(v, 0.3, 2.2))


def add_noise(y: np.ndarray, t: np.ndarray, rng: np.random.Generator, white: float = 0.015) -> np.ndarray:
    wander = 0.05 * np.sin(2 * np.pi * 0.25 * t + rng.uniform(0, 6.28))
    mains = 0.01 * np.sin(2 * np.pi * 50 * t)
    return y + wander + mains + rng.normal(0, white, y.shape)
