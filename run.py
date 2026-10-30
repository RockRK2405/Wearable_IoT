#!/usr/bin/env python3
"""Start the CardioWatch backend + dashboard.

  python run.py                      # simulated mode (no hardware)
  python run.py --mode cloud         # devices POST to /api/ingest over WiFi/internet
  python run.py --mode serial        # real ESP32 over USB serial
  python run.py --mode mqtt          # real ESP32 over WiFi/MQTT
"""
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=["simulated", "cloud", "serial", "mqtt"], help="overrides MODE in .env")
    ap.add_argument("--serial-port", help="e.g. COM5 or /dev/ttyUSB0 (default: auto-detect)")
    ap.add_argument("--host")
    ap.add_argument("--port", type=int)
    a = ap.parse_args()
    if a.mode:
        os.environ["MODE"] = a.mode
    if a.serial_port:
        os.environ["SERIAL_PORT"] = a.serial_port
    if a.host:
        os.environ["HOST"] = a.host
    if a.port:
        os.environ["PORT"] = str(a.port)

    import uvicorn
    from backend.app.main import create_app
    from backend.app.settings import load_settings

    s = load_settings()
    label = {"simulated": "SIMULATED data (no hardware)",
             "cloud": "CLOUD ingest (devices POST to /api/ingest)",
             "serial": f"REAL sensor via USB serial ({s.serial_port})",
             "mqtt": f"REAL sensor via MQTT ({s.mqtt_host}:{s.mqtt_port})"}[s.mode]
    notif = "Twilio (real SMS)" if (s.twilio_sid and s.twilio_token and s.twilio_from) else "SIMULATED (messages logged, not sent)"
    print(f"\n  CardioWatch Cloud -> http://{s.host}:{s.port}   mode: {label}")
    print(f"  Emergency notifier: {notif}\n")
    uvicorn.run(create_app(s), host=s.host, port=s.port, log_level="warning")


if __name__ == "__main__":
    main()
