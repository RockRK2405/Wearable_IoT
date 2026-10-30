#!/usr/bin/env python3
"""Pretend to be the ESP32 so you can test the serial / MQTT plumbing without hardware.

Every message carries "sim":1, so the backend labels the data SIMULATED (it is logged to
live_simulated.csv, never to live_real.csv).

Serial example (Linux, needs socat):
  socat -d -d pty,raw,echo=0 pty,raw,echo=0        # prints two /dev/pts/N ports
  python scripts/fake_device.py --serial /dev/pts/3
  python run.py --mode serial --serial-port /dev/pts/4

MQTT example:
  python scripts/fake_device.py --mqtt-host localhost
  python run.py --mode mqtt
"""
import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.app.settings import load_settings  # noqa: E402
from backend.app.sources.simulator import TICK_S, SimPatient  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--serial")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--mqtt-host")
    ap.add_argument("--mqtt-port", type=int, default=1883)
    ap.add_argument("--prefix", default="healthmon")
    ap.add_argument("--scenario", default="demo", choices=["demo", "normal", "random"])
    a = ap.parse_args()
    if not (a.serial or a.mqtt_host):
        ap.error("give --serial PORT or --mqtt-host HOST")

    s = load_settings(sim_scenario=a.scenario)
    p = SimPatient(0, s, np.random.default_rng())
    ser = client = None
    if a.serial:
        import serial
        ser = serial.Serial(a.serial, a.baud)
    else:
        import paho.mqtt.client as mqtt
        try:
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="fake-device")
        except AttributeError:
            client = mqtt.Client(client_id="fake-device")
        client.connect(a.mqtt_host, a.mqtt_port)
        client.loop_start()

    print("fake device running (SIMULATED data) - Ctrl+C to stop")
    nxt = time.monotonic()
    while True:
        for msg in p.step():
            line = json.dumps(msg, separators=(",", ":"))
            if ser:
                ser.write((line + "\n").encode())
            else:
                client.publish(f"{a.prefix}/{msg['dev']}/{msg['type']}", line)
        nxt += TICK_S
        time.sleep(max(0.0, nxt - time.monotonic()))


if __name__ == "__main__":
    main()
