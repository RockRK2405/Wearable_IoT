"""Device -> backend wire protocol (one JSON object per line over Serial, or per MQTT message).

ECG batch (10 Hz, 25 samples at 250 Hz, raw 12-bit ADC counts):
  {"type":"ecg","dev":"ESP32-01","pid":"P-REAL-01","ts":123456,"fs":250,"lo":0,"s":[2048,2051,...]}
Vitals (1 Hz):
  {"type":"vitals","dev":"ESP32-01","pid":"P-REAL-01","ts":123456,"spo2":97,"spo2_valid":1,
   "pulse":72,"pulse_valid":1,"finger":1}
Lines starting with '#' are debug text and ignored.
A field "sim":1 marks data as simulated (only the simulator / fake_device tool set it).
"""
from __future__ import annotations

import json
import math

MAX_SAMPLES = 2000


class ProtocolError(ValueError):
    pass


def _truthy(v) -> bool:
    return v in (1, True, "1", "true", "True")


def _num(v):
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def parse_message(raw, default_dev: str = "device", topic_dev: str | None = None):
    """Return a normalised dict, None for ignorable lines, or raise ProtocolError."""
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "replace")
    if isinstance(raw, str):
        line = raw.strip()
        if not line or line.startswith("#"):
            return None
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as e:
            raise ProtocolError(f"invalid JSON: {e.msg}") from None
    else:
        obj = raw
    if not isinstance(obj, dict):
        raise ProtocolError("message must be a JSON object")

    t = obj.get("type")
    dev = str(obj.get("dev") or topic_dev or default_dev)[:64]
    pid = obj.get("pid")
    base = {"dev": dev, "pid": str(pid)[:64] if pid else None,
            "pname": str(obj["pname"])[:40] if obj.get("pname") else None,
            "ts": int(_num(obj.get("ts")) or 0), "sim": _truthy(obj.get("sim"))}

    if t == "ecg":
        s = obj.get("s")
        if not isinstance(s, list) or not (0 < len(s) <= MAX_SAMPLES):
            raise ProtocolError("ecg message needs a non-empty 's' array")
        samples = []
        for v in s:
            f = _num(v)
            if f is None:
                raise ProtocolError("ecg sample is not a finite number")
            samples.append(int(min(4095, max(0, round(f)))))
        fs = int(_num(obj.get("fs")) or 250)
        if not 20 <= fs <= 1000:
            raise ProtocolError("fs out of range")
        return {**base, "type": "ecg", "fs": fs, "lo": _truthy(obj.get("lo")), "s": samples}

    if t == "vitals":
        spo2 = _num(obj.get("spo2"))
        spo2_ok = _truthy(obj.get("spo2_valid", 1 if spo2 is not None else 0)) and spo2 is not None and 50 < spo2 <= 100
        pulse = _num(obj.get("pulse"))
        pulse_ok = _truthy(obj.get("pulse_valid", 1 if pulse is not None else 0)) and pulse is not None and 25 <= pulse <= 250
        temp = _num(obj.get("temp") if "temp" in obj else obj.get("temperature_c") if "temperature_c" in obj else obj.get("temperature"))
        temp_ok = temp is not None and 20.0 <= temp <= 48.0
        vib = _num(obj.get("vib") if "vib" in obj else obj.get("vibration_intensity") if "vibration_intensity" in obj else obj.get("vibration"))
        vib_ok = vib is not None and 0.0 <= vib <= 50.0
        return {**base, "type": "vitals", "spo2": spo2 if spo2_ok else None,
                "pulse": pulse if pulse_ok else None, "finger": _truthy(obj.get("finger", 1)),
                "temp": temp if temp_ok else None, "vib": vib if vib_ok else None}

    if t in ("hello", "status"):
        return None
    raise ProtocolError(f"unknown message type {t!r}")
