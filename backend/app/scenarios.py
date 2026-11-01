"""Scenario definitions shared by the live simulator and the dataset generator."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

SCENARIOS = [
    "normal", "sleep", "light_activity", "exercise", "tachycardia", "bradycardia", "hypoxia",
    "seizure_clonic", "febrile_seizure",
    "irregular_rhythm", "pause", "leads_off", "finger_off", "motion_artifact",
]


@dataclass
class Baseline:
    hr: float
    hrv_cv: float
    spo2: float
    amp: float
    temp: float = 36.8
    vib: float = 0.05

    @classmethod
    def random(cls, rng: np.random.Generator) -> "Baseline":
        return cls(hr=float(rng.uniform(58, 82)), hrv_cv=float(rng.uniform(0.03, 0.06)),
                    spo2=float(rng.uniform(96, 99)), amp=float(rng.uniform(0.7, 1.0)),
                    temp=float(rng.uniform(36.5, 37.1)), vib=float(rng.uniform(0.03, 0.08)))


@dataclass
class Params:
    hr: float
    rr_cv: float
    spo2: float
    temp: float = 36.8
    vib: float = 0.05
    leads_off: bool = False
    finger_on: bool = True
    noise_mv: float = 0.015
    pause: bool = False
    label: str = "normal"


def target_params(name: str, b: Baseline, rng: np.random.Generator) -> Params:
    p = Params(hr=b.hr, rr_cv=b.hrv_cv, spo2=b.spo2, temp=b.temp, vib=b.vib, label=name)
    if name == "sleep":
        p.hr = b.hr * 0.88
        p.spo2 = b.spo2 - 0.5
        p.vib = 0.02
    elif name == "light_activity":
        p.hr = b.hr + rng.uniform(15, 30)
        p.vib = rng.uniform(0.15, 0.35)
    elif name == "exercise":
        p.hr = rng.uniform(120, 150)
        p.vib = rng.uniform(0.65, 1.05)
        p.spo2 = rng.uniform(96, 99)
        p.temp = rng.uniform(36.9, 37.4)
    elif name == "tachycardia":
        p.hr = rng.uniform(112, 150)
    elif name == "bradycardia":
        p.hr = rng.uniform(36, 48)
    elif name == "hypoxia":
        p.spo2 = rng.uniform(84, 92.5)
        p.hr = b.hr + rng.uniform(8, 22)
    elif name == "seizure_clonic":
        p.hr = rng.uniform(108, 138)
        p.spo2 = rng.uniform(79, 88.5)
        p.vib = rng.uniform(0.70, 1.30)
        p.temp = rng.uniform(38.2, 39.8)
        p.noise_mv = float(rng.uniform(0.15, 0.35))
    elif name == "febrile_seizure":
        p.hr = rng.uniform(115, 142)
        p.temp = rng.uniform(39.2, 40.5)
        p.vib = rng.uniform(0.60, 1.00)
        p.spo2 = rng.uniform(86, 92)
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
        p.vib = float(rng.uniform(0.3, 0.6))
    p.spo2 = float(min(p.spo2, 99.5))
    return p
