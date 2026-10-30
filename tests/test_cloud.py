"""Tests for the cloud additions: HTTP ingest, patient/emergency store, notifier."""
import numpy as np
import pytest
from fastapi.testclient import TestClient

from backend.app.emergency import EmergencyNotifier
from backend.app.hub import Hub
from backend.app.main import create_app
from backend.app.patients import PatientStore, normalise_phone
from backend.app.pipeline import Pipeline
from backend.app.settings import load_settings
from backend.app.thresholds import ThresholdStore


def mk_settings(tmp_path, **kw):
    return load_settings(data_dir=tmp_path, results_csv=tmp_path / "results.csv",
                         simulated_csv=tmp_path / "sim.csv", real_csv=tmp_path / "real.csv",
                         thresholds_path=tmp_path / "th.json",
                         patients_path=tmp_path / "patients.json",
                         notifications_log=tmp_path / "notif.log", **kw)


# ---------------- patient store ----------------
def test_phone_normalise():
    assert normalise_phone("+91 98123 45678") == "+919812345678"
    assert normalise_phone("98123-45678") == "+9812345678"
    assert normalise_phone("") == ""


def test_patient_store_update_and_maps(tmp_path):
    st = PatientStore(tmp_path / "p.json", default_id="P-1", default_name="A")
    p = st.update("P-1", {"name": "Rudra",
                          "emergency": {"contact_phone": "+91 9812345678", "contact_name": "Mom"},
                          "location": {"lat": 12.9716, "lng": 77.5946, "label": "Home"}})
    assert p.emergency.contact_phone == "+919812345678"
    assert p.location.maps_url() == "https://www.google.com/maps?q=12.971600,77.594600"
    # reload from disk keeps it
    st2 = PatientStore(tmp_path / "p.json")
    assert st2.get("P-1").emergency.contact_name == "Mom"


def test_patient_store_rejects_bad_phone(tmp_path):
    st = PatientStore(tmp_path / "p.json")
    with pytest.raises(Exception):
        st.update("P-1", {"emergency": {"contact_phone": "not-a-number"}})


def test_location_label_only_maps():
    from backend.app.patients import Location
    loc = Location(label="VIT Vellore")
    assert "maps/search" in loc.maps_url()
    assert Location().maps_url() is None


# ---------------- notifier gating ----------------
def _profile(tmp_path, **em):
    st = PatientStore(tmp_path / "p.json", default_id="P-1", default_name="Pat")
    base = {"contact_phone": "+919812345678", "contact_name": "Mom",
            "notify_enabled": True, "min_severity": "critical"}
    base.update(em)
    st.update("P-1", {"emergency": base, "location": {"lat": 1.0, "lng": 2.0}})
    return st


def _episode(**kw):
    ep = {"status": "active", "category": "vital", "severity": "critical",
          "code": "SPO2_LOW", "patient_id": "P-1", "message": "SpO2 80% below 90%",
          "hr_bpm": 110, "spo2_pct": 80}
    ep.update(kw)
    return ep


def test_notifier_sends_on_critical(tmp_path):
    st = _profile(tmp_path)
    n = EmergencyNotifier(mk_settings(tmp_path), st.get)
    rec = n.maybe_notify(_episode(), now=1000.0)
    assert rec is not None and rec["to"] == "+919812345678"
    assert rec["maps_url"] == "https://www.google.com/maps?q=1.000000,2.000000"
    # within cooldown -> no second notification for the same code
    assert n.maybe_notify(_episode(), now=1001.0) is None
    # after cooldown -> notifies again
    assert n.maybe_notify(_episode(), now=1000.0 + 301) is not None


def test_notifier_gates(tmp_path):
    st = _profile(tmp_path)
    n = EmergencyNotifier(mk_settings(tmp_path), st.get)
    # warning when min_severity is critical -> skipped
    assert n.maybe_notify(_episode(severity="warning", code="HR_HIGH"), now=1.0) is None
    # sensor category -> skipped
    assert n.maybe_notify(_episode(category="sensor", code="LEADS_OFF"), now=2.0) is None
    # not active -> skipped
    assert n.maybe_notify(_episode(status="resolved", code="HR_LOW"), now=3.0) is None


