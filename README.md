# CardioWatch Cloud — Health IoT & Epilepsy / Clonic Seizure Monitoring

An intelligent IoT health monitor combining continuous cardiovascular screening with real-time **Clonic Seizure / Epilepsy Detection** based on the IEEE PuneCon published architecture (*"Smart AIOT Band for Clonic Seizure Monitoring in Epilepsy Management"*).

An ESP32 wearable band reads **ECG (AD8232)**, **SpO₂/pulse (MAX30102)**, **Core Body Temperature (DS18B20)**, and **Muscle Tremors (Vibration Sensor)**, streaming readings to a **cloud backend over WiFi**. When the system detects an **abnormal ECG/SpO₂ episode or a clonic seizure event**, it automatically **texts the patient's emergency contact an SMS alert and places a phone call with a Google Maps location link**.

> ⚠️ **Not a medical device.** Alerts are threshold screening & machine learning heuristics. Do not use for definitive clinical diagnosis or treatment.

---

## What it does

```
  ESP32 Wearable Band                                   Cloud Backend (FastAPI + ML)            People
  ┌─────────────────────────────────┐   HTTPS POST     ┌────────────────────────────────────┐
  │ • ECG @250Hz (AD8232)            │ ──/api/ingest───▶│ Ingest & Preprocessing             │   📱 Live Dashboard
  │ • SpO₂ & Pulse (MAX30102)        │   every 1 s      │ Random Forest Seizure ML           │──▶ (Web Browser)
  │ • Body Temp (DS18B20)            │                  │ Multimodal Abnormality Detection   │
  │ • Muscle Tremor (SW-420/Vib)     │                  │ CSV Logging & ThingSpeak Mirror    │   📨 Emergency
  │ • Optional GPS (NEO-6M Lat/Lng)  │                  │        │ Critical?                 │──▶ Contact (SMS
  └─────────────────────────────────┘                  │        ▼                           │    + Voice Call
                                                        │ Emergency Alert Dispatcher (Twilio)│    + Maps Link)
                                                        └────────────────────────────────────┘
```

**Key Capabilities:**
- **Clonic Seizure Detection (Random Forest ML)**: Analyzes co-occurring physiological anomalies—sudden tachycardia, rhythmic muscle vibrations (m/s²), SpO₂ desaturation, and thermal dysregulation—achieving up to **96.36% accuracy** under noisy wearable data.
- **Exercise Discrimination**: Accurately differentiates between intense physical exercise (high HR + motion, but normal SpO₂ & temperature) and pathological clonic seizures, minimizing false alarms.
- **Cardiovascular Monitoring**: Continuous ECG waveform streaming, real-time R-peak detection, RMSSD Heart Rate Variability (HRV), and hypoxia screening.
- **Instant Emergency Contact Alerts**: Automatic SMS and text-to-speech phone call with Google Maps location coordinates dispatched on critical episodes.
- **ThingSpeak Cloud Mirroring**: Public 8-field telemetry channel support for external analytics and reports.

---

## Quick start (no hardware — 2 minutes)

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env            # defaults are fine for a demo

python run.py --mode cloud      # starts backend + dashboard on http://127.0.0.1:8000
```

Open **http://127.0.0.1:8000**, then in the dashboard set an emergency contact phone
and a location and press **Save**. In a second terminal, stream fake device data:

```bash
source .venv/bin/activate
python scripts/cloud_device_sim.py --scenario normal          --seconds 10
python scripts/cloud_device_sim.py --scenario seizure_clonic  # Tachycardia + Rhythmic Tremors + SpO2 drop -> CRITICAL SEIZURE
python scripts/cloud_device_sim.py --scenario exercise        # High HR + High motion -> Normal (Discriminates non-seizure!)
python scripts/cloud_device_sim.py --scenario febrile_seizure # High fever (40°C) + Tremor -> CRITICAL SEIZURE
```

When the seizure episode opens you'll see the dashboard flash red, the **Epilepsy Monitor** badge switch to **SEIZURE ALERT**, and an emergency alert dispatched with Google Maps coordinates under **Emergency notifications sent**. With no Twilio account configured the notifier runs in **simulated** mode: the alert text is recorded to `data/notifications.log` and displayed in the live UI.

> Prefer the built‑in simulator instead of the poster script? `python run.py` (simulated
> mode) generates patients automatically; set `NOTIFY_ON_SIMULATED=true` in `.env` to let
> a simulated episode fire a (simulated) alert.

---

## Hardware & firmware (real ESP32)

**Wiring**

| Module | Pin | ESP32 | Description |
|---|---|---|---|
| AD8232 | OUTPUT | GPIO34 | ECG analog signal (ADC1 input-only) |
| AD8232 | LO+ / LO− | GPIO32 / GPIO33 | Leads-off detection |
| AD8232 | 3.3V / GND | 3V3 / GND | Power |
| MAX30102 | SDA / SCL | GPIO21 / GPIO22 | I2C pulse oximetry & heart rate |
| MAX30102 | VIN / GND | 3V3 / GND | Power |
| **DS18B20** | DATA | GPIO4 | Digital temperature (OneWire with 4.7kΩ pull-up) |
| **SW-420** | DO / OUT | GPIO27 | Vibration / clonic muscle tremor sensor |
| *(optional)* NEO‑6M GPS | TX / RX | GPIO16 / GPIO17 | Real-time GPS location tracking |

**ThingSpeak 8-Field Mapping:**
- **Field 1**: Heart rate (BPM)
- **Field 2**: SpO₂ (%)
- **Field 3**: Temperature (°C)
- **Field 4**: Vibration intensity (m/s²)
- **Field 5**: Seizure detected flag (0 = normal, 1 = clonic seizure)
- **Field 6**: Latitude (GPS)
- **Field 7**: Longitude (GPS)
- **Field 8**: Sensor validity / status

**Flashing** (`firmware/esp32_cloud_health_monitor/`)

1. Arduino IDE → install **ESP32** board support, plus libraries:
   - **SparkFun MAX3010x**
   - **OneWire** & **DallasTemperature** (for DS18B20)
   - **TinyGPSPlus** (only if `USE_GPS 1`)
2. Edit `config.h`:
   - `WIFI_SSID` / `WIFI_PASS`
   - `SERVER_URL` → your backend, e.g. `http://192.168.1.10:8000` (laptop on same WiFi)
     or `https://your-app.onrender.com` (and set `USE_HTTPS 1`).
   - `INGEST_TOKEN` must match the backend's if you set one.
