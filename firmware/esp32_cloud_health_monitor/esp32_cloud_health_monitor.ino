/*
 * esp32_cloud_health_monitor.ino
 *
 * ESP32 + AD8232 (ECG) + MAX30102 (SpO2 / pulse)  ->  JSON over WiFi (HTTP POST)
 * to the CardioWatch Cloud backend at SERVER_URL + "/api/ingest".
 *
 * Once per second the device uploads:
 *   { "dev", "pid", "fs", "leads_off", "ecg":[raw...], "spo2", "pulse", "finger",
 *     "lat"?, "lng"? }
 * The backend detects abnormal ECG/SpO2 and, when configured, texts the
 * patient's emergency contact with a map link. No cloud logic lives on the device.
 *
 * Libraries (Arduino Library Manager):
 *   - "SparkFun MAX3010x Pulse and Proximity Sensor Library"
 *   - "TinyGPSPlus"   (only if USE_GPS 1)
 * Board: "ESP32 Dev Module" (arduino-esp32 core 2.x or 3.x).
 *
 * NOT a medical device. SpO2 comes from the Maxim reference algorithm and is uncalibrated.
 */
#include <Arduino.h>
#include <Wire.h>
#include <WiFi.h>
#include "config.h"

#if USE_HTTPS
  #include <WiFiClientSecure.h>
#endif
#include <HTTPClient.h>

#include "MAX30105.h"
#include "spo2_algorithm.h"

#if USE_DS18B20
  #include <OneWire.h>
  #include <DallasTemperature.h>
  OneWire oneWire(PIN_DS18B20);
  DallasTemperature tempSensors(&oneWire);
  float currentTempC = 36.8f;
  uint32_t lastTempRequest = 0;
  bool tempRequested = false;
#endif

#if USE_VIBRATION
  volatile uint32_t vibrationPulses = 0;
  float currentVibration = 0.05f;
  void IRAM_ATTR onVibPulse() { vibrationPulses++; }
#endif

#if USE_GPS
  #include <TinyGPSPlus.h>
  TinyGPSPlus gps;
  HardwareSerial GPSser(2);
#endif

MAX30105 particleSensor;
bool haveMax = false;

// ---- ECG sampling ----
static const int ECG_CAP = ECG_FS;         // one second of samples
uint16_t ecgBuf[ECG_CAP];
volatile int ecgCount = 0;
uint32_t nextSampleUs = 0;
const uint32_t sampleIntervalUs = 1000000UL / ECG_FS;

// ---- MAX30102 PPG buffers for the Maxim SpO2 algorithm ----
#define PPG_N 100
uint32_t irBuf[PPG_N], redBuf[PPG_N];
int ppgCount = 0;
int32_t spo2 = 0, heartRate = 0;
int8_t spo2Valid = 0, hrValid = 0;
uint32_t lastIr = 0;

uint32_t lastUpload = 0;
uint32_t lastThingSpeakUpload = 0;

// ---------------------------------------------------------------------------
void connectWiFi() {
  Serial.printf("WiFi: connecting to %s", WIFI_SSID);
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  uint32_t t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < 20000) {
    delay(400); Serial.print(".");
  }
  if (WiFi.status() == WL_CONNECTED) Serial.printf("  connected, IP %s\n", WiFi.localIP().toString().c_str());
  else Serial.println("  FAILED (will retry in loop)");
}

