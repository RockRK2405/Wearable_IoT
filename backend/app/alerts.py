"""Live alert episodes with debounce (open after N s, close after M s clear)."""
from __future__ import annotations

from collections import OrderedDict
from datetime import datetime, timezone

from .detection import NOTES, SEVERITY, Finding


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class AlertManager:
    def __init__(self, get_rules, max_keep: int = 300):
        self.get_rules = get_rules
        self.max_keep = max_keep
        self.active: dict[tuple, dict] = {}
        self.pending: dict[tuple, tuple[float, float]] = {}  # key -> (seconds seen, first ts)
        self.absent: dict[tuple, float] = {}
        self.episodes: "OrderedDict[str, dict]" = OrderedDict()
        self._seq = 0

    def recent(self, n: int = 100) -> list[dict]:
        return list(self.episodes.values())[-n:]

    def update(self, pid: str, dev: str, ts: float, dt: float, findings: list[Finding],
               hr: float | None, spo2: float | None, data_source: str) -> list[dict]:
        rules = self.get_rules()
        out: list[dict] = []
        seen = {f.code: f for f in findings}

        for code, f in seen.items():
            key = (pid, code)
            self.absent.pop(key, None)
            ep = self.active.get(key)
            if ep is None:
                secs, first = self.pending.get(key, (0.0, ts))
                secs += dt
                self.pending[key] = (secs, first)
                if secs >= rules.open_after_s - 1e-9:
                    self._seq += 1
                    ep = {
                        "id": f"A{self._seq:06d}", "patient_id": pid, "device_id": dev, "code": code,
                        "category": f.category, "severity": f.severity, "message": f.message,
                        "value": f.value, "limit": f.limit, "started_at": iso(first),
                        "started_ms": int(first * 1000), "ended_at": None, "ended_ms": None,
                        "status": "active", "data_source": data_source, "hr_bpm": hr, "spo2_pct": spo2,
                        "note": NOTES[f.category], "clinically_validated": False,
                    }
                    self.active[key] = ep
                    self.pending.pop(key, None)
                    self.episodes[ep["id"]] = ep
                    out.append(dict(ep))
            else:
                if SEVERITY[f.severity] > SEVERITY[ep["severity"]]:
                    ep.update(severity=f.severity, message=f.message, value=f.value, limit=f.limit,
                              hr_bpm=hr, spo2_pct=spo2)
                    out.append(dict(ep))

        for key in [k for k in self.pending if k[0] == pid and k[1] not in seen]:
            del self.pending[key]
        for key in [k for k in self.active if k[0] == pid and k[1] not in seen]:
            self.absent[key] = self.absent.get(key, 0.0) + dt
            if self.absent[key] >= rules.close_after_s - 1e-9:
                ep = self.active.pop(key)
                self.absent.pop(key, None)
                ep.update(status="resolved", ended_at=iso(ts), ended_ms=int(ts * 1000))
                out.append(dict(ep))

        while len(self.episodes) > self.max_keep:
            oldest = next(iter(self.episodes))
            if self.episodes[oldest]["status"] == "active":
                break
            self.episodes.pop(oldest)
        return out
