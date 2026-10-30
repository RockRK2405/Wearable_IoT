#!/usr/bin/env python3
"""Generate a large SYNTHETIC results.csv (clearly labelled data_source=synthetic).

Nothing in this file comes from a real sensor. Statuses/alerts are produced by the SAME
detection code used by the live pipeline, so synthetic and live data are comparable.

  python scripts/generate_dataset.py                       # 5 patients x 3 days x 30 s
  python scripts/generate_dataset.py --patients 8 --days 7 --interval 30 --seed 7
"""
from __future__ import annotations

import argparse
import csv
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.app.alerts import iso  # noqa: E402
from backend.app.detection import Measurements, evaluate, format_codes  # noqa: E402
from backend.app.ecg_synth import add_noise, next_rr, render  # noqa: E402
from backend.app.scenarios import Baseline, target_params  # noqa: E402
from backend.app.storage import COLUMNS, _fmt  # noqa: E402
from backend.app.thresholds import Thresholds  # noqa: E402

# per-step probability of starting an episode, and (min, max) length in steps
RATES = {"tachycardia": 0.00045, "bradycardia": 0.00025, "hypoxia": 0.0006, "irregular_rhythm": 0.0005,
         "pause": 0.00012, "leads_off": 0.0008, "finger_off": 0.0010, "motion_artifact": 0.0008}
LENGTH = {"tachycardia": (6, 40), "bradycardia": (6, 30), "hypoxia": (6, 50), "irregular_rhythm": (8, 60),
          "pause": (1, 1), "leads_off": (2, 12), "finger_off": (2, 14), "motion_artifact": (2, 10),
          "light_activity": (6, 40)}
WINDOW_S, FS, SNIP_FS = 10.0, 250, 50


def _beats(rng, hr, cv, pause):
    """Beat (R-peak) times over a 10 s analysis window (+margin) with optional single pause."""
    t = -float(rng.uniform(0, 60.0 / hr))
    beats, rr_prev = [], 60.0 / hr
    pause_at = int(rng.integers(3, max(4, int(hr / 60 * 7)))) if pause else -1
    i = 0
    while t < WINDOW_S + 0.7:
        rr = float(rng.uniform(2.3, 3.5)) if i == pause_at else next_rr(rng, hr, cv, t)
        beats.append((t, rr_prev))
        rr_prev = rr
        t += rr
        i += 1
    return beats


def _metrics(beats):
    times = np.array([b[0] for b in beats])
    inwin = times[(times >= 0) & (times <= WINDOW_S)]
    if inwin.size < 3:
        return None
    rr = np.diff(inwin) * 1000.0
    mean = rr.mean()
    sdnn = rr.std(ddof=1)
    max_rr = max(rr.max(), (WINDOW_S - inwin[-1]) * 1000.0, inwin[0] * 1000.0)
    return dict(n=int(inwin.size), mean=mean, hr=60000.0 / mean, sdnn=sdnn,
                rmssd=float(np.sqrt(np.mean(np.diff(rr) ** 2))), cv=sdnn / mean, max_rr=max_rr)


