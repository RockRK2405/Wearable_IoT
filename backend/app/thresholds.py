"""Configurable alert thresholds (validated, persisted to config/thresholds.json)."""
from __future__ import annotations

import json
import threading
from pathlib import Path

from pydantic import BaseModel, Field, model_validator


class HeartRateLimits(BaseModel):
    crit_low: float = Field(40, ge=10, le=120)
    warn_low: float = Field(50, ge=10, le=150)
    warn_high: float = Field(100, ge=40, le=250)
    crit_high: float = Field(130, ge=40, le=300)
    valid_min: float = Field(25, ge=0, le=100)
    valid_max: float = Field(250, ge=100, le=400)

    @model_validator(mode="after")
    def _ordered(self):
        if not (self.crit_low < self.warn_low < self.warn_high < self.crit_high):
            raise ValueError("heart_rate: need crit_low < warn_low < warn_high < crit_high")
        return self


class Spo2Limits(BaseModel):
    warn_below: float = Field(94, ge=50, le=100)
    crit_below: float = Field(90, ge=50, le=100)

    @model_validator(mode="after")
    def _ordered(self):
        if not self.crit_below < self.warn_below:
            raise ValueError("spo2: need crit_below < warn_below")
        return self


class EcgLimits(BaseModel):
    rr_cv_warn: float = Field(0.15, gt=0, le=1)
    rr_cv_crit: float = Field(0.30, gt=0, le=2)
    pause_warn_ms: float = Field(2000, ge=500, le=10000)
    pause_crit_ms: float = Field(3000, ge=500, le=15000)
    min_beats: int = Field(6, ge=3, le=30)
    min_quality: float = Field(0.5, ge=0, le=1)

    @model_validator(mode="after")
    def _ordered(self):
        if not self.rr_cv_warn < self.rr_cv_crit:
            raise ValueError("ecg: need rr_cv_warn < rr_cv_crit")
        if not self.pause_warn_ms < self.pause_crit_ms:
            raise ValueError("ecg: need pause_warn_ms < pause_crit_ms")
        return self


class AlertingRules(BaseModel):
    open_after_s: float = Field(3, ge=0, le=120)
    close_after_s: float = Field(5, ge=0, le=300)


class EpilepsyLimits(BaseModel):
    vibration_tremor_warn: float = Field(0.40, ge=0.05, le=5.0)  # m/s^2
    vibration_tremor_crit: float = Field(0.70, ge=0.10, le=10.0) # m/s^2
    temp_warn_low: float = Field(35.5, ge=30.0, le=37.0)        # deg C
    temp_warn_high: float = Field(37.8, ge=36.0, le=41.0)       # deg C
    temp_crit_high: float = Field(39.0, ge=37.0, le=43.0)       # deg C
    seizure_prob_threshold: float = Field(0.60, ge=0.1, le=0.99)


class Thresholds(BaseModel):
    heart_rate: HeartRateLimits = HeartRateLimits()
    spo2: Spo2Limits = Spo2Limits()
    ecg: EcgLimits = EcgLimits()
    alerting: AlertingRules = AlertingRules()
    epilepsy: EpilepsyLimits = EpilepsyLimits()


def _merge(base: dict, patch: dict) -> dict:
    out = dict(base)
    for k, v in patch.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


class ThresholdStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._t = self._load()

    def _load(self) -> Thresholds:
        if self.path.exists():
            try:
                return Thresholds.model_validate(json.loads(self.path.read_text()))
            except Exception:  # corrupt file -> fall back to defaults
                pass
        t = Thresholds()
        self._save(t)
        return t

    def _save(self, t: Thresholds) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(t.model_dump(), indent=2) + "\n")

    def get(self) -> Thresholds:
        return self._t

    def update(self, patch: dict) -> Thresholds:
        with self._lock:
            new = Thresholds.model_validate(_merge(self._t.model_dump(), patch))
            self._t = new
            self._save(new)
            return new

    def reset(self) -> Thresholds:
        with self._lock:
            self._t = Thresholds()
            self._save(self._t)
            return self._t
