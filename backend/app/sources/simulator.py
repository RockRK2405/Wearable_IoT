"""SIMULATED data source. Everything emitted here carries "sim": 1 and is labelled
'simulated' end-to-end (UI + CSV). It is never presented as real device data."""
from __future__ import annotations

import asyncio
import logging

import numpy as np

from ..ecg_synth import add_noise, next_rr, render
from ..scenarios import Baseline, Params, target_params
from ..settings import Settings

log = logging.getLogger("simulator")

TICK_S = 0.1

DEMO_PROGRAMS = {
    0: [("normal", 40), ("tachycardia", 30), ("normal", 35), ("seizure_clonic", 25), ("normal", 35),
        ("hypoxia", 35), ("normal", 40), ("irregular_rhythm", 30), ("normal", 35), ("bradycardia", 30),
        ("normal", 30), ("pause", 4), ("normal", 35), ("leads_off", 12), ("normal", 25), ("finger_off", 10), ("normal", 25)],
    1: [("normal", 90), ("seizure_clonic", 25), ("normal", 60), ("hypoxia", 30), ("normal", 120), ("tachycardia", 25), ("normal", 100)],
}
NAMES = ["Simulated Patient A", "Simulated Patient B", "Simulated Patient C",
         "Simulated Patient D", "Simulated Patient E", "Simulated Patient F"]
RANDOM_WEIGHTS = {"normal": 0.58, "seizure_clonic": 0.04, "exercise": 0.03, "tachycardia": 0.06, "bradycardia": 0.04, "hypoxia": 0.07,
                  "irregular_rhythm": 0.05, "pause": 0.02, "leads_off": 0.05, "finger_off": 0.04, "motion_artifact": 0.02}


