#!/usr/bin/env python3
"""Pretend to be the ESP32 and POST readings to the cloud backend over HTTP.

Use it to demo the whole cloud + emergency-alert flow without any hardware:

  # terminal 1: start the backend in cloud mode
  python run.py --mode cloud

  # terminal 2: stream normal readings, then trigger a critical episode
  python scripts/cloud_device_sim.py --url http://127.0.0.1:8000 --scenario normal
  python scripts/cloud_device_sim.py --url http://127.0.0.1:8000 --scenario hypoxia

Set an emergency contact + location on the dashboard first; a critical episode
then posts a (simulated) SMS visible under "Emergency notifications sent".
"""
import argparse
import sys
import time
from pathlib import Path

import numpy as np
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def ecg_second(fs: int, hr: float, noise: float, rng) -> list[int]:
    """A crude but ECG-shaped waveform: P-QRS-T-ish bumps at the given rate."""
    n = fs
    x = np.zeros(n)
    rr = 60.0 / max(30.0, hr)
    t = 0.0
    while t < n / fs:
        c = int(t * fs)
        for off, amp in ((-2, -0.1), (0, 1.0), (1, -0.25), (10, 0.25)):  # Q,R,S,T
            i = c + off
            if 0 <= i < n:
                x[i] += amp
        t += rr
    x += rng.normal(0, noise, n)
    raw = (x * 700 + 2048).clip(0, 4095).astype(int)
    return raw.tolist()


SCENARIOS = {
    "normal": dict(hr=74, spo2=98, noise=0.03),
    "tachycardia": dict(hr=135, spo2=96, noise=0.04),
    "bradycardia": dict(hr=42, spo2=97, noise=0.03),
    "hypoxia": dict(hr=112, spo2=82, noise=0.05),
}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default="http://127.0.0.1:8000", help="backend base URL")
    ap.add_argument("--device", default="ESP32-01")
    ap.add_argument("--patient", default="P-REAL-01")
    ap.add_argument("--scenario", default="normal", choices=list(SCENARIOS))
    ap.add_argument("--fs", type=int, default=250)
    ap.add_argument("--seconds", type=int, default=0, help="0 = run until Ctrl-C")
    ap.add_argument("--token", default="", help="X-Device-Token if the backend requires one")
    ap.add_argument("--lat", type=float, default=None)
    ap.add_argument("--lng", type=float, default=None)
    a = ap.parse_args()

    cfg = SCENARIOS[a.scenario]
    rng = np.random.default_rng()
    endpoint = a.url.rstrip("/") + "/api/ingest"
    headers = {"X-Device-Token": a.token} if a.token else {}
    print(f"POSTing {a.scenario} readings to {endpoint}  (Ctrl-C to stop)")
    i = 0
    try:
        while a.seconds == 0 or i < a.seconds:
            body = {
                "dev": a.device, "pid": a.patient, "fs": a.fs, "leads_off": 0,
                "ecg": ecg_second(a.fs, cfg["hr"], cfg["noise"], rng),
                "spo2": int(round(cfg["spo2"] + rng.normal(0, 0.4))),
                "pulse": int(round(cfg["hr"] + rng.normal(0, 2))),
                "finger": True,
            }
            if a.lat is not None and a.lng is not None:
                body["lat"], body["lng"] = a.lat, a.lng
            try:
                r = requests.post(endpoint, json=body, headers=headers, timeout=5)
                print(f"  t={i:3d}s  HTTP {r.status_code}  {r.json() if r.ok else r.text[:80]}")
            except Exception as e:
                print(f"  t={i:3d}s  POST failed: {e}")
            i += 1
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
