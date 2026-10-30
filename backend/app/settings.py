"""Runtime settings, loaded from environment variables / .env."""
from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]


def _s(name: str, default: str) -> str:
    v = os.getenv(name)
    return default if v is None or v == "" else v


def _f(name: str, default: float) -> float:
    v = os.getenv(name)
    return default if v is None or v == "" else float(v)


def _i(name: str, default: int) -> int:
    v = os.getenv(name)
    return default if v is None or v == "" else int(v)


def _b(name: str, default: bool) -> bool:
    v = os.getenv(name)
    return default if v is None or v == "" else v.strip().lower() in ("1", "true", "yes", "on")


def _p(name: str, default: str) -> Path:
    p = Path(_s(name, default))
    return p if p.is_absolute() else ROOT / p


@dataclass
class Settings:
    mode: str = "simulated"  # simulated | serial | mqtt | cloud
    host: str = "127.0.0.1"
    port: int = 8000
    serial_port: str = "auto"
    serial_baud: int = 115200
    mqtt_host: str = "localhost"
    mqtt_port: int = 1883
    mqtt_username: str = ""
    mqtt_password: str = ""
    mqtt_tls: bool = False
    mqtt_topic_prefix: str = "healthmon"
    patient_id: str = "P-REAL-01"
    patient_name: str = "Real patient"
    device_id: str = "ESP32-01"
    ecg_fs: int = 250
    adc_vref: float = 3.3
    adc_mid_v: float = 1.65
    ecg_gain: float = 1100.0
    reading_interval_s: float = 1.0
    ecg_window_s: float = 8.0
    stale_after_s: float = 3.0
    data_dir: Path = ROOT / "data"
    results_csv: Path = ROOT / "data" / "results.csv"
    simulated_csv: Path = ROOT / "data" / "live_simulated.csv"
    real_csv: Path = ROOT / "data" / "live_real.csv"
    thresholds_path: Path = ROOT / "config" / "thresholds.json"
    patients_path: Path = ROOT / "config" / "patients.json"
    notifications_log: Path = ROOT / "data" / "notifications.log"
    sim_scenario: str = "demo"  # demo | normal | random
    sim_patients: int = 3
    sim_seed: int | None = None
    log_level: str = "INFO"
    # ---- cloud ingest (HTTP) ----
    ingest_token: str = ""          # if set, devices must send it as X-Device-Token
    allow_ingest: bool = True       # expose POST /api/ingest regardless of mode
    # ---- emergency notifier (Twilio; blank => simulated/logged) ----
    twilio_sid: str = ""
    twilio_token: str = ""
    twilio_from: str = ""
    notify_on_simulated: bool = False   # let simulated episodes trigger (simulated) alerts, for demos

    @property
    def is_real(self) -> bool:
        return self.mode in ("serial", "mqtt", "cloud")


def load_settings(**overrides) -> Settings:
    load_dotenv(ROOT / ".env", override=False)
    seed = os.getenv("SIM_SEED")
    s = Settings(
        mode=_s("MODE", "simulated").lower(),
        host=_s("HOST", "127.0.0.1"),
        port=_i("PORT", 8000),
        serial_port=_s("SERIAL_PORT", "auto"),
        serial_baud=_i("SERIAL_BAUD", 115200),
        mqtt_host=_s("MQTT_HOST", "localhost"),
        mqtt_port=_i("MQTT_PORT", 1883),
        mqtt_username=_s("MQTT_USERNAME", ""),
        mqtt_password=_s("MQTT_PASSWORD", ""),
        mqtt_tls=_b("MQTT_TLS", False),
        mqtt_topic_prefix=_s("MQTT_TOPIC_PREFIX", "healthmon").strip("/"),
        patient_id=_s("PATIENT_ID", "P-REAL-01"),
        patient_name=_s("PATIENT_NAME", "Real patient"),
        device_id=_s("DEVICE_ID", "ESP32-01"),
        ecg_fs=_i("ECG_SAMPLE_RATE", 250),
        adc_vref=_f("ADC_VREF", 3.3),
        adc_mid_v=_f("ADC_MIDPOINT_V", 1.65),
        ecg_gain=_f("ECG_GAIN", 1100.0),
        reading_interval_s=_f("READING_INTERVAL_S", 1.0),
        ecg_window_s=_f("ECG_WINDOW_S", 8.0),
        stale_after_s=_f("STALE_AFTER_S", 3.0),
        data_dir=_p("DATA_DIR", "data"),
        results_csv=_p("RESULTS_CSV", "data/results.csv"),
        simulated_csv=_p("SIMULATED_CSV", "data/live_simulated.csv"),
        real_csv=_p("REAL_CSV", "data/live_real.csv"),
        thresholds_path=_p("THRESHOLDS_FILE", "config/thresholds.json"),
        patients_path=_p("PATIENTS_FILE", "config/patients.json"),
        notifications_log=_p("NOTIFICATIONS_LOG", "data/notifications.log"),
        sim_scenario=_s("SIM_SCENARIO", "demo").lower(),
        sim_patients=max(1, min(6, _i("SIM_PATIENTS", 3))),
        sim_seed=int(seed) if seed not in (None, "") else None,
        log_level=_s("LOG_LEVEL", "INFO").upper(),
        ingest_token=_s("INGEST_TOKEN", ""),
        allow_ingest=_b("ALLOW_INGEST", True),
        twilio_sid=_s("TWILIO_ACCOUNT_SID", ""),
        twilio_token=_s("TWILIO_AUTH_TOKEN", ""),
        twilio_from=_s("TWILIO_FROM_NUMBER", ""),
        notify_on_simulated=_b("NOTIFY_ON_SIMULATED", False),
    )
    if overrides:
        s = replace(s, **overrides)
    if s.mode not in ("simulated", "serial", "mqtt", "cloud"):
        raise ValueError(f"MODE must be simulated, serial, mqtt or cloud (got {s.mode!r})")
    return s
