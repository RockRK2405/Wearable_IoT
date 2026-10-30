# CardioWatch Cloud — Health IoT Monitor with Emergency Alerts

An ESP32 reads **ECG (AD8232)** and **SpO₂/pulse (MAX30102)**, streams the readings
to a **cloud backend over WiFi**, and a **live dashboard** shows them from anywhere.
When the backend detects an **abnormal ECG or SpO₂ episode**, it automatically
**texts the patient's emergency contact a message with a Google Maps link to their
location** (and can place a voice call too).

> ⚠️ **Not a medical device.** Alerts are threshold / heuristic screening only, and
> the SpO₂ value from the MAX30102 reference algorithm is uncalibrated. Do not use
> for diagnosis or treatment.

---

## What it does

```
  ESP32 (AD8232 + MAX30102)                 Cloud backend (FastAPI)              People
  ┌──────────────────────┐   HTTPS POST    ┌───────────────────────────┐
  │ sample ECG @250Hz     │ ──/api/ingest──▶│ ingest → detect abnormal   │        📱 Dashboard
  │ read SpO₂ / pulse     │   every 1 s     │ ECG / SpO₂ episodes        │──live──▶  (any browser)
  │ (optional GPS lat/lng)│                 │ log to CSV, push to UI     │
  └──────────────────────┘                 │        │ critical?         │        📨 Emergency
                                            │        ▼                   │──SMS/──▶  contact
                                            │ emergency notifier ────────┼─call──▶  (+ map link)
                                            └───────────────────────────┘
```

**Dashboard features (kept simple):**
- Live **heart rate**, **SpO₂**, and **ECG waveform**, with a colour‑coded NORMAL /
  WARNING / CRITICAL banner.
- **Emergency contact & location** panel: set the contact's phone, choose *critical
  only* vs *warning + critical*, toggle an optional call, and set the location (type
  coordinates, or press **“Use my location”**, or let the device send GPS).
- **Alerts** list and **Emergency notifications sent** feed (shows every SMS, with the
  map link and whether it was real or simulated).
- A **“Send test alert”** button to verify the whole notification path.

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
python scripts/cloud_device_sim.py --scenario normal   --seconds 10
python scripts/cloud_device_sim.py --scenario hypoxia   # SpO₂ 82% → CRITICAL
```

When the hypoxia episode opens you'll see the banner turn red and a new entry under
**Emergency notifications sent**. With no Twilio account configured the notifier runs
in **simulated** mode: the SMS text (including the maps link) is shown on the dashboard
and written to `data/notifications.log`, but not actually sent.

> Prefer the built‑in simulator instead of the poster script? `python run.py` (simulated
> mode) generates patients automatically; set `NOTIFY_ON_SIMULATED=true` in `.env` to let
> a simulated hypoxia/arrhythmia episode also fire a (simulated) alert.

---

## Sending real SMS / calls (Twilio)

1. Create a free [Twilio](https://www.twilio.com/try-twilio) account and get an
   **Account SID**, **Auth Token**, and a **From number** (SMS‑capable; on a trial
   account you can only text *verified* numbers — verify your emergency contact first).
2. Put them in `.env`:
   ```ini
   TWILIO_ACCOUNT_SID=ACxxxxxxxx
   TWILIO_AUTH_TOKEN=xxxxxxxx
   TWILIO_FROM_NUMBER=+1xxxxxxxxxx
   ```
3. `pip install twilio`, restart, and the dashboard badge switches from
   `alerts: simulated` to `alerts: twilio`. Press **Send test alert** to confirm.

The contact number must be in international format, e.g. `+9198XXXXXXXX`.

---

## Hardware & firmware (real ESP32)

**Wiring**

| Module | Pin | ESP32 |
|---|---|---|
| AD8232 | OUTPUT | GPIO34 (ADC, input‑only) |
| AD8232 | LO+ / LO− | GPIO32 / GPIO33 |
| AD8232 | 3.3V / GND | 3V3 / GND |
| MAX30102 | SDA / SCL | GPIO21 / GPIO22 |
| MAX30102 | VIN / GND | 3V3 / GND |
| *(optional)* NEO‑6M GPS | TX / RX | GPIO16 / GPIO17 |

**Flashing** (`firmware/esp32_cloud_health_monitor/`)

1. Arduino IDE → install **ESP32** boards and the **SparkFun MAX3010x** library
   (plus **TinyGPSPlus** only if you set `USE_GPS 1`).
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
