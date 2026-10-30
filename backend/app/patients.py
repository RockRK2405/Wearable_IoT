"""Patient profiles: emergency contact + location, persisted to config/patients.json.

This is the data the emergency notifier needs when an abnormal episode opens:
who to call/text, and where the patient is. It is edited from the dashboard
(Settings panel) and validated before it is saved.
"""
from __future__ import annotations

import json
import re
import threading
from pathlib import Path

from pydantic import BaseModel, Field, field_validator

# E.164-ish: a leading +, then 7-15 digits. We keep it permissive but sane.
_PHONE = re.compile(r"^\+?[0-9][0-9\- ]{6,18}[0-9]$")


def normalise_phone(v: str) -> str:
    v = (v or "").strip()
    if not v:
        return ""
    compact = re.sub(r"[\s\-]", "", v)
    if not compact.startswith("+"):
        compact = "+" + compact
    return compact


class EmergencyContact(BaseModel):
    contact_name: str = Field("", max_length=80)
    contact_phone: str = Field("", max_length=24)
    notify_enabled: bool = True
    min_severity: str = Field("critical")  # warning | critical
    call_enabled: bool = False             # also place a voice call, not only SMS
    cooldown_s: float = Field(300, ge=30, le=3600)

    @field_validator("contact_phone")
    @classmethod
    def _phone(cls, v: str) -> str:
        v = normalise_phone(v)
        if v and not _PHONE.match(v):
            raise ValueError("contact_phone must look like +919812345678")
        return v

    @field_validator("min_severity")
    @classmethod
    def _sev(cls, v: str) -> str:
        v = (v or "critical").lower()
        if v not in ("warning", "critical"):
            raise ValueError("min_severity must be 'warning' or 'critical'")
        return v


class Location(BaseModel):
    lat: float | None = Field(None, ge=-90, le=90)
    lng: float | None = Field(None, ge=-180, le=180)
    label: str = Field("", max_length=160)

    @property
    def has_coords(self) -> bool:
        return self.lat is not None and self.lng is not None

    def maps_url(self) -> str | None:
        if self.has_coords:
            return f"https://www.google.com/maps?q={self.lat:.6f},{self.lng:.6f}"
        if self.label.strip():
            from urllib.parse import quote_plus
            return "https://www.google.com/maps/search/?api=1&query=" + quote_plus(self.label.strip())
        return None

    def describe(self) -> str:
        parts = []
        if self.label.strip():
            parts.append(self.label.strip())
        if self.has_coords:
            parts.append(f"{self.lat:.5f}, {self.lng:.5f}")
        return " - ".join(parts) if parts else "location not set"


class PatientProfile(BaseModel):
    patient_id: str
    name: str = Field("", max_length=80)
    emergency: EmergencyContact = EmergencyContact()
    location: Location = Location()


class PatientStore:
    def __init__(self, path: Path, default_id: str = "P-REAL-01", default_name: str = "Patient"):
        self.path = Path(path)
        self.default_id = default_id
        self.default_name = default_name
        self._lock = threading.Lock()
        self._p: dict[str, PatientProfile] = self._load()

    def _load(self) -> dict[str, PatientProfile]:
        out: dict[str, PatientProfile] = {}
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text())
                for pid, body in raw.items():
                    body["patient_id"] = pid
                    out[pid] = PatientProfile.model_validate(body)
            except Exception:
                out = {}
        if self.default_id not in out:
            out[self.default_id] = PatientProfile(patient_id=self.default_id, name=self.default_name)
            self._save(out)
        return out

    def _save(self, data: dict[str, PatientProfile]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        body = {pid: p.model_dump(exclude={"patient_id"}) for pid, p in data.items()}
        self.path.write_text(json.dumps(body, indent=2) + "\n")

    def ensure(self, pid: str, name: str = "") -> PatientProfile:
        """Called when a new device/patient appears, so there is always a profile to edit."""
        with self._lock:
            if pid not in self._p:
                self._p[pid] = PatientProfile(patient_id=pid, name=name or pid)
                self._save(self._p)
            elif name and not self._p[pid].name:
                self._p[pid].name = name
                self._save(self._p)
            return self._p[pid]

    def get(self, pid: str) -> PatientProfile | None:
        return self._p.get(pid)

    def all(self) -> list[PatientProfile]:
        return list(self._p.values())

    def update(self, pid: str, patch: dict) -> PatientProfile:
        with self._lock:
            cur = self._p.get(pid) or PatientProfile(patient_id=pid, name=pid)
            merged = cur.model_dump(exclude={"patient_id"})
            for k, v in patch.items():
                if k in ("emergency", "location") and isinstance(v, dict) and isinstance(merged.get(k), dict):
                    merged[k] = {**merged[k], **v}
                elif k != "patient_id":
                    merged[k] = v
            merged["patient_id"] = pid
            new = PatientProfile.model_validate(merged)
            self._p[pid] = new
            self._save(self._p)
            return new
