"""Data acquisition -> processing -> abnormality detection -> CSV storage -> alerts -> live push."""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque

import numpy as np

from .alerts import AlertManager, iso
from .detection import Measurements, evaluate, format_codes
from .ecg_processing import analyze
from .hub import Hub
from .settings import Settings
from .storage import CsvLogger
from .thresholds import ThresholdStore

log = logging.getLogger("pipeline")
OFFLINE_AFTER_S = 30.0


class DeviceState:
    def __init__(self, dev: str, pid: str, name: str, fs: int, cap_s: float):
        self.dev, self.pid, self.name, self.fs = dev, pid, name, fs
        self.raw: deque[int] = deque(maxlen=int(cap_s * fs))
        self.leads_off = False
        self.is_sim = False
        self.last_ecg_rx = 0.0
        self.last_vitals_rx = 0.0
        self.vitals: dict = {}
        self.offline = False
        self.last_tick = 0.0


class Pipeline:
    def __init__(self, settings: Settings, thresholds: ThresholdStore, hub: Hub,
                 patients=None, notifier=None):
        self.s = settings
        self.thresholds = thresholds
        self.hub = hub
        self.patient_store = patients   # PatientStore | None
        self.notifier = notifier        # EmergencyNotifier | None
        self.devices: dict[str, DeviceState] = {}
        self.alerts = AlertManager(lambda: thresholds.get().alerting)
        self.latest: dict[str, dict] = {}
        self.recent: dict[str, deque] = {}
        self.loggers: dict[str, CsvLogger] = {}
        self.stats = {"messages": 0, "parse_errors": 0, "rows_logged": 0, "notifications": 0}
        self.loop: asyncio.AbstractEventLoop | None = None

    # ---------- ingestion ----------
    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop

    def submit_threadsafe(self, msg: dict) -> None:
        if self.loop:
            self.loop.call_soon_threadsafe(self.handle, msg)

    def raw_to_mv(self, raw) -> np.ndarray:
        raw = np.asarray(raw, dtype=float)
        volts = raw / 4095.0 * self.s.adc_vref
        return (volts - self.s.adc_mid_v) / self.s.ecg_gain * 1000.0

    def handle(self, msg: dict) -> None:
        try:
            self._handle(msg)
        except Exception:
            log.exception("failed to process message")

    def _handle(self, msg: dict) -> None:
        now = time.time()
        self.stats["messages"] += 1
        dev = msg["dev"]
        pid = msg.get("pid") or self.s.patient_id
        st = self.devices.get(dev)
        if st is None:
            name = msg.get("pname") or (self.s.patient_name if not msg.get("sim") else pid)
            st = self.devices[dev] = DeviceState(dev, pid, name, msg.get("fs", self.s.ecg_fs), self.s.ecg_window_s * 1.5)
            log.info("new device %s -> patient %s (%s)", dev, pid, "simulated" if msg.get("sim") else "REAL")
            if self.patient_store is not None and not msg.get("sim"):
                self.patient_store.ensure(pid, name)
        st.is_sim = bool(msg.get("sim"))

        if msg["type"] == "ecg":
            fs = msg["fs"]
            if fs != st.fs:
                st.fs = fs
                st.raw = deque(maxlen=int(self.s.ecg_window_s * 1.5 * fs))
            lo = msg["lo"]
            if st.leads_off and not lo:
                st.raw.clear()  # restart analysis window after re-attaching electrodes
            st.leads_off = lo
            st.last_ecg_rx = now
            if lo:
                samples = [0.0] * len(msg["s"])
            else:
                st.raw.extend(msg["s"])
                samples = [round(float(v), 3) for v in self.raw_to_mv(msg["s"])]
            self.hub.publish({"type": "ecg", "patient_id": st.pid, "fs": fs, "leads_off": lo,
                              "data_source": "simulated" if st.is_sim else "real", "samples": samples})
        else:
            st.last_vitals_rx = now
            st.vitals = {
                "spo2": msg.get("spo2"),
                "pulse": msg.get("pulse"),
                "finger": msg.get("finger"),
                "temp": msg.get("temp"),
                "vib": msg.get("vib"),
            }

    # ---------- periodic reading ----------
    async def run_ticks(self) -> None:
        while True:
            await asyncio.sleep(self.s.reading_interval_s)
            now = time.time()
            for st in list(self.devices.values()):
                try:
                    self._tick(st, now)
                except Exception:
                    log.exception("tick failed for %s", st.dev)

    def _tick(self, st: DeviceState, now: float) -> None:
        silent = now - max(st.last_ecg_rx, st.last_vitals_rx)
        if silent > OFFLINE_AFTER_S:
            if not st.offline:
                st.offline = True
                self.hub.publish({"type": "device_state", "patient_id": st.pid, "online": False})
            return
        if st.offline:
            st.offline = False
            self.hub.publish({"type": "device_state", "patient_id": st.pid, "online": True})
        dt = (now - st.last_tick) if st.last_tick else self.s.reading_interval_s
        st.last_tick = now
        row, findings = self._build_row(st, now)
        source = row["data_source"]
        lg = self.loggers.get(source)
        if lg is None:
            lg = self.loggers[source] = CsvLogger(self.s.simulated_csv if source == "simulated" else self.s.real_csv)
        lg.append(row)
        self.stats["rows_logged"] += 1

        public = {k: v for k, v in row.items() if k != "ecg_snippet"}
        public["patient_name"] = st.name
        public["findings"] = [f.to_dict() for f in findings]
        self.latest[st.pid] = public
        self.recent.setdefault(st.pid, deque(maxlen=900)).append(public)
        self.hub.publish({"type": "reading", **public})
        for ev in self.alerts.update(st.pid, st.dev, now, dt, findings, row["hr_bpm"], row["spo2_pct"], source):
            self.hub.publish({"type": "alert", "alert": ev})
            # Real abnormal episode opening -> notify the emergency contact.
            # (simulated data only notifies when NOTIFY_ON_SIMULATED is on, for demos)
            if self.notifier is not None and (not st.is_sim or self.s.notify_on_simulated):
                rec = self.notifier.maybe_notify(ev, now)
                if rec is not None:
                    self.stats["notifications"] += 1
                    self.hub.publish({"type": "notification", "notification": rec})

    def _build_row(self, st: DeviceState, now: float):
        th = self.thresholds.get()
        fs = st.fs
        ecg_fresh = (now - st.last_ecg_rx) <= self.s.stale_after_s
        vit_fresh = (now - st.last_vitals_rx) <= self.s.stale_after_s + 2
        ready = len(st.raw) >= int(5 * fs)
        if not ecg_fresh:
            ecg_state = "stale"
        elif st.leads_off:
            ecg_state = "leads_off"
        elif not ready:
            ecg_state = "warming"
        else:
            ecg_state = "ok"

        metrics = None
        snippet = ""
        if ecg_state == "ok":
            arr = np.fromiter(st.raw, dtype=float, count=len(st.raw))[-int(self.s.ecg_window_s * fs):]
            clip = float(np.mean((arr <= 1) | (arr >= 4094)))
            metrics = analyze(self.raw_to_mv(arr), fs, clip_fraction=clip)
            step = max(1, fs // 50)
            last = self.raw_to_mv(arr[-(fs // step) * step:]).reshape(-1, step).mean(axis=1)
            snippet = " ".join(str(int(round(v * 100))) for v in last)

        vit = st.vitals if vit_fresh else {}
        m = Measurements(
            ecg_state=ecg_state, vitals_state="ok" if vit_fresh else "stale",
            ecg_hr=metrics.hr_bpm if metrics else None, n_beats=metrics.n_beats if metrics else 0,
            rr_cv=metrics.rr_cv if metrics else None, max_rr_ms=metrics.max_rr_ms if metrics else None,
            ecg_quality=metrics.quality if metrics else None,
            pulse_bpm=vit.get("pulse"), spo2=vit.get("spo2"), finger=vit.get("finger") if vit_fresh else None,
            temperature_c=vit.get("temp"), vibration_intensity=vit.get("vib"),
            rmssd_ms=metrics.rmssd_ms if metrics else None)
        ev = evaluate(m, th)
        finger = vit.get("finger") if vit_fresh else None
        row = {
            "timestamp": iso(now), "epoch_ms": int(now * 1000),
            "data_source": "simulated" if st.is_sim else "real",
            "patient_id": st.pid, "device_id": st.dev,
            "hr_bpm": round(ev.hr_bpm, 1) if ev.hr_bpm is not None else None, "hr_source": ev.hr_source,
            "pulse_bpm": vit.get("pulse"), "spo2_pct": vit.get("spo2") if finger is not False else None,
            "temperature_c": round(vit["temp"], 1) if vit.get("temp") is not None else None,
            "vibration_intensity": round(vit["vib"], 2) if vit.get("vib") is not None else None,
            "seizure_detected": int(ev.seizure_detected),
            "seizure_prob": round(ev.seizure_prob, 3),
            "seizure_class": ev.seizure_class,
            "rr_mean_ms": round(metrics.rr_mean_ms, 1) if metrics and metrics.rr_mean_ms else None,
            "sdnn_ms": round(metrics.sdnn_ms, 1) if metrics and metrics.sdnn_ms else None,
            "rmssd_ms": round(metrics.rmssd_ms, 1) if metrics and metrics.rmssd_ms else None,
            "rr_cv": round(metrics.rr_cv, 3) if metrics and metrics.rr_cv is not None else None,
            "beats_in_window": m.n_beats if metrics else None,
            "ecg_quality": round(metrics.quality, 2) if metrics else None,
            "leads_off": int(st.leads_off), "finger_detected": None if finger is None else int(bool(finger)),
            "ecg_irregular_flag": int(ev.ecg_irregular), "status": ev.status,
            "alert_codes": format_codes(ev.findings), "alert_message": " | ".join(f.message for f in ev.findings),
            "scenario": "", "ecg_fs": fs, "ecg_snippet": snippet,
        }
        return row, ev.findings

    # ---------- snapshots ----------
    def patients(self) -> list[dict]:
        now = time.time()
        out = []
        for st in self.devices.values():
            out.append({"patient_id": st.pid, "name": st.name, "device_id": st.dev,
                        "data_source": "simulated" if st.is_sim else "real",
                        "online": (now - max(st.last_ecg_rx, st.last_vitals_rx)) <= 10,
                        "latest": self.latest.get(st.pid)})
        return out

    def snapshot(self) -> dict:
        return {"type": "snapshot", "mode": self.s.mode, "patients": self.patients(),
                "recent": {pid: list(d)[-600:] for pid, d in self.recent.items()},
                "alerts": self.alerts.recent(100), "thresholds": self.thresholds.get().model_dump()}