3. Upload. The device POSTs one reading per second to `/api/ingest`. Watch the Serial
   Monitor (115200) for `POST ... -> 200`.

No GPS? Leave `USE_GPS 0` and set the location on the dashboard instead — the alert
still carries a maps link.

---

## Deploy to the cloud

The backend is a standard FastAPI app and ships with a `Dockerfile`, `Procfile`, and
`render.yaml`.

- **Render / Railway / Fly:** push this repo to GitHub and deploy the Docker image
  (Render: *New → Blueprint* picks up `render.yaml`). Set `TWILIO_*` and optionally
  `INGEST_TOKEN` as environment variables in the host's dashboard.
- The host provides `$PORT`; the container already binds to it.
- Point the ESP32's `SERVER_URL` at the deployed HTTPS URL.

> The free tiers use an ephemeral disk, so the CSV logs and `config/patients.json` reset
> on redeploy. For permanent history attach a persistent disk, or set `PATIENTS_FILE`
> and the `*_CSV` paths to a mounted volume.

---

## HTTP API (what the device and dashboard use)

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/ingest` | device pushes one reading (ECG + vitals, optional GPS) |
| `GET` | `/api/status` | mode, notifier provider, ingest info |
| `GET` | `/api/live/patients` | latest reading per patient |
| `GET` | `/api/live/alerts` | recent alert episodes |
| `GET` `PUT` | `/api/patients[/{id}]` | read / edit emergency contact + location |
| `GET` | `/api/notifications` | emergency messages sent |
| `POST` | `/api/notifications/test` | send a test alert |
| `GET` `PUT` | `/api/thresholds` | abnormality limits |
| `WS` | `/ws` | live stream to the dashboard |

**Ingest body** (what `esp32_cloud_health_monitor.ino` sends each second):
```json
{ "dev": "ESP32-01", "pid": "P-REAL-01", "fs": 250, "leads_off": 0,
  "ecg": [2048, 2051, ...], "spo2": 97, "pulse": 72, "finger": true,
  "lat": 12.9716, "lng": 77.5946 }
```
If `INGEST_TOKEN` is set, send it as header `X-Device-Token`.

---

## How abnormal → alert works

1. Each second the backend turns raw ECG into heart rate and rhythm metrics and reads
   SpO₂/pulse. `backend/app/detection.py` flags values outside the configurable limits
   in `config/thresholds.json` (HR too high/low, SpO₂ low, irregular R‑R, long pause…).
2. `backend/app/alerts.py` debounces flags into **episodes** (open after a few seconds of
   a sustained problem, close after it clears) so a one‑off blip doesn't alert.
3. When a **critical** episode opens for a *vital* or *ECG* reason, `backend/app/emergency.py`
   texts the emergency contact once per episode (per `cooldown_s`), with the vitals and a
   Google Maps link. Sensor notices (leads off, no finger) never trigger a call‑out.

Tune everything from the dashboard or `config/thresholds.json` + the patient's settings.

---

## Tests

```bash
pip install -r requirements-dev.txt
pytest -q          # 33 tests: pipeline, detection, cloud ingest, patient store, notifier
```

## Project layout

```
backend/app/        FastAPI app, pipeline, detection, alerts
  ├─ main.py        routes incl. /api/ingest, /api/patients, /api/notifications
  ├─ patients.py    emergency contact + location store
  ├─ emergency.py   SMS/call notifier (Twilio or simulated)
  └─ sources/cloud_source.py   cloud (HTTP) ingest mode
frontend/           simple dashboard (index.html, app.js, styles.css)
firmware/esp32_cloud_health_monitor/   ESP32 WiFi sketch
scripts/cloud_device_sim.py            post fake readings (no hardware)
Dockerfile, Procfile, render.yaml      cloud deploy
```

## Privacy & safety notes

- Health data is sensitive — run the backend over HTTPS in the cloud, and set an
  `INGEST_TOKEN` so only your device can post.
- Keep Twilio credentials in environment variables, never in the firmware or git.
- This is a student/educational project, **not a certified medical device**.