def test_notifier_disabled_or_no_phone(tmp_path):
    st = _profile(tmp_path, notify_enabled=False)
    n = EmergencyNotifier(mk_settings(tmp_path), st.get)
    assert n.maybe_notify(_episode(), now=1.0) is None
    st2 = PatientStore(tmp_path / "q.json", default_id="P-1")
    st2.update("P-1", {"emergency": {"contact_phone": ""}})
    n2 = EmergencyNotifier(mk_settings(tmp_path), st2.get)
    assert n2.maybe_notify(_episode(), now=1.0) is None


# ---------------- cloud ingest -> auto notify (time-controlled) ----------------
def test_cloud_ingest_triggers_emergency(tmp_path, monkeypatch):
    s = mk_settings(tmp_path, mode="cloud")
    store = PatientStore(s.patients_path, default_id=s.patient_id, default_name="Pat")
    store.update(s.patient_id, {"emergency": {"contact_phone": "+919812345678",
                 "notify_enabled": True, "min_severity": "critical"},
                 "location": {"lat": 12.97, "lng": 79.15}})
    notifier = EmergencyNotifier(s, store.get)
    hub = Hub()
    p = Pipeline(s, ThresholdStore(s.thresholds_path), hub, patients=store, notifier=notifier)

    import backend.app.pipeline as pl
    clock = [1_000_000.0]
    monkeypatch.setattr(pl.time, "time", lambda: clock[0])

    st = None
    for i in range(8):
        # low SpO2 vitals each simulated second (critical hypoxia)
        p.handle({"type": "vitals", "dev": "ESP32-01", "pid": s.patient_id,
                  "spo2": 80, "pulse": 118, "finger": True, "sim": False})
        st = p.devices["ESP32-01"]
        p._tick(st, clock[0])
        clock[0] += 1.0

    assert p.latest[s.patient_id]["spo2_pct"] == 80
    assert p.latest[s.patient_id]["status"] == "critical"
    assert len(notifier.feed) >= 1
    sent = [r for r in notifier.feed if r["code"] == "SPO2_LOW"]
    assert sent and sent[0]["maps_url"].startswith("https://www.google.com/maps")


# ---------------- ingest endpoint + token ----------------
def test_ingest_endpoint_and_token(tmp_path):
    s = mk_settings(tmp_path, mode="cloud", ingest_token="secret123")
    client = TestClient(create_app(s, start_source=False))
    payload = {"dev": "ESP32-01", "spo2": 97, "pulse": 70, "finger": True,
               "fs": 250, "ecg": [2048] * 50}
    # missing token -> 401
    assert client.post("/api/ingest", json=payload).status_code == 401
    # correct token -> 200
    r = client.post("/api/ingest", json=payload, headers={"X-Device-Token": "secret123"})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert "vitals" in r.json()["accepted"] and "ecg" in r.json()["accepted"]
    # GPS updates stored coordinates
    client.post("/api/ingest", json={"dev": "ESP32-01", "lat": 19.0, "lng": 72.8, "spo2": 96, "finger": True},
                headers={"X-Device-Token": "secret123"})
    loc = client.get(f"/api/patients/{s.patient_id}").json()["location"]
    assert loc["lat"] == 19.0 and loc["lng"] == 72.8


def test_test_notification_endpoint(tmp_path):
    s = mk_settings(tmp_path, mode="cloud")
    client = TestClient(create_app(s, start_source=False))
    client.put(f"/api/patients/{s.patient_id}", json={"emergency": {"contact_phone": "+919812345678"},
               "location": {"label": "Home"}})
    r = client.post("/api/notifications/test", json={"patient_id": s.patient_id})
    assert r.status_code == 200 and r.json()["ok"] is True
    items = client.get("/api/notifications").json()["items"]
    assert items and items[0]["code"] == "TEST"
