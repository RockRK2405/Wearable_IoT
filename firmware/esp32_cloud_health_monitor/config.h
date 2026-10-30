// ===== User configuration for esp32_cloud_health_monitor.ino =====
#pragma once

// ---- identity (should match PATIENT_ID / DEVICE_ID / INGEST_TOKEN in the backend .env) ----
#define DEVICE_ID   "ESP32-01"
#define PATIENT_ID  "P-REAL-01"

// ---- WiFi ----
#define WIFI_SSID   "Rudra"
#define WIFI_PASS   "12345678"

// ---- Cloud backend ----
// The base URL of your deployed backend (no trailing slash). Examples:
//   http://192.168.1.10:8000           (your laptop on the same WiFi)
//   https://cardiowatch.onrender.com   (deployed to the cloud)
#define SERVER_URL   "http://172.20.10.2:8000"
#define INGEST_PATH  "/api/ingest"
// If you set INGEST_TOKEN in the backend .env, put the SAME value here (else leave "").
#define INGEST_TOKEN ""
// Set to 1 if SERVER_URL is https:// (uses an insecure TLS client; fine for a student demo).
#define USE_HTTPS 0

// ---- ECG (AD8232) ----
#define PIN_ECG       34   // AD8232 OUTPUT  (ADC1 channel, input-only pin)
#define PIN_LO_PLUS   32   // AD8232 LO+
#define PIN_LO_MINUS  33   // AD8232 LO-
#define ECG_FS        250  // samples per second (backend default expects 250)

// ---- MAX30102 (I2C) ----
#define PIN_SDA 21
#define PIN_SCL 22
#define FINGER_IR_THRESHOLD 30000   // raw IR counts above this = finger present

// ---- how often to upload one reading (ms). 1000 = once per second. ----
#define UPLOAD_INTERVAL_MS 1000

// ---- optional GPS (NEO-6M) on Serial2; set to 1 to send live lat/lng ----
#define USE_GPS 0
#define GPS_RX_PIN 16      // ESP32 RX  <- GPS TX
#define GPS_TX_PIN 17      // ESP32 TX  -> GPS RX
#define GPS_BAUD 9600

// ---- optional: also mirror readings to a ThingSpeak channel ----
// This is IN ADDITION to the CardioWatch backend above, not instead of it:
// every reading still goes to SERVER_URL as before; a throttled subset also
// goes to ThingSpeak so the channel's public chart can be shown separately
// (e.g. to a professor), without touching the detection/alert pipeline.
// Create a channel at thingspeak.com, then fill in its Write API Key below.
//   Field 1 = heart rate (bpm)        Field 4 = latitude  (if USE_GPS)
//   Field 2 = SpO2 (%)                Field 5 = longitude (if USE_GPS)
//   Field 3 = leads-off / no-finger status (0 = ok, 1 = sensor issue)
#define USE_THINGSPEAK 1
#define THINGSPEAK_API_KEY "Q17MAE6JKKA6QL3V"
// ThingSpeak's free tier accepts at most one update every 15 s; 20 s leaves margin.
#define THINGSPEAK_UPLOAD_INTERVAL_MS 20000
