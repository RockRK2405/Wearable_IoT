import asyncio
import json
import os
import pty
import sys
import time

import numpy as np
import pytest
from fastapi.testclient import TestClient

from backend.app.hub import Hub
from backend.app.main import create_app
from backend.app.pipeline import Pipeline
from backend.app.settings import load_settings
from backend.app.sources.simulator import SimPatient
from backend.app.thresholds import ThresholdStore

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
from generate_dataset import generate  # noqa: E402


def mk_settings(tmp_path, **kw):
    kw.setdefault("mode", "simulated")
    return load_settings(data_dir=tmp_path, results_csv=tmp_path / "results.csv",
                         simulated_csv=tmp_path / "sim.csv", real_csv=tmp_path / "real.csv",
                         thresholds_path=tmp_path / "th.json",
                         patients_path=tmp_path / "patients.json",
                         notifications_log=tmp_path / "notifications.log", sim_seed=5, **kw)


def run_pipeline(tmp_path, scenario_seq, seconds_each=14, sim=True):
    """Feed simulator messages (faster than real time) through the pipeline; tick each simulated second."""
    s = mk_settings(tmp_path, sim_scenario="normal")
    hub = Hub()
    p = Pipeline(s, ThresholdStore(s.thresholds_path), hub)
    sp = SimPatient(0, s, np.random.default_rng(11))
    clock = [1_000_000.0]
    import backend.app.pipeline as pl
    real_time = pl.time.time
    pl.time.time = lambda: clock[0]
    try:
        for name in scenario_seq:
            sp.inject_scenario(name, seconds_each + 1)
            for step in range(seconds_each * 10):
                for msg in sp.step():
                    if not sim:
                        msg = {**msg, "sim": False}
                    p.handle(msg)
                clock[0] += 0.1
                if step % 10 == 9:
                    for st in list(p.devices.values()):
                        p._tick(st, clock[0])
    finally:
        pl.time.time = real_time
    return p, s


def test_pipeline_detects_scenarios_and_alerts(tmp_path):
    p, s = run_pipeline(tmp_path, ["normal", "tachycardia", "normal", "hypoxia"], seconds_each=30)
    import csv
    rows = list(csv.DictReader(open(s.simulated_csv)))
    assert rows and all(r["data_source"] == "simulated" for r in rows)
    codes = " ".join(r["alert_codes"] for r in rows)
    assert "HR_HIGH" in codes and "SPO2_LOW" in codes
    eps = list(p.alerts.episodes.values())
    assert any(e["code"] == "HR_HIGH" for e in eps) and any(e["code"] == "SPO2_LOW" for e in eps)
    assert all(e["clinically_validated"] is False for e in eps)
    assert not s.real_csv.exists()  # simulated data never lands in the real log


def test_sim_flag_false_goes_to_real_log(tmp_path):
    p, s = run_pipeline(tmp_path, ["normal"], seconds_each=12, sim=False)
    assert s.real_csv.exists() and not s.simulated_csv.exists()


def test_leads_off_and_irregular(tmp_path):
    p, s = run_pipeline(tmp_path, ["normal", "irregular_rhythm", "normal", "leads_off"], seconds_each=30)
    codes = {e["code"] for e in p.alerts.episodes.values()}
    assert "ECG_IRREGULAR" in codes and "LEADS_OFF" in codes


def test_normal_has_no_clinical_alerts(tmp_path):
    p, s = run_pipeline(tmp_path, ["normal"], seconds_each=40)
    assert not [e for e in p.alerts.episodes.values() if e["category"] != "sensor"], list(p.alerts.episodes.values())


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("api")
    s = mk_settings(tmp)
    generate(s.results_csv, patients=2, days=0.5, interval_s=60, seed=1)
    app = create_app(s)
    with TestClient(app) as c:
        yield c


def test_api_history(client):
    meta = client.get("/api/history/meta?dataset=synthetic").json()
    assert meta["available"] and meta["data_sources"] == ["synthetic"] and len(meta["patients"]) == 2
    ser = client.get("/api/history/series?dataset=synthetic&max_points=100").json()
    assert 0 < len(ser["t"]) <= 100 and ser["bucket_ms"] > 0
    rows = client.get("/api/history/rows?dataset=synthetic&limit=5&status=warning,critical").json()
    assert rows["rows"] and all(r["status"] != "normal" for r in rows["rows"])
    ecg = client.get(f"/api/history/rows?dataset=synthetic&limit=1&include_ecg=true").json()["rows"][0]
    assert len(ecg["ecg_snippet"]) == 50
    eps = client.get("/api/history/alerts?dataset=synthetic").json()
    assert isinstance(eps, list)
    csv_text = client.get("/api/history/export.csv?dataset=synthetic&patient_id=P-2001").text
    assert csv_text.startswith("timestamp,") and "P-2002" not in csv_text
    assert client.get("/api/history/rows?dataset=real").status_code == 404
    assert client.get("/api/history/rows?dataset=bogus").status_code == 422


def test_api_thresholds_and_status(client):
    st = client.get("/api/status").json()
    assert st["mode"] == "simulated" and "not a medical device" in st["disclaimer"].lower()
    r = client.put("/api/thresholds", json={"heart_rate": {"warn_high": 105}})
    assert r.status_code == 200 and r.json()["heart_rate"]["warn_high"] == 105
    assert client.put("/api/thresholds", json={"spo2": {"warn_below": 80}}).status_code == 422
    client.post("/api/thresholds/reset")


def test_websocket_live_stream(client):
    with client.websocket_connect("/ws") as ws:
        snap = json.loads(ws.receive_text())
        assert snap["type"] == "snapshot" and snap["mode"] == "simulated"
        seen = set()
        deadline = time.time() + 6
        while time.time() < deadline and not {"ecg", "reading"} <= seen:
            m = json.loads(ws.receive_text())
            seen.add(m["type"])
            if m["type"] == "reading":
                assert m["data_source"] == "simulated"
        assert {"ecg", "reading"} <= seen
    assert client.post("/api/simulator/inject", json={"patient_id": "P-1001", "scenario": "tachycardia"}).status_code == 200


def test_serial_source_with_pty(tmp_path):
    """End-to-end serial path using a pseudo-terminal as a fake USB port."""
    try:
        master, slave = pty.openpty()
        port = os.ttyname(slave)
    except (OSError, Exception) as e:
        pytest.skip(f"PTY not available in sandbox: {e}")
    s = mk_settings(tmp_path, mode="serial", serial_port=port)
    app = create_app(s)
    sp = SimPatient(0, s, np.random.default_rng(2))
    with TestClient(app) as c:
        for _ in range(60):  # 6 s of device data, written like firmware would (no 'sim' flag)
            for msg in sp.step():
                msg.pop("sim", None)
                os.write(master, (json.dumps(msg) + "\n").encode())
            os.write(master, b"# debug line from firmware\n")
        time.sleep(1.5)
        pats = c.get("/api/live/patients").json()
        assert pats and pats[0]["data_source"] == "real"
        assert c.get("/api/status").json()["source"]["connected"] is True
    os.close(master)