void setup() {
  Serial.begin(115200);
  delay(200);
  pinMode(PIN_LO_PLUS, INPUT);
  pinMode(PIN_LO_MINUS, INPUT);
  analogReadResolution(12);                 // 0..4095, matches backend ADC scaling
  analogSetPinAttenuation(PIN_ECG, ADC_11db);

  Wire.begin(PIN_SDA, PIN_SCL);
  if (particleSensor.begin(Wire, I2C_SPEED_FAST)) {
    haveMax = true;
    // 50 Hz PPG, works well with the 100-sample Maxim buffer
    particleSensor.setup(60, 4, 2, 50, 411, 4096);
    Serial.println("MAX30102: ready");
  } else {
    Serial.println("MAX30102: NOT found (SpO2/pulse will be blank)");
  }

#if USE_DS18B20
  tempSensors.begin();
  tempSensors.setWaitForConversion(false);
  tempSensors.requestTemperatures();
  lastTempRequest = millis();
  tempRequested = true;
  Serial.println("DS18B20: ready (epilepsy temperature monitoring)");
#endif

#if USE_VIBRATION
  pinMode(PIN_VIBRATION, INPUT_PULLDOWN);
  attachInterrupt(digitalPinToInterrupt(PIN_VIBRATION), onVibPulse, RISING);
  Serial.println("Vibration sensor: ready (clonic tremor monitoring)");
#endif

#if USE_GPS
  GPSser.begin(GPS_BAUD, SERIAL_8N1, GPS_RX_PIN, GPS_TX_PIN);
#endif

  connectWiFi();
  nextSampleUs = micros();
  lastUpload = millis();
}

// Sample the ECG at a steady ECG_FS using micros() scheduling.
void sampleEcg() {
  uint32_t now = micros();
  if ((int32_t)(now - nextSampleUs) < 0) return;
  nextSampleUs += sampleIntervalUs;
  if (ecgCount < ECG_CAP) {
    int v = analogRead(PIN_ECG);
    ecgBuf[ecgCount++] = (uint16_t)v;
  }
}

bool leadsOff() {
  return digitalRead(PIN_LO_PLUS) == HIGH || digitalRead(PIN_LO_MINUS) == HIGH;
}

// Pull any available MAX30102 samples; recompute SpO2/HR once we have a full window.
void serviceMax() {
  if (!haveMax) return;
  while (particleSensor.available() == false && particleSensor.check() == false) break;
  if (particleSensor.available()) {
    uint32_t red = particleSensor.getRed();
    uint32_t ir = particleSensor.getIR();
    particleSensor.nextSample();
    lastIr = ir;
    if (ppgCount < PPG_N) {
      redBuf[ppgCount] = red; irBuf[ppgCount] = ir; ppgCount++;
    } else {
      for (int i = 1; i < PPG_N; i++) { redBuf[i - 1] = redBuf[i]; irBuf[i - 1] = irBuf[i]; }
      redBuf[PPG_N - 1] = red; irBuf[PPG_N - 1] = ir;
    }
    if (ppgCount >= PPG_N) {
      maxim_heart_rate_and_oxygen_saturation(irBuf, PPG_N, redBuf, &spo2, &spo2Valid, &heartRate, &hrValid);
    }
  }
}

#if USE_GPS
void serviceGps() { while (GPSser.available()) gps.encode(GPSser.read()); }
#endif

#if USE_DS18B20
void serviceTemperature() {
  uint32_t now = millis();
  if (tempRequested && (now - lastTempRequest >= 750)) {
    float t = tempSensors.getTempCByIndex(0);
    if (t > -50.0f && t < 85.0f) { currentTempC = t; }
    tempSensors.requestTemperatures();
    lastTempRequest = now;
  }
}
#endif

#if USE_VIBRATION
void serviceVibration() {
  uint32_t p = vibrationPulses;
  vibrationPulses = 0;
  // Convert pulse count per second into approximate tremor intensity (m/s^2)
  currentVibration = (p * 0.045f) * VIBRATION_CAL_SCALE;
  if (currentVibration < 0.02f) currentVibration = 0.02f;
}
#endif

String buildJson(bool lo) {
  String j; j.reserve(ECG_CAP * 5 + 250);
  j += "{\"dev\":\""; j += DEVICE_ID; j += "\",\"pid\":\""; j += PATIENT_ID;
  j += "\",\"fs\":"; j += ECG_FS;
  j += ",\"leads_off\":"; j += (lo ? 1 : 0);
  j += ",\"ecg\":[";
  for (int i = 0; i < ecgCount; i++) { if (i) j += ','; j += ecgBuf[i]; }
  j += "]";
  bool finger = haveMax && lastIr > FINGER_IR_THRESHOLD;
  j += ",\"finger\":"; j += (finger ? "true" : "false");
  if (finger && spo2Valid && spo2 > 0) { j += ",\"spo2\":"; j += spo2; }
  if (finger && hrValid && heartRate > 0) { j += ",\"pulse\":"; j += heartRate; }
#if USE_DS18B20
  j += ",\"temp\":"; j += String(currentTempC, 1);
#endif
#if USE_VIBRATION
  j += ",\"vib\":"; j += String(currentVibration, 2);
#endif
#if USE_GPS
  if (gps.location.isValid()) {
    j += ",\"lat\":"; j += String(gps.location.lat(), 6);
    j += ",\"lng\":"; j += String(gps.location.lng(), 6);
  }
#endif
  j += "}";
  return j;
}