def generate(out: Path, patients=5, days=3, interval_s=30, seed=42, end: datetime | None = None,
             thresholds: Thresholds | None = None) -> dict:
    th = thresholds or Thresholds()
    rng = np.random.default_rng(seed)
    end = (end or datetime.now(timezone.utc)).replace(microsecond=0, second=0)
    steps = int(days * 86400 / interval_s)
    start = end - timedelta(seconds=steps * interval_s)
    out.parent.mkdir(parents=True, exist_ok=True)
    stats = {"rows": 0, "status": {}, "scenario": {}}

    with open(out, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, COLUMNS)
        w.writeheader()
        for pi in range(patients):
            pid, dev = f"P-{2001 + pi}", f"SYN-DEV-{pi + 1:02d}"
            base = Baseline.random(rng)
            risk = float(rng.uniform(0.6, 2.0))
            ep_name, ep_left = None, 0
            for k in range(steps):
                ts = start + timedelta(seconds=k * interval_s)
                hour = (ts.hour + ts.minute / 60 + 5.5) % 24  # local-ish (UTC+5:30) hour of day
                circ = 1 + 0.07 * np.cos(2 * np.pi * (hour - 15) / 24)
                if ep_left <= 0:
                    ep_name = None
                    for name, p in RATES.items():
                        if rng.random() < p * risk:
                            ep_name = name
                            break
                    if ep_name is None and 7 < hour < 21 and rng.random() < 0.004:
                        ep_name = "light_activity"
                    if ep_name:
                        lo, hi = LENGTH[ep_name]
                        ep_left = int(rng.integers(lo, hi + 1))
                scen = ep_name if ep_left > 0 else ("sleep" if hour < 5.5 else "normal")
                ep_left -= 1

                p = target_params(scen, base, rng)
                hr_true = max(30.0, p.hr * circ + float(rng.normal(0, 1.5)))
                beats = _beats(rng, hr_true, p.rr_cv, p.pause)
                mt = _metrics(beats)

                if p.leads_off:
                    ecg_state, quality = "leads_off", 0.0
                elif p.noise_mv > 0.1:
                    ecg_state, quality = "ok", float(rng.uniform(0.15, 0.45))
                else:
                    ecg_state, quality = "ok", float(np.clip(rng.normal(0.92, 0.04), 0.7, 1.0))
                spo2 = float(min(100, round(p.spo2 + rng.normal(0, 0.5))))
                pulse = round(hr_true + float(rng.normal(0, 1.5)))
                m = Measurements(
                    ecg_state=ecg_state, vitals_state="ok",
                    ecg_hr=mt["hr"] if mt and ecg_state == "ok" else None, n_beats=mt["n"] if mt else 0,
                    rr_cv=mt["cv"] if mt else None, max_rr_ms=mt["max_rr"] if mt else None,
                    ecg_quality=quality if ecg_state == "ok" else None,
                    pulse_bpm=pulse if p.finger_on else None, spo2=spo2 if p.finger_on else None,
                    finger=p.finger_on)
                ev = evaluate(m, th)

                # 1-second ECG snippet at 50 Hz (hundredths of mV, integers)
                if p.leads_off:
                    snip = np.zeros(SNIP_FS)
                else:
                    t = (WINDOW_S - 1.0) + np.arange(FS) / FS
                    y = add_noise(render(t, [b for b in beats if WINDOW_S - 1.7 < b[0] < WINDOW_S + 0.7], base.amp),
                                  t, rng, p.noise_mv)
                    snip = y.reshape(SNIP_FS, FS // SNIP_FS).mean(axis=1)
                snippet = " ".join(str(int(round(v * 100))) for v in snip)

                row = {
                    "timestamp": iso(ts.timestamp()), "epoch_ms": int(ts.timestamp() * 1000),
                    "data_source": "synthetic", "patient_id": pid, "device_id": dev,
                    "hr_bpm": round(ev.hr_bpm, 1) if ev.hr_bpm is not None else None, "hr_source": ev.hr_source,
                    "pulse_bpm": m.pulse_bpm, "spo2_pct": m.spo2,
                    "rr_mean_ms": round(mt["mean"], 1) if mt and ecg_state == "ok" else None,
                    "sdnn_ms": round(mt["sdnn"], 1) if mt and ecg_state == "ok" else None,
                    "rmssd_ms": round(mt["rmssd"], 1) if mt and ecg_state == "ok" else None,
                    "rr_cv": round(mt["cv"], 3) if mt and ecg_state == "ok" else None,
                    "beats_in_window": mt["n"] if mt and ecg_state == "ok" else None,
                    "ecg_quality": round(quality, 2) if ecg_state == "ok" else None,
                    "leads_off": int(p.leads_off), "finger_detected": int(p.finger_on),
                    "ecg_irregular_flag": int(ev.ecg_irregular), "status": ev.status,
                    "alert_codes": format_codes(ev.findings),
                    "alert_message": " | ".join(f.message for f in ev.findings),
                    "scenario": scen, "ecg_fs": SNIP_FS, "ecg_snippet": snippet,
                }
                w.writerow({c: _fmt(row.get(c)) for c in COLUMNS})
                stats["rows"] += 1
                stats["status"][ev.status] = stats["status"].get(ev.status, 0) + 1
                stats["scenario"][scen] = stats["scenario"].get(scen, 0) + 1
    stats.update(start=iso(start.timestamp()), end=iso(end.timestamp()), patients=patients, interval_s=interval_s)
    return stats


CARD = """# results.csv - SYNTHETIC dataset

**Every row in `results.csv` is synthetic (`data_source = synthetic`).** It was produced by
`scripts/generate_dataset.py` (seed {seed}) from simulated ECG/vitals. It is NOT patient data and NOT
output of a real sensor. Live logs go to separate files: `live_simulated.csv` (simulator) and
`live_real.csv` (real hardware).

| column | meaning |
|---|---|
| timestamp / epoch_ms | UTC time of the reading |
| data_source | `synthetic` here; `simulated` or `real` in live logs |
| patient_id / device_id | patient and (virtual) device identifiers |
| hr_bpm / hr_source | heart rate used for alerting and where it came from (`ecg` R-peaks, `ppg` SpO2-sensor pulse, `none`) |
| pulse_bpm, spo2_pct | SpO2-sensor pulse and oxygen saturation (blank if no finger) |
| rr_mean_ms, sdnn_ms, rmssd_ms, rr_cv, beats_in_window | R-R interval metrics over a 10 s window |
| ecg_quality | 0..1 signal quality heuristic |
| leads_off, finger_detected | sensor-contact flags |
| ecg_irregular_flag | 1 if a heuristic R-R irregularity / pause rule fired (NOT a diagnosis) |
| status | `normal` / `warning` / `critical` from the configurable thresholds |
| alert_codes / alert_message | `CODE:severity` list separated by `|`, and readable text |
| scenario | ground-truth label of the simulated condition (synthetic data only) |
| ecg_fs / ecg_snippet | 1 s of ECG at `ecg_fs` Hz, integers in hundredths of mV (space-separated) |

Generated: {rows} rows, {patients} patients, {start} -> {end}, one reading every {interval_s} s.
Status mix: {mix}
"""


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(ROOT / "data" / "results.csv"))
    ap.add_argument("--patients", type=int, default=5)
    ap.add_argument("--days", type=float, default=3)
    ap.add_argument("--interval", type=int, default=30, help="seconds between readings")
    ap.add_argument("--seed", type=int, default=42)
    a = ap.parse_args()
    out = Path(a.out)
    st = generate(out, a.patients, a.days, a.interval, a.seed)
    tot = st["rows"]
    mix = ", ".join(f"{k} {v} ({100 * v / tot:.1f}%)" for k, v in sorted(st["status"].items()))
    (out.parent / "DATA_CARD.md").write_text(CARD.format(seed=a.seed, mix=mix, **{k: v for k, v in st.items() if k != 'status'}))
    print(f"wrote {out} ({out.stat().st_size / 1e6:.1f} MB): {tot} rows\nstatus mix: {mix}")
    print("scenarios:", dict(sorted(st["scenario"].items())))


if __name__ == "__main__":
    main()
