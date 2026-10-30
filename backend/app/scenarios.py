"""Scenario definitions shared by the live simulator and the dataset generator."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

SCENARIOS = [
    "normal", "sleep", "light_activity", "tachycardia", "bradycardia", "hypoxia",
    "irregular_rhythm", "pause", "leads_off", "finger_off", "motion_artifact",
]


@dataclass
class Baseline:
    hr: float
    hrv_cv: float
    spo2: float
    amp: float

    @classmethod
    def random(cls, rng: np.random.Generator) -> "Baseline":
        return cls(hr=float(rng.uniform(58, 82)), hrv_cv=float(rng.uniform(0.03, 0.06)),
                   spo2=float(rng.uniform(96, 99)), amp=float(rng.uniform(0.7, 1.0)))


@dataclass
class Params:
    hr: float
    rr_cv: float
    spo2: float
    leads_off: bool = False
    finger_on: bool = True
    noise_mv: float = 0.015
    pause: bool = False
    label: str = "normal"


def target_params(name: str, b: Baseline, rng: np.random.Generator) -> Params:
    p = Params(hr=b.hr, rr_cv=b.hrv_cv, spo2=b.spo2, label=name)
    if name == "sleep":
        p.hr = b.hr * 0.88
        p.spo2 = b.spo2 - 0.5
    elif name == "light_activity":
        p.hr = b.hr + rng.uniform(15, 30)
    elif name == "tachycardia":
        p.hr = rng.uniform(112, 150)
    elif name == "bradycardia":
        p.hr = rng.uniform(36, 48)
    elif name == "hypoxia":
        p.spo2 = rng.uniform(84, 92.5)
        p.hr = b.hr + rng.uniform(8, 22)
    elif name == "irregular_rhythm":
        p.rr_cv = rng.uniform(0.22, 0.38)
        p.hr = b.hr + rng.uniform(10, 40)
    elif name == "pause":
        p.pause = True
    elif name == "leads_off":
        p.leads_off = True
    elif name == "finger_off":
        p.finger_on = False
    elif name == "motion_artifact":
        p.noise_mv = float(rng.uniform(0.25, 0.5))
    p.spo2 = float(min(p.spo2, 99.5))
    return p