void upload(const String &json) {
  if (WiFi.status() != WL_CONNECTED) { connectWiFi(); if (WiFi.status() != WL_CONNECTED) return; }
  String url = String(SERVER_URL) + INGEST_PATH;
  HTTPClient http;
  bool ok;
#if USE_HTTPS
  WiFiClientSecure client; client.setInsecure();
  ok = http.begin(client, url);
#else
  ok = http.begin(url);
#endif
  if (!ok) { Serial.println("http.begin failed"); return; }
  http.addHeader("Content-Type", "application/json");
  if (strlen(INGEST_TOKEN) > 0) http.addHeader("X-Device-Token", INGEST_TOKEN);
  int code = http.POST((uint8_t *)json.c_str(), json.length());
  if (code > 0) Serial.printf("POST %s -> %d\n", url.c_str(), code);
  else Serial.printf("POST failed: %s\n", http.errorToString(code).c_str());
  http.end();
}

#if USE_THINGSPEAK
// Mirror readings to ThingSpeak channel using 8-field layout (IEEE PuneCon paper)
void uploadThingSpeak(bool lo) {
  if (WiFi.status() != WL_CONNECTED) return;
  bool finger = haveMax && lastIr > FINGER_IR_THRESHOLD;
  String url = "http://api.thingspeak.com/update?api_key=";
  url += THINGSPEAK_API_KEY;
  if (finger && hrValid && heartRate > 0)  { url += "&field1="; url += heartRate; }
  if (finger && spo2Valid && spo2 > 0)     { url += "&field2="; url += spo2; }
#if USE_DS18B20
  url += "&field3="; url += String(currentTempC, 1);
#endif
#if USE_VIBRATION
  url += "&field4="; url += String(currentVibration, 2);
#endif
  // Field 5: Seizure Flag (1 = suspected seizure event, 0 = normal)
  bool seizure = (heartRate >= 100 && spo2 > 0 && spo2 <= 90 && currentVibration >= 0.60f);
  url += "&field5="; url += (seizure ? 1 : 0);
#if USE_GPS
  if (gps.location.isValid()) {
    url += "&field6="; url += String(gps.location.lat(), 6);
    url += "&field7="; url += String(gps.location.lng(), 6);
  }
#endif
  url += "&field8="; url += (lo || !finger) ? 1 : 0;

  HTTPClient http;
  if (!http.begin(url)) { Serial.println("ThingSpeak: http.begin failed"); return; }
  int code = http.GET();
  if (code > 0) Serial.printf("ThingSpeak update -> %d\n", code);
  else Serial.printf("ThingSpeak update failed: %s\n", http.errorToString(code).c_str());
  http.end();
}
#endif

void loop() {
  sampleEcg();
  serviceMax();
#if USE_DS18B20
  serviceTemperature();
#endif
#if USE_GPS
  serviceGps();
#endif
  if (millis() - lastUpload >= UPLOAD_INTERVAL_MS) {
    lastUpload += UPLOAD_INTERVAL_MS;
#if USE_VIBRATION
    serviceVibration();
#endif
    bool lo = leadsOff();
    String json = buildJson(lo);
    ecgCount = 0;                 // start a fresh second
    upload(json);
#if USE_THINGSPEAK
    if (millis() - lastThingSpeakUpload >= THINGSPEAK_UPLOAD_INTERVAL_MS) {
      lastThingSpeakUpload = millis();
      uploadThingSpeak(lo);
    }
#endif
  }
}
