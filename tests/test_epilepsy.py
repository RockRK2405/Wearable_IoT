import pytest
from fastapi.testclient import TestClient

from backend.app.detection import Measurements, evaluate
from backend.app.main import create_app
from backend.app.seizure_ml import FEATURE_IMPORTANCES, predict_seizure
from backend.app.settings import load_settings
from backend.app.thresholds import Thresholds


def test_seizure_ml_paper_case():
    """Verify sample point reported in IEEE paper Section III-H:
    HR 102 bpm, Temp 40 C, SpO2 89%, Vibration 0.75 m/s^2.
    """
    pred = predict_seizure(
        heart_rate=102,
        vibration_intensity=0.75,
        spo2=89,
        temperature_c=40.0,
    )
    assert pred.is_seizure is True
    assert pred.probability >= 0.70
    assert pred.classification in ("clonic_seizure", "febrile_seizure")


def test_exercise_discrimination():
    """Verify that physical exercise is NOT misclassified as seizure (Paper Section III-E)."""
    # High HR + High motion, but normal SpO2 and normal Temp
    pred = predict_seizure(
        heart_rate=135,
        vibration_intensity=0.85,
        spo2=98,
        temperature_c=37.1,
    )
    assert pred.is_seizure is False
    assert pred.classification == "exercise"
    assert pred.probability < 0.40


def test_normal_baseline():
    pred = predict_seizure(
        heart_rate=72,
        vibration_intensity=0.04,
        spo2=98,
        temperature_c=36.8,
    )
    assert pred.is_seizure is False
    assert pred.classification == "normal"
    assert pred.probability < 0.10


def test_feature_importances_hierarchy():
    """Heart rate > Vibration > SpO2 > Temperature according to paper Section III-F."""
    fi = FEATURE_IMPORTANCES
    assert fi["heart_rate"] > fi["vibration_intensity"]
    assert fi["vibration_intensity"] > fi["spo2"]
    assert fi["spo2"] > fi["temperature_c"]


def test_evaluate_triggers_clonic_seizure_finding():
    th = Thresholds()
    m = Measurements(
        ecg_state="ok",
        vitals_state="ok",
        pulse_bpm=120,
        spo2=82,
        finger=True,
        vibration_intensity=0.95,
        temperature_c=39.0,
    )
    ev = evaluate(m, th)
    assert ev.status == "critical"
    assert ev.seizure_detected is True
    codes = [f.code for f in ev.findings]
    assert "SEIZURE_CLONIC" in codes or "FEBRILE_SEIZURE" in codes


def test_evaluate_temperature_thresholds():
    th = Thresholds()
    # Fever
    m_fever = Measurements(
        ecg_state="ok", vitals_state="ok", pulse_bpm=75, spo2=98, finger=True,
        temperature_c=39.5, vibration_intensity=0.05,
    )
    ev_fever = evaluate(m_fever, th)
    fever_findings = {f.code: f.severity for f in ev_fever.findings}
    assert fever_findings.get("TEMP_HIGH") == "critical"

    # Hypothermia
    m_hypo = Measurements(
        ecg_state="ok", vitals_state="ok", pulse_bpm=75, spo2=98, finger=True,
        temperature_c=35.0, vibration_intensity=0.05,
    )
    ev_hypo = evaluate(m_hypo, th)
    hypo_findings = {f.code: f.severity for f in ev_hypo.findings}
    assert hypo_findings.get("TEMP_LOW") == "warning"


def test_api_epilepsy_endpoints(tmp_path):
    s = load_settings(data_dir=tmp_path, results_csv=tmp_path / "res.csv",
                      simulated_csv=tmp_path / "sim.csv", real_csv=tmp_path / "real.csv",
                      thresholds_path=tmp_path / "th.json", patients_path=tmp_path / "p.json",
                      notifications_log=tmp_path / "n.log", mode="cloud", allow_ingest=True)
    app = create_app(s)
    with TestClient(app) as c:
        # Check model metadata
        meta = c.get("/api/epilepsy/model").json()
        assert meta["model"] == "RandomForestClassifier"
        assert meta["accuracy_noisy"] == 0.9636
        assert "clonic_seizure" in meta["target_classes"]

        # Test classify endpoint
        res = c.post("/api/epilepsy/classify", json={
            "hr_bpm": 115, "spo2_pct": 82, "temperature_c": 39.2, "vibration_intensity": 0.88,
        }).json()
        assert res["is_seizure"] is True

        # Test ingest with temp & vib
        ingest_res = c.post("/api/ingest", json={
            "dev": "ESP32-01",
            "pid": "P-REAL-01",
            "pulse": 110,
            "spo2": 85,
            "temp": 39.0,
            "vib": 0.80,
            "finger": True,
        })
        assert ingest_res.status_code == 200
        assert "vitals" in ingest_res.json()["accepted"]