class SimPatient:
    def __init__(self, idx: int, s: Settings, rng: np.random.Generator):
        self.idx = idx
        self.s = s
        self.rng = rng
        self.pid = f"P-{1001 + idx}"
        self.name = NAMES[idx % len(NAMES)]
        self.dev = f"SIM-ESP32-{idx + 1:02d}"
        self.fs = s.ecg_fs
        self.base = Baseline.random(rng)
        self.params = target_params("normal", self.base, rng)
        self.hr, self.spo2 = self.params.hr, self.params.spo2
        self.temp, self.vib = self.params.temp, self.params.vib
        self.t_idx = 0
        self.sim_t = 0.0
        self.beats: list[tuple[float, float]] = []
        self.next_beat = float(rng.uniform(0.2, 0.8))
        self.prev_rr = 60.0 / self.hr
        self.scenario = "normal"
        self.pending_pause = False
        self.inject: tuple[str, float] | None = None
        self.rand_until = 0.0
        self.rand_name = "normal"
        self.program = DEMO_PROGRAMS.get(idx, [("normal", 600)]) if s.sim_scenario == "demo" else None
        self.offset = float(rng.uniform(0, 60)) if idx else 0.0
        self.tick = 0

    # ----- scenario control -----
    def inject_scenario(self, name: str, duration_s: float) -> None:
        self.inject = (name, self.sim_t + duration_s)

    def _scenario_name(self) -> str:
        if self.inject:
            if self.sim_t < self.inject[1]:
                return self.inject[0]
            self.inject = None
        mode = self.s.sim_scenario
        if mode == "normal":
            return "normal"
        if mode == "random":
            if self.sim_t >= self.rand_until:
                names = list(RANDOM_WEIGHTS)
                w = np.array(list(RANDOM_WEIGHTS.values()))
                self.rand_name = str(self.rng.choice(names, p=w / w.sum()))
                self.rand_until = self.sim_t + float(self.rng.uniform(15, 45))
            return self.rand_name
        total = sum(d for _, d in self.program)
        e = (self.sim_t + self.offset) % total
        for name, dur in self.program:
            if e < dur:
                return name
            e -= dur
        return "normal"

    def _update_params(self) -> None:
        name = self._scenario_name()
        if name != self.scenario:
            self.scenario = name
            self.params = target_params(name, self.base, self.rng)
            if self.params.pause:
                self.pending_pause = True
        a_hr = 1 - np.exp(-TICK_S / 5.0)
        a_sp = 1 - np.exp(-TICK_S / 8.0)
        a_temp = 1 - np.exp(-TICK_S / 10.0)
        a_vib = 1 - np.exp(-TICK_S / 2.0)
        self.hr += (self.params.hr - self.hr) * a_hr
        self.spo2 += (self.params.spo2 - self.spo2) * a_sp
        self.temp += (self.params.temp - self.temp) * a_temp
        self.vib += (self.params.vib - self.vib) * a_vib

    # ----- signal generation -----
    def _schedule_beats(self, until: float) -> None:
        while self.next_beat < until:
            if self.pending_pause:
                rr = float(self.rng.uniform(2.3, 3.4))
                self.pending_pause = False
            else:
                rr = next_rr(self.rng, self.hr, self.params.rr_cv, self.next_beat)
            self.beats.append((self.next_beat, self.prev_rr))
            self.prev_rr = rr
            self.next_beat += rr

    def _ecg_batch(self) -> dict:
        n = int(self.fs * TICK_S)
        t0 = self.t_idx / self.fs
        self._schedule_beats(t0 + TICK_S + 0.7)
        t = t0 + np.arange(n) / self.fs
        if self.params.leads_off:
            mv = self.rng.normal(0, 0.002, n)
        else:
            mv = add_noise(render(t, [b for b in self.beats if b[0] > t0 - 0.7], self.base.amp), t, self.rng,
                           self.params.noise_mv)
        volts = self.s.adc_mid_v + mv * self.s.ecg_gain / 1000.0
        raw = np.clip(np.round(volts / self.s.adc_vref * 4095), 0, 4095).astype(int)
        self.t_idx += n
        self.beats = [b for b in self.beats if b[0] > t0 - 1.5]
        return {"type": "ecg", "dev": self.dev, "pid": self.pid, "pname": self.name, "sim": 1,
                "ts": int(self.sim_t * 1000), "fs": self.fs, "lo": int(self.params.leads_off), "s": raw.tolist()}

    def _vitals(self) -> dict:
        p = self.params
        spo2 = min(100.0, max(60.0, round(self.spo2 + float(self.rng.normal(0, 0.4)))))
        pulse = round(self.hr + float(self.rng.normal(0, 1.2)))
        temp = round(self.temp + float(self.rng.normal(0, 0.05)), 1)
        vib = max(0.01, round(self.vib + float(self.rng.normal(0, 0.02)), 2))
        return {"type": "vitals", "dev": self.dev, "pid": self.pid, "pname": self.name, "sim": 1,
                "ts": int(self.sim_t * 1000), "finger": int(p.finger_on),
                "spo2": spo2 if p.finger_on else None, "spo2_valid": int(p.finger_on),
                "pulse": pulse if p.finger_on else None, "pulse_valid": int(p.finger_on),
                "temp": temp, "vib": vib}

    def step(self) -> list[dict]:
        self._update_params()
        out = [self._ecg_batch()]
        self.tick += 1
        self.sim_t += TICK_S
        if self.tick % int(1 / TICK_S) == 0:
            out.append(self._vitals())
        return out


class SimulatorSource:
    def __init__(self, settings: Settings, pipeline):
        self.s = settings
        self.pipeline = pipeline
        seed = settings.sim_seed
        self.patients = [SimPatient(i, settings, np.random.default_rng(None if seed is None else seed + i))
                         for i in range(settings.sim_patients)]
        self._task: asyncio.Task | None = None

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        self._task = loop.create_task(self._run())
        log.info("SIMULATED mode: %d simulated patients (no real sensor data)", len(self.patients))

    def stop(self) -> None:
        if self._task:
            self._task.cancel()

    def status(self) -> dict:
        return {"type": "simulator", "connected": True, "detail": f"{len(self.patients)} simulated patients"}

    def inject(self, pid: str, scenario: str, duration_s: float) -> bool:
        for p in self.patients:
            if p.pid == pid:
                p.inject_scenario(scenario, duration_s)
                return True
        return False

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        nxt = loop.time()
        while True:
            for p in self.patients:
                for msg in p.step():
                    self.pipeline.handle(msg)
            nxt += TICK_S
            delay = nxt - loop.time()
            if delay < -1.0:  # fell far behind (e.g. laptop sleep): resync
                nxt = loop.time()
            await asyncio.sleep(max(0.0, delay))
